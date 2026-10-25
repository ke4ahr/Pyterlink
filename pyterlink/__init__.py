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
"""pyterlink -- MS-DOS 6.22 LNKCLIENT / LNKSERVER for Linux.

LNKCLIENT redirects whole server drives at the sector level, so the client
exports a server drive as a Linux block device (mounted -t msdos) and the
server serves disk images and Linux directories to a real LNKCLIENT as FAT
drives.

Subpackages / modules:
  pyterlink.protocol  -- framing, client session, server session
  pyterlink.transport -- serial byte link, wire trace (parallel: stub)
  pyterlink.block     -- cached sector device over a client session
  pyterlink.nbd       -- NBD export (kernel attach or NBD server)
  pyterlink.errors    -- one-line error reporting for the programs
  pyterlink.client    -- pyterlink-client CLI
  pyterlink.server    -- pyterlink-server CLI, runtime, status UI, drives
"""
# The single source of the version (pyproject.toml reads it). 1.0.1 is the
# first numbered release; the patchlevel goes up with each pushed change to
# the code (D-084). History: CHANGELOG.md.
__version__ = "1.1.0"


def command_line() -> str:
    """The command that started this program, as typed (for logs and
    trace headers): python3 -m ... or the installed script."""
    import shlex
    import sys
    return shlex.join(getattr(sys, "orig_argv", None) or sys.argv)
