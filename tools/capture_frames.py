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
"""Extract CRC-validated LNKCLIENT frames from com3 proxy captures (.log/.gz).

Each direction's byte stream is scanned for  tag | count(1|2) | data | crc(2)
where tag&0x3c == 0 (bit 6 marks a data chunk with more to follow; bit 2,
retransmission, is not decoded), count is 8-bit if tag&0x80 else 16-bit
LE, and CRC-16 (framing._crc16_prng, init 0xffff) over count+data+crc leaves
residue 0x1d0f. Bytes between frames (echoes, acks, polls, post-ack) are
skipped. Output: one line per frame, time-ordered across both directions.

Usage: tools/capture_frames.py logs/com3_traffic_*.log.gz [--session N]
       [--opcodes]  (summary of request opcodes and their reply shapes)
"""
from __future__ import annotations

import argparse
import collections
import gzip
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pyterlink.protocol.framing import _crc16_prng, CRC_RESIDUE  # noqa: E402

LINE = re.compile(r"^\[([^\]]+)\] \[([A-Z>\-]+)\] ([0-9a-f]+)")
C2S, S2C = "LNKCLIENT->LNKSERVER", "LNKSERVER->LNKCLIENT"
MAX_COUNT = 0x4400


def sessions(path):
    """Yield per-proxy-session lists of (ts, dir, bytes)."""
    op = gzip.open if path.endswith(".gz") else open
    cur = []
    with op(path, "rt", errors="replace") as f:
        for line in f:
            if line.startswith("=== "):
                if cur:
                    yield cur
                cur = []
                continue
            m = LINE.match(line)
            if m and m.group(2) in (C2S, S2C):
                cur.append((m.group(1), m.group(2), bytes.fromhex(m.group(3))))
    if cur:
        yield cur


def frames_in(stream):
    """stream: list of (byte, ts, global_index). Return list of frames."""
    out = []
    data = bytes(b for b, _, _ in stream)
    n = len(data)
    i = 0
    while i < n:
        tag = data[i]
        if tag & 0x3C:
            i += 1
            continue
        if tag & 0x80:
            hdr = 1
            if i + 1 >= n:
                break
            count = data[i + 1]
        else:
            hdr = 2
            if i + 2 >= n:
                break
            count = data[i + 1] | (data[i + 2] << 8)
        end = i + 1 + hdr + count + 2
        if count == 0 or count > MAX_COUNT or end > n:
            i += 1
            continue
        if _crc16_prng(data[i + 1:end], 0xFFFF) == CRC_RESIDUE:
            out.append({"tag": tag, "data": data[i + 1 + hdr:end - 2],
                        "ts": stream[i][1], "gi": stream[i][2]})
            i = end
        else:
            i += 1
    return out


def decode_session(entries):
    streams = {C2S: [], S2C: []}
    gi = 0
    for ts, d, bs in entries:
        for b in bs:
            streams[d].append((b, ts, gi))
            gi += 1
    frames = []
    for d, s in streams.items():
        for fr in frames_in(s):
            fr["dir"] = "C>S" if d == C2S else "S>C"
            frames.append(fr)
    frames.sort(key=lambda f: f["gi"])
    return frames


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("logs", nargs="+")
    ap.add_argument("--session", type=int)
    ap.add_argument("--opcodes", action="store_true")
    ap.add_argument("--max", type=int, default=64,
                    help="max data bytes of hex shown per frame")
    a = ap.parse_args()
    stats = collections.Counter()
    shapes = collections.defaultdict(collections.Counter)
    for path in a.logs:
        for sn, ent in enumerate(sessions(path)):
            if a.session is not None and sn != a.session:
                continue
            frames = decode_session(ent)
            if not a.opcodes:
                print(f"### {path} session {sn}: {len(frames)} frames")
            last_req = None
            for fr in frames:
                d = fr["data"]
                if a.opcodes:
                    if fr["dir"] == "C>S":
                        last_req = d[:2].hex()
                        stats[last_req] += 1
                    elif last_req:
                        shapes[last_req][(len(d), d[:2].hex())] += 1
                    continue
                h = d[:a.max].hex()
                print(f"{fr['ts']} {fr['dir']} tag={fr['tag']:02x} "
                      f"len={len(d):4d} {h}{'..' if len(d) > a.max else ''}")
    if a.opcodes:
        for k, v in sorted(stats.items()):
            top = ", ".join(f"len{l}/{p}x{c}" for (l, p), c in
                            shapes[k].most_common(4))
            print(f"req {k}: {v:6d}  replies: {top}")


if __name__ == "__main__":
    main()
