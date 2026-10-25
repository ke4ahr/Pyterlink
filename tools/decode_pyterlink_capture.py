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
"""Decode an LNKCLIENT capture into a list of requests and replies.

Reads capture files in the format pyterlink's -trace writes (plain or
.gz; the same format as the com3 proxy logs). Frames are found and
CRC-checked by tools/capture_frames.py. Each request from the LNKCLIENT
side and each reply from the LNKSERVER side is printed on one line, with
its name and fields:

  12:00:01.250 C>S #04 read        unit 2  lba 1234  count 16
  12:00:01.900 S>C     reply       status 0x0100 done  count 16  data 8192 bytes

Sector data that follows a read reply or a write request arrives as
separate chunk frames; they are added up into the "data" figure of the
request or reply they belong to.

Usage:
  tools/decode_pyterlink_capture.py capture.log[.gz] ... [--session N]
                                    [--json] [--hex]

  --session N  decode only proxy session N (counted from 0)
  --json       one JSON object per line instead of text
  --hex        add the first 32 bytes of each frame in hex
"""
from __future__ import annotations

import argparse
import json
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from capture_frames import decode_session, sessions  # noqa: E402

OP_NAMES = {
    0x00: "install",
    0x01: "drive map",
    0x02: "media check",
    0x03: "build BPB",
    0x04: "read",
    0x05: "write",
    0x06: "write+verify",
    0x1C: "drive info",
    0x1D: "IOCTL",
}

# DOS device driver status word: bit 15 = error (low byte = error code),
# bit 9 = busy, bit 8 = done.
DOS_ERRORS = {
    0x00: "write protected", 0x01: "unknown unit", 0x02: "drive not ready",
    0x03: "unknown command", 0x04: "CRC error", 0x05: "bad request length",
    0x06: "seek error", 0x07: "unknown media", 0x08: "sector not found",
    0x09: "printer out of paper", 0x0A: "write fault", 0x0B: "read fault",
    0x0C: "general failure", 0x0F: "invalid disk change",
}


def status_name(status: int) -> str:
    """Spell out a DOS device driver status word."""
    if status & 0x8000:
        code = status & 0xFF
        return "error: " + DOS_ERRORS.get(code, f"code {code:02x}")
    words = []
    if status & 0x0100:
        words.append("done")
    if status & 0x0200:
        words.append("busy")
    if status & 0xFF:
        words.append(f"low byte {status & 0xFF:02x}")
    return ", ".join(words) or "no flags"

MORE_FOLLOWS = 0x40     # tag bit: another data chunk follows this one
SECTOR_OPS = (0x04, 0x05, 0x06)


def describe_request(data: bytes) -> dict:
    """Name a request frame and pull out its fields."""
    opcode = data[0]
    info = {"kind": "request", "op": opcode,
            "name": OP_NAMES.get(opcode, f"op {opcode:02x}")}
    if len(data) >= 2:
        info["unit"] = data[1]
    if opcode in SECTOR_OPS and len(data) >= 11:
        info["count"] = struct.unpack_from("<H", data, 3)[0]
        info["lba"] = struct.unpack_from("<I", data, 7)[0]
    if opcode == 0x1D and len(data) >= 4:
        info["ioctl"] = f"{data[2]:02x}/{data[3]:02x}"
    return info


def printable(data: bytes) -> str:
    return bytes(byte for byte in data if 32 <= byte < 127).decode("ascii").strip()


def describe_reply(data: bytes, request_op, first: bool) -> dict:
    """Read the status word and the fields that follow it in a reply.
    Only the first reply frame to a request starts with a status word; an
    IOCTL Get Media ID sends a second frame with the serial, label and
    file system type."""
    info = {"kind": "reply"}
    if not first:
        if request_op == 0x1D:
            info["media_id"] = printable(data[6:])
        return info
    if request_op in (0x02, 0x03, 0x04, 0x05, 0x06, 0x1D) and len(data) >= 2:
        status = struct.unpack_from("<H", data, 0)[0]
        info["status"] = status
        info["status_name"] = status_name(status)
        if request_op == 0x02 and len(data) >= 3:
            info["changed"] = data[2] == 0xFF
        if request_op in SECTOR_OPS and len(data) >= 4:
            info["count"] = struct.unpack_from("<H", data, 2)[0]
    if request_op == 0x1C:
        info["text"] = printable(data)
    return info


def decode(frames) -> list:
    """Turn a time-ordered frame list into request and reply records."""
    records = []
    open_record = {"C>S": None, "S>C": None}
    last_request_op = None
    replies_seen = 0
    for frame in frames:
        data = frame["data"]
        direction = frame["dir"]
        chunk_owner = open_record[direction]
        if chunk_owner is not None:
            chunk_owner["data_bytes"] += len(data)
            if not frame["tag"] & MORE_FOLLOWS:
                open_record[direction] = None
            continue
        if direction == "C>S":
            record = describe_request(data)
            last_request_op = record["op"]
            replies_seen = 0
        else:
            record = describe_reply(data, last_request_op, replies_seen == 0)
            replies_seen += 1
        record.update(time=frame["ts"], dir=direction, tag=frame["tag"],
                      length=len(data), data_bytes=0, raw=data[:32].hex())
        records.append(record)
        sends_data = ((direction == "C>S" and record.get("op") in (0x05, 0x06))
                      or (direction == "S>C" and last_request_op == 0x04
                          and record.get("status") == 0x0100))
        if sends_data and record.get("count"):
            open_record[direction] = record
    return records


def as_text(record: dict, show_hex: bool) -> str:
    if record["kind"] == "request":
        parts = [f"#{record['op']:02x} {record['name']:<12}"]
        if "unit" in record:
            parts.append(f"unit {record['unit']}")
        if "lba" in record:
            parts.append(f"lba {record['lba']}  count {record['count']}")
        if "ioctl" in record:
            parts.append(f"ioctl {record['ioctl']}")
    else:
        parts = ["    reply       "]
        if "status" in record:
            parts.append(f"status 0x{record['status']:04x} "
                         f"{record['status_name']}")
        if "changed" in record:
            parts.append("media changed" if record["changed"]
                         else "media not changed")
        if "count" in record:
            parts.append(f"count {record['count']}")
        if "text" in record:
            parts.append(repr(record["text"]))
        if "media_id" in record:
            parts.append(f"media ID {record['media_id']!r}")
        if len(parts) == 1:
            parts.append(f"{record['length']} bytes")
    if record["data_bytes"]:
        parts.append(f"data {record['data_bytes']} bytes")
    line = f"{record['time']} {record['dir']} " + "  ".join(parts)
    if show_hex:
        line += f"  [{record['raw']}]"
    return line


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Decode LNKCLIENT captures into requests and replies.")
    parser.add_argument("files", nargs="+", help="capture files (.log or .log.gz)")
    parser.add_argument("--session", type=int,
                        help="decode only this proxy session (from 0)")
    parser.add_argument("--json", action="store_true",
                        help="one JSON object per line")
    parser.add_argument("--hex", action="store_true",
                        help="add the first 32 bytes of each frame in hex")
    args = parser.parse_args()
    for path in args.files:
        if not os.path.exists(path):
            print(f"decode_pyterlink_capture: {path}: no such file",
                  file=sys.stderr)
            return 1
        for number, entries in enumerate(sessions(path)):
            if args.session is not None and number != args.session:
                continue
            records = decode(decode_session(entries))
            if not args.json:
                print(f"### {path} session {number}: {len(records)} "
                      f"requests and replies")
            for record in records:
                if args.json:
                    record = dict(record, file=path, session=number)
                    print(json.dumps(record))
                else:
                    print(as_text(record, args.hex))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        # the reader (head, grep -m) stopped early: exit quietly
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        sys.exit(0)
