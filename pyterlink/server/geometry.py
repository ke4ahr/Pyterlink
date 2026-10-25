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
"""Disk geometry (CHS) for served and generated drives.

The LNKCLIENT wire carries volume-relative sectors only; geometry appears in
the BPB a drive reports (sectors/track, heads) and matters to whoever later
uses the image as a disk (DOSBox imgmount, a BIOS). Two sources:

  * the user's own, written as DOSBox's imgmount -size takes it:
    "bytes_per_sector,sectors_per_track,heads,cylinders" (e.g. 512,63,16,100);
  * the default: 63 sectors x 16 heads while the disk fits in 1024
    cylinders (the classic BIOS CHS disk, <= 504 MiB, what DOSBox uses),
    else 63 x 255 (the LBA-translated geometry of larger disks).
"""
from __future__ import annotations

from dataclasses import dataclass

CHS_MAX_CYLINDERS = 1024


@dataclass(frozen=True)
class Geometry:
    bytes_per_sector: int
    sectors_per_track: int
    heads: int
    cylinders: int

    @property
    def total_sectors(self) -> int:
        return self.sectors_per_track * self.heads * self.cylinders

    def __str__(self) -> str:
        return (f"{self.bytes_per_sector},{self.sectors_per_track},"
                f"{self.heads},{self.cylinders}")


def parse_chs(text: str) -> Geometry:
    """"512,63,16,100" (imgmount -size order) -> Geometry."""
    try:
        bps, spt, heads, cyl = (int(x) for x in text.split(","))
    except ValueError:
        raise ValueError(f"geometry {text!r}: expected bytes,sectors,heads,"
                         f"cylinders (e.g. 512,63,16,100)")
    if bps != 512 or not 1 <= spt <= 63 or not 1 <= heads <= 255 \
            or cyl < 1:
        raise ValueError(f"geometry {text!r}: need 512-byte sectors, 1-63 "
                         f"sectors/track, 1-255 heads")
    return Geometry(bps, spt, heads, cyl)


def default_geometry(total_sectors: int) -> Geometry:
    """16 heads while it fits in 1024 cylinders, else 255 (LBA)."""
    for heads in (16, 255):
        cyl = -(-total_sectors // (63 * heads))
        if cyl <= CHS_MAX_CYLINDERS or heads == 255:
            return Geometry(512, 63, heads, cyl)
    raise AssertionError
