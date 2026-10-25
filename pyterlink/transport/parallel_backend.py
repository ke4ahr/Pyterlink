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
"""Parallel (LPT) transport -- stub (D-047: deferred).

LNKCLIENT/LNKSERVER also run over a parallel cable. No parallel captures exist
yet, so its byte link is not implemented; the frame protocol above it
(protocol/session.py, protocol/server.py) is transport-independent and needs
only an object with tx_byte / rx_byte(timeout) / flush.
"""


class ParallelLinkByteIO:
    def __init__(self, device: str) -> None:
        raise NotImplementedError(
            "parallel-port link not implemented (no parallel captures yet)")
