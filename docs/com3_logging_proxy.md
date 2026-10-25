# COM3 Logging Proxy

## Overview
Records the bytes between the LNKCLIENT DOSBox and the LNKSERVER DOSBox on
their COM3 ports, which are DOSBox TCP `nullmodem` ports (see
`docs/dosbox-serial.md`). The session-3 captures
`logs/com3_traffic_*.log.gz` were taken with it. These are real LNKCLIENT
<-> real LNKSERVER traffic; pyterlink is not involved (pyterlink uses the
real serial ports on COM2, `dosbox_x/README.md`).

## Architecture
- **LNKCLIENT DOSBox**: COM3 = `nullmodem server:127.0.0.1 port:11430 ... transparent:1` (TCP client)
- **LNKSERVER DOSBox**: COM3 = `nullmodem 127.0.0.1 port:11330 ... transparent:1` (TCP server)
- **Proxy**: listens on 127.0.0.1:11430, connects to 127.0.0.1:11330, logs both directions

A TCP nullmodem has no line rate, so the captures show no rate-step
timing. The DOS programs must be told to use (or find) COM3; LNKCLIENT
searches its ports when given `/COM` without a number.

## Files
- Script: `tools/com3_logging_proxy.py`
- Log file: `logs/com3_traffic.log`, relative to the current directory
  (run it from the project root; `logs/` is created if it is missing).

## Usage
```bash
python3 tools/com3_logging_proxy.py        # -d / --debug also prints each line to stdout
```
Start it after the LNKSERVER DOSBox (it connects to 11330 at startup, and
again when LNKCLIENT connects if that failed) and before LNKCLIENT opens COM3.

## Ports
- Listen: 11430 (LNKCLIENT connects here)
- Forward: 11330 (LNKSERVER listens here)

## Log Format
One line per TCP read, data as hex. `SYSTEM` and `ERROR` messages are
hex-encoded text as well.
```
=== COM3 Proxy started at 2026-10-03T12:34:49.108185 ===
[2026-10-03T12:34:49.111637] [SYSTEM] 436f6e6e656374656420746f20...
[2026-10-03T12:35:41.952731] [LNKCLIENT->LNKSERVER] aa
[2026-10-03T12:35:41.953566] [LNKSERVER->LNKCLIENT] 00
[2026-10-03T12:35:41.954371] [LNKCLIENT->LNKSERVER] 55
[2026-10-03T12:35:41.955656] [LNKSERVER->LNKCLIENT] ff
```
The handshake is one byte at a time: `aa`/`00`, `55`/`ff`, `5a`/`11`
(`docs/interlnk_protocol_spec.md` section 3). Decode logs with
`tools/decode_pyterlink_capture.py` and `tools/capture_frames.py`.
