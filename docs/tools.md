# tools/

Helper scripts that ship with pyterlink. Run them from the project root.

| Script | What it does |
|---|---|
| `decode_pyterlink_capture.py` | Decode a capture into requests and replies, one line each: operation name, unit, LBA and sector count, data bytes moved, and the DOS status word spelled out (done, write fault, unknown unit, ...). Works on captures of either pyterlink role and of the real programs. `--json` for one JSON object per line, `--session N`, `--hex`. |
| `capture_frames.py` | Extract the CRC-checked LNKCLIENT frames from a capture, one line per frame in time order. `--opcodes` gives a summary of request opcodes and the shapes of their replies. `decode_pyterlink_capture.py` uses it to find frames. |
| `serial_latency.sh` | Set the latency timer of FTDI USB serial adapters to 1 ms (`sudo tools/serial_latency.sh`; `reset` restores 16 ms). Run it after every replug. The default 16 ms costs about 15 ms per turnaround at 115200 baud. |
| `klog_tee.py` | Copy a program's output to a log file with a timestamp on every line. The `dosbox_x/run-*.sh` scripts use it for DOSBox's messages. See `docs/klog_tee.md`. |
| `console_proxy.py` | Keeps the one connection DOSBox allows to a DOS console on a TCP serial port (the LNKCLIENT DOSBox's COM1, port 11233) and lets several local clients use it in turn, logging the console traffic. See `docs/console_proxy.md`. |
| `com3_logging_proxy.py` | TCP proxy between two DOSBox instances' COM3 null-modem ports that records every byte in the capture format, for sessions between the real LNKCLIENT and LNKSERVER. See `docs/com3_logging_proxy.md`. |
| `pyterlink_nbdkit_plugin.py` | nbdkit Python plugin: exports an LNKSERVER drive as an NBD export, using pyterlink in the client (LNKCLIENT) role. `LNKCLIENT_PORT=/dev/ttyUSB1 LNKCLIENT_BAUD=115200 nbdkit python tools/pyterlink_nbdkit_plugin.py` |

Captures are written by the `-trace file` option of `pyterlink-server` and
`pyterlink-client`. Both tools read plain (`.log`) and gzip-compressed
(`.log.gz`) captures:

```sh
pyterlink-server C:=/srv/dos -port /dev/ttyUSB0 -trace session.log
python3 tools/decode_pyterlink_capture.py session.log
python3 tools/capture_frames.py session.log --opcodes
```

The decoder works at the protocol level (requests such as "read 16
sectors at LBA 447"). It does not yet tell which DOS file, directory or FAT
those sectors belong to; see `ROADMAP.md`.
