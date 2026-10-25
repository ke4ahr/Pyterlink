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
"""pyterlink-client: an LNKCLIENT for a real LNKSERVER server.

  pyterlink-client [-com n | -port device] [-baud rate] [-v]
                   [-unit n] [-rw] [-write n] [-nokeepalive] [-trace file]
                   (-attach /dev/nbdN | -listen host:port | -status)

Runs LNKCLIENT's establishment (handshake, install, drive map, drive info,
media check, build BPB) and exports the server drive as a block device for
Linux to mount with -t msdos (8.3 names, as LNKCLIENT; D-053):

  -attach /dev/nbdN    attach to the kernel NBD device (root; modprobe nbd),
                       then: mount -t msdos /dev/nbdN /mnt/lnkclient
  -listen host:port    run an NBD server for nbd-client / nbdkit / qemu
  -status              print the drive map LNKSERVER offers and exit

-com n is /dev/ttyS(n-1) (default -com 1); -port names any serial device.
-baud is the rate to step the line up to after the 9600 handshake, as
LNKCLIENT's /BAUD (9600, 19200, 38400, 57600 or 115200; default 115200; a
step the line cannot carry is retried lower). Above 9600 the keepalive
must run: LNKSERVER drops back to 9600 after 2 s of silence, so
-nokeepalive needs -baud 9600. -unit picks the server unit (A: = 0;
default: the first one mapped). Read-only unless -rw (writes go out as
LNKCLIENT's op05). Write requests start at 127 sectors, as LNKCLIENT's do;
one that is lost is sent again at half the size, which stays the limit
for the run. -write caps the size (1-127). -v logs progress every 5 s.
-trace appends every wire byte to the named file in the com3 capture
format, for tools/decode_pyterlink_capture.py and tools/capture_frames.py.

-attach needs root (the kernel takes NBD setup only from root); -status
and -listen run as any user in the serial port's group (dialout).
Options take their value as the next word or after '=' (-port /dev/ttyUSB1
or -port=/dev/ttyUSB1). -h shows this help; -version the version.
"""

import logging
import sys
from typing import List, Optional

from ..transport.serial_backend import (
    DEFAULT_BAUD, VALID_BAUD_RATES, com_url)

# Flags that take a value token when given in bare -flag form (no = or :).
_VALUE_FLAGS = {'COM', 'PORT', 'BAUD', 'UNIT', 'ATTACH', 'LISTEN', 'TRACE', 'WRITE'}


def _normalize_argv(argv: List[str]) -> List[str]:
    """Convert -flag value / -flag=value to /FLAG=value for the parser.

    --help / -h / /? pass through unchanged. Old /FLAG style is rejected.
    """
    out: List[str] = []
    idx = 0
    while idx < len(argv):
        arg = argv[idx]
        if arg in ('-h', '--help', '/?', '--version') or not arg.startswith('-') \
                or arg.startswith('--'):
            if arg.startswith('/') and arg != '/?':
                raise ValueError(
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


def main(argv=None) -> int:
    """pyterlink-client: any failure is one line, never a traceback (errors.py)."""
    from ..errors import run
    return run("pyterlink-client", _main, argv)


def _main(argv: Optional[List[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    from .. import __version__
    if not argv or any(a in ("-h", "--help", "/?") for a in argv):
        print(f"pyterlink-client {__version__}")
        print(__doc__)
        return 0
    if any(a.upper() in ("/VERSION", "--VERSION", "-VERSION") for a in argv):
        print(f"pyterlink-client {__version__}")
        return 0
    try:
        argv = _normalize_argv(argv)
    except ValueError as err:
        print(f"pyterlink-client: {err}", file=sys.stderr)
        return 2
    url, baud, verbose, unit, rw, ka = None, DEFAULT_BAUD, False, None, \
        False, True
    attach = listen = trace = None
    write_max = None
    status = False
    for a in argv:
        u = a.upper()
        try:
            if u.startswith("/COM="):
                url = com_url(int(a[5:]))
            elif u.startswith("/PORT="):
                url = a[6:]
            elif u.startswith("/BAUD="):
                baud = int(a[6:])
                if baud not in VALID_BAUD_RATES:
                    raise ValueError(f"rate not in {VALID_BAUD_RATES}")
            elif u.startswith("/UNIT="):
                unit = int(a[6:])
            elif u.startswith("/ATTACH="):
                attach = a[8:]
            elif u.startswith("/LISTEN="):
                listen = a[8:]
            elif u.startswith("/TRACE="):
                trace = a[7:]
            elif u == "/STATUS":
                status = True
            elif u == "/RW":
                rw = True
            elif u.startswith("/WRITE="):
                write_max = int(a[7:])
                if not 1 <= write_max <= 127:
                    raise ValueError("sectors per write: 1 to 127")
            elif u == "/NOKEEPALIVE":
                ka = False
            elif u == "/V":
                verbose = True
            elif u.startswith("/LPT"):
                raise ValueError("parallel transport not implemented (D-047)")
            else:
                raise ValueError("unknown flag -- use -flag value style")
        except ValueError as e:
            print(f"pyterlink-client: {a}: {e}", file=sys.stderr)
            return 2
    if url is None:
        url = com_url(1)
    if not ka and baud != 9600 and not status:
        print("pyterlink-client: -nokeepalive requires -baud 9600 (LNKSERVER "
              "leaves a faster rate after 2 s idle)", file=sys.stderr)
        return 2
    if sum(map(bool, (attach, listen, status))) != 1:
        print("pyterlink-client: give one of -attach, -listen, -status",
              file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.INFO if verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(message)s")
    from ..errors import quiet_logging
    quiet_logging()
    from .. import command_line
    logging.getLogger(__name__).info("pyterlink-client %s; command: %s",
                                     __version__, command_line())

    from ..block import RemoteBlockDevice, BlockDeviceError
    from ..protocol.session import DeviceError, LinkError
    from .. import nbd
    try:
        if status:
            return _status(url, baud, trace)
        dev = RemoteBlockDevice.open_serial(url, baud, trace=trace,
                                            unit=unit, writable=rw,
                                            keepalive=ka)
        if write_max:
            dev.write_max = write_max
    except LinkError as e:
        print(f"pyterlink-client: no LNKSERVER session on {url} ({e}) -- is "
              f"LNKSERVER running on the other end?", file=sys.stderr)
        return 1
    except (DeviceError, BlockDeviceError) as e:
        print(f"pyterlink-client: {e} -- is there a readable disk in that "
              f"server drive? (-status lists them; -unit n picks one)",
              file=sys.stderr)
        return 1
    try:
        if attach:
            nbd.serve_attach(dev, attach)
        else:
            host, _, port = listen.rpartition(":")
            nbd.serve_listen(dev, host or "127.0.0.1", int(port))
    except KeyboardInterrupt:
        pass
    finally:
        dev.close()
    return 0


def _status(url: str, baud: int, trace: Optional[str]) -> int:
    """LNKCLIENT's establishment up to the drive list (handshake, rate
    steps, install, drive map, drive info per mapped unit); no unit is
    opened, so an empty server drive does not stop it."""
    from ..protocol.session import LinkSession
    from ..transport.serial_backend import open_link
    link = open_link(url)
    if trace:
        from ..transport.trace import TraceIO
        link = TraceIO(link, trace, local="LNKCLIENT")
    try:
        s = LinkSession(link, max_baud=baud)
        rate = s.establish()
        s.install()
        for letter, u in s.drive_map().mapped().items():
            info = s.drive_info(u)
            print(f"{letter}: equals server {chr(65 + u)}: "
                  f"{info.size_text} {info.label}")
        print(f"line rate {rate} baud")
    finally:
        link.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
