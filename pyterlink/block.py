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
"""A server unit as an on-demand block device (D-053).

LNKCLIENT redirects a whole server drive at the sector level, so the client
side exposes it as a block device and lets Linux parse the FAT itself
(mount -t msdos: LNKCLIENT/LNKSERVER have no long filenames). Reads are on
demand and cached; only the sectors a mount or a file access touches cross
the wire, a run of uncached sectors in one op04 (up to 127, as DOS asks
LNKCLIENT) and a run of whole written sectors in one op05.
"""
from __future__ import annotations

import logging
import time
from typing import Dict, Optional

from .protocol.session import (
    LinkSession, UnitDevice, DriveMap, SECTOR_SIZE, MAX_SECTORS,
    HANDSHAKE_BAUD, LinkError)

# Sectors per op05. Any count 1..127 is protocol-legal. Like the real
# LNKCLIENT, pyterlink starts at the maximum and does not ramp. A write lost
# (no ack: LNKSERVER has not stored it) re-establishes the link, is sent
# again at half that size, and that half is the cap for the rest of the run.

log = logging.getLogger(__name__)


class BlockDeviceError(Exception):
    """The remote unit cannot be established or accessed."""


class RemoteBlockDevice:
    """Cached sector access to one server unit, sized from its BPB."""

    def __init__(self, unit: UnitDevice, total_sectors: int,
                 writable: bool = False) -> None:
        self._unit = unit
        self.sector_size = SECTOR_SIZE
        self.total_sectors = total_sectors
        self.writable = writable
        self._cache: Dict[int, bytes] = {}
        self.volume_label: Optional[str] = None
        self.fs_type: Optional[str] = None
        self.serial: Optional[int] = None
        self.session: Optional[LinkSession] = None
        self.wire_reads = 0           # sectors fetched over the link
        self.wire_writes = 0          # sectors written over the link
        self._t0 = time.monotonic()
        self._last_report = self._t0
        self._reported = (0, 0)       # counters at the last report
        self.drive_map: Optional[DriveMap] = None
        self._link = None
        self.write_max = MAX_SECTORS      # -write n: largest op05
        self._wsize = MAX_SECTORS         # current op05 size
        self._wcap = MAX_SECTORS          # learned from a lost write

    @property
    def size(self) -> int:
        return self.total_sectors * self.sector_size

    def read_sector(self, lba: int) -> bytes:
        data = self._cache.get(lba)
        if data is None:
            data = self._unit.read_sector(lba)
            if len(data) != self.sector_size:
                raise BlockDeviceError(
                    f"read_sector({lba}): got {len(data)} bytes")
            self._cache[lba] = data
            self.wire_reads += 1
            self._progress(lba)
        return data

    def _progress(self, lba: int) -> None:
        """Every 5 s while sectors move (reads or writes): totals, the
        rate over the last interval, the current write size."""
        now = time.monotonic()
        if now - self._last_report < 5.0:
            return
        dt = now - self._last_report
        r0, w0 = self._reported
        rate = ((self.wire_reads - r0) + (self.wire_writes - w0)) \
            * self.sector_size / dt / 1000
        self._last_report = now
        self._reported = (self.wire_reads, self.wire_writes)
        log.info("link: %d sectors read, %d written; %.1f KB/s over the "
                 "last %.0f s; write size %d; last LBA %d",
                 self.wire_reads, self.wire_writes, rate, dt,
                 min(self._wsize, self.write_max, self._wcap), lba)

    def _fetch(self, lba: int, count: int) -> None:
        """Read sectors [lba, lba+count) that are not cached, each run of
        uncached sectors in as few op04s as the 127-sector limit allows."""
        end = lba + count
        while lba < end:
            if lba in self._cache:
                lba += 1
                continue
            run = 1
            while (lba + run < end and run < MAX_SECTORS
                   and lba + run not in self._cache):
                run += 1
            data = self._unit.read_sectors(lba, run)
            if len(data) != run * self.sector_size:
                raise BlockDeviceError(
                    f"read_sectors({lba}, {run}): got {len(data)} bytes")
            for i in range(run):
                self._cache[lba + i] = data[i * self.sector_size:
                                            (i + 1) * self.sector_size]
            self.wire_reads += run
            self._progress(lba + run - 1)
            lba += run

    def write_sector(self, lba: int, data: bytes) -> None:
        self.write_sectors(lba, data)

    def write_sectors(self, lba: int, data: bytes) -> None:
        """Write whole sectors starting at lba, in op05s of the adaptive
        size (WRITE_MAX_SECTORS above)."""
        if not self.writable:
            raise BlockDeviceError("device is read-only")
        ss = self.sector_size
        off = 0
        while off < len(data):
            n = max(1, min(self._wsize, self.write_max, self._wcap))
            chunk = bytes(data[off:off + n * ss])
            first = lba + off // ss
            if n > 1:
                try:
                    self._unit.write_sectors(first, chunk, retry=False)
                except LinkError as e:
                    self._wcap = self._wsize = max(n // 2, 1)
                    log.warning("a %d-sector write was lost (%s); "
                                "writing %d sectors at a time from now on",
                                n, e, self._wcap)
                    self._unit.session.reestablish(self._unit.drive_map)
                    continue                   # same data, smaller
            else:
                self._unit.write_sectors(first, chunk)  # retry once, then raise
            for i in range(len(chunk) // ss):
                self._cache[first + i] = chunk[i * ss:(i + 1) * ss]
            off += len(chunk)
            self.wire_writes += len(chunk) // ss
            self._progress(first + len(chunk) // ss - 1)

    def read(self, offset: int, length: int) -> bytes:
        """Bytes [offset, offset+length), spanning sectors as needed."""
        ss = self.sector_size
        end = offset + length
        first = offset // ss
        last = (end - 1) // ss if length else first - 1
        self._fetch(first, last - first + 1)
        out = bytearray()
        for lba in range(first, last + 1):
            sector = self._cache[lba]
            lo = max(offset - lba * ss, 0)
            hi = min(end - lba * ss, ss)
            out += sector[lo:hi]
        return bytes(out)

    def write(self, offset: int, data: bytes) -> None:
        """Write bytes at offset: partial head/tail sectors are
        read-modify-written, the whole sectors between go in one run."""
        ss = self.sector_size
        if not data:
            return
        end = offset + len(data)
        first, last = offset // ss, (end - 1) // ss
        if offset % ss or end - first * ss < ss:
            # partial first sector (or everything inside one sector)
            sector = bytearray(self.read_sector(first))
            lo = offset - first * ss
            hi = min(ss, end - first * ss)
            sector[lo:hi] = data[:hi - lo]
            self.write_sectors(first, bytes(sector))
            if first == last:
                return
            data, offset = data[hi - lo:], (first + 1) * ss
            first += 1
        if end % ss:
            sector = bytearray(self.read_sector(last))
            sector[:end - last * ss] = data[(last * ss) - offset:]
            self.write_sectors(last, bytes(sector))
            data = data[:(last * ss) - offset]
        if data:
            self.write_sectors(first, bytes(data))

    @classmethod
    def connect(cls, link, unit: Optional[int] = None,
                writable: bool = False, keepalive: bool = True,
                max_baud: int = HANDSHAKE_BAUD) -> "RemoteBlockDevice":
        """Run LNKCLIENT's establishment on `link` and open a server unit.

        The sequence is LNKCLIENT's: handshake at 9600, rate steps up to
        `max_baud` (-baud, as LNKCLIENT's /BAUD), install, drive map, drive info per mapped
        unit; then media check + build BPB on the unit used. `unit`
        defaults to the first unit the server maps.
        """
        s = LinkSession(link, max_baud=max_baud)
        s.establish()
        s.install()
        dmap = s.drive_map()
        if keepalive:
            s.start_keepalive()
        mapped = dmap.mapped()
        if not mapped:
            raise BlockDeviceError("the server offers no drives")
        for u in mapped.values():
            s.drive_info(u)
        if unit is None:
            unit = next(iter(mapped.values()))
        elif unit not in mapped.values():
            # LNKCLIENT only addresses units the drive map gave it. A request
            # for another unit reaches the server's DOS anyway: an unoffered
            # B: on a one-floppy machine stops LNKSERVER at DOS's "Insert
            # diskette for drive B:" prompt (live, s57).
            offered = ", ".join(f"{u} ({chr(65 + u)}:)"
                                for u in sorted(set(mapped.values())))
            raise BlockDeviceError(
                f"server unit {unit} ({chr(65 + unit)}:) is not offered by "
                f"the server; offered units: {offered}")
        # As LNKCLIENT: media descriptor 00 ("not known yet") until the BPB
        # has been built, then the BPB's own (F8 fixed disk, F0 1.44M, ...)
        # on every later request to the unit (session 3: 020200, 030200,
        # then 0202f8 / 0402f8...).
        mc = s.media_check(unit, 0x00)
        bpb = s.build_bpb(unit, 0x00)
        if bpb.bytes_per_sector != SECTOR_SIZE:
            raise BlockDeviceError(
                f"unit {unit}: {bpb.bytes_per_sector}-byte sectors")
        dev = cls(UnitDevice(s, unit, media=bpb.media,
                             drive_map=dmap.drive_map),
                  bpb.total_sectors, writable)
        dev.session, dev.drive_map, dev._link = s, dmap, link
        dev.volume_label, dev.serial = mc.label, mc.serial
        log.info("server unit %d: %d sectors (%d bytes), label %r, "
                 "serial %08X", unit, dev.total_sectors, dev.size,
                 dev.volume_label, dev.serial)
        return dev

    @classmethod
    def open_serial(cls, url: str, baud: int, trace: Optional[str] = None,
                    **kw) -> "RemoteBlockDevice":
        """Open a serial port and connect. `trace`: append a wire trace of
        every byte to this file (com3 proxy log format)."""
        from .transport.serial_backend import open_link
        link = open_link(url, HANDSHAKE_BAUD)
        if trace:
            from .transport.trace import TraceIO
            link = TraceIO(link, trace, local="LNKCLIENT")
        return cls.connect(link, max_baud=baud, **kw)

    def close(self) -> None:
        if self.session is not None:
            self.session.stop_keepalive()
        if self._link is not None:
            self._link.close()
            self._link = None
