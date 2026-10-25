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
"""Directory drives: a Linux directory served as a FAT16 drive kept in step
with the directory both ways -- the client's changes written back to it
(D-065, approach 3), and changes made on the Linux side followed (D-079).
-ro drives are the same with client writes refused.

What the server holds is a virtual FAT16 disk: boot sector, FAT and
directories in memory; file data is never copied, it is read from the
Linux files when the client asks for it.

The drive starts as DirUnit's generated volume (server/dirunit.py). LNKCLIENT
writes sectors, not files, so the client's writes are kept in an overlay:
the boot sector, FATs and root directory in memory, written data clusters
in a sparse temporary file. The client always reads back exactly what it
wrote, as from a real disk.

Once the client has written nothing for SYNC_QUIET seconds, the FAT and
directories as the client left them are parsed and compared with the state
last written to Linux, and the difference is applied to the directory:
  * new files and directories are created, deleted ones removed;
  * an entry whose 8.3 name or directory changed is renamed / moved (an
    entry is identified by its first cluster, an empty file by its
    directory and 8.3 name);
  * a file whose size, cluster chain or cluster contents changed is
    rewritten (via a temporary file and rename, so it is never half
    written);
  * the DOS date and time become the file's mtime; the read-only attribute
    clears or sets the write permission bits.
A file the client replaces (COPY over it, which truncates and reallocates)
keeps its Linux name and permissions; a file that is a symlink on Linux is
written through the link, or, with -no-follow-symlinks, the link is
replaced by a regular file. DOS writes upper-case 8.3 names, so
files it creates or renames get those names; files it only modifies keep
their Linux names (long names, lower case). Hidden/system/archive
attributes and the volume label are not written back.

State in the middle of a DOS operation is not guessed at: an entry whose
cluster chain is broken, too short for its size or shared with another
entry (MOVE, DEL and COPY pass through such states while their FAT,
directory and data writes arrive) is left as it was on Linux, and the sync
is retried until it is consistent. A directory DOS removes that still holds
files the drive does not show (symlinks, special files, files over 2 GB)
is left in place.

Replaced and deleted files are not destroyed at once: they move to a
.pyterlink-trash-* directory in the share and stay readable for TRASH_KEEP
seconds, because the client may still read their clusters (a MOVE seen half
done: the old entry gone, the new one not written yet -- the file then
comes back by rename; or a cluster the client wrote that is now only held
in such a file). Before a kept file is removed, any of its clusters the
client's FAT still has allocated is copied into the overlay. Every sector
the client wrote reads back as written, including bytes past the end of a
file in its last cluster.

Changes made on the Linux side (another program, a copy into the shared
directory) are picked up by rescans: every FOLLOW_RW seconds for a
read-write drive, FOLLOW_RO for -ro, and never after the first scan when
the directory is on a read-only file system (a mounted ISO or CD). A
rescan reads metadata only -- directory listings and stat(), answered from
the OS's cache; a directory whose mtime is unchanged is not listed again.
Differences are applied to the volume as DOS itself would make them: new
entries (8.3 + long names) in free directory slots and free clusters,
deleted entries freed, a changed file's chain resized and its size, date
and read-only bit updated. Unchanged files keep their clusters. A rescan
waits while the client is writing or has changes not yet written back.
The server then answers the next media check "don't know" (LNKSERVER's
floppy answer) until DOS asks for the BPB again: DOS re-reads the FAT and
directories then, unless it has unwritten buffers for the drive. A Linux
rename is seen as a deletion plus a new file. A file DOS has open while it
is deleted or rewritten on Linux reads whatever the volume now holds, as
on a network drive; avoid changing the same files from both sides at once.
"""
from __future__ import annotations

import logging
import os
import stat
import struct
import tempfile
import threading
import time
from dataclasses import dataclass, replace
from typing import Dict, List, Optional, Set, Tuple, Union

from ..protocol.session import SECTOR_SIZE
from .dirunit import (ATTR_ARCHIVE, ATTR_DIR, ATTR_HIDDEN, ATTR_LFN,
                      ATTR_READONLY, ATTR_VOLUME, LFN_MAX_UNITS, MAX_FILE_SIZE,
                      TMP_PREFIX, DirUnit, Node, VirtualFatUnit, _display,
                      dos_datetime, lfn_checksum, lfn_entries, render_short,
                      short_name)
from .geometry import Geometry

log = logging.getLogger(__name__)

SYNC_QUIET = 2.0         # seconds without writes before a sync
SYNC_POLL = 0.5
FAT16_BAD = 0xFFF7
FAT16_EOC = 0xFFF8
TRASH_KEEP = 60.0        # seconds a replaced/deleted file is kept
FOLLOW_RW = 1.0          # seconds between rescans of a read-write tree
FOLLOW_RO = 10.0         # ... of a -ro tree (none on a read-only mount)
ROOT = 0                 # ident of the root directory

# An entry's identity: its first cluster, or for an empty file (no
# cluster) ("empty", directory ident, 11-byte short name).
Ident = Union[int, Tuple[str, object, bytes]]


@dataclass
class Entry:
    parent: Ident
    short: bytes             # 11-byte 8.3 name as on disk
    name: str                # the name it stands for (long name if valid)
    is_dir: bool
    size: int
    chain: Tuple[int, ...]
    attr: int
    date: int
    time: int
    linux: str = ""          # its name in the Linux directory


@dataclass
class Trashed:
    """A deleted file, or the old contents of a rewritten one, kept for a
    while: its clusters may still be read (a MOVE seen half done: the old
    entry removed, the new one not yet written)."""
    path: str
    ident: Ident
    entry: Entry
    since: float


def _epoch(date: int, tm: int) -> Optional[float]:
    if not date:
        return None
    try:
        return time.mktime((1980 + (date >> 9), (date >> 5) & 15, date & 31,
                            tm >> 11, (tm >> 5) & 63, (tm & 31) * 2,
                            0, 0, -1))
    except (OverflowError, ValueError):
        return None


def _linux_name(name: str) -> str:
    out = name.replace("/", "_").replace("\x00", "_")
    if out in ("", ".", ".."):
        out = "_" + out
    return out


class SyncedDirUnit(DirUnit):
    """A Linux directory as a FAT16 unit kept in step with the directory
    both ways (module docstring); writable=False refuses client writes."""

    def __init__(self, path: str, label: Optional[str] = None,
                 geometry: Optional[Geometry] = None,
                 free_bytes: Optional[int] = None,
                 writable: bool = True,
                 follow_symlinks: bool = True) -> None:
        path = os.path.abspath(path)
        if free_bytes is None:
            st = os.statvfs(path)
            free_bytes = st.f_bavail * st.f_frsize
        super().__init__(path, label, geometry, free_bytes)
        self.writable = writable
        self._follow_symlinks = follow_symlinks
        # Changes made on the Linux side are applied to the volume; the
        # next media check then answers "don't know" (protocol/server.py)
        # so DOS re-reads the FAT and directories.
        self.media_changed = False
        self.generation = 0                # bumped per Linux-side update
        ro_fs = bool(os.statvfs(path).f_flag & os.ST_RDONLY)
        self._follow = (None if ro_fs else
                        FOLLOW_RW if writable else FOLLOW_RO)
        self._last_scan = time.monotonic()
        self._listing: Dict[str, Tuple[int, List[str]]] = {}
        self._lstat: Dict[Ident, Tuple[int, int, bool]] = {}
        self._lock = threading.RLock()
        um = os.umask(0)
        os.umask(um)
        self._file_mode = 0o666 & ~um
        cb = self.cluster_bytes
        # Overlay: sectors below data_start in memory, data clusters the
        # client wrote (and all directory clusters) in a sparse file.
        self._meta = bytearray(self.boot + self._fat + self._fat + self._root)
        self._spill = tempfile.TemporaryFile(prefix=TMP_PREFIX)
        self._mat: Set[int] = set()        # clusters held in the spill file
        for first, raw in self._dir_bytes.items():
            self._spill.seek((first - 2) * cb)
            self._spill.write(raw)
            self._mat.update(range(first, first + len(raw) // cb))
        del self._fat, self._root, self._dir_bytes
        self._dirty: Set[int] = set()      # data clusters written, unsynced
        self._retry: Set[Ident] = set()    # write-back failed: redo
        self._pending = False
        self._last_write = 0.0
        self._bad_seen: Set[Ident] = set()
        self._known: Dict[Ident, Entry] = {}
        self._owner: Dict[int, Tuple[object, int]] = {}
        self._trash: Dict[Tuple[str, int], Trashed] = {}
        self._trash_dir: Optional[str] = None
        self._trash_n = 0
        # The state on Linux = the generated tree, with the real names.
        entries, bad = self._parse()
        if bad:
            raise ValueError(f"{path}: generated volume inconsistent")
        names = self._node_names()
        for k, e in entries.items():
            e.linux = names[k]
        self._known = entries
        self._index_owners()
        snap = self._scan_linux()
        for rel, k in self._kpaths().items():
            if rel in snap and not snap[rel][0]:
                self._lstat[k] = snap[rel][1:]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._syncer, daemon=True,
                                        name=f"dirsync {path}")
        self._thread.start()
        log.info("%s: %s, %d MB free, %s", path,
                 "read-write" if writable else "read-only",
                 (self.n_clusters - len(self._used())) * cb // 1000000,
                 "read-only file system: not rescanned" if ro_fs else
                 f"rescanned every {self._follow:g} s")

    def _node_names(self) -> Dict[Ident, str]:
        names: Dict[Ident, str] = {}

        def walk(d: Node, ident: Ident) -> None:
            for c in d.children:
                short = c.entries[-1][:11]
                k = c.first_cluster or ("empty", ident, short)
                names[k] = os.path.basename(c.path)
                if c.is_dir:
                    walk(c, k)
        walk(self.root, ROOT)
        return names

    def _used(self) -> Set[int]:
        return {c for e in self._known.values() for c in e.chain}

    # -- sector access -------------------------------------------------------

    def read(self, lba: int, count: int = 1) -> Optional[bytes]:
        with self._lock:
            return super().read(lba, count)

    def _sector(self, n: int) -> bytes:
        if n < self.data_start:
            return bytes(self._meta[n * SECTOR_SIZE:(n + 1) * SECTOR_SIZE])
        rel = n - self.data_start
        c, within = 2 + rel // self.spc, rel % self.spc
        if c in self._mat:
            self._spill.seek(rel * SECTOR_SIZE)
            return self._spill.read(SECTOR_SIZE).ljust(SECTOR_SIZE, b"\x00")
        return self._backing(c, within * SECTOR_SIZE, SECTOR_SIZE)

    def _backing(self, c: int, off: int, n: int) -> bytes:
        """Bytes of cluster c not written by the client: from the Linux
        file it belonged to at the last sync, or that file's kept old
        version (else zeros)."""
        ref = self._owner.get(c)
        data = b""
        p = None
        if ref is not None:
            key, i = ref
            if key in self._trash:
                p = self._trash[key].path
            elif key in self._known:
                p = self._path(key)
        if p is not None:
            try:
                with open(p, "rb") as f:
                    f.seek(i * self.cluster_bytes + off)
                    data = f.read(n)
            except OSError as e:
                log.warning("%s: %s", p, e)
        return data.ljust(n, b"\x00")

    def _cluster(self, c: int) -> bytes:
        cb = self.cluster_bytes
        if c in self._mat:
            self._spill.seek((c - 2) * cb)
            return self._spill.read(cb).ljust(cb, b"\x00")
        return self._backing(c, 0, cb)

    def write(self, lba: int, data: bytes) -> bool:
        if not self.writable:
            return False
        count = len(data) // SECTOR_SIZE
        if lba < 0 or lba + count > self.total_sectors:
            return False
        with self._lock:
            for k in range(count):
                n, sec = lba + k, data[k * SECTOR_SIZE:(k + 1) * SECTOR_SIZE]
                if n < self.data_start:
                    self._meta[n * SECTOR_SIZE:(n + 1) * SECTOR_SIZE] = sec
                    continue
                rel = n - self.data_start
                c = 2 + rel // self.spc
                if c not in self._mat:
                    self._materialize(c)
                self._spill.seek(rel * SECTOR_SIZE)
                self._spill.write(sec)
                self._dirty.add(c)
            self._pending = True
            self._last_write = time.monotonic()
        return True

    def _materialize(self, c: int) -> None:
        """Hold cluster c in the spill file from now on."""
        cb = self.cluster_bytes
        data = self._backing(c, 0, cb)
        self._spill.seek((c - 2) * cb)
        self._spill.write(data)
        self._mat.add(c)

    # -- the volume as the client left it -------------------------------------

    def _fat_table(self) -> Tuple[int, ...]:
        return struct.unpack_from(f"<{self.n_clusters + 2}H", self._meta,
                                  self.fat_start * SECTOR_SIZE)

    def _chain(self, fat, first: int) -> Optional[List[int]]:
        out: List[int] = []
        c = first
        while 2 <= c < self.n_clusters + 2 and len(out) <= self.n_clusters:
            out.append(c)
            nxt = fat[c]
            if nxt >= FAT16_EOC:
                return out
            if nxt in (0, 1, FAT16_BAD):
                return None
            c = nxt
        return None

    def _parse(self) -> Tuple[Dict[Ident, Entry], Set[Ident]]:
        """(entries, bad): the directory tree in the overlay, and the
        entries that are not consistent (yet): broken or too short chains,
        shared clusters, duplicate names. A bad directory's contents are
        not read."""
        fat = self._fat_table()
        cb = self.cluster_bytes
        entries: Dict[Ident, Entry] = {}
        bad: Set[Ident] = set()
        owner: Dict[int, Ident] = {}

        def claim(ident: Ident, chain) -> bool:
            ok = True
            for c in chain:
                other = owner.setdefault(c, ident)
                if other != ident:
                    bad.update((ident, other))
                    ok = False
            return ok

        def walk(dir_ident: Ident, raw: bytes) -> None:
            lfn: List[bytes] = []
            lfn_sum: Optional[int] = None
            for off in range(0, len(raw) - 31, 32):
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
                short = bytes(e[0:11])
                long_name = None
                if lfn and lfn_sum == lfn_checksum(short):
                    long_name = b"".join(lfn).decode(
                        "utf-16-le", "replace").split("\x00", 1)[0]
                lfn = []
                if attr & ATTR_VOLUME or short[0] == 0x2E:   # label, . ..
                    continue
                first = struct.unpack_from("<H", e, 26)[0]
                size = struct.unpack_from("<I", e, 28)[0]
                tm, date = struct.unpack_from("<HH", e, 22)
                is_dir = bool(attr & ATTR_DIR)
                ident: Ident = first or ("empty", dir_ident, short)
                chain = self._chain(fat, first) if first else []
                ent = Entry(dir_ident, short,
                            long_name or render_short(short, e[12] & 0x18),
                            is_dir, 0 if is_dir else size,
                            tuple(chain or ()), attr, date, tm)
                if ident in entries:
                    bad.add(ident)
                    continue
                entries[ident] = ent
                if (chain is None or (is_dir and not first)
                        or (not is_dir and len(chain) < -(-size // cb))
                        or not claim(ident, chain)):
                    bad.add(ident)
                    continue
                if is_dir:
                    walk(ident, b"".join(self._cluster(c) for c in chain))

        rs = self.root_start * SECTOR_SIZE
        walk(ROOT, bytes(self._meta[rs:self.data_start * SECTOR_SIZE]))
        return entries, bad

    # -- write-back --------------------------------------------------------

    def _path(self, ident: Ident) -> str:
        parts = []
        while ident != ROOT:
            e = self._known[ident]
            parts.append(e.linux)
            ident = e.parent
        return os.path.join(self.path, *reversed(parts))

    def _depth(self, ident: Ident, ents: Dict[Ident, Entry]) -> int:
        n = 0
        while ident != ROOT and ident in ents:
            ident = ents[ident].parent
            n += 1
        return n

    @staticmethod
    def _free_name(dirpath: str, name: str) -> str:
        """name, or name~n if something DOS does not see has it."""
        out, n = name, 1
        while os.path.lexists(os.path.join(dirpath, out)):
            n += 1
            out = f"{name}~{n}"
        if out != name:
            log.warning("%s: %s exists on Linux (not served); using %s",
                        dirpath, name, out)
        return out

    def _under_bad(self, ident: Ident, bad: Set[Ident]) -> bool:
        while ident != ROOT:
            if ident in bad:
                return True
            e = self._known.get(ident)
            if e is None:
                return False
            ident = e.parent
        return False

    def _to_trash(self, path: str, ident: Ident, entry: Entry) -> None:
        """Move a file out of the tree, keeping its data readable for
        TRASH_KEEP seconds."""
        if self._trash_dir is None:
            self._trash_dir = tempfile.mkdtemp(prefix=TMP_PREFIX + "trash-",
                                               dir=self.path)
        self._trash_n += 1
        dst = os.path.join(self._trash_dir, str(self._trash_n))
        try:
            os.rename(path, dst)
        except OSError:
            # another file system below the share: keep the clusters
            # instead (the ones not already held)
            key = ("old", self._trash_n)
            self._owner.update({c: (key, i) for i, c in
                                enumerate(entry.chain)})
            self._trash[key] = Trashed(path, ident, entry, 0)
            for c in entry.chain:
                if self._owner.get(c, (None,))[0] == key and \
                        c not in self._mat:
                    self._materialize(c)
            del self._trash[key]
            os.unlink(path)
            return
        self._trash[("trash", self._trash_n)] = Trashed(
            dst, ident, entry, time.monotonic())

    def _purge(self, force: bool = False) -> None:
        """Remove kept files older than TRASH_KEEP (all if force); a
        cluster of theirs the client's FAT still has allocated is held in
        the spill file first."""
        now = time.monotonic()
        due = [k for k, t in self._trash.items()
               if force or now - t.since >= TRASH_KEEP]
        if not due:
            return
        fat = self._fat_table()
        for key in due:
            t = self._trash[key]
            for i, c in enumerate(t.entry.chain):
                if (fat[c] and c not in self._mat
                        and self._owner.get(c) == (key, i)):
                    self._materialize(c)
            try:
                os.unlink(t.path)
            except OSError as ex:
                log.warning("%s: %s", t.path, ex)
            del self._trash[key]
        self._index_owners()
        if not self._trash and self._trash_dir is not None:
            try:
                os.rmdir(self._trash_dir)
                self._trash_dir = None
            except OSError:
                pass

    def _write_file(self, target: str, e: Entry, mode: int,
                    old: Optional[Tuple[Ident, Entry]] = None) -> None:
        """Write e's contents to target (via a temporary file); old: the
        entry target holds now, whose contents are kept a while."""
        try:
            ts = os.stat(target)
            if ts.st_size > MAX_FILE_SIZE:
                raise OSError(f"{target}: existing file is larger than "
                              f"2 GB; write refused")
        except FileNotFoundError:
            pass
        fd, tmp = tempfile.mkstemp(prefix=TMP_PREFIX,
                                   dir=os.path.dirname(target))
        try:
            with os.fdopen(fd, "wb") as f:
                left = e.size
                for c in e.chain:
                    if left <= 0:
                        break
                    data = self._cluster(c)[:left]
                    f.write(data)
                    left -= len(data)
            os.chmod(tmp, mode)
            if old is not None and os.path.lexists(target):
                self._to_trash(target, *old)
            os.rename(tmp, target)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    @staticmethod
    def _apply_meta(p: str, e: Entry, old: Optional[Entry]) -> None:
        if old is None or (old.date, old.time) != (e.date, e.time):
            t = _epoch(e.date, e.time)
            if t is not None:
                os.utime(p, (t, t))
        ro = bool(e.attr & ATTR_READONLY)
        if old is None or ro != bool(old.attr & ATTR_READONLY):
            mode = os.stat(p).st_mode & 0o7777
            os.chmod(p, mode & ~0o222 if ro else mode | 0o200)

    def sync(self) -> bool:
        """Write the client's changes back to the directory. False if some
        entries were left for later (inconsistent, or a Linux error)."""
        with self._lock:
            return self._sync()

    def _sync(self) -> bool:
        cur, bad = self._parse()
        if bad != self._bad_seen:
            if bad - self._bad_seen:
                log.info("%s: %d entries mid-update; synced when complete",
                         self.path, len(bad))
            self._bad_seen = set(bad)
        good = {k: e for k, e in cur.items() if k not in bad}
        known = self._known
        # Identity is the first cluster, and a freed cluster can start
        # another entry: a change between file and directory, or a file
        # renamed or moved with new contents (DOS's REN and MOVE never
        # change contents), is a deletion plus a new entry.
        reborn = set()
        for k in good:
            o, e = known.get(k), good[k]
            if o is None:
                continue
            if o.is_dir != e.is_dir or (
                    not e.is_dir
                    and (o.parent, o.short) != (e.parent, e.short)
                    and (o.size != e.size or o.chain != e.chain
                         or self._dirty.intersection(e.chain))):
                reborn.add(k)
        deleted = [k for k in known if k in reborn
                   or (k not in cur and not self._under_bad(k, bad))]
        created = [k for k in good if k not in known or k in reborn]
        common = [k for k in good if k in known and k not in reborn]
        failed: Set[Ident] = set()
        touched: Set[Ident] = set()      # changed on Linux by us: restat

        # A file the client replaced (truncated and reallocated): same
        # directory and 8.3 name, new identity -> rewrite it in place.
        by_name = {(good[k].parent, good[k].short): k for k in created
                   if not good[k].is_dir}
        pairs: Dict[Ident, Ident] = {}          # new ident -> old ident
        for k in deleted:
            if not known[k].is_dir:
                kc = by_name.get((known[k].parent, known[k].short))
                if kc is not None and kc not in pairs:
                    pairs[kc] = k
        deleted = [k for k in deleted if k not in pairs.values()]
        created = [k for k in created if k not in pairs]

        def act(what: str, ident: Ident, fn, *a) -> bool:
            try:
                fn(*a)
                touched.add(ident)
                return True
            except OSError as ex:
                log.error("%s: %s: %s", self.path, what, ex)
                failed.add(ident)
                return False

        # Renames and moves: first out of the way (to a temporary name in
        # the root), then to their final place once deletions and new
        # directories are done -- so swaps and moves out of removed
        # directories work.
        moves = [k for k in common
                 if (good[k].parent, good[k].short)
                 != (known[k].parent, known[k].short)]
        final_name = {}
        for n, k in enumerate(moves):
            final_name[k] = (_linux_name(good[k].name)
                             if good[k].short != known[k].short
                             else known[k].linux)
            tmp = f"{TMP_PREFIX}mv-{os.getpid()}-{n}"
            if act("rename", k, os.rename, self._path(k),
                   os.path.join(self.path, tmp)):
                known[k] = replace(known[k], parent=ROOT, linux=tmp)

        del_dirs = [k for k in deleted if known[k].is_dir]
        for k in [k for k in deleted if not known[k].is_dir]:
            p = self._path(k)
            try:
                self._to_trash(p, k, known[k])
            except FileNotFoundError:
                pass
            except OSError as ex:
                log.error("%s: delete %s: %s", self.path, p, ex)
            del known[k]
        for k in sorted(del_dirs, key=lambda k: self._depth(k, known),
                        reverse=True):
            p = self._path(k)
            try:
                os.rmdir(p)
            except FileNotFoundError:
                pass
            except OSError as ex:
                log.warning("%s: removed by the client but left on Linux "
                            "(%s)", p, ex.strerror)
            del known[k]

        for k in sorted((k for k in created if good[k].is_dir),
                        key=lambda k: self._depth(k, good)):
            e = good[k]
            if e.parent not in known and e.parent != ROOT:
                failed.add(k)
                continue
            d = self._path(e.parent)
            name = self._free_name(d, _linux_name(e.name))
            if act("mkdir", k, os.mkdir, os.path.join(d, name)):
                known[k] = replace(e, linux=name)

        for k in moves:
            if k in failed:
                continue
            e = good[k]
            if e.parent not in known and e.parent != ROOT:
                failed.add(k)
                continue
            d = self._path(e.parent)
            linux_name = final_name[k]
            existing = os.path.join(d, linux_name)
            try:
                ets = os.stat(existing)
                if ets.st_size > MAX_FILE_SIZE:
                    log.error("%s: %s: existing file is larger than "
                              "2 GB; write refused", self.path, existing)
                    self.media_changed = True
                    failed.add(k)
                    continue
            except FileNotFoundError:
                pass
            name = self._free_name(d, linux_name)
            if act("rename", k, os.rename, self._path(k),
                   os.path.join(d, name)):
                known[k] = replace(known[k], parent=e.parent, linux=name)

        # Contents, then dates and the read-only bit.
        for k in created + list(pairs) + common:
            e = good[k]
            if e.is_dir or k in failed:
                continue
            old_k = pairs.get(k, k)
            old = known.get(old_k)
            if old is None:                       # new file
                if e.parent not in known and e.parent != ROOT:
                    failed.add(k)
                    continue
                d = self._path(e.parent)
                linux_name = _linux_name(e.name)
                existing = os.path.join(d, linux_name)
                try:
                    ets = os.stat(existing)
                    if ets.st_size > MAX_FILE_SIZE:
                        log.error("%s: %s: existing file is larger than "
                                  "2 GB; write refused", self.path, existing)
                        self.media_changed = True
                        failed.add(k)
                        continue
                except FileNotFoundError:
                    pass
                name = self._free_name(d, linux_name)
                target, mode = os.path.join(d, name), self._file_mode
                back = self._restorable(k, e)
                if back is not None:
                    # deleted at an earlier sync, now back unchanged (a
                    # MOVE seen half done): the kept file, as it was
                    t = self._trash.pop(back)
                    if act(f"restore {target}", k, os.rename, t.path,
                           target):
                        act("set attributes", k, self._apply_meta, target,
                            e, t.entry)
                        known[k] = replace(e, linux=name)
                    continue
            else:
                name = old.linux
                target = self._path(pairs.get(k, k))
                if (k not in pairs and k not in self._retry
                        and e.size == old.size and e.chain == old.chain
                        and not self._dirty.intersection(e.chain)):
                    if not act("set attributes", k, self._apply_meta,
                               target, e, old):
                        continue
                    known[k] = replace(e, linux=name)
                    continue
                if os.path.islink(target):
                    if self._follow_symlinks:
                        target = os.path.realpath(target)
                    else:
                        os.unlink(target)   # replace symlink with regular file
                try:
                    mode = os.stat(target).st_mode & 0o7777
                except OSError:
                    mode = self._file_mode
            if e.size % self.cluster_bytes:
                # keep the last cluster's bytes past the end (see below)
                last = e.chain[e.size // self.cluster_bytes]
                if last not in self._mat:
                    self._materialize(last)
            if not act(f"write {target}", k, self._write_file, target, e,
                       mode, None if old is None else (old_k, old)):
                continue
            act("set attributes", k, self._apply_meta, target, e,
                None if old is None else replace(old, date=-1))
            if k in pairs:
                del known[pairs[k]]
            known[k] = replace(e, linux=name)
            log.info("%s: %s (%d bytes)", self.path, target, e.size)

        # Unchanged directories (moves were handled above): take the
        # client's entry (dates) under the existing name.
        for k in common:
            if good[k].is_dir and k not in failed:
                known[k] = replace(good[k], linux=known[k].linux,
                                   parent=known[k].parent)

        # Clusters now on Linux: those of the files written back (within
        # their size: a chain longer than that is an append in progress).
        cb = self.cluster_bytes
        synced = {c for k in good if k not in failed and k in known
                  and not known[k].is_dir
                  for c in known[k].chain[:-(-known[k].size // cb)]}
        dir_clusters = {c for e in good.values() if e.is_dir
                        for c in e.chain}
        self._dirty -= synced | dir_clusters
        # now read from the Linux files -- except a last, partial cluster,
        # whose bytes past the end of the file (left over in DOS's buffer)
        # the client also reads back as it wrote them
        partial = {known[k].chain[known[k].size // cb] for k in good
                   if k not in failed and k in known and not known[k].is_dir
                   and known[k].size % cb}
        self._mat -= synced - partial
        self._retry = {k for k in failed if k in good}
        # what Linux now holds for the files we wrote, so the next rescan
        # does not take our own changes for someone else's
        for k in touched:
            if k in known and not known[k].is_dir:
                try:
                    st = os.stat(self._path(k))
                    self._lstat[k] = (st.st_size, st.st_mtime_ns,
                                      not st.st_mode & 0o200)
                except OSError:
                    pass
        for k in [k for k in self._lstat if k not in known]:
            del self._lstat[k]
        self._index_owners()
        self._purge()
        self._pending = bool(bad)
        return not bad and not failed

    def _restorable(self, k: Ident, e: Entry) -> Optional[Tuple[str, int]]:
        for key, t in self._trash.items():
            if (t.ident == k and t.entry.size == e.size
                    and t.entry.chain == e.chain
                    and not self._dirty.intersection(e.chain)
                    and key[0] == "trash"):
                return key
        return None

    def _index_owners(self) -> None:
        """Where each cluster not held in the spill file reads from: the
        current Linux files, then kept old versions (newest first)."""
        self._owner = {}
        cb = self.cluster_bytes
        # a Linux file holds its chain only up to its size; clusters past
        # that (an append, or a rewrite, still in progress) read on from
        # older versions
        for k, e in self._known.items():
            if not e.is_dir:
                for i, c in enumerate(e.chain[:-(-e.size // cb)]):
                    self._owner.setdefault(c, (k, i))
        for key in sorted(self._trash, reverse=True):
            t = self._trash[key].entry
            for i, c in enumerate(t.chain[:-(-t.size // cb)]):
                self._owner.setdefault(c, (key, i))

    def _syncer(self) -> None:
        while not self._stop.wait(SYNC_POLL):
            with self._lock:
                due = (self._pending and time.monotonic() - self._last_write
                       >= SYNC_QUIET)
                try:
                    if due:
                        self._sync()
                    elif self._trash:
                        self._purge()
                except Exception:
                    log.exception("%s: write-back failed", self.path)
                    self._pending = False
            now = time.monotonic()
            if self._follow and now - self._last_scan >= self._follow:
                self._last_scan = now
                try:
                    self.follow(self._scan_linux())
                except Exception:
                    log.exception("%s: rescan failed", self.path)

    # -- changes made on the Linux side ----------------------------------

    def _scan_linux(self) -> Dict[Tuple[str, ...], Tuple[bool, int, int,
                                                          bool]]:
        """The served tree as {relative path: (is_dir, size, mtime_ns,
        read-only)}, with DirUnit's rules for what is served. Only
        metadata is read -- directory listings and stat(), which the OS
        answers from its cache -- and a directory whose mtime has not
        changed is not listed again."""
        out: Dict[Tuple[str, ...], Tuple[bool, int, int, bool]] = {}

        def walk(dpath: str, rel: Tuple[str, ...], depth: int) -> None:
            try:
                mt = os.stat(dpath).st_mtime_ns
            except OSError:
                return
            cached = self._listing.get(dpath)
            if cached is None or cached[0] != mt:
                try:
                    names = sorted(os.listdir(dpath))
                except OSError:
                    return
                self._listing[dpath] = (mt, names)
            else:
                names = cached[1]
            for n in names:
                if n.startswith(TMP_PREFIX):
                    continue
                p = os.path.join(dpath, n)
                try:
                    lst = os.lstat(p)
                    st = os.stat(p)
                except OSError:
                    continue
                if stat.S_ISDIR(st.st_mode):
                    if stat.S_ISLNK(lst.st_mode) or depth > 64:
                        continue
                    out[rel + (n,)] = (True, 0, 0, False)
                    walk(p, rel + (n,), depth + 1)
                elif stat.S_ISREG(st.st_mode) and st.st_size <= MAX_FILE_SIZE:
                    out[rel + (n,)] = (False, st.st_size, st.st_mtime_ns,
                                       not st.st_mode & 0o200)

        walk(self.path, (), 0)
        return out

    def _krel(self, ident: Ident) -> Tuple[str, ...]:
        parts = []
        while ident != ROOT:
            e = self._known[ident]
            parts.append(e.linux)
            ident = e.parent
        return tuple(reversed(parts))

    def _kpaths(self) -> Dict[Tuple[str, ...], Ident]:
        return {self._krel(k): k for k in self._known}

    def follow(self, snap) -> bool:
        """Apply changes made on the Linux side (a _scan_linux result) to
        the volume. Waits (False) while the client is writing or has
        changes not yet written back."""
        with self._lock:
            if (self._pending or self._bad_seen or time.monotonic()
                    - self._last_write < SYNC_QUIET):
                return False
            return self._follow_linux(snap)

    def _follow_linux(self, snap) -> bool:
        known = self._known
        kp = self._kpaths()
        removed = [k for rel, k in kp.items()
                   if rel not in snap or snap[rel][0] != known[k].is_dir]
        added = sorted((rel for rel in snap
                        if rel not in kp or snap[rel][0] != known[kp[rel]].is_dir),
                       key=len)
        changed = [k for rel, k in kp.items()
                   if rel in snap and not snap[rel][0] and not known[k].is_dir
                   and snap[rel][1:] != self._lstat.get(k)]
        if not (removed or added or changed):
            return True
        n_rm = n_add = n_ch = 0
        for k in sorted(removed, key=lambda k: self._depth(k, known),
                        reverse=True):
            if k in known:
                self._remove_entry(k)
                n_rm += 1
        for k in changed:
            if k in known and self._update_entry(k, snap[self._krel(k)]):
                n_ch += 1
        kp = self._kpaths()
        for rel in added:
            parent = kp.get(rel[:-1], ROOT if len(rel) == 1 else None)
            if parent is None:
                continue
            k = self._add_entry(parent, rel[-1], snap[rel])
            if k is not None:
                kp[rel] = k
                n_add += 1
        self._index_owners()
        self.media_changed = True
        self.generation += 1
        log.info("%s: changed on the server side: %d added, %d removed, "
                 "%d updated", self.path, n_add, n_rm, n_ch)
        return True

    # FAT and directory editing (the overlay), as DOS itself would do it

    def _fat_get(self, c: int) -> int:
        return struct.unpack_from("<H", self._meta,
                                  self.fat_start * SECTOR_SIZE + 2 * c)[0]

    def _fat_set(self, c: int, v: int) -> None:
        for i in range(2):
            struct.pack_into("<H", self._meta, (self.fat_start + i * self.spf)
                             * SECTOR_SIZE + 2 * c, v)

    def _alloc(self, n: int) -> Optional[List[int]]:
        """n free clusters, chained (last = end of chain); None if the
        volume is full."""
        if n <= 0:
            return []
        fat = self._fat_table()
        free = [c for c in range(2, self.n_clusters + 2) if fat[c] == 0][:n]
        if len(free) < n:
            return None
        for a, b in zip(free, free[1:] + [0xFFFF]):
            self._fat_set(a, b)
        self._mat.difference_update(free)
        self._dirty.difference_update(free)
        return free

    def _free(self, chain) -> None:
        for c in chain:
            self._fat_set(c, 0)
        self._mat.difference_update(chain)
        self._dirty.difference_update(chain)

    def _dir_raw(self, d: Ident) -> bytes:
        if d == ROOT:
            return bytes(self._meta[self.root_start * SECTOR_SIZE:
                                    self.data_start * SECTOR_SIZE])
        return b"".join(self._cluster(c) for c in self._known[d].chain)

    def _dir_write(self, d: Ident, off: int, data: bytes) -> None:
        if d == ROOT:
            base = self.root_start * SECTOR_SIZE + off
            self._meta[base:base + len(data)] = data
            return
        cb = self.cluster_bytes
        c = self._known[d].chain[off // cb]
        if c not in self._mat:
            self._materialize(c)
        self._spill.seek((c - 2) * cb + off % cb)
        self._spill.write(data)

    @staticmethod
    def _live(raw: bytes):
        """(offset, entry) of a directory's entries in use."""
        for off in range(0, len(raw) - 31, 32):
            e = raw[off:off + 32]
            if e[0] == 0:
                return
            if e[0] != 0xE5:
                yield off, e

    def _find_slot(self, d: Ident, short: bytes) -> Optional[int]:
        for off, e in self._live(self._dir_raw(d)):
            if e[11] != ATTR_LFN and not e[11] & ATTR_VOLUME \
                    and e[0:11] == short:
                return off
        return None

    def _remove_entry(self, k: Ident) -> None:
        e = self._known[k]
        if e.is_dir:
            for c in [c for c, ce in self._known.items() if ce.parent == k]:
                self._remove_entry(c)
        raw = self._dir_raw(e.parent)
        off = self._find_slot(e.parent, e.short)
        if off is not None:
            self._dir_write(e.parent, off, b"\xe5")
            csum = lfn_checksum(e.short)
            o = off - 32
            while (o >= 0 and raw[o + 11] == ATTR_LFN and raw[o] != 0xE5
                   and raw[o + 13] == csum):
                self._dir_write(e.parent, o, b"\xe5")
                o -= 32
        self._free(e.chain)
        del self._known[k]
        self._lstat.pop(k, None)

    def _update_entry(self, k: Ident, info) -> bool:
        """A file changed on Linux: resize its chain, new size/date/RO."""
        _, size, mtime_ns, ro = info
        e = self._known[k]
        cb = self.cluster_bytes
        need = -(-size // cb)
        chain = list(e.chain)
        if need > len(chain):
            more = self._alloc(need - len(chain))
            if more is None:
                log.warning("%s: %s grew but the drive is full (-free)",
                            self.path, self._path(k))
                return False
            if chain:
                self._fat_set(chain[-1], more[0])
            chain += more
        elif need < len(chain):
            self._free(chain[need:])
            chain = chain[:need]
            if chain:
                self._fat_set(chain[-1], 0xFFFF)
        date, tm = dos_datetime(mtime_ns / 1e9)
        attr = (e.attr & ~ATTR_READONLY) | (ATTR_READONLY if ro else 0)
        off = self._find_slot(e.parent, e.short)
        if off is None:
            return False
        raw = bytearray(self._dir_raw(e.parent)[off:off + 32])
        raw[11] = attr
        struct.pack_into("<HHHHHHHI", raw, 14, tm, date, date, 0, tm, date,
                         chain[0] if chain else 0, size)
        self._dir_write(e.parent, off, bytes(raw))
        # contents come from the Linux file again
        self._mat.difference_update(chain)
        self._dirty.difference_update(chain)
        new = replace(e, size=size, chain=tuple(chain), attr=attr,
                      date=date, time=tm)
        nk: Ident = chain[0] if chain else ("empty", e.parent, e.short)
        del self._known[k]
        self._lstat.pop(k, None)
        self._known[nk] = new
        self._lstat[nk] = (size, mtime_ns, ro)
        return True

    def _add_entry(self, parent: Ident, name: str, info) -> Optional[Ident]:
        is_dir, size, mtime_ns, ro = info
        raw = self._dir_raw(parent)
        taken = {e[0:11].decode("cp437") for _, e in self._live(raw)
                 if e[11] != ATTR_LFN}
        disp = _display(name)
        short, flags, need_lfn = short_name(disp, taken)
        need_lfn = need_lfn or any(ord(ch) > 127 for ch in disp)
        lfn = (lfn_entries(disp, short) if need_lfn and
               len(disp.encode("utf-16-le")) // 2 <= LFN_MAX_UNITS else [])
        date, tm = dos_datetime(mtime_ns / 1e9 if mtime_ns else time.time())
        cb = self.cluster_bytes
        chain = self._alloc(1 if is_dir else -(-size // cb))
        if chain is None:
            log.warning("%s: no room for %s (-free)", self.path, name)
            return None
        attr = (ATTR_DIR if is_dir else ATTR_ARCHIVE) \
            | (ATTR_READONLY if ro else 0) \
            | (ATTR_HIDDEN if name.startswith(".") else 0)
        ents = lfn + [VirtualFatUnit._dirent(
            short, attr, flags, date, tm, chain[0] if chain else 0,
            0 if is_dir else size)]
        if is_dir:
            c = chain[0]
            pc = 0 if parent == ROOT else self._known[parent].chain[0]
            body = (VirtualFatUnit._dirent(b".".ljust(11), ATTR_DIR, 0, date,
                                           tm, c, 0)
                    + VirtualFatUnit._dirent(b"..".ljust(11), ATTR_DIR, 0,
                                             date, tm, pc, 0)).ljust(cb, b"\x00")
            self._spill.seek((c - 2) * cb)
            self._spill.write(body)
            self._mat.add(c)
        off = self._free_slots(parent, len(ents))
        if off is None:
            log.warning("%s: directory full, %s not added", self.path, name)
            self._free(chain)
            return None
        self._dir_write(parent, off, b"".join(ents))
        k: Ident = chain[0] if chain else ("empty", parent, short)
        self._known[k] = Entry(parent, short, disp, is_dir,
                               0 if is_dir else size, tuple(chain), attr,
                               date, tm, linux=name)
        if not is_dir:
            self._lstat[k] = (size, mtime_ns, ro)
        return k

    def _free_slots(self, d: Ident, n: int) -> Optional[int]:
        """Offset of n free consecutive directory slots, growing a
        subdirectory by a cluster if needed (the root has a fixed size)."""
        while True:
            raw = self._dir_raw(d)
            run = 0
            for off in range(0, len(raw) - 31, 32):
                if raw[off] == 0:                  # end: all free after
                    if (len(raw) - off) // 32 >= n - run:
                        return off - 32 * run
                    break
                run = run + 1 if raw[off] == 0xE5 else 0
                if run == n:
                    return off - 32 * (n - 1)
            if d == ROOT:
                return None
            more = self._alloc(1)
            if more is None:
                return None
            chain = self._known[d].chain
            self._fat_set(chain[-1], more[0])
            self._spill.seek((more[0] - 2) * self.cluster_bytes)
            self._spill.write(bytes(self.cluster_bytes))
            self._mat.add(more[0])
            self._known[d] = replace(self._known[d], chain=chain + (more[0],))

    def close(self) -> None:
        self._stop.set()
        self._thread.join()
        with self._lock:
            if self._pending or self._dirty:
                try:
                    if not self._sync():
                        log.warning("%s: closed with changes not written "
                                    "back (client mid-update)", self.path)
                except Exception:
                    log.exception("%s: write-back failed", self.path)
            self._purge(force=True)
            self._spill.close()
