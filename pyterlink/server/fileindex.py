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
"""Which file a sector belongs to, for the server's status display.

LNKCLIENT moves sectors, not files (docs/interlnk_protocol_spec.md section
1), so the display names the file by looking the sector up in the served
volume's own FAT and directories, read through the unit as the client
would see them. Works for any FAT12/FAT16 unit (images, partitions,
generated drives); FAT32 sent raw (-fat32) is shown by sector number.

While DOS writes a new file its clusters are not in any chain yet (the FAT
is updated last); writes to such clusters are credited to the newest file
entry the index has seen, which is the file being copied.
"""
from __future__ import annotations

import logging
import struct
import time
from typing import Dict, List, Optional

from ..protocol.session import SECTOR_SIZE

log = logging.getLogger(__name__)

REFRESH = 1.0          # seconds between rebuilds after the client wrote
CHUNK = 64             # sectors per read while indexing


class VolumeIndex:
    def __init__(self, unit, letter: str) -> None:
        self.unit, self.letter = unit, letter
        b = unit.boot
        (bps, self.spc, res, nfats, root, t16, _, spf) = struct.unpack_from(
            "<HBHBHHBH", b, 0x0B)
        t32 = struct.unpack_from("<I", b, 0x20)[0]
        self.ok = bps == SECTOR_SIZE and spf != 0 and self.spc != 0
        self.fat_start, self.spf = res, spf
        self.root_start = res + nfats * spf
        self.data_start = self.root_start + -(-root * 32 // SECTOR_SIZE)
        total = t16 or t32
        self.n_clusters = max(0, (total - self.data_start) // max(self.spc, 1))
        self.fat12 = self.n_clusters < 4085
        self._owner: Dict[int, str] = {}       # cluster -> path
        self._dirs: set = set()                 # directory clusters
        self._paths: Optional[set] = None
        self.new_file: Optional[str] = None
        self._stale, self._built = True, 0.0
        self._gen = getattr(unit, "generation", 0)

    def touched(self) -> None:
        """The client wrote to this unit: rebuild before the next lookup
        (at most every REFRESH seconds)."""
        self._stale = True

    def describe(self, lba: int, write: bool = False) -> str:
        if not self.ok:
            return f"{self.letter} sector {lba}"
        if lba < self.fat_start:
            return f"{self.letter} boot sector"
        if lba < self.root_start:
            return f"{self.letter} FAT"
        if lba < self.data_start:
            return f"{self.letter} root directory"
        self._refresh()
        c = 2 + (lba - self.data_start) // self.spc
        path = self._owner.get(c)
        if path is None:
            if write and self.new_file:
                return f"{self.letter}\\{self.new_file}"
            return f"{self.letter} free space"
        if c in self._dirs:
            return f"{self.letter}\\{path} (directory)" if path else \
                f"{self.letter} root directory"
        return f"{self.letter}\\{path}"

    # -- indexing --------------------------------------------------------

    def _refresh(self) -> None:
        gen = getattr(self.unit, "generation", 0)   # server-side changes
        if gen != self._gen:
            self._gen, self._stale = gen, True
        now = time.monotonic()
        if not self._stale or (now - self._built < REFRESH
                               and self._paths is not None):
            return
        self._stale, self._built = False, now
        try:
            self._build()
        except Exception as e:              # a half-written volume
            log.debug("%s: index: %s", self.letter, e)

    def _read(self, lba: int, n: int) -> bytes:
        out = bytearray()
        while n > 0:
            k = min(n, CHUNK)
            data = self.unit.read(lba, k)
            if data is None:
                break
            out += data
            lba, n = lba + k, n - k
        return bytes(out)

    def _fat(self) -> List[int]:
        raw = self._read(self.fat_start, self.spf)
        n = self.n_clusters + 2
        if self.fat12:
            out = []
            for c in range(n):
                if c * 3 // 2 + 2 > len(raw):
                    break
                v = struct.unpack_from("<H", raw, c * 3 // 2)[0]
                v = v >> 4 if c & 1 else v & 0xFFF
                out.append(v | 0xF000 if v >= 0xFF7 else v)
            return out
        return list(struct.unpack_from(f"<{min(n, len(raw) // 2)}H", raw))

    @staticmethod
    def _chain(fat: List[int], first: int) -> List[int]:
        out, c = [], first
        while 2 <= c < len(fat) and len(out) < len(fat):
            out.append(c)
            c = fat[c]
            if c >= 0xFFF7 or c < 2:
                break
        return out

    def _build(self) -> None:
        fat = self._fat()
        owner: Dict[int, str] = {}
        dirs: set = set()
        paths: List[str] = []

        def walk(raw: bytes, prefix: str, depth: int) -> None:
            lfn: List[bytes] = []
            for off in range(0, len(raw) - 31, 32):
                e = raw[off:off + 32]
                if e[0] == 0:
                    return
                if e[0] == 0xE5:
                    lfn = []
                    continue
                if e[11] == 0x0F:
                    if e[0] & 0x40:
                        lfn = []
                    lfn.insert(0, e[1:11] + e[14:26] + e[28:32])
                    continue
                if e[11] & 0x08 or e[0] == 0x2E:
                    lfn = []
                    continue
                name = _short(e[0:11])
                if lfn:
                    long_name = b"".join(lfn).decode(
                        "utf-16-le", "replace").split("\x00", 1)[0]
                    name = long_name or name
                    lfn = []
                path = prefix + name
                first = struct.unpack_from("<H", e, 26)[0]
                chain = self._chain(fat, first) if first else []
                for c in chain:
                    owner.setdefault(c, path)
                if e[11] & 0x10:
                    dirs.update(chain)
                    if depth < 32 and chain:
                        walk(b"".join(self._read(
                            self.data_start + (c - 2) * self.spc, self.spc)
                            for c in chain), path + "\\", depth + 1)
                else:
                    paths.append(path)

        walk(self._read(self.root_start, self.data_start - self.root_start),
             "", 0)
        if self._paths is not None:
            new = [p for p in paths if p not in self._paths]
            if new:
                self.new_file = new[-1]
        self._owner, self._dirs, self._paths = owner, dirs, set(paths)


def _short(raw: bytes) -> str:
    raw = raw[:1].replace(b"\x05", b"\xe5") + raw[1:]
    base = raw[:8].rstrip(b" ").decode("cp437")
    ext = raw[8:].rstrip(b" ").decode("cp437")
    return base + ("." + ext if ext else "")
