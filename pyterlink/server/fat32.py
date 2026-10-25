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
"""A FAT32 volume served to LNKCLIENT as a FAT16 drive.

MS-DOS 6.22 (and so LNKCLIENT) cannot read FAT32, so its sectors cannot go on
the wire as they are. The volume's file tree is read here -- directories,
long names, short names with their NT case flags, attributes and DOS
timestamps as stored -- and presented through VirtualFatUnit as a generated
FAT16 volume; file data is read from the FAT32 clusters on demand. Read-only.
The 2 GB FAT16 limit applies to the files' total size, not the volume's.
"""
from __future__ import annotations

import logging
import struct
from typing import Dict, List, Optional

from ..protocol.session import SECTOR_SIZE
from .dirunit import (ATTR_DIR, ATTR_HIDDEN, ATTR_LFN, ATTR_READONLY,
                      ATTR_VOLUME, Node, VirtualFatUnit, lfn_checksum)
from .geometry import Geometry

log = logging.getLogger(__name__)

FAT32_EOC = 0x0FFFFFF8
FAT32_MASK = 0x0FFFFFFF


def is_fat32_boot(sector: bytes) -> bool:
    """A FAT32 BPB: 0 root entries, 0 16-bit FAT size, a 32-bit one."""
    if len(sector) < 512 or sector[510:512] != b"\x55\xaa":
        return False
    root, spf16 = struct.unpack_from("<H", sector, 0x11)[0], \
        struct.unpack_from("<H", sector, 0x16)[0]
    spf32 = struct.unpack_from("<I", sector, 0x24)[0]
    return root == 0 and spf16 == 0 and spf32 != 0 and sector[0x0D] != 0


class Fat32Volume:
    """Read access to a FAT32 volume at sector `offset` of an image."""

    def __init__(self, path: str, offset: int) -> None:
        self.path, self.offset = path, offset
        self._f = open(path, "rb")
        b = self._sector(0)
        if not is_fat32_boot(b):
            raise ValueError(f"{path} @ sector {offset}: not a FAT32 volume")
        (self.bps, self.spc, self.reserved, self.nfats) = struct.unpack_from(
            "<HBHB", b, 0x0B)
        if self.bps != SECTOR_SIZE:
            raise ValueError(f"{path}: {self.bps}-byte sectors")
        self.spf = struct.unpack_from("<I", b, 0x24)[0]
        self.root_cluster = struct.unpack_from("<I", b, 0x2C)[0]
        self.serial = b[0x43:0x47]
        self.label = b[0x47:0x52]
        self.data_start = self.reserved + self.nfats * self.spf
        self.cluster_bytes = self.spc * SECTOR_SIZE
        self._fat_cache: Dict[int, bytes] = {}

    def _sector(self, n: int, count: int = 1) -> bytes:
        self._f.seek((self.offset + n) * SECTOR_SIZE)
        return self._f.read(SECTOR_SIZE * count)

    def fat(self, cluster: int) -> int:
        sec, off = divmod(cluster * 4, SECTOR_SIZE)
        data = self._fat_cache.get(sec)
        if data is None:
            data = self._fat_cache[sec] = self._sector(self.reserved + sec)
        return struct.unpack_from("<I", data, off)[0] & FAT32_MASK

    def chain(self, first: int) -> List[int]:
        out, c, seen = [], first, set()
        while 2 <= c < FAT32_EOC and c not in seen:
            seen.add(c)
            out.append(c)
            c = self.fat(c)
        return out

    def read_cluster(self, c: int) -> bytes:
        return self._sector(self.data_start + (c - 2) * self.spc, self.spc)

    def reader(self, first: int, size: int):
        """(offset, n) -> bytes over a file's cluster chain."""
        chain: List[int] = []

        def read(off: int, n: int) -> bytes:
            if not chain:
                chain.extend(self.chain(first))
            out = bytearray()
            while n > 0:
                i, within = divmod(off, self.cluster_bytes)
                if i >= len(chain):
                    break
                data = self.read_cluster(chain[i])[within:within + n]
                out += data
                off += len(data)
                n -= len(data)
            return bytes(out)
        return read

    def tree(self) -> Node:
        root = Node(f"{self.path}@{self.offset}", "/", True)
        self._fill(root, self.root_cluster, set())
        return root

    def _fill(self, d: Node, first: int, seen: set, depth: int = 0) -> None:
        if depth >= 64:
            log.warning("fat32: directory depth limit reached at %s", d.path)
            return
        if first in seen:
            return
        seen.add(first)
        raw = b"".join(self.read_cluster(c) for c in self.chain(first))
        lfn: List[bytes] = []
        lfn_sum: Optional[int] = None
        for off in range(0, len(raw), 32):
            e = raw[off:off + 32]
            if e[0] == 0:
                break
            if e[0] == 0xE5:
                lfn = []
                continue
            attr = e[11]
            if attr == ATTR_LFN:
                if e[0] & 0x40:
                    lfn, lfn_sum = [], e[13]
                lfn.insert(0, e[1:11] + e[14:26] + e[28:32])
                continue
            short = e[0:11]
            long_name = None
            if lfn and lfn_sum == lfn_checksum(short):
                units = b"".join(lfn)
                name = units.decode("utf-16-le", "replace")
                long_name = name.split("\x00", 1)[0]
            lfn = []
            if attr & ATTR_VOLUME or short[:2] in (b". ", b".."):
                continue
            from .dirunit import render_short
            flags = e[12] & 0x18
            name = long_name or render_short(short, flags)
            cluster = (struct.unpack_from("<H", e, 20)[0] << 16) | \
                struct.unpack_from("<H", e, 26)[0]
            size = struct.unpack_from("<I", e, 28)[0]
            tm, date = struct.unpack_from("<HH", e, 22)
            node = Node(f"{d.path}/{name}", name, bool(attr & ATTR_DIR),
                        0 if attr & ATTR_DIR else size,
                        readonly=bool(attr & ATTR_READONLY),
                        short=bytes(short), case=flags, dos_dt=(date, tm),
                        hidden=bool(attr & ATTR_HIDDEN))
            if node.is_dir:
                if cluster:
                    self._fill(node, cluster, seen, depth + 1)
            else:
                node.reader = self.reader(cluster, size)
            d.children.append(node)


class Fat32Unit(VirtualFatUnit):
    """A FAT32 volume of an image served as a read-only FAT16 drive."""

    def __init__(self, path: str, offset: int,
                 geometry: Optional[Geometry] = None) -> None:
        vol = Fat32Volume(path, offset)
        label = vol.label.rstrip(b" ")
        if label == b"NO NAME":
            label = b""
        super().__init__(vol.tree(), f"{path} (FAT32 @ sector {offset})",
                         label, bytes(vol.serial), geometry)
        self._vol = vol

    def close(self) -> None:
        self._vol._f.close()
