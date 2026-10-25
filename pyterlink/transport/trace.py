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
"""Wire trace: record every byte on a link in the com3 proxy log format.

    [2026-10-04T16:07:53.405123] [LNKCLIENT->LNKSERVER] aa
    [2026-10-04T16:07:53.406001] [LNKSERVER->LNKCLIENT] 00

so tools/decode_pyterlink_capture.py and tools/capture_frames.py
read a live pyterlink session exactly as they read proxy captures.
`local` names this end: "LNKCLIENT" for pyterlink-client, "LNKSERVER" for
pyterlink-server.
"""
import datetime
import threading


class TraceIO:
    """Wraps a byte link (tx_byte / flush / rx_byte / close) and logs it."""

    def __init__(self, inner, path: str, local: str = "LNKCLIENT") -> None:
        self._inner = inner
        self._f = open(path, "a", buffering=1)
        peer = "LNKSERVER" if local == "LNKCLIENT" else "LNKCLIENT"
        self._tx_tag = f"[{local}->{peer}]"
        self._rx_tag = f"[{peer}->{local}]"
        self._pending = bytearray()
        self._lock = threading.Lock()
        stamp = datetime.datetime.now().isoformat()
        from .. import __version__, command_line
        self._f.write(f"\n=== pyterlink trace started at {stamp} "
                      f"(pyterlink {__version__}) command: {command_line()}"
                      f" ===\n")

    def _log(self, tag: str, data: bytes) -> None:
        stamp = datetime.datetime.now().isoformat()
        with self._lock:
            self._f.write(f"[{stamp}] {tag} {data.hex()}\n")

    def tx_byte(self, b: int) -> None:
        self._pending.append(b & 0xFF)
        self._inner.tx_byte(b)

    def flush(self) -> None:
        if self._pending:
            self._log(self._tx_tag, bytes(self._pending))
            self._pending.clear()
        f = getattr(self._inner, "flush", None)
        if callable(f):
            f()

    def rx_byte(self, timeout_sec=1.0):
        self.flush()   # the inner rx flushes too; log the burst first
        b = self._inner.rx_byte(timeout_sec)
        if b is not None:
            self._log(self._rx_tag, bytes([b]))
        return b

    def rx_bytes(self, n: int, timeout_sec: float) -> bytes:
        self.flush()
        bulk = getattr(self._inner, "rx_bytes", None)
        if callable(bulk):
            data = bulk(n, timeout_sec)
        else:
            data = bytearray()
            while len(data) < n:
                b = self._inner.rx_byte(timeout_sec)
                if b is None:
                    break
                data.append(b)
            data = bytes(data)
        if data:
            self._log(self._rx_tag, data)
        return data

    def set_baud(self, baud: int) -> None:
        """Change the line rate; logged as a [SYSTEM] line like
        captures converted from strace carry ("line rate N baud")."""
        self.flush()
        f = getattr(self._inner, "set_baud", None)
        if callable(f):
            f(baud)
        stamp = datetime.datetime.now().isoformat()
        with self._lock:
            self._f.write(f"[{stamp}] [SYSTEM] line rate {baud} baud\n")

    def close(self) -> None:
        self.flush()
        try:
            self._inner.close()
        finally:
            self._f.close()

    @property
    def name(self) -> str:
        return getattr(self._inner, "name", "link")
