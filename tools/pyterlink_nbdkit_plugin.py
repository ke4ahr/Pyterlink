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
"""nbdkit python plugin: an LNKSERVER drive as an NBD export.

  LNKCLIENT_PORT=/dev/ttyUSB1 LNKCLIENT_BAUD=19200 \
      nbdkit python tools/pyterlink_nbdkit_plugin.py [unit=N] [rw=1]
  nbd-client localhost /dev/nbd0 && mount -t msdos /dev/nbd0 /mnt/lnkclient

Runs LNKCLIENT's establishment once when nbdkit loads the plugin
(pyterlink.block.RemoteBlockDevice). The serial link carries one request at
a time, so the plugin uses nbdkit's serialize-all-requests thread model.
nbdkit's default -r is not implied: without rw=1 the export is read-only.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import nbdkit

from pyterlink.block import RemoteBlockDevice

API_VERSION = 2

_cfg = {"unit": None, "rw": False}
_dev = None


def thread_model():
    return nbdkit.THREAD_MODEL_SERIALIZE_ALL_REQUESTS


def config(key, value):
    if key == "unit":
        _cfg["unit"] = int(value)
    elif key == "rw":
        _cfg["rw"] = value not in ("0", "false", "no")
    else:
        raise RuntimeError(f"unknown parameter {key}")


def get_ready():
    global _dev
    _dev = RemoteBlockDevice.open_serial(
        os.environ.get("LNKCLIENT_PORT", "/dev/ttyUSB0"),
        int(os.environ.get("LNKCLIENT_BAUD", "9600")),
        unit=_cfg["unit"], writable=_cfg["rw"])


def unload():
    if _dev is not None:
        _dev.close()


def open(readonly):
    return {}


def get_size(h):
    return _dev.size


def can_write(h):
    return _dev.writable


def pread(h, buf, offset, flags):
    buf[:] = _dev.read(offset, len(buf))


def pwrite(h, buf, offset, flags):
    _dev.write(offset, bytes(buf))
