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
"""Serial (COM) transport: the raw byte link LNKCLIENT/LNKSERVER run over.

8N1, no flow control. RTS and DTR stay de-asserted, as real LNKCLIENT and
LNKSERVER leave them (D-022: pyserial would otherwise raise both on open).
The link handshake and frame layer above this are in protocol/session.py
(client) and protocol/server.py (server).
"""

import logging
import time
from typing import Optional

import serial

log = logging.getLogger(__name__)

DEFAULT_BAUD = 115200
PACE_SLICE = 512           # bytes per write when pacing a long burst
# Throughput a DOSBox LNKCLIENT/LNKSERVER was seen to sustain at 115200 (s57).
DOSBOX_SAFE_RATE = 4500.0
# The /BAUD rates LNKCLIENT and LNKSERVER accept.
VALID_BAUD_RATES = (9600, 19200, 38400, 57600, 115200)
VALID_COM_NUMBERS = (1, 2, 3, 4)


class SerialError(Exception):
    """Serial transport error."""


def com_url(com_number: int) -> str:
    """Device path for COM1..COM4 (/dev/ttyS0..3)."""
    if com_number not in VALID_COM_NUMBERS:
        raise ValueError(f"COM number {com_number} not in 1..4")
    return f"/dev/ttyS{com_number - 1}"


def validate_baud(baud: int) -> int:
    if baud not in VALID_BAUD_RATES:
        raise ValueError(f"baud {baud} not in valid rates {VALID_BAUD_RATES}")
    return baud


class SerialPort:
    """One serial port (a device path or any pyserial URL)."""

    def __init__(self, url: str, baud: int = DEFAULT_BAUD,
                 timeout: float = 1.0) -> None:
        validate_baud(baud)
        self._url = url
        self._baud = baud
        self._timeout = timeout
        self._ser: Optional[serial.Serial] = None

    def open(self) -> None:
        if self._ser is not None:
            return
        # do_not_open so RTS/DTR are set low before the port opens (D-022).
        self._ser = serial.serial_for_url(
            self._url, baudrate=self._baud, bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE,
            timeout=self._timeout, rtscts=False, xonxoff=False, dsrdtr=False,
            do_not_open=True)
        self._ser.rts = False
        self._ser.dtr = False
        self._ser.open()
        log.info("serial port %s opened at %d baud", self._url, self._baud)

    def close(self) -> None:
        if self._ser is not None:
            self._ser.close()
            self._ser = None

    @property
    def url(self) -> str:
        return self._url

    @property
    def baud(self) -> int:
        return self._baud

    def _require_open(self) -> serial.Serial:
        if self._ser is None:
            raise SerialError("port not open")
        return self._ser

    def write(self, data: bytes) -> None:
        self._require_open().write(data)

    def read_byte(self) -> Optional[int]:
        data = self.read(1)
        return data[0] if data else None

    def read(self, n: int) -> bytes:
        """Up to n bytes; returns early only on timeout. A port closed
        under a reading thread (program stopping) reads as a dead line:
        nothing, i.e. a timeout to the caller."""
        ser = self._require_open()
        try:
            return ser.read(n)
        except (TypeError, AttributeError):
            if not ser.is_open:
                return b""
            raise

    def set_baud(self, baud: int) -> None:
        """Reprogram the line rate once everything written has gone out
        (LNKCLIENT/LNKSERVER change rate mid-session; see session.py)."""
        ser = self._require_open()
        ser.flush()                 # tcdrain: the old-rate bytes leave first
        # tcdrain returns once the driver is empty; a USB adapter's FIFO may
        # still hold the last byte. Allow it three character times.
        time.sleep(30.0 / self._baud)
        ser.baudrate = baud
        self._baud = baud
        log.debug("serial port %s now %d baud", self._url, baud)

    def set_timeout(self, t: Optional[float]) -> None:
        if t != self._timeout:
            self._timeout = t
            self._require_open().timeout = t

    def __enter__(self) -> "SerialPort":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class SerialLinkByteIO:
    """Byte link over a SerialPort: tx_byte / flush / rx_byte(timeout).

    Transmitted bytes are buffered and written in one burst on flush() or
    before the next read, which is how a frame's count+data+crc go out (and
    which DOSBox's UART emulation needs: single-byte writes drop bytes).
    """

    def __init__(self, serial_port: SerialPort) -> None:
        self._port = serial_port
        self._tx_buf = bytearray()
        # Bytes/s cap for long bursts (None: line rate). A DOSBox peer drains
        # its port at ~5 KB/s whatever the nominal rate, through a 4096-byte
        # tty buffer without flow control (s57 live_write / live_read115);
        # bursts above that lose bytes. Real LNKSERVER/LNKCLIENT in DOSBox
        # never exceed it, so pacing keeps us within what they produce.
        self.max_tx_rate: Optional[float] = None

    def tx_byte(self, b: int) -> None:
        self._tx_buf.append(b & 0xFF)

    def flush(self) -> None:
        if not self._tx_buf:
            return
        data = bytes(self._tx_buf)
        self._tx_buf.clear()
        rate = self.max_tx_rate
        if not rate or len(data) <= PACE_SLICE or rate >= self._port.baud / 10:
            self._port.write(data)
            return
        start = time.monotonic()
        for off in range(0, len(data), PACE_SLICE):
            self._port.write(data[off:off + PACE_SLICE])
            due = start + (off + PACE_SLICE) / rate
            delay = due - time.monotonic()
            if delay > 0:
                time.sleep(delay)

    def rx_byte(self, timeout_sec: Optional[float] = 1.0) -> Optional[int]:
        """Flush pending tx, then read one byte (None on timeout).
        timeout_sec None blocks until a byte arrives."""
        self.flush()
        self._port.set_timeout(timeout_sec)
        return self._port.read_byte()

    def rx_bytes(self, n: int, timeout_sec: float) -> bytes:
        """Flush pending tx, then read n bytes in one call (fewer on
        timeout)."""
        self.flush()
        self._port.set_timeout(timeout_sec)
        return self._port.read(n)

    def set_baud(self, baud: int) -> None:
        self.flush()
        self._port.set_baud(baud)

    def close(self) -> None:
        self.flush()
        self._port.close()

    @property
    def name(self) -> str:
        return f"serial {self._port.url} @ {self._port.baud}"


def open_link(url: str, baud: int = 9600) -> SerialLinkByteIO:
    """Open `url` and return its byte link. Links start at 9600, the only
    rate the handshake runs at; the session steps up from there."""
    port = SerialPort(url, baud)
    port.open()
    return SerialLinkByteIO(port)
