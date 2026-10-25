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
"""No tracebacks on the user's screen.

Every pyterlink command runs through run(): a failure anywhere -- in the
command, in a background thread, or logged with exc_info by any module --
is reported as one line, `<prog>: <what went wrong> (details: <file>)`, and
the full traceback is appended to that file (in the system temp directory)
for debugging.
"""
from __future__ import annotations

import logging
import os
import sys
import tempfile
import threading
import time
import traceback
from typing import Callable, List, Optional

_prog = "pyterlink"


def error_file() -> str:
    return os.path.join(tempfile.gettempdir(), f"{_prog}-errors.log")


def save(exc: BaseException) -> str:
    """Append exc's traceback to the error file; returns its path."""
    path = error_file()
    try:
        with open(path, "a") as f:
            from . import __version__
            f.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} {_prog} "
                    f"{__version__}\n")
            f.write("".join(traceback.format_exception(
                type(exc), exc, exc.__traceback__)))
    except OSError:
        return "(could not be saved)"
    return path


def describe(exc: BaseException) -> str:
    """One readable line for an exception."""
    if isinstance(exc, OSError) and exc.strerror:
        where = exc.filename or ""
        return f"{where}: {exc.strerror}" if where else exc.strerror
    text = str(exc).strip()
    # pyterlink's own errors, ValueError and pyserial's are written to be
    # read; anything else gets its type for context
    mod = type(exc).__module__
    if text and (mod.startswith(("pyterlink", "serial"))
                 or type(exc) is ValueError):
        return text
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def one_line(exc: BaseException) -> str:
    return f"{describe(exc)} (details: {save(exc)})"


class QuietFormatter(logging.Formatter):
    """Log records carrying an exception get its one-line description; the
    traceback goes to the error file."""

    def format(self, record: logging.LogRecord) -> str:
        if record.exc_info and record.exc_info[1] is not None:
            exc = record.exc_info[1]
            record = logging.makeLogRecord(record.__dict__)
            record.msg = f"{record.getMessage()}: {one_line(exc)}"
            record.args = None
            record.exc_info = None
            record.exc_text = None
        return super().format(record)


def quiet_logging() -> None:
    """Give every root handler a QuietFormatter (same format string)."""
    for h in logging.getLogger().handlers:
        fmt = h.formatter._fmt if h.formatter else None
        h.setFormatter(QuietFormatter(fmt))


def run(prog: str, fn: Callable[[Optional[List[str]]], int],
        argv: Optional[List[str]] = None) -> int:
    """Run a command's main; any failure -> one line on stderr, exit 1.
    Ctrl-C ends quietly."""
    global _prog
    _prog = prog

    def thread_hook(args) -> None:
        if args.exc_type is SystemExit:
            return
        print(f"{prog}: {args.thread.name if args.thread else 'thread'}: "
              f"{one_line(args.exc_value)}", file=sys.stderr)
    threading.excepthook = thread_hook
    sys.excepthook = lambda t, e, tb: print(f"{prog}: {one_line(e)}",
                                            file=sys.stderr)
    try:
        return fn(argv)
    except KeyboardInterrupt:
        return 130
    except SystemExit:
        raise
    except BaseException as e:
        print(f"{prog}: {one_line(e)}", file=sys.stderr)
        return 1
