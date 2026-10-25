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
"""pyterlink server status screen (curses), laid out like LNKSERVER's own.

  * the title, centred on the top line;
  * one blank line, then a box headed "This Computer (Server)" and "Other
    Computer (Client)" over a box of drive rows, "C:   equals   D:" with
    "equals" in the middle of the line; a "*"
    one space left of a server drive while the client is accessing it;
  * under the boxes, the transfer rate and sector counts, then the file
    being read or written (full width);
  * the log (with -v), its newest line just above the bottom bar;
  * the bottom bar: "Transfer: Reading | Port=COM2:  Speed=115200 |
    Q=Exit". Transfer: shows Reading/Writing while frames move on the
    line, Idle while only keepalives flow, nothing when the line is
    silent; Port= and Speed= go blank once the link's idle characters stop
    (LNKSERVER shows them the same way).

The title, the boxes and the bottom bar are laid out for 80 columns
whatever the terminal's width (LNKSERVER's screen); the file line and the
log use the full width.

render() lays the screen out as text (testable without a terminal);
CursesUI paints it.
"""

import curses
import locale
import time
from dataclasses import dataclass, field
from typing import List, Optional

from .. import __version__

TITLE = f"pyterlink server (LNKSERVER) {__version__}"
COLS = 80                    # the fixed screen width of title, boxes, bar
EXIT_KEY = "Q=Exit"          # q, Q or Esc


@dataclass
class DriveMapping:
    """A single drive redirection: server drive -> client drive."""
    server_drive: str
    client_drive: Optional[str]  # None while not mapped
    busy: bool = False           # being read or written right now


@dataclass
class StatusSnapshot:
    """The server's state for display."""
    mappings: List[DriveMapping] = field(default_factory=list)
    transport: str = ""
    connection: str = "Disconnected"
    reads: int = 0                    # sectors read by the client
    writes: int = 0                   # sectors written by the client
    speed: int = 9600                 # current line rate (rate steps)
    log: List[str] = field(default_factory=list)   # recent log lines
    rate: float = 0.0                 # bytes/s moved, last few seconds
    activity: str = ""                # "Reading C:\\DOCS\\X.TXT"; "" = idle
    port: str = ""                    # "COM2:" / "ttyUSB0"
    link_up: bool = False             # bytes (incl. idle chars) arriving
    transfer: str = ""                # "Reading" / "Writing" / ""


def format_rate(bps: float) -> str:
    if bps < 1:
        return "0 B/s"
    if bps < 1000:
        return f"{bps:.0f} B/s"
    return f"{bps / 1000:.1f} KB/s"


_UNI = {"h": "\u2500", "v": "\u2502", "tl": "\u250c", "tr": "\u2510",
        "bl": "\u2514", "br": "\u2518", "lt": "\u251c", "rt": "\u2524"}
_ASCII = {"h": "-", "v": "|", "tl": "+", "tr": "+", "bl": "+", "br": "+",
          "lt": "+", "rt": "+"}


def _boxes(snap: StatusSnapshot, w: int, g: dict) -> List[str]:
    """The two LNKSERVER boxes, centred in width w."""
    bw = 52                               # box width incl. borders
    inner = bw - 2
    half = inner // 2
    pad = " " * max((w - bw) // 2, 0)

    def row(left: str, right: str) -> str:
        return pad + g["v"] + (left.center(half) + right.center(
            inner - half))[:inner] + g["v"]

    out = [pad + g["tl"] + g["h"] * inner + g["tr"],
           row("This Computer", "Other Computer"),
           row("(Server)", "(Client)"),
           pad + g["lt"] + g["h"] * inner + g["rt"]]
    for m in snap.mappings:
        # the server drive centred under "This Computer", "*" one space to
        # its left while busy; "equals" in the middle of the line; the
        # client drive centred under "Other Computer"
        cells = [" "] * inner
        sd = m.server_drive
        at = (half - len(sd)) // 2
        cells[at:at + len(sd)] = sd
        if m.busy and at >= 2:
            cells[at - 2] = "*"
        if m.client_drive:
            mid = (inner - len("equals")) // 2
            cells[mid:mid + 6] = "equals"
            cd = m.client_drive
            at = half + (inner - half - len(cd)) // 2
            cells[at:at + len(cd)] = cd
        out.append(pad + g["v"] + "".join(cells) + g["v"])
    if not snap.mappings:
        out.append(row("(no drives)", ""))
    out.append(pad + g["bl"] + g["h"] * inner + g["br"])
    return out


def bottom_bar(snap: StatusSnapshot, w: int = COLS) -> str:
    """The bar, always COLS wide (cut only by a narrower terminal)."""
    port = snap.port if snap.link_up else ""
    speed = str(snap.speed) if snap.link_up else ""
    text = (f" Transfer: {snap.transfer:<7} |     Port={port:<8}"
            f"     Speed={speed:<6}     | {EXIT_KEY}")
    return text[:COLS].ljust(COLS)[:w]


def render(snap: StatusSnapshot, h: int, w: int,
           unicode_ok: bool = True) -> List[str]:
    """The whole screen as h lines of width <= w; the last line is the
    bottom bar."""
    g = _UNI if unicode_ok else _ASCII
    lines = [TITLE.center(COLS).rstrip()[:w], ""]
    lines += [b[:w] for b in _boxes(snap, COLS, g)] + [""]
    lines.append(f"{format_rate(snap.rate):>10}   Read: {snap.reads}"
                 f"  Written: {snap.writes}"[:w])
    lines.append((snap.activity or "Idle")[:w])
    room = h - 1 - len(lines) - 1        # a blank line, the log, the bar
    if room > 0:
        rows: List[str] = []
        for line in snap.log:
            rows += [line[i:i + w - 1] for i in
                     range(0, max(len(line), 1), max(w - 1, 1))]
        rows = rows[-room:]
        lines += [""] * (room + 1 - len(rows)) + rows
    lines = lines[:h - 1]
    lines += [""] * (h - 1 - len(lines))
    return lines + [bottom_bar(snap, w)]


class CursesUI:
    """Live curses screen; snapshot_fn() is polled 4 times a second.
    q, Q or Esc quits."""

    def __init__(self, snapshot_fn) -> None:
        self._snapshot_fn = snapshot_fn
        self._stdscr: Optional["curses.window"] = None
        self._unicode = "UTF" in (locale.getpreferredencoding(False)
                                  or "").upper()

    def _draw(self) -> None:
        if self._stdscr is None:
            return
        snap = self._snapshot_fn()
        self._stdscr.erase()
        h, w = self._stdscr.getmaxyx()
        lines = render(snap, h, w, self._unicode)
        for y, line in enumerate(lines):
            attr = curses.A_REVERSE if y == h - 1 else 0
            try:
                # the last cell of the screen cannot be written
                self._stdscr.addnstr(y, 0, line, w - 1 if y == h - 1
                                     else w, attr)
            except curses.error:
                pass
        self._stdscr.refresh()

    def run(self, stdscr) -> int:
        self._stdscr = stdscr
        stdscr.nodelay(True)
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        last = 0.0
        while True:
            now = time.monotonic()
            if now - last > 0.25:
                self._draw()
                last = now
            try:
                ch = stdscr.getch()
            except curses.error:
                ch = -1
            if ch in (ord("q"), ord("Q"), 27):
                break
            time.sleep(0.05)
        return 0


def run_ui(snapshot_fn) -> int:
    """curses.wrapper around CursesUI.run (UTF-8 line drawing when the
    terminal's locale allows it)."""
    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        pass
    return curses.wrapper(lambda stdscr: CursesUI(snapshot_fn).run(stdscr))
