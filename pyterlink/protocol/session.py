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
"""LNKCLIENT client session: the complete LNKCLIENT <-> LNKSERVER wire
protocol as observed in the com3 captures (logs/com3_traffic_*.log.gz).

The protocol is a remote DOS block-device driver. After the link handshake
(aa/55/5a -> 00/ff/11) the client issues requests whose byte 1 is the SERVER
UNIT (0 = A:, 1 = B:, 2 = C: ... on the LNKSERVER machine), and the block ops
mirror the DOS device-driver request set: media check, build BPB, input,
output, generic IOCTL. Replies to block ops start with the 16-bit DOS
device-driver status word (bit 15 error, bit 8 done, low byte error code).

Link layer (every byte in the captures is accounted for by these rules)
-----------------------------------------------------------------------
frame         tag | count (1 byte if tag&0x80 else 2 LE) | data | crc (2)
              tag low 2 bits = shared mod-4 seq, advanced by every frame in
              either direction. crc: framing.encode_envelope.
send          sender tx tag; receiver echoes (~tag & 0xfb); sender bursts
              count+data+crc; receiver acks with the tag byte.
after ack     the SENDER owns the line and its next byte is one of:
                * a tag with bit7 set     -> another frame follows (it keeps
                                             the line; e.g. op1d status -> data)
                * a bare seq (bit7 clear) -> it yields the line, OR (where the
                                             opcode says sector data follows:
                                             op04 reply, op05 request) the tag
                                             of a 16-bit-count data chunk.
data chunks   sector data moves in chunks of up to 8192 bytes (16 sectors).
              Every chunk but the last has tag 0x40|seq ("another follows");
              the last has the bare seq.
idle          the line owner sends 0xfc | (next_seq ^ 1) about once per BIOS
              tick (55 ms); the peer echoes it. (fd->80 fc->81 ff->82 fe->83)

Line speed (strace captures of both DOSBoxes, logs/strace_*.log.gz, s57)
-----------------------------------------------------------------------
The handshake is always at 9600. LNKCLIENT then steps the rate up one code at
a time to its /BAUD, both sides reprogramming their UARTs:
  L ff -> S 00, L code       (sent at the old rate; codes 04 19200, 05 38400,
                              06 57600, 07 115200)
  at the new rate:  S code<<4, L code<<4, S ~(code<<4), then 15 test pairs
                    (RATE_TEST below), LNKCLIENT sending, LNKSERVER answering.
                    LNKSERVER repeats code<<4 every ~7 ms until it is echoed
                    (18:28:11, a slow LNKCLIENT); the extra copies, and bytes
                    garbled by the switch before the first, are skipped.
After the last step LNKCLIENT's next byte is the install frame's tag. At a
raised rate LNKSERVER drops back to 9600 after 2 s of silence, so the idle
keepalive is required there; LNKCLIENT re-handshakes after a lost link.

If LNKCLIENT stopped below LNKSERVER's own /BAUD, LNKSERVER probes one code
higher at the client's first yield (after the install request):
  L seq -> S ~seq (fd), L seq again -> S next code, and LNKSERVER switches to
  that rate; LNKCLIENT stays put, and after ~0.45 s (8 ticks) of silence
  LNKSERVER switches back and sends the install reply as usual.

Requests (client -> server) and replies
---------------------------------------
00 install    [00 00]                      fire-and-forget, then immediately
              [00 00][info 12][0 x12][client cookie 4][server cookie 4]
              -> [00][00 00 00 00 00 0a 01 01 00 01 00 20][0 x12]
                 [previous client's cookie 4][server cookie 4]
              The client sends the server cookie it last saw (0 on a first
              connect); the server reports the last client it served (0 after
              its start) and its own cookie, constant for its lifetime.
01 drive map  [01 01 00 16 06 03 03][map 29]
              -> [01 0a 16 06 n_units][map 29][attr16 x n_units][0 ...]
              (87 bytes; 16 06 = DOS 6.22). map[i] for client drive letter
              i (A: = 0; entries past Z: are always fe): ff local, fe
              unassigned, else the server unit it redirects to.
1c drive info [1c unit] -> [size text 10][volume label 13]  e.g. "(106Mb)"
02 media chk  [02 unit media]  -> [status 2][changed 1][label 12][serial 4]
03 build BPB  [03 unit media]  -> [status 2][BPB 31]
04 read       [04 unit media][count 2][ffff][lba 4]
              -> [status 2][count 2]  then the sectors as data chunks
05 write      [05 unit media][count 2][ffff][lba 4]  then the sectors as
              data chunks  -> [status 2][count 2]
1d ioctl      [1d unit 08 66] (generic IOCTL, Get Media ID)
              -> [status 2]  then [info 2][serial 4][label 11][fstype 8]
count is 1..127 sectors (DOS asks for up to 0x7f; 1, 9, 16, 32, 112, 127
captured). The 0xffff start-sector word selects the 32-bit lba field (DOS
4+ huge-sector convention).
"""
from __future__ import annotations

import logging
import os
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional

from .framing import encode_envelope, _crc16_prng, CRC_RESIDUE, MORE_FLAG

log = logging.getLogger(__name__)

OP_INSTALL = 0x00
OP_DRIVE_MAP = 0x01
OP_MEDIA_CHECK = 0x02
OP_BUILD_BPB = 0x03
OP_READ = 0x04
OP_WRITE = 0x05
# Write with verify (DOS device command 9, e.g. VERIFY ON or XCOPY /V):
# LNKSERVER runs it through the write handler (1000:1c67, the request table
# at DS:0x2c8 maps 05 and 06 to it); same request, data push and reply.
OP_WRITE_VERIFY = 0x06
OP_DRIVE_INFO = 0x1C
OP_IOCTL = 0x1D

IOCTL_CAT_DISK = 0x08
IOCTL_GET_MEDIA_ID = 0x66
IOCTL_SET_MEDIA_ID = 0x46

STATUS_ERROR = 0x8000
STATUS_DONE = 0x0100

SECTOR_SIZE = 512
CHUNK_BYTES = 8192        # sector data per frame (16 sectors)
MAX_SECTORS = 0x7F        # largest op04/op05 count LNKCLIENT sends

HANDSHAKE_BAUD = 9600
# Rate codes LNKCLIENT sends to step the line speed up (one at a time, from
# 04, up to its /BAUD).
RATE_CODES = {19200: 0x04, 38400: 0x05, 57600: 0x06, 115200: 0x07}
CODE_RATES = {c: r for r, c in RATE_CODES.items()}
# The 15 (LNKCLIENT sends, LNKSERVER answers) test pairs at each new rate,
# after the code<<4 / echo / complement exchange.
RATE_TEST = ((0xFF, 0x00), (0x00, 0xFF), (0x5A, 0xA5), (0x55, 0xAA),
             (0xAA, 0x55), (0xF0, 0xF0), (0x0F, 0x0F), (0xE7, 0xFF),
             (0x7E, 0x00), (0xC3, 0x18), (0x3C, 0x81), (0x81, 0x3C),
             (0x18, 0xC3), (0x00, 0x7E), (0xFF, 0xE7))
RATE_REQUEST = (0xFF, 0x00)   # LNKCLIENT: "next byte is a rate code"
# LNKSERVER falls back to 9600 after this much silence at a raised rate.
RAISED_RATE_IDLE = 2.0
# How long LNKSERVER listens at a probed rate before switching back.
RATE_PROBE_WAIT = 0.45
# LNKSERVER's repeat interval for an unechoed code<<4 announce.
RATE_ANNOUNCE_REPEAT = 0.007
MAP_LOCAL = 0xFF
MAP_NONE = 0xFE
N_DRIVES = 26
MAP_LEN = 29              # drive-map field on the wire (26 letters + 3 fe)

# Constant install-check body fields, as LNKCLIENT sends them.
_INSTALL_INFO = bytes.fromhex("000000000001010000010020")
# Constant drive-map request header, as LNKCLIENT sends it.
_MAP_HEADER = bytes.fromhex("0100160603" "03")

# DOS device-driver error codes (low byte of an error status).
DEVICE_ERRORS = {
    0x00: "write-protect violation", 0x01: "unknown unit",
    0x02: "drive not ready", 0x03: "unknown command", 0x04: "CRC error",
    0x05: "bad request length", 0x06: "seek error", 0x07: "unknown media",
    0x08: "sector not found", 0x09: "printer out of paper",
    0x0A: "write fault", 0x0B: "read fault", 0x0C: "general failure",
    0x0F: "invalid disk change",
}


class LinkError(Exception):
    """Link-layer failure (timeout, bad echo/ack, CRC)."""


class DeviceError(Exception):
    """The server's device driver returned an error status."""

    def __init__(self, op: int, unit: int, status: int) -> None:
        self.op, self.unit, self.status = op, unit, status
        self.code = status & 0xFF
        super().__init__(
            f"op{op:02x} unit {unit}: device error 0x{status:04x} "
            f"({DEVICE_ERRORS.get(self.code, 'unknown')})")


@dataclass
class InstallReply:
    raw: bytes
    client_cookie: bytes
    server_cookie: bytes


@dataclass
class DriveMap:
    raw: bytes
    n_units: int
    drive_map: bytes          # 29 entries, index = client drive letter
    unit_attrs: List[int] = field(default_factory=list)

    def mapped(self) -> dict:
        """{client drive letter: server unit} for redirected letters."""
        return {chr(ord("A") + i): u
                for i, u in enumerate(self.drive_map[:N_DRIVES])
                if u not in (MAP_LOCAL, MAP_NONE)}


@dataclass
class DriveInfo:
    size_text: str
    label: str


@dataclass
class MediaCheck:
    status: int
    changed: int              # 1 unchanged, 0 don't know, 0xff changed
    label: str
    serial: int


@dataclass
class BPB:
    raw: bytes
    bytes_per_sector: int
    sectors_per_cluster: int
    reserved_sectors: int
    n_fats: int
    root_entries: int
    total_sectors: int
    media: int
    sectors_per_fat: int
    sectors_per_track: int
    heads: int
    hidden_sectors: int

    @classmethod
    def parse(cls, b: bytes) -> "BPB":
        if len(b) < 25:
            raise LinkError(f"op03 BPB reply too short: {len(b)} bytes")
        (bps, spc, res, nf, root, t16, media, spf, spt, heads, hidden,
         t32) = struct.unpack_from("<HBHBHHBHHHII", b, 0)
        return cls(bytes(b), bps, spc, res, nf, root, t16 or t32, media, spf,
                   spt, heads, hidden)


@dataclass
class MediaID:
    serial: int
    label: str
    fs_type: str


def _status(data: bytes) -> int:
    return struct.unpack_from("<H", data, 0)[0] if len(data) >= 2 else 0


def _text(b: bytes) -> str:
    return b.split(b"\x00", 1)[0].decode("latin-1").rstrip()


class LinkSession:
    """One client-side LNKCLIENT session over a raw byte link.

    `io` provides tx_byte(b), rx_byte(timeout) -> int|None and optionally
    flush() (transport.serial_backend.SerialLinkByteIO). The link handshake
    must already have run (see handshake()); the shared frame seq then starts
    at 0, as LNKCLIENT's first frame after every handshake in the captures
    is tag 0x80 -- including a reconnect to an LNKSERVER whose previous
    session ended at seq 3.

    All public operations are serialised by an internal lock, so the optional
    keepalive thread (start_keepalive) can share the link safely.
    """

    def __init__(self, io, seq: int = 0, timeout: float = 4.0,
                 max_baud: int = HANDSHAKE_BAUD) -> None:
        self.io = io
        self.max_baud = max_baud      # /BAUD: the rate establish() steps to
        self.baud = HANDSHAKE_BAUD
        self.seq = seq & 3
        self.timeout = timeout
        self.lock = threading.RLock()
        self._last_activity = time.monotonic()
        self._ka_thread: Optional[threading.Thread] = None
        self._ka_stop = threading.Event()
        self.client_cookie: Optional[bytes] = None
        self.server_cookie = b"\x00\x00\x00\x00"

    # ------------------------------------------------------------------
    # byte / frame primitives
    # ------------------------------------------------------------------

    def _tx(self, b: int) -> None:
        self.io.tx_byte(b & 0xFF)

    def _flush(self) -> None:
        f = getattr(self.io, "flush", None)
        if callable(f):
            f()

    def _rx(self, what: str) -> int:
        self._flush()
        b = self.io.rx_byte(self.timeout)
        if b is None:
            raise LinkError(f"timeout waiting for {what}")
        return b

    def _rx_n(self, n: int, what: str) -> bytes:
        """Receive n bytes. Uses the link's bulk read when it has one, so a
        512-byte block is not 500+ separate reads: LNKSERVER abandons the
        exchange if our ack is ~1 s late (18-tick timeout, 1000:7257)."""
        self._flush()
        bulk = getattr(self.io, "rx_bytes", None)
        if callable(bulk):
            # per-byte timeout plus the line time of n bytes at >= 9600 baud
            data = bulk(n, self.timeout + n * 0.0011)
            if len(data) != n:
                raise LinkError(f"timeout in {what} ({len(data)} of {n})")
            return data
        return bytes(self._rx(what) for _ in range(n))

    def _send_frame(self, payload: bytes, more: bool = False) -> None:
        """Send one frame at self.seq (we own the line); end owning it.
        `more`: a data chunk with another after it (tag bit 6)."""
        env = encode_envelope(payload, self.seq, more)
        tag = env[0]
        self._tx(tag)
        echo = self._rx(f"echo of tag 0x{tag:02x}")
        if echo != (~tag) & 0xFB:
            raise LinkError(f"tag 0x{tag:02x}: echo 0x{echo:02x}, expected "
                            f"0x{(~tag) & 0xFB:02x} (seq desync?)")
        for b in env[1:]:
            self._tx(b)
        ack = self._rx(f"ack of tag 0x{tag:02x}")
        if ack != tag:
            raise LinkError(f"tag 0x{tag:02x}: ack 0x{ack:02x} (frame rejected)")
        self.seq = (self.seq + 1) & 3

    def _yield_line(self) -> int:
        """Hand the line to the server; return the tag of its next frame."""
        self._tx(self.seq)
        tag = self._rx("reply tag")
        if tag == (~self.seq) & 0xFF:
            # LNKSERVER's rate probe: repeat the yield, take (and, as
            # LNKCLIENT at its /BAUD, decline) the offered code; the reply
            # follows at the current rate once LNKSERVER gives up.
            self._tx(self.seq)
            code = self._rx("rate probe code")
            log.info("server offered rate code 0x%02x; staying at %d baud",
                     code, self.baud)
            tag = self._rx("reply tag")
        if tag & 3 != self.seq:
            raise LinkError(f"reply tag 0x{tag:02x}: seq {tag & 3}, "
                            f"expected {self.seq}")
        return tag

    def _recv_frame(self, tag: int) -> "tuple[bytes, int]":
        """Receive the frame whose tag was just read; return (data, next).

        `next` is the server's byte after our ack: a bit7 tag (another frame
        follows), or a bare seq (yield, or a pushed 16-bit frame's tag).
        """
        if tag & 3 != self.seq:
            raise LinkError(f"frame tag 0x{tag:02x}: seq {tag & 3}, "
                            f"expected {self.seq}")
        self._tx((~tag) & 0xFB)
        if tag & 0x80:
            cb = bytes([self._rx("count")])
            count = cb[0]
        else:
            cb = bytes([self._rx("count lo"), self._rx("count hi")])
            count = cb[0] | (cb[1] << 8)
        body = self._rx_n(count + 2, "frame data")
        data, crc = body[:-2], body[-2:]
        if _crc16_prng(cb + data + crc, 0xFFFF) != CRC_RESIDUE:
            raise LinkError(f"frame tag 0x{tag:02x}: CRC error")
        self._tx(tag)
        self.seq = (self.seq + 1) & 3
        nxt = self._rx("post-ack byte")
        return data, nxt

    def _expect_yield(self, nxt: int) -> None:
        if nxt & 0x80 or (nxt & 3) != self.seq:
            log.warning("expected server yield seq %d, got 0x%02x",
                        self.seq, nxt)
        self._touch()

    def _touch(self) -> None:
        self._last_activity = time.monotonic()

    def _transact(self, payload: bytes) -> "tuple[bytes, int]":
        """Send a request, yield, receive the first reply frame."""
        self._send_frame(payload)
        tag = self._yield_line()
        return self._recv_frame(tag)

    # ------------------------------------------------------------------
    # link establishment
    # ------------------------------------------------------------------

    @staticmethod
    def handshake(io, timeout: float = 2.0) -> None:
        """The 3-round link handshake: aa/55/5a -> 00/ff/11."""
        for tx, exp in ((0xAA, 0x00), (0x55, 0xFF), (0x5A, 0x11)):
            io.tx_byte(tx)
            f = getattr(io, "flush", None)
            if callable(f):
                f()
            rx = io.rx_byte(timeout)
            if rx != exp:
                raise LinkError(
                    f"handshake: sent 0x{tx:02x}, got "
                    f"{'None' if rx is None else '0x%02x' % rx}, "
                    f"expected 0x{exp:02x}")

    @staticmethod
    def _set_baud(io, baud: int) -> None:
        f = getattr(io, "set_baud", None)
        if callable(f):
            f(baud)

    @staticmethod
    def step_rate(io, code: int, timeout: float = 2.0) -> None:
        """One rate step, right after the handshake or a previous step:
        ff -> 00, the code, then the test exchange at the new rate."""
        def xchg(tx, exp, what):
            io.tx_byte(tx)
            rx = io.rx_byte(timeout)
            if rx != exp:
                raise LinkError(
                    f"rate step {code:02x} ({what}): sent 0x{tx:02x}, got "
                    f"{'None' if rx is None else '0x%02x' % rx}, "
                    f"expected 0x{exp:02x}")
        xchg(*RATE_REQUEST, "request")
        io.tx_byte(code)
        LinkSession._set_baud(io, CODE_RATES[code])   # drains first
        hi = (code << 4) & 0xFF
        # Skip what the switch garbled (strace capture 18:28:11: LNKCLIENT
        # read ff, then LNKSERVER's 40) until the announce arrives.
        end = time.monotonic() + timeout
        rx = io.rx_byte(timeout)
        while rx is not None and rx != hi and time.monotonic() < end:
            rx = io.rx_byte(timeout)
        if rx != hi:
            raise LinkError(f"rate step {code:02x}: got "
                            f"{'None' if rx is None else '0x%02x' % rx}, "
                            f"expected 0x{hi:02x}")
        io.tx_byte(hi)
        rx = io.rx_byte(timeout)
        while rx == hi:                 # LNKSERVER's repeated announce
            rx = io.rx_byte(timeout)
        if rx != (~hi) & 0xFF:
            raise LinkError(f"rate step {code:02x}: got "
                            f"{'None' if rx is None else '0x%02x' % rx}, "
                            f"expected 0x{(~hi) & 0xFF:02x}")
        for i, (tx, exp) in enumerate(RATE_TEST):
            xchg(tx, exp, f"test {i}")

    def establish(self) -> int:
        """Link handshake at 9600, then step up to max_baud as LNKCLIENT
        does. A failed step (a line that cannot carry the faster rate) is
        retried from scratch with the last rate that worked as the
        ceiling, after the 2 s LNKSERVER needs to fall back to 9600.
        Returns the rate reached; seq restarts at 0."""
        ceiling = self.max_baud
        while True:
            self._set_baud(self.io, HANDSHAKE_BAUD)
            self.baud = HANDSHAKE_BAUD
            self.handshake(self.io)
            try:
                for rate, code in sorted(RATE_CODES.items()):
                    if rate > ceiling:
                        break
                    self.step_rate(self.io, code)
                    self.baud = rate
                break
            except LinkError as e:
                log.warning("%s; retrying at %d baud at most", e, self.baud)
                ceiling = self.baud
                self._set_baud(self.io, HANDSHAKE_BAUD)
                self._drain(RAISED_RATE_IDLE + 0.5)
        self.seq = 0
        if self.baud != HANDSHAKE_BAUD:
            log.info("line rate %d baud", self.baud)
        return self.baud

    def _drain(self, seconds: float) -> None:
        """Discard input for `seconds` (lets the server time out)."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.io.rx_byte(0.1)

    # ------------------------------------------------------------------
    # operations
    # ------------------------------------------------------------------

    def install(self, client_cookie: Optional[bytes] = None) -> InstallReply:
        """op00 install + install-check. Returns the server's reply."""
        with self.lock:
            if client_cookie is not None:
                self.client_cookie = bytes(client_cookie)
            elif self.client_cookie is None:
                self.client_cookie = os.urandom(4)
            self._send_frame(bytes([OP_INSTALL, 0]))
            body = (bytes([OP_INSTALL, 0]) + _INSTALL_INFO + b"\x00" * 12
                    + self.client_cookie + self.server_cookie)
            data, nxt = self._transact(body)
            self._expect_yield(nxt)
            if len(data) < 33:
                raise LinkError(f"install reply too short ({len(data)})")
            rep = InstallReply(data, data[25:29], data[29:33])
            self.server_cookie = rep.server_cookie
            return rep

    def drive_map(self, drive_map: Optional[bytes] = None,
                  n_local: int = 3) -> DriveMap:
        """op01: exchange the drive-letter map.

        With no map, request the default: the first `n_local` letters local
        (A:, B:, C:) and the rest unassigned; the server fills in its units.
        A shorter map is padded with fe (unassigned) to the 29-entry field.
        """
        with self.lock:
            if drive_map is None:
                drive_map = bytes([MAP_LOCAL]) * n_local
            drive_map = bytes(drive_map).ljust(MAP_LEN, bytes([MAP_NONE]))
            if len(drive_map) != MAP_LEN:
                raise ValueError(f"drive map has more than {MAP_LEN} entries")
            req = bytes([OP_DRIVE_MAP]) + _MAP_HEADER + drive_map
            data, nxt = self._transact(req)
            self._expect_yield(nxt)
            if len(data) < 5 + MAP_LEN:
                raise LinkError(
                    f"op01 drive_map reply too short: {len(data)} bytes")
            n = data[4]
            dmap = data[5:5 + MAP_LEN]
            if len(data) < 5 + MAP_LEN + n * 2:
                raise LinkError(
                    f"op01 drive_map: reply too short for {n} attrs")
            attrs = list(struct.unpack_from(f"<{n}H", data, 5 + MAP_LEN))
            return DriveMap(data, n, dmap, attrs)

    def drive_info(self, unit: int) -> DriveInfo:
        """op1c: the size/label text LNKCLIENT shows ("(106Mb)", label)."""
        with self.lock:
            data, nxt = self._transact(bytes([OP_DRIVE_INFO, unit]))
            self._expect_yield(nxt)
            return DriveInfo(_text(data[:10]), _text(data[10:23]))

    def media_check(self, unit: int, media: int = 0xF8) -> MediaCheck:
        """op02: device-driver MEDIA CHECK."""
        with self.lock:
            data, nxt = self._transact(bytes([OP_MEDIA_CHECK, unit, media]))
            self._expect_yield(nxt)
            st = _status(data)
            if st & STATUS_ERROR:
                raise DeviceError(OP_MEDIA_CHECK, unit, st)
            serial = struct.unpack_from("<I", data, 15)[0] \
                if len(data) >= 19 else 0
            return MediaCheck(st, data[2] if len(data) > 2 else 0,
                              _text(data[3:15]), serial)

    def build_bpb(self, unit: int, media: int = 0xF8) -> BPB:
        """op03: device-driver BUILD BPB."""
        with self.lock:
            data, nxt = self._transact(bytes([OP_BUILD_BPB, unit, media]))
            self._expect_yield(nxt)
            st = _status(data)
            if st & STATUS_ERROR:
                raise DeviceError(OP_BUILD_BPB, unit, st)
            return BPB.parse(data[2:])

    @staticmethod
    def _io_request(op: int, unit: int, media: int, lba: int,
                    count: int = 1) -> bytes:
        return (bytes([op, unit, media]) + struct.pack("<H", count)
                + b"\xff\xff" + struct.pack("<I", lba & 0xFFFFFFFF))

    def read_sectors(self, unit: int, lba: int, count: int = 1,
                     media: int = 0xF8) -> bytes:
        """op04: device-driver INPUT of `count` (1..127) sectors."""
        if not 1 <= count <= MAX_SECTORS:
            raise ValueError(f"count {count} not in 1..{MAX_SECTORS}")
        with self.lock:
            status, nxt = self._transact(
                self._io_request(OP_READ, unit, media, lba, count))
            st = _status(status)
            if st & STATUS_ERROR:
                self._expect_yield(nxt)
                raise DeviceError(OP_READ, unit, st)
            # The server keeps the line: `nxt` is the first chunk's tag.
            out = bytearray()
            limit = count * SECTOR_SIZE
            while True:
                tag = nxt
                data, nxt = self._recv_frame(tag)
                out += data
                if len(out) > limit:
                    raise LinkError(
                        f"op04 count {count}: server sent too much data")
                if not tag & MORE_FLAG:
                    break
            self._expect_yield(nxt)
            if len(out) != limit:
                raise LinkError(f"op04 count {count}: got {len(out)} bytes")
            return bytes(out)

    def read_sector(self, unit: int, lba: int, media: int = 0xF8) -> bytes:
        return self.read_sectors(unit, lba, 1, media)

    def write_sectors(self, unit: int, lba: int, data: bytes,
                      media: int = 0xF8) -> None:
        """op05: device-driver OUTPUT of len(data)/512 (1..127) sectors."""
        count, rem = divmod(len(data), SECTOR_SIZE)
        if rem or not 1 <= count <= MAX_SECTORS:
            raise ValueError(f"write of {len(data)} bytes: need 1.."
                             f"{MAX_SECTORS} whole sectors")
        with self.lock:
            self._send_frame(
                self._io_request(OP_WRITE, unit, media, lba, count))
            # We keep the line and push the sectors in 8 KB chunks.
            for off in range(0, len(data), CHUNK_BYTES):
                self._send_frame(bytes(data[off:off + CHUNK_BYTES]),
                                 more=off + CHUNK_BYTES < len(data))
            tag = self._yield_line()
            status, nxt = self._recv_frame(tag)
            self._expect_yield(nxt)
            st = _status(status)
            if st & STATUS_ERROR:
                raise DeviceError(OP_WRITE, unit, st)

    def write_sector(self, unit: int, lba: int, data: bytes,
                     media: int = 0xF8) -> None:
        if len(data) != SECTOR_SIZE:
            raise ValueError(f"write_sector needs {SECTOR_SIZE} bytes")
        self.write_sectors(unit, lba, data, media)

    def get_media_id(self, unit: int) -> MediaID:
        """op1d: generic IOCTL 08/66 (Get Media ID)."""
        with self.lock:
            status, nxt = self._transact(
                bytes([OP_IOCTL, unit, IOCTL_CAT_DISK, IOCTL_GET_MEDIA_ID]))
            st = _status(status)
            if st & STATUS_ERROR or not nxt & 0x80:
                self._expect_yield(nxt)
                raise DeviceError(OP_IOCTL, unit, st)
            data, nxt = self._recv_frame(nxt)
            self._expect_yield(nxt)
            if len(data) < 25:
                raise LinkError(
                    f"op1d get_media_id reply too short: {len(data)} bytes")
            serial = struct.unpack_from("<I", data, 2)[0]
            return MediaID(serial, data[6:17].decode("latin-1").rstrip(),
                           data[17:25].decode("latin-1").rstrip())

    def reestablish(self, drive_map: Optional[bytes] = None) -> None:
        """Handshake again and re-run install + drive map (seq restarts at
        0). Lets a lost link or a restarted LNKSERVER be picked up."""
        with self.lock:
            # Let the server time out of any half-received frame (and, at a
            # raised rate, fall back to 9600) first.
            self._drain(RAISED_RATE_IDLE + 0.5
                        if self.baud != HANDSHAKE_BAUD else 1.0)
            self.establish()
            self.install()
            self.drive_map(drive_map)

    # ------------------------------------------------------------------
    # idle keepalive
    # ------------------------------------------------------------------

    def keepalive(self) -> None:
        """Send one idle byte (0xfc | next_seq ^ 1) and check its echo."""
        with self.lock:
            b = 0xFC | ((self.seq ^ 1) & 3)
            self._tx(b)
            echo = self._rx("keepalive echo")
            if echo != b:
                raise LinkError(f"keepalive 0x{b:02x}: echo 0x{echo:02x}")
            self._touch()

    def start_keepalive(self, interval: float = 0.055) -> None:
        """Send keepalives while idle, as LNKCLIENT does once per BIOS tick."""
        if self._ka_thread is not None:
            return
        self._ka_stop.clear()

        def run():
            while not self._ka_stop.wait(interval):
                if time.monotonic() - self._last_activity < interval:
                    continue
                if not self.lock.acquire(blocking=False):
                    continue
                try:
                    self.keepalive()
                except LinkError as e:
                    log.warning("keepalive: %s", e)
                finally:
                    self.lock.release()

        self._ka_thread = threading.Thread(target=run, name="lnkclient-ka",
                                           daemon=True)
        self._ka_thread.start()

    def stop_keepalive(self) -> None:
        if self._ka_thread is not None:
            self._ka_stop.set()
            self._ka_thread.join()
            self._ka_thread = None


class UnitDevice:
    """One server unit as a sector device (read_sector/write_sector(lba)),
    the shape RemoteBlockDevice expects of its client.

    On a link failure the session is re-established the way LNKCLIENT does
    it -- handshake, install with the saved cookies (the server recognises
    the reconnect), drive map -- and the request is retried once.
    """

    def __init__(self, session: LinkSession, unit: int,
                 media: int = 0xF8, drive_map: Optional[bytes] = None) -> None:
        self.session, self.unit, self.media = session, unit, media
        self.drive_map = drive_map

    def _retry(self, fn):
        try:
            return fn()
        except LinkError as e:
            log.warning("link error (%s); re-establishing", e)
            self.session.reestablish(self.drive_map)
            return fn()

    def read_sectors(self, lba: int, count: int = 1) -> bytes:
        return self._retry(lambda: self.session.read_sectors(
            self.unit, lba, count, self.media))

    def write_sectors(self, lba: int, data: bytes,
                      retry: bool = True) -> None:
        """retry=False: a link failure is raised as is (the caller
        re-establishes; block.py's adaptive write size)."""
        fn = lambda: self.session.write_sectors(       # noqa: E731
            self.unit, lba, data, self.media)
        if retry:
            self._retry(fn)
        else:
            fn()

    def read_sector(self, lba: int) -> bytes:
        return self.read_sectors(lba, 1)

    def write_sector(self, lba: int, data: bytes) -> None:
        self.write_sectors(lba, data)
