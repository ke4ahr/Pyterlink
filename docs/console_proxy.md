# Console Proxy

## Overview
Maintains persistent connection to LNKCLIENT DOSBox console on TCP 11233.
Allows multiple local clients to connect/disconnect without dropping the upstream connection.

The LNKCLIENT image's `AUTOEXEC.BAT` ends with `CTTY COM1`, so the DOS
console of the LNKCLIENT DOSBox is its COM1, a DOSBox TCP `nullmodem` port
(`dosbox_x/lnkclient-msdos.conf`). The LNKSERVER DOSBox has the same kind of
port on 11232, but its image leaves `CTTY COM1` commented out.

## Architecture
- **LNKCLIENT DOSBox**: COM1 = `nullmodem 127.0.0.1 port:11233 txdelay:0 rxdelay:0 transparent:1` (TCP server)
- **Proxy**: 
  - Upstream: Persistent connection to 11233
  - Downstream: Listens on 11234 for local clients
  - Upstream connection persists across local client connections/disconnections

## Files
- Script: `tools/console_proxy.py`
- Log directory: `logs/`
- Log file: `logs/console_traffic.log`, relative to the current directory
  (run it from the project root; `logs/` is created if it is missing).
  Every line is also printed to stdout.

## Usage
```bash
python3 tools/console_proxy.py
```

## Ports
- Upstream: 11233 (LNKCLIENT console)
- Local listen: 11234 (clients connect here)

## Usage Example
```bash
# Terminal 1: Start proxy
python3 tools/console_proxy.py

# Terminal 2: Send command
echo -e "interlnk.exe\r\n" | nc 127.0.0.1 11234

# Terminal 3: Send another command (same upstream connection)
# (D: here is whichever letter LNKCLIENT's drive table shows for the server drive)
echo -e "DIR D:\r\n" | nc 127.0.0.1 11234
```

## Features
- Persistent upstream connection to 11233
- Auto-reconnect on upstream disconnect
- Multiple local clients can connect/disconnect
- Timestamps on all traffic
- Upstream connection survives local client disconnects

## Log Format
Data as hex, then `|` and the printable ASCII (`.` for other bytes).
`SYSTEM` and `ERROR` messages are encoded the same way. On connect the proxy
sends CR LF upstream and logs any reply as `INITIAL`.
```
[2026-10-03T11:56:04.416574] [SYSTEM] 436f6e6e656374656420...  |  Connected to LNKCLIENT console at 127.0.0.1:11233
[2026-10-03T11:56:10.443463] [LOCAL->UPSTREAM] 696e7465726c6e6b2e6578650d0a  |  lnkclient..
[2026-10-03T11:56:10.443463] [UPSTREAM->LOCAL] 5468697320436f6d707574657220...  |  This Computer ...
```

## Run
```bash
python3 tools/console_proxy.py
```
