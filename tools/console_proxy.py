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
Console Proxy - persistent connection to LNKCLIENT console (11233).
- Maintains persistent upstream connection to DOSBox console on 11233
- Listens on local port 11234 for client connections
- Upstream connection stays alive regardless of local client connections
- Logs all traffic with timestamps
"""
import asyncio
import os
from datetime import datetime

class ConsoleProxy:
    def __init__(self, upstream_host='127.0.0.1', upstream_port=11233, local_port=11234):
        self.upstream_host = upstream_host
        self.upstream_port = upstream_port
        self.local_port = local_port
        self.upstream_reader = None
        self.upstream_writer = None
        self.upstream_connected = False
        self.local_clients = set()
        os.makedirs('logs', exist_ok=True)
        self.log_file = open('logs/console_traffic.log', 'a')
        self.log_file.write(f"\n=== Console Proxy started at {datetime.now().isoformat()} ===\n")
        self.log_file.flush()

    def log(self, direction, data):
        timestamp = datetime.now().isoformat()
        hex_str = data.hex()
        # ASCII representation with . for non-printable characters
        ascii_repr = ''.join(chr(b) if 32 <= b < 127 else '.' for b in data)
        line = f"[{timestamp}] [{direction}] {hex_str}  |  {ascii_repr}\n"
        print(line.strip())
        self.log_file.write(line)
        self.log_file.flush()
    
    async def connect_upstream(self):
        try:
            self.upstream_reader, self.upstream_writer = await asyncio.open_connection(
                self.upstream_host, self.upstream_port, family=2)
            self.upstream_connected = True
            self.log("SYSTEM", f"Connected to LNKCLIENT console at {self.upstream_host}:{self.upstream_port}".encode())
            
            # Send initial newline to get prompt
            self.upstream_writer.write(b'\r\n')
            await self.upstream_writer.drain()
            await asyncio.sleep(1)
            
            try:
                data = await asyncio.wait_for(self.upstream_reader.read(4096), timeout=5.0)
                if data:
                    self.log("INITIAL", data)
            except asyncio.TimeoutError:
                self.log("SYSTEM", "No initial prompt received".encode())
                
        except Exception as e:
            self.log("ERROR", f"Failed to connect upstream: {e}".encode())
            self.upstream_connected = False
    
    async def upstream_reader_task(self):
        try:
            while self.upstream_connected:
                data = await self.upstream_reader.read(4096)
                if not data:
                    self.log("SYSTEM", "Upstream connection closed".encode())
                    self.upstream_connected = False
                    break
                self.log("UPSTREAM->LOCAL", data)
                # Broadcast to all connected local clients
                for writer in self.local_clients.copy():
                    try:
                        writer.write(data)
                        await writer.drain()
                    except Exception:
                        pass
        except Exception as e:
            self.log("ERROR", f"Upstream reader error: {e}".encode())
            self.upstream_connected = False
    
    async def handle_local_client(self, reader, writer):
        addr = writer.get_extra_info('peername')
        self.log("SYSTEM", f"Local client connected: {addr}".encode())
        self.local_clients.add(writer)
        
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                self.log("LOCAL->UPSTREAM", data)
                if self.upstream_connected and self.upstream_writer:
                    try:
                        self.upstream_writer.write(data)
                        await self.upstream_writer.drain()
                    except Exception:
                        break
                else:
                    writer.write(b"[PROXY] Upstream not connected\r\n")
                    await writer.drain()
        except Exception:
            pass
        finally:
            self.local_clients.discard(writer)
            writer.close()
            await writer.wait_closed()
            self.log("SYSTEM", f"Local client disconnected: {addr}".encode())
    
    async def reconnect_upstream(self):
        while True:
            if not self.upstream_connected:
                self.log("SYSTEM", "Attempting to reconnect upstream...".encode())
                await asyncio.sleep(5)
                await self.connect_upstream()
                if self.upstream_connected:
                    asyncio.create_task(self.upstream_reader_task())
            await asyncio.sleep(10)
    
    async def run(self):
        await self.connect_upstream()
        if not self.upstream_connected:
            return
        
        asyncio.create_task(self.upstream_reader_task())
        asyncio.create_task(self.reconnect_upstream())
        
        server = await asyncio.start_server(
            self.handle_local_client,
            '127.0.0.1', self.local_port
        )
        print(f"[CONSOLE PROXY] Listening on 127.0.0.1:{self.local_port}")
        print(f"[CONSOLE PROXY] Connect with: nc 127.0.0.1 {self.local_port}")
        print(f"[CONSOLE PROXY] Logging to logs/console_traffic.log")
        
        async with server:
            await server.serve_forever()

if __name__ == '__main__':
    proxy = ConsoleProxy()
    try:
        asyncio.run(proxy.run())
    except KeyboardInterrupt:
        print("\n[CONSOLE PROXY] Shutting down")
