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
"""Generated FAT16 drives: a Linux directory (DirUnit), or any file tree
(VirtualFatUnit, e.g. a FAT32 volume's contents, server/fat32.py), served as
an LNKSERVER drive.

LNKCLIENT redirects a whole drive at the sector level (protocol/session.py),
so a directory is presented as the disk a DOS machine would hold: boot
sector, two FATs, root directory and data clusters, generated from the
directory tree when the unit is created. File contents are read from the
Linux files on demand; nothing is copied.

Names are made VFAT-correct, as Windows does it, so that any reader gets
the right answer:
  * every entry gets a unique 8.3 short name (upper case, invalid
    characters -> "_", leading dots and spaces dropped, a "~n" tail when
    the name had to be changed or clashes);
  * a name that differs from its short name only by being all lower case
    in the base and/or extension uses the NT case flags (directory entry
    byte 0x0C) instead of long-name entries;
  * any other name that does not fit 8.3 also gets standard VFAT long-name
    entries (UTF-16, checksummed against the short name). MS-DOS 6.22 and
    LNKCLIENT ignore those entries (that is how VFAT was designed); Linux
    vfat or Windows readers see the real names.
Dot files are given the hidden attribute.

DirUnit is the tree as scanned once, read-only (writes get DOS's
write-protect error). The server serves directories through
server/dirsync.py's SyncedDirUnit, built on it: client writes written back
to the directory, and changes made on the Linux side followed by periodic
rescans. Names starting with TMP_PREFIX belong to the write-back and are
not served.
"""
from __future__ import annotations

import bisect
import hashlib
import logging
import os
import stat
import struct
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from ..protocol.server import Unit
from ..protocol.session import SECTOR_SIZE
from .geometry import Geometry, default_geometry

log = logging.getLogger(__name__)

ATTR_READONLY, ATTR_HIDDEN, ATTR_VOLUME, ATTR_DIR, ATTR_ARCHIVE = \
    0x01, 0x02, 0x08, 0x10, 0x20
ATTR_LFN = 0x0F
NT_LOWER_BASE, NT_LOWER_EXT = 0x08, 0x10

ROOT_ENTRIES_MIN = 512
FAT16_MIN_CLUSTERS = 4085      # fewer: readers take the volume for FAT12
FAT16_MAX_CLUSTERS = 65524
MAX_FILE_SIZE = 0x7FFFFFFF   # 2 GB - 1; FAT16 per-file limit for MS-DOS 6.22
LFN_MAX_UNITS = 255
# Names the server's write-back uses for temporary files and its trash
# (server/dirsync.py); never served.
TMP_PREFIX = ".pyterlink-"

# Characters allowed in a short name besides A-Z and 0-9.
_SHORT_EXTRA = set("!#$%&'()-@^_`{}~")


def _short_char_ok(c: str) -> bool:
    return ("A" <= c <= "Z") or ("0" <= c <= "9") or c in _SHORT_EXTRA


def _split(name: str) -> Tuple[str, str]:
    i = name.rfind(".")
    if i <= 0:
        return name, ""
    return name[:i], name[i + 1:]


def _fits_83(name: str) -> bool:
    """name (any case) is already a valid 8.3 name."""
    if name in (".", "..") or name.startswith(".") or name.count(".") > 1:
        return False
    base, ext = _split(name)
    if not 1 <= len(base) <= 8 or len(ext) > 3:
        return False
    return all(_short_char_ok(c) for c in (base + ext).upper())


def _case_flags(name: str) -> Optional[int]:
    """NT case flags if the name's case is expressible by them (each part
    all upper or all lower), else None (a long-name entry is needed)."""
    base, ext = _split(name)
    flags = 0
    for part, bit in ((base, NT_LOWER_BASE), (ext, NT_LOWER_EXT)):
        letters = [c for c in part if c.isalpha()]
        if not letters or all(c.isupper() for c in letters):
            continue
        if all(c.islower() for c in letters):
            flags |= bit
        else:
            return None
    return flags


def _clean(part: str) -> Tuple[str, bool]:
    """Upper-case and map to short-name characters; (result, lossy)."""
    out, lossy = [], False
    for c in part.upper():
        if c == " " or c == ".":
            lossy = True
            continue
        if _short_char_ok(c):
            out.append(c)
        else:
            out.append("_")
            lossy = True
    return "".join(out), lossy


def short_name(name: str, taken: set) -> Tuple[bytes, int, bool]:
    """(11-byte short name, NT case flags, needs long-name entries) for a
    Linux name, unique among `taken` (which it is added to)."""
    if _fits_83(name):
        base, ext = _split(name.upper())
        sn = base.ljust(8) + ext.ljust(3)
        if sn not in taken:
            flags = _case_flags(name)
            taken.add(sn)
            if flags is not None:
                return sn.encode("ascii"), flags, False
            return sn.encode("ascii"), 0, True
        lossy = True
        base_c, ext_c = base, ext
    else:
        stripped = name.lstrip(".")
        base_raw, ext_raw = _split(stripped) if stripped else ("", "")
        base_c, lossy_b = _clean(base_raw)
        ext_c, lossy_e = _clean(ext_raw)
        lossy = (lossy_b or lossy_e or len(base_c) > 8 or len(ext_c) > 3
                 or stripped != name or not base_c)
        ext_c = ext_c[:3]
        if not base_c:
            base_c = "_"
    if not lossy:
        sn = base_c[:8].ljust(8) + ext_c.ljust(3)
        if sn not in taken:
            taken.add(sn)
            return sn.encode("ascii"), 0, True
    n = 1
    while True:
        tail = f"~{n}"
        sn = (base_c[:8 - len(tail)] + tail).ljust(8) + ext_c.ljust(3)
        if sn not in taken:
            taken.add(sn)
            return sn.encode("ascii"), 0, True
        n += 1


def lfn_checksum(short: bytes) -> int:
    s = 0
    for b in short:
        s = (((s & 1) << 7) + (s >> 1) + b) & 0xFF
    return s


def lfn_entries(name: str, short: bytes) -> List[bytes]:
    """VFAT long-name entries for `name`, in on-disk order (last part
    first), to precede the short entry."""
    units = name.encode("utf-16-le")
    n_units = len(units) // 2
    chunks = (n_units + 12) // 13
    data = units + b"\x00\x00" if n_units % 13 else units
    data = data.ljust(chunks * 26, b"\xff")
    csum = lfn_checksum(short)
    out = []
    for i in range(chunks):
        part = data[i * 26:(i + 1) * 26]
        seq = (i + 1) | (0x40 if i == chunks - 1 else 0)
        out.append(bytes([seq]) + part[0:10] + bytes([ATTR_LFN, 0, csum])
                   + part[10:22] + b"\x00\x00" + part[22:26])
    return out[::-1]


def dos_datetime(t: float) -> Tuple[int, int]:
    lt = time.localtime(t)
    y = min(max(lt.tm_year, 1980), 2107)
    if lt.tm_year < 1980:
        return (0 << 9) | (1 << 5) | 1, 0
    date = ((y - 1980) << 9) | (lt.tm_mon << 5) | lt.tm_mday
    tm = (lt.tm_hour << 11) | (lt.tm_min << 5) | (lt.tm_sec // 2)
    return date, tm


def _display(name: str) -> str:
    """A str that encodes to UTF-16 (undecodable bytes -> U+FFFD)."""
    return name.encode("utf-8", "surrogateescape").decode("utf-8", "replace")


@dataclass
class Node:
    path: str
    name: str                     # display name (long name)
    is_dir: bool
    size: int = 0
    mtime: float = 0.0
    readonly: bool = False
    children: List["Node"] = field(default_factory=list)
    entries: List[bytes] = field(default_factory=list)   # this node's dirents
    first_cluster: int = 0
    n_clusters: int = 0
    # Set for trees not read from Linux files (e.g. a FAT32 volume):
    reader: Optional[Callable[[int, int], bytes]] = None   # (offset, n)
    short: Optional[bytes] = None      # keep this 11-byte short name
    case: int = 0                      # ... with these NT case flags
    dos_dt: Optional[Tuple[int, int]] = None   # (date, time) as stored
    hidden: Optional[bool] = None      # else: dot files are hidden


def render_short(short: bytes, flags: int) -> str:
    """The name a short entry stands for, with its NT case flags."""
    raw = short[:1].replace(b"\x05", b"\xe5") + short[1:]
    # short names are in the OEM code page (437 for US MS-DOS 6.22)
    base = raw[:8].rstrip(b" ").decode("cp437")
    ext = raw[8:].rstrip(b" ").decode("cp437")
    if flags & NT_LOWER_BASE:
        base = base.lower()
    if flags & NT_LOWER_EXT:
        ext = ext.lower()
    return base + ("." + ext if ext else "")


class VirtualFatUnit(Unit):
    """A file tree (Node) as a read-only FAT16 unit (module docstring)."""

    def __init__(self, root: Node, origin: str, volume_label: bytes,
                 serial: bytes, geometry: Optional[Geometry] = None,
                 skipped: Optional[List[str]] = None,
                 free_bytes: Optional[int] = None) -> None:
        """free_bytes: free space to offer (writable drives); the volume
        grows to the FAT16 limit at most. Default: a quarter of the
        contents, at least 1024 clusters."""
        self.path = origin
        self._free_bytes = free_bytes
        self.writable = False
        self.offset = 0
        self.root = root
        self._skipped = skipped if skipped is not None else []
        self.volume_label = volume_label
        self._serial = serial
        self._geometry = geometry
        self._names(self.root, is_root=True)
        self._layout()
        self._build()
        for s in self._skipped:
            log.warning("not served: %s", s)
        log.info("%s: %d files, %d dirs, %d sectors (%d-byte clusters, "
                 "CHS %s), label %r", origin, self.n_files, self.n_dirs,
                 self.total_sectors, self.cluster_bytes, self.geometry,
                 self.volume_label.decode("latin-1"))

    # -- tree ------------------------------------------------------------

    def _names(self, d: Node, is_root: bool = False) -> None:
        """Short names + long-name entries for d's children; entry bytes
        are completed in _build once clusters are known."""
        taken: set = set(c.short.decode("cp437") for c in d.children
                          if c.short)
        for c in d.children:
            if c.short:
                short, flags = c.short, c.case
                need_lfn = c.name != render_short(short, flags)
            else:
                short, flags, need_lfn = short_name(c.name, taken)
            # Non-ASCII in a short name depends on the reader's code page
            # (and its idea of case): carry the exact name in long-name
            # entries as well.
            need_lfn = need_lfn or any(ord(ch) > 127 for ch in c.name)
            lfn = []
            if need_lfn:
                if len(c.name.encode("utf-16-le")) // 2 > LFN_MAX_UNITS:
                    self._skipped.append(f"{c.path}: name over 255 chars "
                                         f"(served as {short.decode()})")
                else:
                    lfn = lfn_entries(c.name, short)
            c.entries = lfn + [short + bytes([flags])]   # completed later
            if c.is_dir:
                self._names(c)

    def _walk(self, d: Node):
        yield d
        for c in d.children:
            if c.is_dir:
                yield from self._walk(c)

    @staticmethod
    def _dirent_count(d: Node, is_root: bool) -> int:
        n = sum(len(c.entries) for c in d.children)
        return n + (1 if is_root else 2)      # label, or "." and ".."

    # -- geometry ----------------------------------------------------------

    def _layout(self) -> None:
        dirs = list(self._walk(self.root))
        files = [c for d in dirs for c in d.children if not c.is_dir]
        self.n_dirs, self.n_files = len(dirs) - 1, len(files)
        root_n = self._dirent_count(self.root, True)
        self.root_entries = max(ROOT_ENTRIES_MIN, (root_n + 15) // 16 * 16)
        for spc in (4, 8, 16, 32, 64):
            cb = spc * SECTOR_SIZE
            need = sum(-(-f.size // cb) for f in files) + sum(
                -(-self._dirent_count(d, False) * 32 // cb)
                for d in dirs[1:])
            extra = (max(need // 4, 1024) if self._free_bytes is None
                     else max(-(-self._free_bytes // cb), 1024))
            n = max(need + extra, FAT16_MIN_CLUSTERS)
            if n <= FAT16_MAX_CLUSTERS:
                break
        else:
            if self._free_bytes is None or need + 1024 > FAT16_MAX_CLUSTERS:
                msg = (f"{self.path}: content exceeds the 2 GB FAT16 limit")
                log.error(msg)
                raise ValueError(msg)
            n = FAT16_MAX_CLUSTERS     # 2 GB volume, free space capped
        self.spc, self.cluster_bytes, self.n_clusters = spc, cb, n
        self.spf = -(-(n + 2) * 2 // SECTOR_SIZE)
        self.root_sectors = self.root_entries * 32 // SECTOR_SIZE
        self.fat_start = 1
        self.root_start = 1 + 2 * self.spf
        self.data_start = self.root_start + self.root_sectors
        self.total_sectors = self.data_start + n * spc
        self.bytes_per_sector = SECTOR_SIZE
        self.geometry = self._geometry or default_geometry(self.total_sectors)
        if self._geometry and self.total_sectors > self._geometry.total_sectors:
            log.warning("%s needs %d sectors, more than geometry %s holds",
                        self.path, self.total_sectors, self._geometry)

    # -- contents ----------------------------------------------------------

    def _build(self) -> None:
        cb = self.cluster_bytes
        fat = [0] * (self.n_clusters + 2)
        fat[0], fat[1] = 0xFFF8, 0xFFFF
        nxt = 2
        self._extents: List[Tuple[int, int, Node]] = []   # (first, n, node)

        def alloc(node: Node, nbytes: int) -> None:
            nonlocal nxt
            k = -(-nbytes // cb)
            if not k:
                return
            node.first_cluster, node.n_clusters = nxt, k
            for c in range(nxt, nxt + k - 1):
                fat[c] = c + 1
            fat[nxt + k - 1] = 0xFFFF
            self._extents.append((nxt, k, node))
            nxt += k

        dirs = list(self._walk(self.root))
        for d in dirs[1:]:
            alloc(d, self._dirent_count(d, False) * 32)
        for d in dirs:
            for c in d.children:
                if not c.is_dir:
                    alloc(c, c.size)
        self._extents.sort(key=lambda e: e[0])
        self._starts = [e[0] for e in self._extents]
        self._fat = struct.pack(f"<{len(fat)}H", *fat).ljust(
            self.spf * SECTOR_SIZE, b"\x00")

        self._dir_bytes: Dict[int, bytes] = {}
        for d in dirs:
            ents = []
            if d is self.root:
                if self.volume_label:
                    date, tm = d.dos_dt or dos_datetime(d.mtime)
                    ents.append(self._dirent(self.volume_label.ljust(11),
                                             ATTR_VOLUME, 0, date, tm, 0, 0))
            else:
                date, tm = d.dos_dt or dos_datetime(d.mtime)
                ents.append(self._dirent(b".".ljust(11), ATTR_DIR, 0, date,
                                         tm, d.first_cluster, 0))
                parent = self._parent_cluster(d)
                ents.append(self._dirent(b"..".ljust(11), ATTR_DIR, 0, date,
                                         tm, parent, 0))
            for c in d.children:
                *lfn, sf = c.entries
                ents.extend(lfn)
                attr = ATTR_DIR if c.is_dir else ATTR_ARCHIVE
                if c.readonly:
                    attr |= ATTR_READONLY
                if (c.hidden if c.hidden is not None
                        else c.name.startswith(".")):
                    attr |= ATTR_HIDDEN
                date, tm = c.dos_dt or dos_datetime(c.mtime)
                ents.append(self._dirent(sf[:11], attr, sf[11], date, tm,
                                         c.first_cluster,
                                         0 if c.is_dir else c.size))
            raw = b"".join(ents)
            if d is self.root:
                self._root = raw.ljust(self.root_sectors * SECTOR_SIZE,
                                       b"\x00")
            else:
                self._dir_bytes[d.first_cluster] = raw.ljust(
                    d.n_clusters * cb, b"\x00")

        self.serial = self._serial
        self.label = (self.volume_label or b"NO NAME").ljust(11)
        self.fs_type = b"FAT16   "
        boot = bytearray(SECTOR_SIZE)
        boot[0:11] = b"\xeb\x3c\x90MSDOS5.0"
        t = self.total_sectors
        struct.pack_into("<HBHBHHBHHHII", boot, 0x0B, SECTOR_SIZE, self.spc,
                         1, 2, self.root_entries, t if t < 0x10000 else 0,
                         0xF8, self.spf, self.geometry.sectors_per_track,
                         self.geometry.heads, 0, t if t >= 0x10000 else 0)
        boot[0x24] = 0x80
        boot[0x26] = 0x29
        boot[0x27:0x2B] = self.serial
        boot[0x2B:0x36] = self.label
        boot[0x36:0x3E] = self.fs_type
        boot[510:512] = b"\x55\xaa"
        self.boot = bytes(boot)

    def _parent_cluster(self, d: Node) -> int:
        for p in self._walk(self.root):
            if d in p.children:
                return 0 if p is self.root else p.first_cluster
        return 0

    @staticmethod
    def _dirent(name11: bytes, attr: int, nt: int, date: int, tm: int,
                cluster: int, size: int) -> bytes:
        return (name11 + bytes([attr, nt, 0]) + struct.pack(
            "<HHHHHHHI", tm, date, date, 0, tm, date, cluster & 0xFFFF,
            size))

    # -- sector access -------------------------------------------------------

    def read(self, lba: int, count: int = 1) -> Optional[bytes]:
        if lba < 0 or lba + count > self.total_sectors:
            return None
        return b"".join(self._sector(n) for n in range(lba, lba + count))

    def _sector(self, n: int) -> bytes:
        if n == 0:
            return self.boot
        if n < self.root_start:
            k = (n - self.fat_start) % self.spf
            return self._fat[k * SECTOR_SIZE:(k + 1) * SECTOR_SIZE]
        if n < self.data_start:
            k = n - self.root_start
            return self._root[k * SECTOR_SIZE:(k + 1) * SECTOR_SIZE]
        rel = n - self.data_start
        cluster, within = 2 + rel // self.spc, rel % self.spc
        i = bisect.bisect_right(self._starts, cluster) - 1
        if i < 0:
            return bytes(SECTOR_SIZE)
        first, k, node = self._extents[i]
        if cluster >= first + k:
            return bytes(SECTOR_SIZE)
        off = ((cluster - first) * self.spc + within) * SECTOR_SIZE
        if node.is_dir:
            return self._dir_bytes[first][off:off + SECTOR_SIZE]
        if node.reader is not None:
            n = max(0, min(SECTOR_SIZE, node.size - off))
            return node.reader(off, n).ljust(SECTOR_SIZE, b"\x00")
        try:
            with open(node.path, "rb") as f:
                f.seek(off)
                data = f.read(SECTOR_SIZE)
        except OSError as e:
            log.warning("%s: %s", node.path, e)
            data = b""
        return data.ljust(SECTOR_SIZE, b"\x00")

    def write(self, lba: int, data: bytes) -> bool:
        return False

    def close(self) -> None:
        pass


class DirUnit(VirtualFatUnit):
    """A Linux directory as a read-only FAT16 unit (module docstring);
    SyncedDirUnit (server/dirsync.py) is the one the server uses."""

    def __init__(self, path: str, label: Optional[str] = None,
                 geometry: Optional[Geometry] = None,
                 free_bytes: Optional[int] = None) -> None:
        path = os.path.abspath(path)
        skipped: List[str] = []
        self._skipped = skipped
        root = self._scan(path, os.path.basename(path) or "/")
        lbl, _ = _clean(label if label is not None
                        else os.path.basename(path))
        super().__init__(root, path,
                         lbl[:11].encode("ascii") if lbl else b"",
                         hashlib.sha1(path.encode()).digest()[:4],
                         geometry, skipped, free_bytes)

    def _scan(self, path: str, name: str, depth: int = 0) -> Node:
        if depth >= 64:
            log.warning("dirunit: directory depth limit reached at %s", path)
            return Node(path, _display(name), True)
        st = os.stat(path)
        node = Node(path, _display(name), True, mtime=st.st_mtime)
        try:
            names = sorted(os.listdir(path))
        except OSError as e:
            self._skipped.append(f"{path}: {e}")
            return node
        for n in names:
            p = os.path.join(path, n)
            if n.startswith(TMP_PREFIX):
                log.warning("%s: left by an interrupted read-write session; "
                            "not served (check it and remove it)", p)
                continue
            try:
                lst = os.lstat(p)
                s = os.stat(p)
            except OSError as e:
                self._skipped.append(f"{p}: {e}")
                continue
            if stat.S_ISDIR(s.st_mode):
                if stat.S_ISLNK(lst.st_mode):
                    self._skipped.append(f"{p}: symlinked directory")
                    continue
                node.children.append(self._scan(p, n, depth + 1))
            elif stat.S_ISREG(s.st_mode):
                if s.st_size > MAX_FILE_SIZE:
                    self._skipped.append(f"{p}: larger than 2 GB (not served)")
                    continue
                node.children.append(Node(
                    p, _display(n), False, s.st_size, s.st_mtime,
                    readonly=not os.access(p, os.W_OK)))
            else:
                self._skipped.append(f"{p}: not a regular file")
        return node
