#!/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# pyterlink
# Copyright (C) 2026 Kris Kirby
# https://github.com/ke4ahr/Pyterlink/
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
#
"""LNKSERVER-side session: serve block devices to a real LNKCLIENT.

The server half of the protocol documented in protocol/session.py, with
every reply byte modelled on what LNKSERVER sent in the logs/ captures.
Units are 0-based server drives (0 = A:, 1 = B:, 2 = C:, ...); each is a
disk image (or block device) holding a FAT volume whose boot sector supplies
the BPB, label and serial, or None for an empty drive (e.g. no floppy).
"""
from __future__ import annotations

import logging
import os
import struct
import time
from typing import Callable, List, Optional, Sequence

from .framing import encode_envelope, _crc16_prng, CRC_RESIDUE, MORE_FLAG
from .session import (
    OP_INSTALL, OP_DRIVE_MAP, OP_MEDIA_CHECK, OP_BUILD_BPB, OP_READ, OP_WRITE,
    OP_WRITE_VERIFY, OP_DRIVE_INFO, OP_IOCTL, IOCTL_CAT_DISK,
    IOCTL_GET_MEDIA_ID, IOCTL_SET_MEDIA_ID, SECTOR_SIZE,
    MAP_LOCAL, MAP_NONE, MAP_LEN, N_DRIVES, CHUNK_BYTES, MAX_SECTORS,
    HANDSHAKE_BAUD, CODE_RATES, RATE_CODES, RATE_TEST, RATE_REQUEST,
    RAISED_RATE_IDLE, RATE_PROBE_WAIT, RATE_ANNOUNCE_REPEAT, LinkError)

log = logging.getLogger(__name__)

# Reply constants as LNKSERVER sends them (logs/ captures).
_INSTALL_REPLY_INFO = bytes.fromhex("00000000000a010100010020")
_MAP_REPLY_HEADER = bytes.fromhex("010a1606")   # [op][0a][DOS 6.22]
_MAP_REPLY_LEN = 87
_UNIT_ATTR = 0x08C2
# Per media byte, as LNKSERVER sent them: (media check status, its "changed"
# byte, build BPB status, the 6 bytes after the 25-byte BPB). The status low
# bytes mean nothing to DOS (no error bit) and the trailers are not traced
# in Ghidra: replayed as observed, fixed disk F8 from logs/ session 3 + the
# s56 live disk (two sizes, same values), 1.44M floppy F0 from live_write
# 57600_2 (s57). Other media fall back to the fixed-disk values. The
# floppy's media-check low byte varied between runs (13, 14, ff), so it is
# LNKSERVER state rather than a property of the disk; 13 is served.
_MEDIA_REPLY = {
    0xF8: (b"\x02\x01", 0x01, b"\x02\x01", bytes.fromhex("400000052100")),
    0xF0: (b"\x13\x01", 0x00, b"\x09\x01", bytes.fromhex("000000073000")),
}
# First two bytes of a Get Media ID record (the info-level word) as sent.
_MID_INFO = b"\x1d\x60"

ST_NO_MEDIA_CHECK = b"\xff\x01"     # status 0x01ff: media check, empty drive
ST_OK_IO = b"\x00\x01"              # status 0x0100: read / write / ioctl
ST_UNKNOWN_UNIT = b"\x01\x81"       # status 0x8101: unit OOR or port not configured
ST_WRITE_FAULT = b"\x0a\x81"        # status 0x810a: data length mismatch
ST_GENERAL_FAILURE = b"\x0c\x81"    # status 0x810c
ST_SECTOR_NOT_FOUND = b"\x08\x81"   # status 0x8108
ST_UNKNOWN_COMMAND = b"\x03\x81"    # status 0x8103
ST_WRITE_PROTECT = b"\x00\x81"      # status 0x8100
ST_NO_PRINTER = b"\x19"             # INT 17h AH status: no printer (LNKSERVER 2a85)

# Opcodes whose LNKSERVER handler returns with no reply frame (bare yield only).
# Source: LNKSERVER 1d88 (MOV AX,1; RET) and 16f2 (SUB AX,AX; RET), s73.
_OPS_NO_REPLY       = frozenset({0x07, 0x08, 0x09, 0x0C, 0x0D, 0x0F, 0x13})
_PRINTER_OUTPUT_OPS = frozenset({0x0A, 0x0B, 0x14, 0x15})  # status(2)+count(2)
_PRINTER_CTRL_OPS   = frozenset({0x0E, 0x10, 0x11, 0x12})  # status(2)
_BLOCK_OPS          = frozenset({0x16, 0x17, 0x18})         # status(2)
_INT17H_OPS         = frozenset({0x19, 0x1A, 0x1B})         # 1-byte printer st.


# Partition types served. FAT12/FAT16 volumes go on the wire as they are,
# whatever the partition type says (CHS 01/04/06 or LBA 0E): LNKCLIENT sees
# only the volume. FAT32 (0B/0C) is read by the server and offered as a
# generated FAT16 drive unless FAT32 is allowed on the wire (server/fat32.py).
# Extended partitions: 05 (CHS) and 0F (LBA). MS-DOS 6.22 itself letters only
# 01/04/06 and 05; the others are served in the same order.
DOS_FAT_TYPES = (0x01, 0x04, 0x06, 0x0E)
FAT32_TYPES = (0x0B, 0x0C)
EXTENDED_TYPES = (0x05, 0x0F)
_PARTITION_NAMES = {0x07: "NTFS/HPFS", 0x83: "Linux", 0x82: "Linux swap",
                    0xEE: "GPT protective"}

KIND_FAT = "fat"            # FAT12/FAT16: served as is
KIND_FAT32 = "fat32"


def _is_fat_boot(sector: bytes) -> bool:
    """A FAT boot sector (BPB) rather than a partition table."""
    bps = struct.unpack_from("<H", sector, 0x0B)[0]
    return (sector[0] in (0xEB, 0xE9) and bps in (512, 1024, 2048, 4096)
            and sector[0x0D] != 0 and sector[0x15] >= 0xF0)


def _entries(sector: bytes):
    for i in range(4):
        e = sector[446 + 16 * i:462 + 16 * i]
        lba, count = struct.unpack_from("<II", e, 8)
        if e[4] and count:
            yield e[4], lba, count


def _is_fat32_boot(sector: bytes) -> bool:
    root, spf16 = struct.unpack_from("<HxxxH", sector, 0x11)
    return (_is_fat_boot(sector) and root == 0 and spf16 == 0
            and struct.unpack_from("<I", sector, 0x24)[0] != 0)


def list_volumes(f) -> List["tuple[int, str]"]:
    """(sector offset, kind) of the FAT volumes in an image, in drive-letter
    order: a volume image (floppy, mkfs.msdos -C) is one volume at 0; a
    partitioned disk (MBR at sector 0) gives its primary partition, then
    the logical drives of its extended partition in chain order. Kinds:
    KIND_FAT (FAT12/16) or KIND_FAT32. Partitions that hold no FAT volume
    are skipped (logged)."""
    def kind_at(lba: int) -> Optional[str]:
        f.seek(lba * SECTOR_SIZE)
        sec = f.read(SECTOR_SIZE)
        if len(sec) < SECTOR_SIZE or not _is_fat_boot(sec):
            return None
        return KIND_FAT32 if _is_fat32_boot(sec) else KIND_FAT

    f.seek(0)
    s0 = f.read(SECTOR_SIZE)
    if len(s0) < SECTOR_SIZE or _is_fat_boot(s0) or s0[510:] != b"\x55\xaa":
        k = kind_at(0)
        return [(0, k or KIND_FAT)]

    def take(typ: int, lba: int, out: list, where: str) -> None:
        k = kind_at(lba)
        if k is None:
            log.warning("%s at sector %d (type %02x): no FAT boot sector "
                        "(not formatted?)", where, lba, typ)
        elif (typ in FAT32_TYPES) != (k == KIND_FAT32):
            log.warning("%s at sector %d: type %02x but the volume is %s",
                        where, lba, typ, k.upper())
            out.append((lba, k))
        else:
            out.append((lba, k))

    primary: list = []
    logical: list = []
    for typ, lba, _ in _entries(s0):
        if typ in DOS_FAT_TYPES or typ in FAT32_TYPES:
            take(typ, lba, primary, "partition")
        elif typ in EXTENDED_TYPES:
            ext, ebr, seen = lba, lba, set()
            while ebr not in seen:
                seen.add(ebr)
                f.seek(ebr * SECTOR_SIZE)
                sec = f.read(SECTOR_SIZE)
                if len(sec) < SECTOR_SIZE or sec[510:] != b"\x55\xaa":
                    log.warning("extended partition: bad EBR at sector %d",
                                ebr)
                    break
                nxt = None
                for t, rel, _ in _entries(sec):
                    if t in DOS_FAT_TYPES or t in FAT32_TYPES:
                        take(t, ebr + rel, logical, "logical drive")
                    elif t in EXTENDED_TYPES:
                        nxt = ext + rel
                    else:
                        log.warning("logical partition type %02x (%s) at "
                                    "sector %d: not a FAT volume, skipped", t,
                                    _PARTITION_NAMES.get(t, "?"), ebr + rel)
                if nxt is None:
                    break
                ebr = nxt
        else:
            log.warning("partition type %02x (%s) at sector %d: not a FAT "
                        "volume, skipped", typ, _PARTITION_NAMES.get(typ, "?"),
                        lba)
    # A disk gives DOS one primary drive (FDISK makes one; with several, DOS
    # uses the first).
    if len(primary) > 1:
        log.warning("%d primary FAT partitions: the first is served (as DOS "
                    "does)", len(primary))
    vols = primary[:1] + logical
    if not vols:
        log.warning("no FAT volume found in the partition table")
    return vols


class Unit:
    """One served drive backed by a disk image / block device: a FAT volume
    image, or one DOS volume (primary partition or logical drive) of a
    partitioned disk image, as LNKSERVER serves a DOS drive, not a disk."""

    def __init__(self, path: str, writable: bool = False,
                 offset: Optional[int] = None) -> None:
        """offset: the volume's first sector in the image (list_volumes);
        default the first DOS volume."""
        self.path = path
        self.writable = writable
        self._f = open(path, "r+b" if writable else "rb")
        if offset is None:
            vols = list_volumes(self._f)
            offset = vols[0][0] if vols else 0
        self.offset = offset
        boot = self.read(0)
        self.boot = boot
        self.bytes_per_sector = struct.unpack_from("<H", boot, 0x0B)[0]
        t16 = struct.unpack_from("<H", boot, 0x13)[0]
        t32 = struct.unpack_from("<I", boot, 0x20)[0]
        self.total_sectors = t16 or t32
        self.serial = boot[0x27:0x2B]
        self.label = boot[0x2B:0x36]
        self.fs_type = boot[0x36:0x3E]
        self.volume_label = self._root_volume_label()

    def _root_volume_label(self) -> bytes:
        """The volume label from the root directory's label entry, which
        is what LNKSERVER reports in media check / drive info (a floppy
        whose BPB says "NO NAME" but has no label entry: empty, s57)."""
        (bps, _, res, nfats, root, _, _, spf) = struct.unpack_from(
            "<HBHBHHBH", self.boot, 0x0B)
        if bps != SECTOR_SIZE or not root:
            return b""
        first = res + nfats * spf
        for n in range((root * 32 + SECTOR_SIZE - 1) // SECTOR_SIZE):
            sector = self.read(first + n)
            if sector is None:
                break
            for off in range(0, SECTOR_SIZE, 32):
                e = sector[off:off + 32]
                if e[0] == 0:
                    return b""
                if e[0] != 0xE5 and e[11] & 0x08 and e[11] != 0x0F:
                    return e[:11].rstrip(b" ")
        return b""

    def read(self, lba: int, count: int = 1) -> Optional[bytes]:
        total = getattr(self, "total_sectors", None)   # unset while probing
        if total is not None and lba + count > total:
            return None
        self._f.seek((self.offset + lba) * SECTOR_SIZE)
        data = self._f.read(SECTOR_SIZE * count)
        if len(data) != SECTOR_SIZE * count:
            return None
        return data

    def write(self, lba: int, data: bytes) -> bool:
        if lba + len(data) // SECTOR_SIZE > self.total_sectors:
            return False
        self._f.seek((self.offset + lba) * SECTOR_SIZE)
        self._f.write(data)
        self._f.flush()
        return True

    @property
    def label_text(self) -> bytes:
        return self.volume_label.rstrip(b" \x00")

    @property
    def media(self) -> int:
        return self.boot[0x15]

    def close(self) -> None:
        self._f.close()


class LinkServer:
    """Serve `units` to an LNKCLIENT client over a raw byte link.

    `io`: tx_byte / rx_byte(timeout) / optional flush (SerialLinkByteIO).
    `offered`: the units LNKCLIENT may map (LNKSERVER's drive list); default
    all non-empty units. `cookie_source` gives this server's 4-byte cookie
    (drawn once: LNKSERVER keeps one cookie for its lifetime).
    """

    def __init__(self, io, units: Sequence[Optional[Unit]],
                 offered: Optional[List[int]] = None,
                 cookie_source: Callable[[], bytes] = lambda: os.urandom(4),
                 timeout: float = 4.0, on_event=None,
                 max_baud: int = 115200) -> None:
        self.io = io
        self.max_baud = max_baud      # highest rate step accepted
        self.baud = HANDSHAKE_BAUD
        self.units = list(units)
        self.offered = (list(offered) if offered is not None else
                        [i for i, u in enumerate(self.units) if u is not None])
        self.cookie_source = cookie_source
        self.timeout = timeout
        self.on_event = on_event or (lambda kind, **kw: None)
        self.seq = 0
        self.cookie = bytes(cookie_source())
        # Cookie of the last client served (zeros until the first install).
        self.client_cookie = b"\x00\x00\x00\x00"
        self.drive_map = bytes([MAP_NONE]) * MAP_LEN
        self.last_rx = 0.0              # when a byte last arrived
        # For the status screen's Transfer: field: when a frame (request,
        # reply or sector data) last moved, and when an idle keepalive did.
        self.last_data = 0.0
        self.last_idle = 0.0
        self.on_burst_lost: Optional[Callable[[int], None]] = None

    # ------------------------------------------------------------------
    # byte / frame primitives
    # ------------------------------------------------------------------

    def _tx(self, b: int) -> None:
        self.io.tx_byte(b & 0xFF)

    def _rx(self, timeout: Optional[float] = None) -> Optional[int]:
        f = getattr(self.io, "flush", None)
        if callable(f):
            f()
        b = self.io.rx_byte(self.timeout if timeout is None else timeout)
        if b is not None:
            self.last_rx = time.monotonic()   # the status screen's link light
        return b

    def _rx_req(self, what: str) -> int:
        b = self._rx()
        if b is None:
            raise LinkError(f"timeout waiting for {what}")
        return b

    def _rx_n(self, n: int) -> bytes:
        """Receive a frame body (data + crc) in one bulk read when the link
        has one: byte-by-byte reads only just keep up with 115200, so any
        other load in the process made the ack late enough for LNKCLIENT to
        give up on an 8 KB chunk and send it again tagged | 04 (live_server
        2026-10-08 19:25:33), which LNKSERVER drops as the wrong seq
        (1000:7257) -- a dead link."""
        f = getattr(self.io, "flush", None)
        if callable(f):
            f()
        bulk = getattr(self.io, "rx_bytes", None)
        if not callable(bulk):
            return bytes(self._rx_req("data") for _ in range(n))
        # per-byte timeout plus the line time of n bytes at >= 9600 baud
        data = bulk(n, self.timeout + n * 0.0011)
        if data:
            self.last_rx = time.monotonic()
        if len(data) != n:
            raise LinkError(f"timeout waiting for data ({len(data)} of {n})")
        return data

    def _recv_frame(self, tag: int) -> Optional[bytes]:
        """Receive a client frame whose tag was just read (seq matches)."""
        self.last_data = time.monotonic()
        self._tx((~tag) & 0xFB)
        if tag & 0x80:
            cb = bytes([self._rx_req("count")])
            count = cb[0]
        else:
            cb = bytes([self._rx_req("count"), self._rx_req("count")])
            count = cb[0] | (cb[1] << 8)
        body = self._rx_n(count + 2)
        data, crc = body[:-2], body[-2:]
        if _crc16_prng(cb + data + crc, 0xFFFF) != CRC_RESIDUE:
            log.warning("frame tag 0x%02x: CRC error; not acknowledged", tag)
            return None
        self._tx(tag)
        self.seq = (self.seq + 1) & 3
        self.last_data = time.monotonic()
        return data

    def _send_frame(self, payload: bytes, more: bool = False) -> None:
        """Send one reply frame (its tag is also the previous frame's
        post-ack byte). `more`: a data chunk with another after it."""
        env = encode_envelope(payload, self.seq, more)
        tag = env[0]
        self.last_data = time.monotonic()
        self._tx(tag)
        echo = self._rx_req(f"echo of 0x{tag:02x}")
        if echo != (~tag) & 0xFB:
            raise LinkError(f"tag 0x{tag:02x}: echo 0x{echo:02x}")
        for b in env[1:]:
            self._tx(b)
        try:
            ack = self._rx_req(f"ack of 0x{tag:02x}")
        except LinkError:
            # LNKCLIENT did not take a long burst (bytes lost: its port
            # could not keep up): let the owner slow the link down
            if len(payload) > 0xFF and self.on_burst_lost is not None:
                self.on_burst_lost(len(payload))
            raise
        if ack != tag:
            raise LinkError(f"tag 0x{tag:02x}: ack 0x{ack:02x}")
        self.seq = (self.seq + 1) & 3
        self.last_data = time.monotonic()

    def _reply(self, frames: List[bytes]) -> None:
        """Send reply frames, then yield the line (bare seq). Sector data
        (frames after the first, over 0xff bytes) goes as 16-bit chunks,
        each but the last tagged 0x40|seq."""
        for i, payload in enumerate(frames):
            chunk = i > 0 and len(payload) > 0xFF
            self._send_frame(payload, more=chunk and i < len(frames) - 1)
        self._tx(self.seq)                  # yield

    def _set_baud(self, baud: int) -> None:
        f = getattr(self.io, "set_baud", None)
        if callable(f):
            f(baud)
        if baud != self.baud:
            self.baud = baud
            self.on_event("rate", baud=baud)

    def _rate_step(self) -> "tuple[bool, Optional[int]]":
        """LNKCLIENT sent ff after the handshake / a step: answer 00, take
        its rate code, switch, and answer the test exchange (session.py,
        "Line speed"). Returns (stepped, byte): stepped False if the step
        failed (the 2 s idle fallback then returns the line to 9600); byte
        is a non-code byte received in place of the code, which LNKSERVER
        processes as usual (strace capture 18:13:03: a garbled 24, then the
        install frame at 9600)."""
        self._tx(RATE_REQUEST[1])
        code = self._rx()
        rate = CODE_RATES.get(code)
        if rate is None:
            return False, code
        if rate > self.max_baud:
            log.warning("rate step: %d baud is above /BAUD", rate)
            return False, None
        self._set_baud(rate)
        hi = (code << 4) & 0xFF
        # Announce, repeating it every ~7 ms until LNKCLIENT echoes it.
        end = time.monotonic() + RAISED_RATE_IDLE
        while True:
            self._tx(hi)
            b = self._rx(RATE_ANNOUNCE_REPEAT)
            if b is not None or time.monotonic() >= end:
                break
        if b != hi:
            log.warning("rate step %d: no echo of 0x%02x", rate, hi)
            return False, None
        self._tx((~hi) & 0xFF)
        for tx, rep in RATE_TEST:
            b = self._rx()
            if b != tx:
                log.warning("rate step %d: test byte %r, expected 0x%02x",
                            rate, b, tx)
                return False, None
            self._tx(rep)
        return True, None

    def _rate_probe(self) -> None:
        """LNKSERVER's own step at the client's first yield when the client
        stopped below our /BAUD: ~seq, the repeated yield, the next code,
        then ~0.45 s at that rate (session.py, "Line speed")."""
        nxt = min((r for r in RATE_CODES if r > self.baud), default=None)
        if nxt is None or nxt > self.max_baud:
            return
        self._tx((~self.seq) & 0xFF)
        if self._rx() != self.seq:
            return
        old = self.baud
        self._tx(RATE_CODES[nxt])
        self._set_baud(nxt)
        b = self._rx(RATE_PROBE_WAIT)
        if b is not None:
            # A client taking the offer was never captured (LNKCLIENT stays
            # at its /BAUD); stay at the old rate.
            log.warning("rate probe %d: unexpected 0x%02x", nxt, b)
        self._set_baud(old)

    # ------------------------------------------------------------------
    # main loop
    # ------------------------------------------------------------------

    @staticmethod
    def handshake_tail(io, timeout: Optional[float]) -> None:
        """After aa -> 00: expect 55 -> ff, 5a -> 11."""
        f = getattr(io, "flush", None)
        for exp, rep in ((0x55, 0xFF), (0x5A, 0x11)):
            if callable(f):
                f()
            b = io.rx_byte(timeout)
            if b != exp:
                raise LinkError(f"handshake: got {b!r}, expected 0x{exp:02x}")
            io.tx_byte(rep)
        if callable(f):
            f()

    def serve(self, idle_timeout: Optional[float] = None) -> None:
        """Process the link until it is quiet for idle_timeout seconds (None:
        forever). LNKCLIENT's aa/55/5a handshake (re)starts a session at
        9600 from seq 0 whenever it arrives; rate steps may follow it."""
        self._set_baud(HANDSHAKE_BAUD)
        pending: Optional[bytes] = None
        push_for: Optional[bytes] = None    # op05 request awaiting its data
        pushed = bytearray()
        stepping = False                    # rate steps may come next
        probe = False                       # rate probe due at next yield
        quiet_since = time.monotonic()
        again: Optional[int] = None         # a byte to process once more
        while True:
            wait = idle_timeout
            if self.baud != HANDSHAKE_BAUD:
                wait = (RAISED_RATE_IDLE if wait is None
                        else min(wait, RAISED_RATE_IDLE))
            if again is not None:
                b, again = again, None
            else:
                b = self._rx(wait)
            if b is None:
                if self.baud != HANDSHAKE_BAUD:
                    # LNKSERVER: 2 s of silence at a raised rate -> 9600,
                    # waiting for a new handshake
                    log.info("link quiet at %d baud; back to %d", self.baud,
                             HANDSHAKE_BAUD)
                    self._set_baud(HANDSHAKE_BAUD)
                    pending, push_for, stepping = None, None, False
                    pushed.clear()
                    if (idle_timeout is not None and time.monotonic()
                            - quiet_since >= idle_timeout):
                        return
                    continue
                if idle_timeout is not None:
                    return
                continue
            quiet_since = time.monotonic()
            if b == 0xAA and push_for is None:
                # LNKCLIENT (re)starting: finish the handshake, reset the seq
                self._tx(0x00)
                try:
                    LinkServer.handshake_tail(self.io, self.timeout)
                except LinkError as e:
                    log.debug("%s", e)
                    continue
                self.seq, pending, stepping, probe = 0, None, True, True
                self.on_event("handshake")
                continue
            if stepping and b == RATE_REQUEST[0]:
                stepping, again = self._rate_step()
                continue
            stepping = False
            if b >= 0xFC and push_for is None:
                self._tx(b)                 # idle keepalive: echo
                self.last_idle = time.monotonic()
                continue
            if (b & 3) != self.seq:
                continue                    # not ours (stale / probe)
            if b & 0x80 or push_for is not None:
                try:
                    data = self._recv_frame(b)
                except LinkError as e:
                    # A frame cut short (bytes lost, client gone): a write
                    # waiting for its data is abandoned too -- unacked, so
                    # the client sends it again -- and the next aa is a
                    # handshake, not more write data.
                    log.debug("frame abandoned: %s", e)
                    if push_for is not None:
                        log.info("write abandoned: its data did not arrive")
                        push_for = None
                        pushed.clear()
                    continue
                if data is None:
                    continue
                if push_for is not None:
                    pushed += data
                    if not b & MORE_FLAG:
                        pending, push_for = (push_for, bytes(pushed)), None
                        pushed.clear()
                elif data[0] in (OP_WRITE, OP_WRITE_VERIFY):
                    push_for = data
                elif (data[0] in _PRINTER_OUTPUT_OPS and len(data) >= 4
                      and struct.unpack_from("<H", data, 2)[0] >= 2):
                    push_for = data
                elif (data[0] == OP_IOCTL and len(data) >= 4
                      and data[2] == IOCTL_CAT_DISK
                      and data[3] == IOCTL_SET_MEDIA_ID):
                    push_for = data
                elif data[:2] == bytes([OP_INSTALL, 0]) and len(data) == 2:
                    self.on_event("install")
                else:
                    pending = data
                continue
            # bare seq, nothing pushed expected: the client yields the line
            if pending is None:
                continue
            req, pending = pending, None
            if probe:
                probe = False
                self._rate_probe()
            try:
                self._reply(self._handle(req))
            except Exception as exc:
                log.warning("request handler error: %s", exc)
                try:
                    self._reply([ST_GENERAL_FAILURE])
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # request handlers
    # ------------------------------------------------------------------

    def _unit(self, n: int) -> Optional[Unit]:
        return self.units[n] if 0 <= n < len(self.units) else None

    def _handle(self, req) -> List[bytes]:
        if isinstance(req, tuple):
            orig, push = req
            if orig[0] in _PRINTER_OUTPUT_OPS:
                count = (struct.unpack_from("<H", orig, 2)[0]
                         if len(orig) >= 4 else 0)
                return [ST_UNKNOWN_UNIT + struct.pack("<H", count)]
            if orig[0] == OP_IOCTL:
                unit = orig[1] if len(orig) > 1 else 0
                if self._unit(unit) is None:
                    return [ST_UNKNOWN_UNIT]
                return [ST_OK_IO]
            return self._write(orig, push)
        op, unit = req[0], req[1] if len(req) > 1 else 0
        self.on_event("request", op=op, unit=unit)
        u = self._unit(unit)
        if op == OP_INSTALL:
            return [self._install(req)]
        if op == OP_DRIVE_MAP:
            return [self._drive_map(req)]
        if op == OP_DRIVE_INFO:
            # LNKSERVER gives size text and label for its fixed disk only:
            # a 1.44M floppy (media F0) answered 23 zero bytes (live_write
            # 57600_2, s57), as an empty drive does.
            if u is None or u.media != 0xF8:
                return [b"\x00" * 23]
            size = f"({u.total_sectors * u.bytes_per_sector // 1000000}Mb)"
            return [size.encode().ljust(10, b"\x00")
                    + u.label_text.ljust(13, b"\x00")]
        if op == OP_MEDIA_CHECK:
            if u is None:
                return [ST_NO_MEDIA_CHECK + b"\x00" * 17]
            mc_status, changed, _, _ = self._media_reply(u)
            if getattr(u, "media_changed", False):
                # The served directory changed on the server side
                # (server/dirsync.py): answer 0xFF "media changed" so DOS
                # re-reads the FAT and directories unconditionally (0x00
                # "don't know" is a floppy answer; for 0xF8 fixed-disk DOS
                # treats it as "not changed" and keeps its cached FAT).
                changed = 0xFF
            return [mc_status + bytes([changed])
                    + u.label_text.ljust(12, b"\x00") + u.serial]
        if op == OP_BUILD_BPB:
            if u is None:
                return [ST_UNKNOWN_UNIT]
            _, _, bpb_status, trailer = self._media_reply(u)
            if getattr(u, "media_changed", False):
                u.media_changed = False     # DOS took the change
            return [bpb_status + u.boot[0x0B:0x24] + trailer]
        if op == OP_READ:
            if u is None:
                return [ST_UNKNOWN_UNIT + b"\x00\x00"]
            if len(req) < 11:
                return [ST_SECTOR_NOT_FOUND + b"\x00\x00"]
            count = struct.unpack_from("<H", req, 3)[0]
            lba = struct.unpack_from("<I", req, 7)[0]
            data = (u.read(lba, count) if 1 <= count <= MAX_SECTORS
                    else None)
            if data is None:
                return [ST_SECTOR_NOT_FOUND + b"\x00\x00"]
            self.on_event("read", unit=unit, lba=lba, count=count)
            return [ST_OK_IO + struct.pack("<H", count)] + [
                data[i:i + CHUNK_BYTES]
                for i in range(0, len(data), CHUNK_BYTES)]
        if op == OP_IOCTL:
            if u is None:
                return [ST_UNKNOWN_UNIT]
            if req[2:4] != bytes([IOCTL_CAT_DISK, IOCTL_GET_MEDIA_ID]):
                return [ST_GENERAL_FAILURE]
            return [ST_OK_IO, _MID_INFO + u.serial + u.label + u.fs_type]
        if op in _OPS_NO_REPLY:
            return []
        if op in _PRINTER_OUTPUT_OPS:
            count = struct.unpack_from("<H", req, 2)[0] if len(req) >= 4 else 0
            return [ST_UNKNOWN_UNIT + struct.pack("<H", count)]
        if op in _PRINTER_CTRL_OPS:
            return [ST_OK_IO]
        if op in _BLOCK_OPS:
            return [ST_OK_IO]
        if op in _INT17H_OPS:
            return [ST_NO_PRINTER]
        return [ST_UNKNOWN_COMMAND]

    @staticmethod
    def _media_reply(u: Unit):
        return _MEDIA_REPLY.get(u.media, _MEDIA_REPLY[0xF8])

    def _write(self, req: bytes, data: bytes) -> List[bytes]:
        if len(req) < 11:
            return [ST_SECTOR_NOT_FOUND + b"\x00\x00"]
        unit = req[1]
        count = struct.unpack_from("<H", req, 3)[0]
        lba = struct.unpack_from("<I", req, 7)[0]
        u = self._unit(unit)
        self.on_event("write", unit=unit, lba=lba, count=count)
        if u is None:
            return [ST_UNKNOWN_UNIT + b"\x00\x00"]
        if not u.writable:
            return [ST_WRITE_PROTECT + b"\x00\x00"]
        if len(data) != count * SECTOR_SIZE or not u.write(lba, data):
            return [ST_WRITE_FAULT + b"\x00\x00"]
        return [ST_OK_IO + struct.pack("<H", count)]

    def _install(self, req: bytes) -> bytes:
        """Reply = [previous client's cookie][this server's cookie]: LNKSERVER
        reports the last client it served (zeros after its start) and its own
        lifetime cookie (logs/ captures + live traces s56)."""
        previous, self.client_cookie = self.client_cookie, req[26:30]
        self.on_event("connected")
        return (bytes([OP_INSTALL]) + _INSTALL_REPLY_INFO + b"\x00" * 12
                + previous + self.cookie)

    def _drive_map(self, req: bytes) -> bytes:
        cmap = bytearray(req[7:7 + MAP_LEN])
        for i, v in enumerate(cmap):
            if v != MAP_LOCAL:
                cmap[i] = MAP_NONE
        free = [i for i, v in enumerate(cmap[:N_DRIVES]) if v == MAP_NONE]
        for unit, slot in zip(self.offered, free):
            cmap[slot] = unit
        self.drive_map = bytes(cmap)
        self.on_event("mapped", drive_map=self.drive_map)
        n = len(self.units)
        out = (_MAP_REPLY_HEADER + bytes([n]) + bytes(cmap)
               + struct.pack(f"<{n}H", *([_UNIT_ATTR] * n)))
        return out.ljust(_MAP_REPLY_LEN, b"\x00")
