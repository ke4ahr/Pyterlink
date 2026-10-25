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
"""pyterlink-server: an LNKSERVER for a real LNKCLIENT client.

  pyterlink-server drive:=image [drive:=image ...] [-x drive:[...]]
                   [-com n | -port device] [-baud rate] [-ro] [-free mb]
                   [-pace bytes] [-fat32] [-no-follow-symlinks] [-v]
                   [-trace file]

Each served drive is a disk image (or block device) holding a FAT volume:
a volume image (floppy, mkfs.msdos -C) or a partitioned hard-disk image
such as the DOSBox ones, whose first FAT partition is served. Do not serve
an image a running DOSBox (or a Linux mount) has open read-write: each side
caches the FAT and they overwrite each other -- serve a copy, or use -ro.
A partitioned image gives one drive per DOS volume, lettered as MS-DOS
6.22 would: C:=disk.img serves its primary DOS partition as C: and the
logical drives of its extended partition as D:, E:, ...; C:=disk.img#3
serves only its third volume; C:=disk.img@512,63,16,100 gives the disk's
geometry as DOSBox's imgmount -size does (checked against the image size;
generated drives use it in their boot sector; default 63 sectors x 16
heads, 255 heads past 1024 cylinders). FAT16 on LBA partition types (0E,
extended 0F) is served as is. FAT32 volumes go out as generated FAT16
drives (read-only), so the wire stays MS-DOS 6.22-compatible; -fat32 sends
them as they are, for an LNKCLIENT on MS-DOS 7.1 or later only.
A drive can also be a Linux directory (C:=/path/to/dir): it is served as a
generated FAT16 volume with VFAT-correct 8.3 short names plus long-name
entries for names that do not fit (DOS ignores those; Linux vfat and
Windows readers see the real names). Like an image it is writable unless
-ro: the client's changes are written back to the directory once it has
been idle for 2 s (new, changed, renamed, moved and deleted files and
directories; DOS dates; the read-only attribute). Files DOS creates or
renames get its 8.3 names. Changes made on the Linux side are picked up by
rescans (every second; -ro every 10 s; never on a read-only file system).
A file that is a symlink in the shared directory is written through the
link; -no-follow-symlinks replaces the link with a regular file instead,
so DOS cannot write outside the directory through a link placed in it.
A mounted CD-ROM or ISO image is a read-only Linux directory; mount it
first (e.g. `mount /dev/sr0 /mnt/cdrom` or
`mount -o loop,ro image.iso /mnt/cdrom`) then serve the mount point
(e.g. `D:=/mnt/cdrom`). The server detects the read-only filesystem and
forces the drive read-only regardless of -ro. Files larger than 2 GB on
the Linux side are not served to DOS (silently skipped during scanning).
-free sets the free space a writable directory drive offers (default 64 MB,
at most what the Linux file system has free; the volume stays under the
FAT16 limit of 2 GB): DOS reads the whole FAT the first time it needs the
free space, so a larger drive makes the first DIR slower (a 2 GB drive:
128 KB of FAT).
Sector data goes out at the full line rate; when LNKCLIENT fails to
acknowledge a burst (its side could not take the bytes that fast -- a
DOSBox LNKCLIENT at 20000 cycles drains about 5 KB/s), the rate is halved
for the rest of the run (down to 1000 bytes/s) and LNKCLIENT's retry gets
through. -pace starts at a cap of n bytes per second instead.
Drive letters are LNKSERVER's server drives (A: = unit 0, B: = unit 1, ...),
so `C:=disk.img` serves unit 2 exactly as `INTERSVR C:` serves C:; letters
below the highest served one are reported as empty drives. -x withholds
drives from LNKCLIENT's mapping. Drives are writable, as LNKSERVER's are,
unless -ro. -com n is /dev/ttyS(n-1); -port names any serial device.
-baud is the highest rate LNKCLIENT may step the line up to (9600, 19200,
38400, 57600 or 115200; default 115200); every session starts at 9600,
the handshake rate, and LNKSERVER's 2 s idle fallback returns it there.
-v shows the LNKSERVER-style status screen (Q or Esc quits). -trace
appends every wire byte to the named file in the com3 capture format (for
tools/decode_pyterlink_capture.py and tools/capture_frames.py).
The -lpt parallel transport is not implemented yet.

Options take their value as the next word or after '=' (-port /dev/ttyUSB0
or -port=/dev/ttyUSB0). The server needs no root: run it as a user in the
serial port's group (dialout) with access to the images and directories
it serves. -h shows this help; -version the version.
"""

import logging
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..transport.serial_backend import DEFAULT_BAUD, VALID_BAUD_RATES

# Flags that take a value token when given in bare -flag form (no = or :).
_VALUE_FLAGS = {'X', 'COM', 'PORT', 'BAUD', 'TRACE', 'PACE', 'FREE'}


class CliError(Exception):
    pass


def _normalize_argv(argv: List[str]) -> List[str]:
    """Convert -flag value / -flag=value to /FLAG=value for the parser.

    Drive specs (C:=image) and --help / -h / /? pass through unchanged.
    Old /FLAG style is rejected.
    """
    out: List[str] = []
    idx = 0
    while idx < len(argv):
        arg = argv[idx]
        if arg in ('-h', '--help', '/?', '--version') or not arg.startswith('-') \
                or arg.startswith('--'):
            if arg.startswith('/') and arg != '/?':
                raise CliError(
                    f"{arg!r}: old /FLAG style removed; use -flag value instead")
            out.append(arg)
            idx += 1
        else:
            body = arg[1:]
            if '=' in body:
                name, _, val = body.partition('=')
                out.append(f'/{name.upper()}={val}')
                idx += 1
            else:
                name_up = body.upper()
                if name_up in _VALUE_FLAGS and idx + 1 < len(argv):
                    out.append(f'/{name_up}={argv[idx + 1]}')
                    idx += 2
                else:
                    out.append(f'/{name_up}')
                    idx += 1
    return out


@dataclass
class ServerArgs:
    drives: Dict[int, str] = field(default_factory=dict)  # unit -> image
    excluded: List[int] = field(default_factory=list)
    com: Optional[int] = None
    port_url: Optional[str] = None
    baud: int = DEFAULT_BAUD
    read_only: bool = False
    verbose: bool = False
    trace: Optional[str] = None
    fat32: bool = False     # FAT32 volumes on the wire as is (DOS 7.1+ only)
    free_mb: int = 64       # free space of a read-write directory drive
    pace: int = 0           # starting cap, bytes/s, on sector data (0 = none)
    no_follow_symlinks: bool = False  # do not follow symlinks on write-back


def _unit(letter: str) -> int:
    d = letter.rstrip(":").upper()
    if len(d) != 1 or not "A" <= d <= "Z":
        raise CliError(f"bad drive letter {letter!r}")
    return ord(d) - ord("A")


def parse_args(argv: Optional[List[str]] = None) -> ServerArgs:
    argv = _normalize_argv(sys.argv[1:] if argv is None else argv)
    out = ServerArgs()
    for a in argv:
        u = a.upper()
        if u.startswith("/X="):
            out.excluded += [_unit(d) for d in a[3:].replace(":", " ").split()]
        elif u.startswith("/COM="):
            try:
                out.com = int(a[5:])
            except ValueError:
                raise CliError(f"{a}: COM port number expected")
        elif u.startswith("/PORT="):
            out.port_url = a[6:]
        elif u.startswith("/BAUD="):
            try:
                out.baud = int(a[6:])
            except ValueError:
                raise CliError(f"{a}: not numeric")
            if out.baud not in VALID_BAUD_RATES:
                raise CliError(f"{a}: rate not in {VALID_BAUD_RATES}")
        elif u.startswith("/LPT"):
            raise CliError("parallel transport not implemented (D-047)")
        elif u.startswith("/TRACE="):
            out.trace = a[7:]
        elif u == "/FAT32":
            out.fat32 = True
        elif u.startswith("/PACE="):
            try:
                out.pace = int(a[6:])
            except ValueError:
                raise CliError(f"{a}: bytes per second expected (0 = off)")
            if out.pace < 0:
                raise CliError(f"{a}: 0 (off) or more")
        elif u.startswith("/FREE="):
            try:
                out.free_mb = int(a[6:])
            except ValueError:
                raise CliError(f"{a}: megabytes expected")
            if out.free_mb < 1:
                raise CliError(f"{a}: at least 1 MB")
        elif u == "/NO-FOLLOW-SYMLINKS":
            out.no_follow_symlinks = True
        elif u == "/RO":
            out.read_only = True
        elif u == "/V":
            out.verbose = True
        elif a.startswith("/"):
            raise CliError(f"unknown flag {a!r} -- use -flag value style")
        elif "=" in a:
            letter, path = a.split("=", 1)
            out.drives[_unit(letter)] = path
        else:
            raise CliError(f"{a!r}: expected drive:=image")
    if not out.drives:
        raise CliError("no drives to serve (e.g. C:=disk.img)")
    return out


def main(argv=None) -> int:
    """pyterlink-server: any failure is one line, never a traceback (errors.py)."""
    from ..errors import run
    return run("pyterlink-server", _main, argv)


def _main(argv: Optional[List[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    from .. import __version__
    if any(a in ("-h", "--help", "/?") for a in argv):
        print(f"pyterlink-server {__version__}")
        print(__doc__)
        return 0
    if any(a.upper() in ("/VERSION", "--VERSION", "-VERSION") for a in argv):
        print(f"pyterlink-server {__version__}")
        return 0
    try:
        args = parse_args(argv)
    except CliError as e:
        print(f"pyterlink-server: {e}", file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    from ..errors import quiet_logging
    quiet_logging()
    from .. import command_line
    logging.getLogger(__name__).info("pyterlink-server %s; command: %s",
                                     __version__, command_line())
    from .runtime import ServerRuntime
    rt = ServerRuntime(args)
    try:
        rt._build()
    except (OSError, ValueError) as e:
        from ..errors import describe
        print(f"pyterlink-server: {describe(e)}", file=sys.stderr)
        return 2
    if args.verbose and sys.stdout.isatty():
        return rt.run_verbose_tty()
    return rt.run()


if __name__ == "__main__":
    sys.exit(main())
