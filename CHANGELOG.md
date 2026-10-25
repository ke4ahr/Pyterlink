# Changelog

The version lives in `pyterlink/__init__.py`. Captures made with `-trace`
record the version in their header.

Entries before 1.1.0 use the option names of their time (`/FLAG` style).
Since 1.1.0 the same options are written `-flag`.

## 1.1.0 -- 2026-10-25

### Changed

- **Option style.** Every option starts with `-` and is written in lower
  case. A value follows as the next word or after `=` (`-port /dev/ttyUSB0`,
  `-baud=115200`). The old `/FLAG=value` and `/FLAG:value` forms are refused
  with a one-line message.
- The client's write requests start at 127 sectors, as the real LNKCLIENT's
  do, instead of growing from 8. A lost request halves the size for the
  rest of the run.
- Error replies use the codes the real LNKSERVER sends: `0x8101` (unknown
  unit) and `0x810A` (write fault).
- `-attach` on a kernel before Linux 6.2 no longer changes `max_ratio`
  (it had no effect there); the warning is kept.

### Added

- Server option `-no-follow-symlinks`: when DOS writes to a file that is a
  symlink in a shared directory, the link is replaced by a regular file
  instead of being written through.
- A mounted CD-ROM or ISO image is detected as a read-only file system and
  served read-only.
- Files larger than 2 GB on the Linux side are not served to DOS, and a DOS
  write to a name that matches such a file is refused. A directory or FAT32
  drive larger than 2 GB is reported at startup.

- `tools/decode_pyterlink_capture.py`, rewritten: decodes any capture
  (either pyterlink role, or the real programs) into requests and replies
  with their fields and the DOS status word; `--json` output.

### Security

A review of all input from the other side of the link led to these fixes:

- pyterlink-server no longer stops on a short read or write request.
- The client limits the data a server may send for one read, and checks
  the length of BPB, drive map and media ID replies.
- The NBD server refuses a write longer than the drive before reading its
  data, and limits option length to 64 KB.
- Directory and FAT32 tree walks stop at 64 levels.
- A DOS name that would be `.`, `..` or empty on Linux is given a `_`
  prefix.
- Control characters in file names are shown as `?` in the server log.
- The `-trace` file is always closed.

### Removed

- Unused modules: `server/rcopy.py`, `server/printer.py`,
  `client/fuse_fs.py`, `transport/uart_calib.py`, `transport/link_byte.py`,
  `protocol/opcodes.py`.
- Tools that no longer ran or were one-off maintenance scripts:
  `probe_lnkclient.py`, `fix_sessions.py`,
  `fix_sessions2.py`, `restore_sessions.py`. Development and test tools
  (capture replay, loopback and directory-drive checks, strace conversion)
  are no longer shipped. Reverse-engineering helpers
  (`unexepack.py`, `update_functrace_renames.py`) are no longer shipped.

### Fixed

- Help text (`-h`): the client's write-size description matches the code
  (requests start at 127 sectors); the server's usage line lists
  `-no-follow-symlinks`.

## 1.0.7 -- 2026-10-08

- Server screen: "equals" in the middle of each drive row, the drives
  centred under their headings.
- Live with 1.0.6 (both adapters plugged into the computer directly):
  XCOPY /V onto a directory drive ran 120 write-with-verify requests (up to
  127 sectors) at 11.1 KB/s with no error; the client wrote 150 requests at
  10.9 KB/s with no hold.

## 1.0.6 -- 2026-10-08

- `/ATTACH` on a kernel older than 6.2 (no per-device limit on unwritten
  data, e.g. 5.15) warns that `cp` will return before the data is written
  and suggests `mount -o flush`; seen live as a 50 s hold until `sync`.

## 1.0.5 -- 2026-10-08

- The command line that started each program is in its log (`/V`: first
  line) and in every `/TRACE` header.
- Server screen: the drive boxes sit at the top, one line under the title;
  the title, boxes and bottom bar are laid out for 80 columns whatever the
  terminal width (the file line and the log use the full width).
- `Transfer:` shows Reading/Writing for as long as frames move on the line,
  `Idle` while only keepalives flow, and nothing when the line is silent;
  the drive's `*` and the file line follow the same rule.

## 1.0.4 -- 2026-10-08

- pyterlink-server: write-with-verify requests (op `06`, sent by LNKCLIENT
  when DOS VERIFY is on, e.g. `XCOPY /V`) are handled like plain writes, as
  LNKSERVER does. Before, the first one deadlocked the link and DOS showed
  "Abort, Retry, Fail, Ignore".
- Frame data is read in one bulk read instead of byte by byte, so the
  server keeps up with 115200 under load.

## 1.0.3 -- 2026-10-07

- The client's 5-second progress line (`/V`) counts writes as well as
  reads: `link: N sectors read, M written; X KB/s over the last 5 s; write
  size W`.

## 1.0.2 -- 2026-10-07

- `/ATTACH` caps the unwritten data Linux holds for the drive at 64 KB
  (Linux 6.2 and later), so a `cp` to the mount waits for the serial link
  instead of returning minutes early. The old values are restored at
  detach.

## 1.0.1 -- 2026-10-07

First numbered version. Compared with the unnumbered development state:

- Directory drives are read-write both ways (DOS changes written back;
  Linux changes followed by rescans), `/FREE`.
- Server status screen like LNKSERVER's, with the file being transferred.
- Adaptive transfer sizes: client write requests 8 to 127 sectors
  (`/WRITE`), server pacing that slows down on a missed burst (`/PACE`).
- No tracebacks: one-line errors, details in a file.
- Fixed: pyterlink-server did not recover when a write's data was lost.

## Before 1.0.1 -- 2026-09 to 2026-10

Development: the protocol decoded from captures and disassembly --
handshake, rate steps, frames, every request -- with both roles verified
live at up to 115200 baud; partitioned, FAT32 and read-only directory
drives; the protocol specification.
