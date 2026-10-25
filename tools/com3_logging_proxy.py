#!/usr/bin/env python3
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
"""
COM3 Logging Proxy - captures traffic between LNKCLIENT (11430) and LNKSERVER (11330).
- Listens on 11430 for LNKCLIENT connections
- Connects to 11330 for LNKSERVER
- Logs all traffic with timestamps
"""
import asyncio
import os
import argparse
from datetime import datetime

class COM3LoggingProxy:
    def __init__(self, lnkserver_host='127.0.0.1', lnkserver_port=11330, listen_port=11430, debug_stdout=False):
        self.lnkserver_host = lnkserver_host
        self.lnkserver_port = lnkserver_port
        self.listen_port = listen_port
        self.debug_stdout = debug_stdout
        self.lnkserver_reader = None
        self.lnkserver_writer = None
        self.lnkserver_connected = False
        os.makedirs('logs', exist_ok=True)
        self.log_file = open('logs/com3_traffic.log', 'a')
        self.log_file.write(f"\n=== COM3 Proxy started at {datetime.now().isoformat()} ===\n")
        self.log_file.flush()

    def log(self, direction, data):
        timestamp = datetime.now().isoformat()
        hex_str = data.hex()
        line = f"[{timestamp}] [{direction}] {hex_str}\n"
        if self.debug_stdout:
            print(line.strip())
        self.log_file.write(line)
        self.log_file.flush()
    
    async def connect_lnkserver(self):
        try:
            self.lnkserver_reader, self.lnkserver_writer = await asyncio.open_connection(
                self.lnkserver_host, self.lnkserver_port)
            self.lnkserver_connected = True
            self.log("SYSTEM", f"Connected to LNKSERVER at {self.lnkserver_host}:{self.lnkserver_port}".encode())
        except Exception as e:
            self.log("ERROR", f"Failed to connect to LNKSERVER: {e}".encode())
            self.lnkserver_connected = False
    
    async def pipe(self, reader, writer, direction):
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                self.log(direction, data)
                writer.write(data)
                await writer.drain()
        except Exception as e:
            self.log("ERROR", f"Pipe {direction} error: {e}".encode())
        finally:
            writer.close()
    
    async def handle_lnkclient(self, client_reader, client_writer):
        addr = client_writer.get_extra_info('peername')
        self.log("SYSTEM", f"LNKCLIENT connected from {addr}".encode())
        
        if not self.lnkserver_connected:
            await self.connect_lnkserver()
        
        if not self.lnkserver_connected:
            self.log("ERROR", "LNKSERVER not available".encode())
            client_writer.close()
            return
        
        self.log("SYSTEM", f"LNKCLIENT<->LNKSERVER pipe established".encode())
        
        await asyncio.gather(
            self.pipe(client_reader, self.lnkserver_writer, "LNKCLIENT->LNKSERVER"),
            self.pipe(self.lnkserver_reader, client_writer, "LNKSERVER->LNKCLIENT")
        )
        
        self.log("SYSTEM", f"LNKCLIENT disconnected".encode())
    
    async def run(self):
        await self.connect_lnkserver()
        
        server = await asyncio.start_server(
            self.handle_lnkclient,
            '127.0.0.1', self.listen_port
        )
        print(f"[COM3 PROXY] Listening on 127.0.0.1:{self.listen_port} (LNKCLIENT)")
        print(f"[COM3 PROXY] Forwarding to {self.lnkserver_host}:{self.lnkserver_port} (LNKSERVER)")
        print(f"[COM3 PROXY] Logging to logs/com3_traffic.log")
        
        async with server:
            await server.serve_forever()

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='COM3 Logging Proxy')
    parser.add_argument('-d', '--debug', action='store_true',
                        help='Enable debug output to stdout')
    args = parser.parse_args()

    proxy = COM3LoggingProxy(debug_stdout=args.debug)
    try:
        asyncio.run(proxy.run())
    except KeyboardInterrupt:
        print("\n[COM3 PROXY] Shutting down")
