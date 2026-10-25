# pyterlink roadmap

Planned work. What 1.1.0 does not do is listed under "Limitations" in
[`README.md`](README.md); changes in each release are in
[`CHANGELOG.md`](CHANGELOG.md).

- [ ] **Parallel-port (LPT) link.** Only the serial link is supported now;
  `pyterlink/transport/parallel_backend.py` is a stub.
- [ ] **A capture decoder at the DOS level**, showing which file, directory
  or FAT a request's sectors belong to (for example the parts of a `COPY`).
  `tools/decode_pyterlink_capture.py` stops at the protocol request level.
- [ ] **A Wireshark dissector**, or PCAP export of `-trace` captures.
