# pyterlink

`LNKCLIENT` / `LNKSERVER` for Linux.

The LNKCLIENT wire protocol is a remote DOS **block device**: LNKCLIENT
redirects whole server drives and moves 512-byte sectors. So:

- **pyterlink-client** talks to a real LNKSERVER and exports the server drive
  as a Linux block device, which Linux mounts as `msdos` (8.3 names only;
  LNKCLIENT and LNKSERVER have no long filenames).
- **pyterlink-server** serves drives to a real LNKCLIENT: disk images, every
  DOS volume of a partitioned image, FAT32 volumes and Linux directories.

Both roles were checked byte for byte against captured LNKCLIENT <-> LNKSERVER
traffic and verified live against the real programs at up to 115200 baud.
The protocol is specified in `../docs/interlnk_protocol_spec.md`.

Options start with `-`; a value follows as the next word or after `=`
(`-port /dev/ttyUSB1` or `-port=/dev/ttyUSB1`). `-h` shows help, `-version`
the version.

## Client (LNKCLIENT side)

```sh
pip install pyserial
sudo modprobe nbd
# attach the server's drive to /dev/nbd0 and mount it
sudo python3 -m pyterlink.client.cli -port /dev/ttyUSB1 -baud 115200 -attach /dev/nbd0 -v &
sudo mount -t msdos -o ro,uid=$(id -u),gid=$(id -g) /dev/nbd0 /mnt/lnkclient
ls -la /mnt/lnkclient
```

With `-rw`, mount without `ro` and add `flush` (see below). `uid=`/`gid=`
give the files to you (msdos has no owners; without them only root can
write). A step-by-step test session is in `../docs/test_procedure.md`.

| Option | Meaning |
|---|---|
| `-com n` or `-port device` | serial port (`-com 1` = `/dev/ttyS0`, the default) |
| `-baud rate` | rate to step up to after the 9600 handshake, as LNKCLIENT's /BAUD (default 115200) |
| `-attach /dev/nbdN` | attach directly to a kernel NBD device (root) |
| `-listen host:port` | run an NBD server instead, for `nbd-client` / `qemu-nbd` |
| `-status` | print the drives LNKSERVER offers and exit |
| `-unit n` | server unit to export (A: = 0; default: the first one mapped) |
| `-rw` | allow writes (default read-only; mount without `-o ro`) |
| `-write n` | largest write request, 1-127 sectors (default 127; see below) |
| `-nokeepalive` | do not send LNKCLIENT's idle bytes (only with `-baud 9600`) |
| `-v` | progress line every 5 s |
| `-trace file` | append every wire byte to `file` (capture format) |

Alternatives: `nbd-client 127.0.0.1 10809 /dev/nbd0` with
`-listen 127.0.0.1:10809`, or nbdkit:

```sh
LNKCLIENT_PORT=/dev/ttyUSB1 LNKCLIENT_BAUD=115200 nbdkit python tools/pyterlink_nbdkit_plugin.py
nbd-client localhost /dev/nbd0
```

The link is ~1 KB/s at 9600 baud (~11 KB/s at 115200), so sectors are fetched
on demand and cached; a run of uncached sectors goes in one read request (up
to 127 sectors, as DOS asks LNKCLIENT). Write requests start at 127 sectors,
as LNKCLIENT's do. A write that is lost (seen with an adapter behind a USB hub
that dropped bytes) costs one re-establishment (a few seconds) and is sent
again at half the size, which then stays the limit for the run. A request is
never larger than what Linux hands the client at once: copies through an
`-attach` mount usually arrive in 8 KB pieces (16 sectors). `-write n` caps
the size.

`-attach` sets `read_ahead_kb`/`max_sectors_kb` small, raises the NBD request
timeout, and caps the data Linux may hold unwritten for the device at 64 KB
(`/sys/class/bdi/<dev>/max_bytes` + `strict_limit`, restored at detach), so a
`cp` to the mount waits for the link instead of returning minutes before the
data is written. Those two settings exist from Linux 6.2; on older kernels
(e.g. Ubuntu 22.04's 5.15) the client warns at attach, `cp` returns early and
the data can wait ~30 s in memory before it is written: mount with
`-o flush` there, which starts the writes as each file is closed. With
nbd-client run `blockdev --setra 8 /dev/nbd0`. If the link drops (LNKSERVER
restarted, cable glitch) the client re-runs LNKCLIENT's establishment with its
saved session cookies and retries the request.

## Server (LNKSERVER side)

```sh
python3 -m pyterlink.server.cli C:=disk.img -port /dev/ttyUSB0 -baud 115200 -v
```

Drive letters are LNKSERVER's server drives (A: = unit 0, ...), so `C:=disk.img`
behaves like `INTERSVR C:`; lower letters are reported as empty drives.

| Drive spec | Served as |
|---|---|
| `C:=floppy.img` | a FAT volume image |
| `C:=disk.img` | every DOS volume of a partitioned image: primary partition as C:, logical drives as D:, E:, ... (FAT16 on LBA types 0E/0F too) |
| `C:=disk.img#3` | only the third volume |
| `C:=disk.img@512,63,16,100` | with this geometry (DOSBox `imgmount -size` form) |
| FAT32 volume in an image | a generated read-only FAT16 drive (default, safe for DOS before 7.1); `-fat32` sends it raw, for DOS 7.1+ clients |
| `C:=/path/to/dir` | a generated FAT16 drive: 8.3 names for DOS, VFAT long names for other readers; the client's changes are written back to the directory |

Drives are writable, as LNKSERVER's are, unless `-ro` (FAT32 volumes served as
FAT16 are read-only). A directory drive keeps the client's writes in an
overlay and, after 2 s without writes, applies the resulting changes to the
directory: new, changed, renamed, moved and deleted files and directories,
DOS dates, the read-only attribute. A state in the middle of a DOS operation
is never applied; replaced and deleted files are kept for a minute (in
`.pyterlink-trash-*`, never served) in case the client still reads them.
DOS's own names are 8.3: files it creates or renames get those, files it
only modifies keep their Linux names. A file that is a symlink is written
through the link; `-no-follow-symlinks` replaces the link with a regular file
instead. Changes made on the Linux side are followed: the tree is rescanned
every second (`-ro`: every 10 s; a read-only file system such as a mounted
ISO: only at start), using only directory listings and file metadata from
the OS's cache, and the next media check tells DOS to re-read the drive.

A mounted CD-ROM or ISO image is a read-only directory drive: mount it on
Linux first (`sudo mount /dev/sr0 /mnt/cdrom` or
`sudo mount -o loop,ro image.iso /mnt/cdrom`), then serve the mount point
(e.g. `D:=/mnt/cdrom`). The server detects the read-only file system and
forces the drive read-only. Files larger than 2 GB on the Linux side are not
served to DOS (skipped during scanning).

| Option | Meaning |
|---|---|
| `-x A:` | withhold a drive from LNKCLIENT's mapping |
| `-com n` or `-port device` | serial port (default `-com 1`) |
| `-baud rate` | highest rate LNKCLIENT may step up to (default 115200); every session starts at 9600 |
| `-ro` | serve every drive read-only |
| `-free mb` | free space a writable directory drive offers (default 64 MB; DOS reads the whole FAT for the free space, so bigger drives make the first DIR slower) |
| `-pace n` | start with a cap of n bytes/s on sector data |
| `-fat32` | send FAT32 volumes raw (DOS 7.1+ clients) |
| `-no-follow-symlinks` | replace a symlink with a regular file on write-back |
| `-v` | status screen |
| `-trace file` | append every wire byte to `file` |

`-v` shows a status screen laid out like LNKSERVER's, 80 columns wide: the
title; one line below it the box "This Computer (Server) / Other Computer
(Client)" over the drive rows (`C:   equals   E:`, `equals` in the middle,
a `*` one space left of a drive while it is being accessed); the transfer
rate and sector counts; the file being read or written; the log, newest line
just above the bottom bar (the first line gives the version and command
line; each file the client starts reading or writing is logged as
`Reading C:\DOCS\X.TXT`, the drive map as `client D: = server C:`); and the
bar `Transfer: Reading | Port=ttyUSB0 Speed=115200 | Q=Exit`. Transfer:
shows Reading or Writing while data moves on the line, `Idle` while only
the idle characters flow, and nothing when the line is silent; Port= and
Speed= go blank when the idle characters stop. Q or Esc quits. The screen
needs `-v` and a terminal; without `-v` the server prints warnings only.

Never serve an image a running DOSBox or a Linux mount also has open
read-write. Sector data goes out at the full line rate; when LNKCLIENT does
not acknowledge a burst, the server halves its sending rate for the rest of
the run (down to 1000 bytes/s, logged as a warning) and LNKCLIENT's retry --
or DOS's *Abort, Retry, Fail?* answered Retry -- gets through. Writes with
verify (DOS `VERIFY ON`, `XCOPY /V`) are handled as LNKSERVER handles them:
as writes.

What a forced abort can leave damaged, on either side, and which disk check
repairs it: `../docs/abort_recovery.md`.

All three programs (server, client, `pyterlink-nbd`) report a failure as one
line; when there is more to know, the line names a file in the system temp
directory (`/tmp/pyterlink-<program>-errors.log`) holding the details.

## Protocol

Specified in `../docs/interlnk_protocol_spec.md`. In brief: after the
`aa/55/5a -> 00/ff/11` handshake, frames are `tag | count(1|2) | data |
crc(2)` with a mod-4 sequence in the tag, a per-tag echo (`~tag & 0xfb`) and
an ack (the tag). Sector data moves in chunks of up to 8 KB, tagged
`0x40|seq` while more follow. Requests carry the server unit in byte 1:

| op | request | reply |
|---|---|---|
| 00 | install (client/server cookies) | server info + cookies |
| 01 | 29-entry drive-letter map | map with server units + unit attributes |
| 1c | drive info | size text `(106Mb)` + label |
| 02 | media check | DOS status, changed, label, serial |
| 03 | build BPB | DOS status + BPB |
| 04 | read 1-127 sectors (32-bit LBA) | status + count, then the data chunks |
| 05 | write 1-127 sectors, then the data chunks | status + count |
| 06 | write with verify (DOS VERIFY ON): as 05 | status + count |
| 1d | IOCTL 08/66 Get Media ID | status, then serial/label/fs type |

Idle: the line owner sends `0xfc | (seq ^ 1)` once per BIOS tick; the peer
echoes it.

Line speed: the handshake is always at 9600; LNKCLIENT then sends `ff`,
LNKSERVER `00`, LNKCLIENT a rate code (`04` 19200, `05` 38400, `06` 57600, `07`
115200), both switch, LNKSERVER announces `code<<4`, LNKCLIENT echoes it,
LNKSERVER sends its complement, and 15 test byte pairs follow -- one step per
code up to LNKCLIENT's /BAUD. If LNKCLIENT stops below LNKSERVER's /BAUD,
LNKSERVER offers the next code at the first yield (`~seq`, then the code);
LNKCLIENT declines it, and so does pyterlink-client.

## Tools

See `../docs/tools.md`.

## Not yet implemented

- Parallel-port link (`transport/parallel_backend.py` is a stub).
- Printer redirection: printer requests (ops 0A-15, 19-1B) and block
  open/close (16-18) are answered as LNKSERVER answers them when no printer
  is attached.
