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
"""Serve a RemoteBlockDevice over NBD so the Linux kernel can mount it.

Read-only unless --rw (writes go out as op05). Two modes, single-threaded (the serial link carries one
request at a time, so concurrency would only reorder the same queue):

  --attach /dev/nbdN   Kernel-direct (root). The device is attached with the
                       NBD ioctls over a socketpair -- no nbd-client, nbdkit or
                       qemu-nbd needed, only the `nbd` module (modprobe nbd).
                       Then: mount -t msdos [-o ro] /dev/nbdN /mnt/lnkclient

  --listen HOST:PORT   Standard NBD server (newstyle fixed handshake), for
                       nbd-client / qemu-nbd -c / qemu-img / nbdinfo.

At 9600 baud the link is ~1 KB/s, so the kernel's default read-ahead (128 KB)
and request size would turn a one-sector metadata read into minutes of wire
time and trip the NBD request timeout. --attach therefore sets
read_ahead_kb / max_sectors_kb small and raises the request timeout before
starting; set them yourself (blockdev --setra) in --listen mode.

--image FILE serves a local disk image instead of the serial link (testing).
"""
from __future__ import annotations

import argparse
import fcntl
import logging
import os
import signal
import socket
import struct
import sys
import threading
from typing import Optional, Protocol

log = logging.getLogger(__name__)

# -- protocol constants (linux/nbd.h, NBD protocol doc) ----------------------

NBDMAGIC = 0x4E42444D41474943           # "NBDMAGIC"
IHAVEOPT = 0x49484156454F5054           # "IHAVEOPT"
OPT_REPLY_MAGIC = 0x0003E889045565A9
REQUEST_MAGIC = 0x25609513
SIMPLE_REPLY_MAGIC = 0x67446698

FLAG_FIXED_NEWSTYLE = 1 << 0
FLAG_NO_ZEROES = 1 << 1
CFLAG_FIXED_NEWSTYLE = 1 << 0
CFLAG_NO_ZEROES = 1 << 1

FLAG_HAS_FLAGS = 1 << 0
FLAG_READ_ONLY = 1 << 1
FLAG_SEND_FLUSH = 1 << 2

OPT_EXPORT_NAME = 1
OPT_ABORT = 2
OPT_LIST = 3
OPT_INFO = 6
OPT_GO = 7

REP_ACK = 1
REP_SERVER = 2
REP_INFO = 3
REP_ERR_UNSUP = (1 << 31) | 1
REP_ERR_INVALID = (1 << 31) | 3

INFO_EXPORT = 0
INFO_BLOCK_SIZE = 3

CMD_READ = 0
CMD_WRITE = 1
CMD_DISC = 2
CMD_FLUSH = 3

EPERM = 1
EIO = 5
EINVAL = 22

# ioctls: _IO(0xab, n)
NBD_SET_SOCK = 0xAB00
NBD_SET_BLKSIZE = 0xAB01
NBD_DO_IT = 0xAB03
NBD_CLEAR_SOCK = 0xAB04
NBD_CLEAR_QUE = 0xAB05
NBD_SET_SIZE_BLOCKS = 0xAB07
NBD_DISCONNECT = 0xAB08
NBD_SET_TIMEOUT = 0xAB09
NBD_SET_FLAGS = 0xAB0A

# Per-request kernel timeout (s). Must exceed the slowest request at ~1 KB/s.
ATTACH_TIMEOUT = 600
ATTACH_READ_AHEAD_KB = 4
ATTACH_MAX_SECTORS_KB = 8
# Unwritten data Linux may hold for the device (its page cache). By default
# a cp to the mount returns once the data is in memory, minutes before the
# serial link has carried it, and umount then waits. A small per-device cap
# makes writers wait for the link instead: cp returns within about this much
# data (64 KB: ~15 s at the ~4.5 KB/s a DOSBox LNKSERVER takes) of the end.
# /sys/class/bdi/<dev>/max_bytes + strict_limit (Linux 6.2+); the old
# values are put back at detach. Older kernels: warning only.
ATTACH_DIRTY_BYTES = 64 * 1024


class BlockSource(Protocol):
    sector_size: int
    writable: bool

    @property
    def size(self) -> int: ...

    def read(self, offset: int, length: int) -> bytes: ...

    def write(self, offset: int, data: bytes) -> None: ...


class ImageFile:
    """A local disk image presented with the RemoteBlockDevice read surface."""

    def __init__(self, path: str, sector_size: int = 512,
                 writable: bool = False) -> None:
        self._f = open(path, "r+b" if writable else "rb")
        self.sector_size = sector_size
        self.writable = writable
        self._size = os.fstat(self._f.fileno()).st_size

    @property
    def size(self) -> int:
        return self._size

    def read(self, offset: int, length: int) -> bytes:
        self._f.seek(offset)
        data = self._f.read(length)
        return data + b"\x00" * (length - len(data))

    def write(self, offset: int, data: bytes) -> None:
        self._f.seek(offset)
        self._f.write(data)

    def close(self) -> None:
        self._f.close()


# -- socket helpers ----------------------------------------------------------

def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("peer closed the NBD connection")
        buf.extend(chunk)
    return bytes(buf)


# -- transmission phase (shared by both modes) -------------------------------

class _JobControl:
    """Make Ctrl-Z / bg / fg safe: a stop request waits until no NBD request
    is in progress and the link is idle (session lock held, so no keepalive
    is mid-byte), then stops the process; it carries on after SIGCONT."""

    def __init__(self, dev: BlockSource) -> None:
        session = getattr(dev, "session", None)
        self._lock = getattr(session, "lock", None)
        self.busy = False
        self._pending = False
        self._installed = False

    def install(self) -> None:
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGTSTP, self._on_tstp)
            self._installed = True

    def uninstall(self) -> None:
        if self._installed:
            signal.signal(signal.SIGTSTP, signal.SIG_DFL)
            self._installed = False

    def _on_tstp(self, signum, frame) -> None:
        if self.busy:
            self._pending = True
        else:
            self._stop()

    def _stop(self) -> None:
        self._pending = False
        if self._lock is not None:
            self._lock.acquire()
        try:
            log.info("NBD: stopping (link idle); resume with fg or bg")
            os.kill(os.getpid(), signal.SIGSTOP)
            log.info("NBD: resumed")
        finally:
            if self._lock is not None:
                self._lock.release()

    def request_done(self) -> None:
        self.busy = False
        if self._pending:
            self._stop()


def serve_transmission(sock: socket.socket, dev: BlockSource) -> None:
    """Answer kernel/client requests until NBD_CMD_DISC or EOF."""
    jc = _JobControl(dev)
    jc.install()
    try:
        while True:
            try:
                hdr = _recv_exact(sock, 28)
            except ConnectionError:
                log.info("NBD: connection closed")
                return
            jc.busy = True
            try:
                if not _handle_request(sock, dev, hdr):
                    return
            finally:
                jc.request_done()
    except (BrokenPipeError, ConnectionResetError) as e:
        log.error("NBD: peer closed the connection (%s); a request probably "
                  "took longer than the kernel/client timeout", e)
    finally:
        jc.uninstall()


def _reply(sock: socket.socket, err: int, handle: int,
           data: bytes = b"") -> None:
    sock.sendall(struct.pack(">IIQ", SIMPLE_REPLY_MAGIC, err, handle) + data)


def _handle_request(sock: socket.socket, dev: BlockSource,
                    hdr: bytes) -> bool:
    """Handle one request. False ends the transmission phase."""
    size = dev.size
    magic, _flags, cmd, handle, offset, length = struct.unpack(">IHHQQI", hdr)
    if magic != REQUEST_MAGIC:
        log.error("NBD: bad request magic 0x%08X; dropping connection", magic)
        return False
    if cmd == CMD_DISC:
        log.info("NBD: disconnect requested")
        return False
    if cmd == CMD_WRITE:
        if length > size:
            _reply(sock, EINVAL, handle)
            return True
        payload = _recv_exact(sock, length)
        if not dev.writable:
            err = EPERM
        elif offset + length > size:
            err = EINVAL
        else:
            try:
                dev.write(offset, payload)
                err = 0
            except Exception:
                log.exception("NBD: write(%d, %d) failed", offset, length)
                err = EIO
        _reply(sock, err, handle)
        return True
    if cmd == CMD_FLUSH:
        _reply(sock, 0, handle)
        return True
    if cmd != CMD_READ or offset + length > size:
        _reply(sock, EINVAL, handle)
        return True
    try:
        data = dev.read(offset, length)
    except Exception:
        log.exception("NBD: read(%d, %d) failed", offset, length)
        _reply(sock, EIO, handle)
        return True
    log.debug("NBD: read off=%d len=%d", offset, length)
    _reply(sock, 0, handle, data)
    return True


# -- newstyle handshake (--listen mode) --------------------------------------

def _opt_reply(sock: socket.socket, opt: int, rtype: int,
               data: bytes = b"") -> None:
    sock.sendall(struct.pack(">QIII", OPT_REPLY_MAGIC, opt, rtype, len(data))
                 + data)


def _tflags(dev: BlockSource) -> int:
    f = FLAG_HAS_FLAGS | FLAG_SEND_FLUSH
    return f if dev.writable else f | FLAG_READ_ONLY


def negotiate(sock: socket.socket, dev: BlockSource) -> bool:
    """Run the fixed-newstyle handshake. True -> enter transmission."""
    tflags = _tflags(dev)
    sock.sendall(struct.pack(">QQH", NBDMAGIC, IHAVEOPT,
                             FLAG_FIXED_NEWSTYLE | FLAG_NO_ZEROES))
    cflags = struct.unpack(">I", _recv_exact(sock, 4))[0]
    no_zeroes = bool(cflags & CFLAG_NO_ZEROES)
    while True:
        magic, opt, length = struct.unpack(">QII", _recv_exact(sock, 16))
        if magic != IHAVEOPT:
            log.error("NBD: bad option magic")
            return False
        if length:
            if length > 65536:
                log.error("NBD: option payload too large (%d bytes)", length)
                return False
            _recv_exact(sock, length)   # export name / info requests: one export
        if opt == OPT_EXPORT_NAME:
            reply = struct.pack(">QH", dev.size, tflags)
            if not no_zeroes:
                reply += b"\x00" * 124
            sock.sendall(reply)
            return True
        if opt == OPT_ABORT:
            _opt_reply(sock, opt, REP_ACK)
            return False
        if opt == OPT_LIST:
            _opt_reply(sock, opt, REP_SERVER, struct.pack(">I", 0))
            _opt_reply(sock, opt, REP_ACK)
            continue
        if opt in (OPT_INFO, OPT_GO):
            # Any export name is accepted: there is exactly one device.
            _opt_reply(sock, opt, REP_INFO,
                       struct.pack(">HQH", INFO_EXPORT, dev.size, tflags))
            ss = dev.sector_size
            _opt_reply(sock, opt, REP_INFO,
                       struct.pack(">HIII", INFO_BLOCK_SIZE, ss, ss, 64 * 1024))
            _opt_reply(sock, opt, REP_ACK)
            if opt == OPT_GO:
                return True
            continue
        # STRUCTURED_REPLY, SET_META_CONTEXT, STARTTLS, ...: not offered.
        _opt_reply(sock, opt, REP_ERR_UNSUP)


def serve_listen(dev: BlockSource, host: str, port: int,
                 once: bool = False) -> None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(1)
    log.info("NBD: listening on %s:%d (%d bytes, %s)", host, port, dev.size,
             "read-write" if dev.writable else "read-only")
    try:
        while True:
            conn, peer = srv.accept()
            log.info("NBD: client %s:%d", *peer[:2])
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            with conn:
                try:
                    if negotiate(conn, dev):
                        serve_transmission(conn, dev)
                except ConnectionError as e:
                    log.info("NBD: %s", e)
            if once:
                return
    finally:
        srv.close()


# -- kernel-direct attach (--attach mode) ------------------------------------

def _sysfs_set(nbd_path: str, attr: str, value: int) -> None:
    name = os.path.basename(os.path.realpath(nbd_path))
    path = f"/sys/block/{name}/queue/{attr}"
    try:
        with open(path, "w") as f:
            f.write(str(value))
    except OSError as e:
        log.warning("could not set %s=%d: %s", path, value, e)


def _bdi_limit(nbd_path: str) -> dict:
    """Cap the device's unwritten page-cache data (ATTACH_DIRTY_BYTES,
    Linux 6.2+); returns the previous values, for _bdi_restore."""
    st = os.stat(nbd_path)
    bdi = f"/sys/class/bdi/{os.major(st.st_rdev)}:{os.minor(st.st_rdev)}"
    old = {}
    if os.path.exists(f"{bdi}/max_bytes"):
        want = {"max_bytes": ATTACH_DIRTY_BYTES, "strict_limit": 1}
    else:
        want = {}
    for attr, value in want.items():
        path = f"{bdi}/{attr}"
        try:
            with open(path) as f:
                old[path] = f.read().strip()
            with open(path, "w") as f:
                f.write(str(value))
        except OSError as e:
            log.debug("could not set %s=%s: %s", path, value, e.strerror)
    if not any(p.endswith("/strict_limit") for p in old):
        # Linux before 6.2 has no per-device strict limit: the data a copy
        # leaves in memory is written when the kernel's own timer expires
        # (~30 s) or on sync (live 2026-10-08, kernel 5.15: a 50 s hold).
        log.warning(
            "this kernel (%s) cannot limit the unwritten data held for %s "
            "(needs Linux 6.2): a cp to the mount returns before the link "
            "has written it, and data can wait ~30 s before it starts. "
            "Mount with -o flush to start writing as each file is closed; "
            "umount always waits for everything.",
            os.uname().release, nbd_path)
    return old


def _bdi_restore(old: dict) -> None:
    for path, value in old.items():
        try:
            with open(path, "w") as f:
                f.write(value)
        except OSError:
            pass


def serve_attach(dev: BlockSource, nbd_path: str) -> None:
    """Attach `dev` to /dev/nbdN via ioctls and serve it until detached.

    A forked child sits in NBD_DO_IT (which blocks for the life of the
    device); this process answers the kernel's requests on the other end of a
    socketpair. Detach with `umount` then `kill -INT <pid>` (or Ctrl-C), which
    sends NBD_DISCONNECT.
    """
    ss = dev.sector_size
    if dev.size % ss:
        raise ValueError(f"device size {dev.size} not a multiple of {ss}")
    fd = os.open(nbd_path, os.O_RDWR)
    kern, ours = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        try:  # drop any stale socket from a previous attach
            fcntl.ioctl(fd, NBD_CLEAR_SOCK)
        except OSError:
            pass
        fcntl.ioctl(fd, NBD_SET_BLKSIZE, ss)
        fcntl.ioctl(fd, NBD_SET_SIZE_BLOCKS, dev.size // ss)
        fcntl.ioctl(fd, NBD_SET_TIMEOUT, ATTACH_TIMEOUT)
        fcntl.ioctl(fd, NBD_SET_FLAGS, _tflags(dev))
        _sysfs_set(nbd_path, "read_ahead_kb", ATTACH_READ_AHEAD_KB)
        _sysfs_set(nbd_path, "max_sectors_kb", ATTACH_MAX_SECTORS_KB)
        bdi_old = _bdi_limit(nbd_path) if dev.writable else {}
        fcntl.ioctl(fd, NBD_SET_SOCK, kern.fileno())
    except OSError:
        os.close(fd)
        raise

    pid = os.fork()
    if pid == 0:  # child: hand the socket to the kernel and block
        ours.close()
        # Any signal delivered while in NBD_DO_IT makes the kernel tear the
        # device down, so keep terminal job control (Ctrl-C/Ctrl-Z/bg/fg)
        # away from this process: own session, those signals blocked.
        os.setsid()
        signal.pthread_sigmask(signal.SIG_BLOCK, {
            signal.SIGINT, signal.SIGQUIT, signal.SIGTSTP, signal.SIGTTIN,
            signal.SIGTTOU, signal.SIGCONT, signal.SIGHUP, signal.SIGWINCH})
        rc = 0
        try:
            fcntl.ioctl(fd, NBD_DO_IT)
        except OSError as e:
            # NBD_DO_IT returns an error on a normal disconnect too.
            log.debug("NBD_DO_IT returned: %s", e)
        except BaseException:
            rc = 1
        finally:
            for req in (NBD_CLEAR_QUE, NBD_CLEAR_SOCK):
                try:
                    fcntl.ioctl(fd, req)
                except OSError:
                    pass
        os._exit(rc)

    kern.close()
    log.info("NBD: %s attached (%d sectors x %d B, %s); "
             "mount -t msdos %s <dir>", nbd_path, dev.size // ss, ss,
             "read-write" if dev.writable else "read-only", nbd_path)
    try:
        serve_transmission(ours, dev)
    except KeyboardInterrupt:
        log.info("NBD: interrupted; disconnecting %s", nbd_path)
    finally:
        try:
            fcntl.ioctl(fd, NBD_DISCONNECT)
        except OSError:
            pass
        ours.close()
        os.waitpid(pid, 0)
        os.close(fd)
        _bdi_restore(bdi_old)


# -- CLI ---------------------------------------------------------------------

def main(argv=None) -> int:
    """pyterlink-nbd: any failure is one line, never a traceback (errors.py)."""
    from .errors import run
    return run("pyterlink-nbd", _main, argv)


def _main(argv: Optional[list] = None) -> int:
    p = argparse.ArgumentParser(
        prog="pyterlink-nbd",
        description="Export an LNKSERVER drive as an NBD block device "
                    "(mount it with -t msdos).")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--serial", metavar="PORT",
                     help="serial port/URL connected to LNKSERVER "
                          "(e.g. /dev/ttyUSB0)")
    src.add_argument("--image", metavar="FILE",
                     help="serve a local disk image instead (testing)")
    p.add_argument("--baud", type=int, default=9600)
    p.add_argument("--unit", type=int, default=None,
                   help="server unit to export (default: first mapped)")
    p.add_argument("--rw", action="store_true",
                   help="allow writes (op05); default read-only")
    p.add_argument("--no-keepalive", action="store_true",
                   help="do not send LNKCLIENT's idle keepalive bytes")
    dst = p.add_mutually_exclusive_group(required=True)
    dst.add_argument("--attach", metavar="/dev/nbdN",
                     help="attach directly to a kernel NBD device (root)")
    dst.add_argument("--listen", metavar="HOST:PORT",
                     help="run an NBD server (e.g. 127.0.0.1:10809)")
    p.add_argument("--once", action="store_true",
                   help="--listen: exit after the first client disconnects")
    p.add_argument("-v", "--verbose", action="count", default=0)
    from . import __version__
    p.add_argument("--version", action="version",
                   version=f"pyterlink-nbd {__version__}")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose > 1 else
        logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    from .errors import quiet_logging
    quiet_logging()
    if not args.verbose:
        log.setLevel(logging.INFO)
    from . import command_line
    log.info("pyterlink-nbd %s; command: %s", __version__, command_line())

    if args.image:
        dev = ImageFile(args.image, writable=args.rw)
    else:
        from .block import RemoteBlockDevice
        dev = RemoteBlockDevice.open_serial(
            args.serial, args.baud, unit=args.unit, writable=args.rw,
            keepalive=not args.no_keepalive)
        log.info("LNKSERVER unit: label=%r serial=%08X, %d bytes",
                 dev.volume_label, dev.serial, dev.size)
    try:
        if args.attach:
            serve_attach(dev, args.attach)
        else:
            host, _, port = args.listen.rpartition(":")
            serve_listen(dev, host or "127.0.0.1", int(port), once=args.once)
    except KeyboardInterrupt:
        pass
    finally:
        dev.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
