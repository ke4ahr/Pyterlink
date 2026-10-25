#!/usr/bin/env python3
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
"""klog_tee - read from stdin, write to stdout and files with kernel-style timestamps.

Like tee(1), but prepends each line with a monotonic timestamp in the format
used by the Linux kernel logger: [762942.864064]

Stderr lines are marked with '*' after the timestamp: [762942.864064]*

Usage: klog_tee [OPTION]... [FILE]...
"""

import sys
import argparse
import signal
import time
import os
import selectors
from typing import List, TextIO, Optional


class KlogTee:
    def __init__(self, files: List[str], append: bool = False,
                 ignore_interrupts: bool = False,
                 output_error_mode: str = 'warn',
                 flush_interval: float = 60.0,
                 null_terminated: bool = False):
        self.files = files
        self.append = append
        self.ignore_interrupts = ignore_interrupts
        self.output_error_mode = output_error_mode
        self.flush_interval = flush_interval
        self.null_terminated = null_terminated
        self.file_handles: List[TextIO] = []
        self.start_time = time.monotonic()
        self._running = True
        self._setup_signals()
        self._open_files()

    def _setup_signals(self):
        """Install signal handlers for clean shutdown."""
        def handler(signum, frame):
            self._running = False
        signal.signal(signal.SIGTERM, handler)
        signal.signal(signal.SIGHUP, handler)
        if not self.ignore_interrupts:
            signal.signal(signal.SIGINT, handler)
        else:
            signal.signal(signal.SIGINT, signal.SIG_IGN)

    def _open_files(self):
        mode = 'a' if self.append else 'w'
        for f in self.files:
            try:
                if f == '-':
                    # '-' means stdout, but we already write to stdout
                    continue
                fh = open(f, mode, buffering=1)  # line-buffered
                self.file_handles.append(fh)
                # Write banner when appending to show wall-clock start time
                if self.append:
                    wall_time = time.strftime('%Y-%m-%d %H:%M:%S %Z')
                    banner = f"=== klog_tee started at {wall_time} (monotonic base: {time.monotonic():.6f}) ===\n"
                    try:
                        fh.write(banner)
                        fh.flush()
                    except OSError:
                        pass
            except OSError as e:
                self._error(f"cannot open {f}: {e}")
                if self.output_error_mode in ('exit', 'exit-nopipe'):
                    sys.exit(1)

    def _error(self, msg: str):
        """Write error to stderr with timestamp."""
        timestamped = self._get_timestamp(stderr=True) + msg + '\n'
        try:
            sys.stderr.write(timestamped)
            sys.stderr.flush()
        except OSError:
            pass  # best effort

    def _get_timestamp(self, stderr: bool = False) -> str:
        elapsed = time.monotonic() - self.start_time
        mark = '*' if stderr else ''
        return f"[{elapsed:12.6f}]{mark} "

    def _write_stdout(self, data: str):
        """Write timestamped data to stdout and all file handles."""
        timestamped = self._get_timestamp(stderr=False) + data
        # Write to stdout
        try:
            sys.stdout.write(timestamped)
            sys.stdout.flush()
        except BrokenPipeError:
            self._running = False
            return
        except OSError as e:
            self._handle_write_error("stdout", e)

        # Write to files
        for i, fh in enumerate(self.file_handles):
            try:
                fh.write(timestamped)
                fh.flush()
            except OSError as e:
                self._handle_write_error(self.files[i], e)

    def _write_stderr(self, data: str):
        """Write timestamped data to stderr."""
        timestamped = self._get_timestamp(stderr=True) + data
        try:
            sys.stderr.write(timestamped)
            sys.stderr.flush()
        except OSError:
            pass  # best effort

    def _flush_all(self):
        """Flush all output buffers (stdout, stderr, files)."""
        try:
            sys.stdout.flush()
        except OSError as e:
            self._handle_write_error("stdout", e)
        try:
            sys.stderr.flush()
        except OSError:
            pass
        for i, fh in enumerate(self.file_handles):
            try:
                fh.flush()
            except OSError as e:
                self._handle_write_error(self.files[i], e)

    def _handle_write_error(self, target: str, error: OSError):
        if self.output_error_mode == 'exit':
            self._error(f"write error on {target}: {error}")
            sys.exit(1)
        elif self.output_error_mode == 'exit-nopipe':
            # Only exit if target is not a pipe
            if target != "stdout" or not self._is_pipe(sys.stdout):
                self._error(f"write error on {target}: {error}")
                sys.exit(1)
        else:  # warn
            self._error(f"write error on {target}: {error}")

    def _is_pipe(self, fh: TextIO) -> bool:
        try:
            return os.fstat(fh.fileno()).st_mode & 0o10000 == 0o10000
        except OSError:
            return False

    def _close_files(self):
        for fh in self.file_handles:
            try:
                fh.close()
            except OSError:
                pass

    def run(self):
        """Read stdin, prepend timestamp, write to outputs.

        Uses selectors for efficient I/O with periodic flush.
        Supports both line-oriented (-print) and null-terminated (-print0) input.
        """
        sel = selectors.DefaultSelector()
        sel.register(sys.stdin, selectors.EVENT_READ)

        last_flush = time.monotonic()
        buffer = ''

        try:
            while self._running:
                # Wait for stdin with timeout to allow periodic flush
                timeout = max(0.0, self.flush_interval - (time.monotonic() - last_flush))
                events = sel.select(timeout)

                now = time.monotonic()
                if now - last_flush >= self.flush_interval:
                    self._flush_all()
                    last_flush = now

                for key, _ in events:
                    if self.null_terminated:
                        # Read raw bytes, split on NUL
                        data = os.read(key.fd, 4096)
                        if not data:
                            self._running = False
                            break
                        parts = data.decode('utf-8', errors='replace').split('\0')
                        # All but last are complete records
                        for part in parts[:-1]:
                            self._write_stdout(part + '\0')
                        # Last part may be incomplete, keep in buffer
                        buffer = parts[-1]
                    else:
                        # Line-oriented (default, like find -print)
                        line = key.fileobj.readline()
                        if not line:  # EOF
                            self._running = False
                            break
                        self._write_stdout(line)
                    last_flush = time.monotonic()

        except KeyboardInterrupt:
            if not self.ignore_interrupts:
                raise
        except BrokenPipeError:
            pass
        finally:
            # Flush any remaining buffer
            if buffer and not self.null_terminated:
                self._write_stdout(buffer)
            self._flush_all()
            self._close_files()


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read from stdin, write to stdout and files with kernel-style timestamps",
        usage="%(prog)s [OPTION]... [FILE]...",
        add_help=False
    )
    parser.add_argument('-a', '--append', action='store_true',
                        help='append to the given FILEs, do not overwrite')
    parser.add_argument('-i', '--ignore-interrupts', action='store_true',
                        help='ignore interrupt signals')
    parser.add_argument('-p', action='store_true',
                        help='diagnose errors writing to non-pipes (alias for --output-error=warn)')
    parser.add_argument('--output-error', choices=['warn', 'exit', 'exit-nopipe'],
                        default='warn', metavar='MODE',
                        help='set behavior on write error (default: warn)')
    parser.add_argument('--flush-interval', type=float, default=60.0, metavar='SECONDS',
                        help='periodic flush interval in seconds (default: 60)')
    parser.add_argument('-0', '--null', '--print0', action='store_true',
                        help='input records are NUL-terminated (like find -print0)')
    parser.add_argument('--help', action='help', help='display this help and exit')
    parser.add_argument('--version', action='version', version='klog_tee 1.0',
                        help='output version information and exit')
    parser.add_argument('files', nargs='*', help='output files (use - for stdout)')
    return parser.parse_args(argv)


def main():
    args = parse_args(sys.argv[1:])

    output_error_mode = args.output_error
    if args.p:
        output_error_mode = 'warn'

    tee = KlogTee(
        files=args.files,
        append=args.append,
        ignore_interrupts=args.ignore_interrupts,
        output_error_mode=output_error_mode,
        flush_interval=args.flush_interval,
        null_terminated=args.null
    )
    tee.run()


if __name__ == '__main__':
    main()