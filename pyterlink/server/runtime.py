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
"""pyterlink server runtime: run LinkServer on a serial link, with the
LNKSERVER-style status display ("This Computer / Other Computer").
"""

import collections
import logging
import os
import sys
import threading
import time
from typing import Dict, List, Optional

from ..protocol.server import LinkServer, Unit, list_volumes, KIND_FAT32
from ..protocol.session import (LinkError, MAP_LOCAL, MAP_NONE, N_DRIVES,
                                SECTOR_SIZE)
from ..transport.serial_backend import open_link, com_url
from .cli import ServerArgs
from .ui import DriveMapping, StatusSnapshot, run_ui

log = logging.getLogger(__name__)


def _letter(n: int) -> str:
    return chr(ord("A") + n) + ":"


RATE_WINDOW = 3.0       # seconds the transfer rate is averaged over
DATA_FOR = 2.0          # a frame moved this recently: the transfer is
                        # still on (an 8 KB chunk takes 0.7 s at 115200)
MIN_PACE = 1000.0       # bytes/s: the slowest sector-data pacing
LINK_IDLE = 2.0         # no byte (not even idle keepalives) this long:
                        # Port= and Speed= go blank, as on LNKSERVER


def describe_map(dmap: bytes) -> str:
    """The client's drive map as letter pairs. On the wire it is one byte
    per client letter A:, B:, ... (29 bytes): ff = a drive of the client's
    own, fe = not redirected, anything else = the server unit (0 = A:)."""
    pairs = [f"client {_letter(i)} = server {_letter(u)}"
             for i, u in enumerate(dmap[:N_DRIVES])
             if u not in (MAP_LOCAL, MAP_NONE)]
    local = sum(1 for u in dmap[:N_DRIVES] if u == MAP_LOCAL)
    return (", ".join(pairs) or "no drives redirected") + \
        f" (client has {local} drive(s) of its own)"


class ServerStatus:
    """Collects LinkServer events into a StatusSnapshot for the UI."""

    def __init__(self, transport: str, served: List[int],
                 units: Optional[Dict[int, Unit]] = None,
                 port: str = "") -> None:
        self._lock = threading.Lock()
        # the link's name without its opening rate ("serial /dev/x @ 9600")
        self.transport = transport.split(" @ ")[0]
        self.speed = 9600
        self.log_lines: collections.deque = collections.deque(maxlen=200)
        self.served = served
        self.connection = "Waiting"
        self.client_of: Dict[int, str] = {}
        self.reads = 0
        self.writes = 0
        self._units = units or {}
        self._index: Dict[int, object] = {}
        self._moved: collections.deque = collections.deque()  # (t, bytes)
        self.activity = ""
        self._logged = ""           # last file logged (INFO, -v)
        self.port = port                # "COM2:" or the device's name
        self.server = None              # LinkServer (its last_rx)
        self._unit: Optional[int] = None     # unit of the last read/write
        self._kind = ""                 # "Reading" / "Writing"

    def _describe(self, unit: int, lba: int, write: bool) -> str:
        """The file (or FAT, directory, ...) a request touches."""
        idx = self._index.get(unit)
        if idx is None:
            u = self._units.get(unit)
            if u is None:
                return f"{_letter(unit)} sector {lba}"
            from .fileindex import VolumeIndex
            idx = self._index[unit] = VolumeIndex(u, _letter(unit))
        try:
            return idx.describe(lba, write)
        except Exception as e:                # never break the link
            log.debug("describe: %s", e)
            return f"{_letter(unit)} sector {lba}"

    def on_event(self, kind: str, **kw) -> None:
        if kind in ("read", "write"):
            # outside the lock: may read the volume to index it
            what = self._describe(kw["unit"], kw["lba"], kind == "write")
            if kind == "write" and kw["unit"] in self._index:
                self._index[kw["unit"]].touched()
        with self._lock:
            if kind == "connected":
                self.connection = "Connected"
            elif kind == "mapped":
                dmap = kw["drive_map"]
                self.client_of = {
                    u: _letter(i) for i, u in enumerate(dmap[:N_DRIVES])
                    if u not in (MAP_LOCAL, MAP_NONE)}
            elif kind in ("read", "write"):
                n = kw.get("count", 1)
                if kind == "read":
                    self.reads += n
                else:
                    self.writes += n
                now = time.monotonic()
                self._moved.append((now, n * SECTOR_SIZE))
                self._kind = "Reading" if kind == "read" else "Writing"
                self.activity = f"{self._kind} {what}"
                self._unit = kw["unit"]
            elif kind == "handshake":
                self.connection = "Connecting"
            elif kind == "rate":
                self.speed = kw["baud"]
        if kind in ("read", "write"):
            log.debug("%s unit %d lba %d count %d: %s", kind, kw["unit"],
                      kw["lba"], kw.get("count", 1), what)
            # One INFO line per file the client starts reading or writing
            # (FAT, directory and free-space sectors are not files).
            if "\\" in what and not what.endswith("(directory)"):
                safe = what.translate(
                    {ch: "?" for ch in range(32)} | {127: "?"})
                line = ("Reading " if kind == "read" else "Writing ") + safe
                with self._lock:
                    new = line != self._logged
                    self._logged = line
                if new:
                    log.info("%s", line)
        elif kind == "rate":
            log.info("line rate %d baud", kw["baud"])
        elif kind == "mapped":
            log.info("drive map: %s", describe_map(kw["drive_map"]))
        elif kind != "request":
            log.info("%s", kind)

    def _rate(self, now: float) -> float:
        """Bytes per second moved over the last RATE_WINDOW seconds."""
        while self._moved and now - self._moved[0][0] > RATE_WINDOW:
            self._moved.popleft()
        return sum(n for _, n in self._moved) / RATE_WINDOW

    def snapshot(self) -> StatusSnapshot:
        """Transfer: Reading/Writing (and the drive's "*", and the file
        line) for as long as frames keep moving on the line; "Idle" while
        only keepalives flow; blank once the line is silent."""
        now = time.monotonic()
        srv = self.server
        last_rx = getattr(srv, "last_rx", 0.0)
        moving = now - getattr(srv, "last_data", 0.0) < DATA_FOR
        idling = now - getattr(srv, "last_idle", 0.0) < LINK_IDLE
        with self._lock:
            transfer = ((self._kind or "Busy") if moving
                        else "Idle" if idling else "")
            return StatusSnapshot(
                mappings=[DriveMapping(
                    _letter(u), self.client_of.get(u),
                    busy=moving and u == self._unit)
                    for u in self.served],
                port=self.port, link_up=now - last_rx < LINK_IDLE,
                transfer=transfer,
                transport=self.transport, connection=self.connection,
                reads=self.reads, writes=self.writes, speed=self.speed,
                log=list(self.log_lines), rate=self._rate(now),
                activity=self.activity if moving else "")


class _UILogHandler(logging.Handler):
    """Logging into the status display instead of over it."""

    def __init__(self, status: ServerStatus) -> None:
        super().__init__()
        self.status = status
        from ..errors import QuietFormatter
        self.setFormatter(QuietFormatter(
            "%(asctime)s %(levelname)s %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.status.log_lines.append(self.format(record))
        except Exception:
            self.handleError(record)


class ServerRuntime:
    """Owns the link and the LinkServer for one pyterlink-server run."""

    def __init__(self, args: ServerArgs, link=None) -> None:
        self._args = args
        self._link = link
        self._units: List[Optional[Unit]] = []
        self.status: Optional[ServerStatus] = None
        self.server: Optional[LinkServer] = None

    def _assign(self) -> Dict[int, Unit]:
        """Server drive letter -> unit. A partitioned image takes one letter
        per DOS volume from its own letter on (C:=disk.img -> C: primary,
        D:, E:, ... its logical drives, as MS-DOS 6.22 letters them);
        "#n" picks the n-th volume only; a directory is one drive."""
        a = self._args
        units: Dict[int, Unit] = {}
        from .geometry import parse_chs
        for letter, spec in sorted(a.drives.items()):
            geom = None
            path, sep, g = spec.rpartition("@")
            if sep and g.count(",") == 3:
                geom = parse_chs(g)
            else:
                path = spec
            base, sep, sel = path.rpartition("#")
            if sep and sel.isdigit():
                path = base
            else:
                sel = ""
            if os.path.isdir(path):
                vols: List = [(None, None)]
            else:
                with open(path, "rb") as f:
                    vols = list_volumes(f)
                if not vols:
                    raise ValueError(f"{path}: no DOS volume to serve")
                if geom is not None:
                    size = os.path.getsize(path) // 512
                    if size != geom.total_sectors:
                        log.warning("%s: %d sectors, geometry %s gives %d",
                                    path, size, geom, geom.total_sectors)
            if sel:
                n = int(sel)
                if not 1 <= n <= len(vols):
                    raise ValueError(f"{spec}: {path} has {len(vols)} DOS "
                                     f"volume(s)")
                vols = [vols[n - 1]]
            for k, (off, kind) in enumerate(vols):
                u = letter + k
                if u > 25 or u in units or (k and u in a.drives):
                    raise ValueError(
                        f"{spec}: volume {k + 1} would be {_letter(u)}, "
                        f"which is already assigned (or past Z:)")
                if off is None:
                    from .dirsync import SyncedDirUnit
                    st = os.statvfs(path)
                    ro_fs = bool(st.f_flag & os.ST_RDONLY)
                    units[u] = SyncedDirUnit(
                        path, geometry=geom,
                        writable=not a.read_only and not ro_fs,
                        free_bytes=min(a.free_mb << 20,
                                       st.f_bavail * st.f_frsize),
                        follow_symlinks=not a.no_follow_symlinks)
                elif kind == KIND_FAT32 and not a.fat32:
                    from .fat32 import Fat32Unit
                    units[u] = Fat32Unit(path, off, geom)     # read-only
                else:
                    units[u] = Unit(path, writable=not a.read_only,
                                    offset=off)
                if len(vols) > 1:
                    log.info("%s = %s volume %d (sector %d, %s)", _letter(u),
                             path, k + 1, off, kind)
        return units

    def _build(self) -> None:
        if self.server is not None:
            return
        a = self._args
        assigned = self._assign()
        self._units = [None] * (max(assigned) + 1)
        for u, unit in assigned.items():
            self._units[u] = unit
        offered = [u for u in sorted(assigned) if u not in a.excluded]
        if self._link is None:
            url = a.port_url or com_url(a.com or 1)
            self._link = open_link(url)       # 9600: the handshake rate
            # Reply data goes out in 8 KB chunks (protocol) at the line
            # rate, or capped at -pace; a burst LNKCLIENT loses halves the
            # cap (_burst_lost).
            self._link.max_tx_rate = a.pace or None
            self._paced = self._link
            if a.trace:
                from ..transport.trace import TraceIO
                self._link = TraceIO(self._link, a.trace, local="LNKSERVER")
        if not hasattr(self, "_paced"):         # a link given by the caller
            self._paced = (self._link if hasattr(self._link, "max_tx_rate")
                           else None)
        if a.port_url:
            port = (a.port_url[5:] if a.port_url.startswith("/dev/")
                    else a.port_url)
        else:
            port = f"COM{a.com or 1}:"
        self.status = ServerStatus(getattr(self._link, "name", "serial"),
                                   offered, assigned, port)
        self.server = LinkServer(self._link, self._units, offered,
                                     on_event=self.status.on_event,
                                     max_baud=a.baud)
        self.status.server = self.server
        self.server.on_burst_lost = self._burst_lost

    def _burst_lost(self, nbytes: int) -> None:
        """LNKCLIENT did not acknowledge a sector-data burst: its side could
        not take the bytes that fast (a DOSBox drains its port at a rate
        set by its cycles). Send sector data at half the rate from now on
        (floor MIN_PACE); LNKCLIENT's retry -- or DOS's Abort, Retry, Fail?
        answered Retry -- then gets through."""
        link = getattr(self, "_paced", None)
        if link is None:
            return
        line = self.server.baud / 10              # bytes/s at this rate
        cur = min(link.max_tx_rate or line, line)
        new = max(cur / 2, MIN_PACE)
        if new >= cur:
            log.warning("LNKCLIENT lost a %d-byte burst at %d bytes/s (already "
                        "the slowest pacing)", nbytes, cur)
            return
        link.max_tx_rate = new
        log.warning("LNKCLIENT lost a %d-byte burst at %d bytes/s; sending "
                    "sector data at %d bytes/s from now on", nbytes, cur,
                    new)

    def run(self) -> int:
        self._build()
        try:
            while True:
                try:
                    self.server.serve()
                except LinkError as e:
                    log.warning("link: %s", e)
        except KeyboardInterrupt:
            return 0
        finally:
            self._cleanup()

    def run_verbose_tty(self) -> int:
        """Serve in a background thread; status display on this thread."""
        self._build()
        root = logging.getLogger()
        saved = root.handlers[:]
        root.handlers = [_UILogHandler(self.status)]
        t = threading.Thread(target=self._serve_forever, daemon=True)
        t.start()
        try:
            return run_curses_ui(self.status)
        finally:
            root.handlers = saved
            self._cleanup()

    def _serve_forever(self) -> None:
        while True:
            try:
                self.server.serve()
            except LinkError as e:
                log.warning("link: %s", e)
            except Exception:
                log.exception("server stopped")
                return

    def _cleanup(self) -> None:
        for u in self._units:
            if u is not None:
                u.close()
        if self._link is not None:
            self._link.close()


def run_curses_ui(ui: ServerStatus) -> int:
    """Run the curses status display (plain-text no-op off a tty)."""
    if not sys.stdout.isatty():
        return 0
    return run_ui(ui.snapshot)
