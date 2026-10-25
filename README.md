# pyterlink

**MS-DOS 6.22 INTERLNK and INTERSVR for Linux -- wire compatible with the real programs.**

Version 1.1.0 -- GPL-3.0-or-later -- <https://github.com/ke4ahr/Pyterlink/>

pyterlink lets a Linux computer share drives with an MS-DOS computer over a
null-modem serial cable, using the same protocol as the INTERLNK and INTERSVR
programs that ship with MS-DOS 6.0 through 6.22. Nothing new has to be
installed on the DOS side: the DOS computer runs its own, unmodified
`INTERLNK.EXE` or `INTERSVR.EXE`.

| Program | Plays the part of | What it does |
|---|---|---|
| `pyterlink-server` | INTERSVR | A DOS computer running INTERLNK sees Linux disk images and Linux directories as DOS drive letters. |
| `pyterlink-client` | INTERLNK | The drives of a DOS computer running INTERSVR are mounted on Linux. |
| `pyterlink-nbd` | -- | A standalone Network Block Device front end for a serial link or a local disk image. |

> **Permissions.** The user who runs pyterlink must be in the `dialout`
> group to open serial ports. Some steps also need root through `sudo`:
> loading the `nbd` module, the client's `-attach` mode, `mount` and
> `umount`, and `tools/serial_latency.sh`. See
> [Permissions: groups and sudo](#permissions-groups-and-sudo) for every
> command.

## Contents

- [How it works](#how-it-works)
- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Permissions: groups and sudo](#permissions-groups-and-sudo)
- [Sharing Linux drives with DOS (server)](#sharing-linux-drives-with-dos-server)
- [Mounting DOS drives on Linux (client)](#mounting-dos-drives-on-linux-client)
- [Option reference](#option-reference)
- [Performance](#performance)
- [Safety and data integrity](#safety-and-data-integrity)
- [Limitations](#limitations)
- [Troubleshooting](#troubleshooting)
- [Verification and compatibility](#verification-and-compatibility)
- [Documentation](#documentation)
- [Project layout](#project-layout)
- [Changes in 1.1.0](#changes-in-110)
- [License](#license)

## How it works

The protocol is not a file-sharing protocol. It is a remote **block device**:
the DOS computer asks for 512-byte disk sectors by number, and the other
side sends them. Because of that:

- **pyterlink-server** presents everything it shares as a FAT disk. A disk
  image is sent as it is. A Linux directory is turned into a FAT16 disk on
  the fly, and changes DOS makes to that disk are written back to the
  directory.
- **pyterlink-client** gives Linux a block device (through the kernel's NBD
  driver). Linux then mounts it as an ordinary `msdos` file system.

Every session starts at 9600 baud, as the DOS programs do, and then steps
up to the fastest rate both sides allow (up to 115200 baud). The complete
protocol is documented in
[`docs/interlnk_protocol_spec.md`](docs/interlnk_protocol_spec.md).

## Features

**Server (`pyterlink-server`)**

- Serves FAT12/FAT16 volume images and floppy images.
- Serves every DOS volume of a partitioned hard-disk image, lettered the
  way DOS 6.22 would letter them (primary partition, then each logical
  drive of the extended partition). FAT16 on LBA partition types (0E, 0F)
  is supported.
- Serves FAT32 volumes as generated, read-only FAT16 drives so that
  DOS 6.22 can read them. `-fat32` sends them unchanged, for clients
  running DOS 7.1 or later.
- Serves a **Linux directory** as a read-write FAT16 drive:
  - DOS sees 8.3 names; long-name entries are included for other readers.
  - Changes made by DOS (new, changed, renamed, moved and deleted files and
    directories, dates, the read-only attribute) are written back to the
    directory after 2 seconds of quiet.
  - Changes made on the Linux side appear on DOS. The directory is rescanned
    every second (every 10 seconds with `-ro`).
- Serves a mounted CD-ROM or ISO image as a read-only drive. A read-only
  file system is detected and the drive is forced read-only.
- Accepts a disk geometry in the same form as DOSBox `imgmount -size`.
- Handles every disk request the DOS client sends, including write with verify
  (DOS `VERIFY ON`, `XCOPY /V`).
- Status screen (`-v`) laid out like the DOS server's own screen: drive mapping,
  the active drive, the file being read or written, line speed and a log.
- Adapts its sending rate when the DOS side cannot keep up.

**Client (`pyterlink-client`)**

- Attaches a DOS drive to a Linux NBD device (`-attach /dev/nbdN`), or runs
  an NBD server for `nbd-client`, `nbdkit` or `qemu` (`-listen host:port`).
- Read-only by default; read-write with `-rw`.
- Reads on demand with a sector cache.
- Sends writes in requests of up to 127 sectors, as the DOS client does, and
  halves the size if a request is lost.
- Reconnects by itself after a dropped link or a restart of the DOS server.

**Both**

- Byte-exact wire compatibility with the real DOS programs.
- `-trace file` records every byte on the line for later analysis.
- Errors are reported as one line, never as a Python traceback. When there
  is more detail, the message names a log file in `/tmp`.
- Every length sent by the other side is checked before it is used.

## Requirements

**Linux computer**

- Linux with Python 3.10 or later.
- [pyserial](https://pypi.org/project/pyserial/) 3.5 or later (installed
  automatically by `pip`).
- A serial port: a built-in port or a USB serial adapter.
- Membership of the `dialout` group to open serial ports without root.
- For the client's `-attach` mode: root access and the `nbd` kernel module.
- Recommended: Linux 6.2 or later for the client (see
  [Safety and data integrity](#safety-and-data-integrity)).

**DOS computer**

- DOS with its own client and server programs (both are in `C:\DOS` on a
  standard install). pyterlink is tested against the 6.22 versions.
- Real hardware, or an emulator such as DOSBox with its serial port passed
  through to a real port. Testing was done with DOSBox.

**Cable**

- A null-modem serial cable between the two computers.
- USB serial adapters should be plugged directly into the computer. In
  testing, an adapter behind a USB hub dropped bytes at 115200 baud.

## Installation

```sh
git clone https://github.com/ke4ahr/Pyterlink.git
cd Pyterlink
pip install .            # or: pip install -e .  for a development install
```

This installs the commands `pyterlink-server`, `pyterlink-client` and
`pyterlink-nbd`. You can also run the programs without installing them:
`python3 -m pyterlink.server.cli` and `python3 -m pyterlink.client.cli`.

Check the install:

```sh
pyterlink-server -version
pyterlink-client -version
```

Then give your user access to the serial ports, as described in the next
section.

## Permissions: groups and sudo

### Serial ports: join the `dialout` group (once)

Serial devices such as `/dev/ttyS0` and `/dev/ttyUSB0` belong to a group,
usually `dialout` (Debian, Ubuntu, Fedora). On Arch Linux the group is
`uucp`. Check the group of your port:

```sh
ls -l /dev/ttyUSB0
# crw-rw---- 1 root dialout 188, 0 ... /dev/ttyUSB0
```

Add your user to that group, then log out and back in:

```sh
sudo usermod -aG dialout $USER
id -nG                  # dialout should now be listed
```

To use the new group in the current shell without logging out, run
`newgrp dialout`.

With this group, `pyterlink-server`, `pyterlink-client -status` and
`pyterlink-client -listen` run as an ordinary user. Do not run
pyterlink-server as root: it writes to the shared directories as the user
who runs it.

### Commands that need root

| Command | Why it needs root |
|---|---|
| `sudo usermod -aG dialout $USER` | Changes group membership (once). |
| `sudo modprobe nbd` | Loads the kernel's Network Block Device driver. |
| `sudo pyterlink-client ... -attach /dev/nbd0` | The kernel accepts NBD setup requests only from root (membership of the `disk` group is not enough). The client also sets the device's write-back limits in `/sys`. |
| `sudo mkdir -p /mnt/interlnk` | Creates a mount point outside your home directory. |
| `sudo mount -t msdos ... /dev/nbd0 /mnt/interlnk` | Mounts a file system. |
| `sudo umount /mnt/interlnk` | Unmounts it. |
| `sudo pkill -INT -f pyterlink.client` | Stops a client that was started as root. |
| `sudo tools/serial_latency.sh` | Writes the USB adapter's latency setting in `/sys` (optional, FTDI adapters). |
| `sudo mount -o loop,ro image.iso /mnt/cdrom` | Mounts an ISO image so the server can share it. |

`sudo` uses its own `PATH`, so it may not find `pyterlink-client`. Give the
full path instead:

```sh
sudo "$(command -v pyterlink-client)" -port /dev/ttyUSB1 -unit 2 -rw -attach /dev/nbd0
```

This works when pyterlink is installed system-wide or in a virtual
environment.

**Serving a raw disk** such as `C:=/dev/sdb` needs read (and for writes,
write) access to that device. Devices belong to the `disk` group, but
membership of `disk` gives full access to every disk on the computer, so
prefer serving an image file made with `dd`.

**Optional:** for FTDI USB adapters, `sudo tools/serial_latency.sh` sets
the adapter's latency timer to 1 ms. Run it again after each replug.

## Sharing Linux drives with DOS (server)

**1. Start the server on Linux first.** The DOS client looks for the server while
DOS boots.

```sh
# Share a directory as the server's drive C:
pyterlink-server C:=/srv/dos-share -port /dev/ttyUSB0 -baud 115200 -v

# Share a disk image, read-only
pyterlink-server C:=disk.img -port /dev/ttyUSB0 -ro

# Share a directory and an image at the same time
pyterlink-server C:=/srv/dos-share D:=games.img -port /dev/ttyUSB0
```

**2. Load the client driver on DOS.** Add a line to `CONFIG.SYS` and reboot:

```
DEVICE=C:\DOS\INTERLNK.EXE /NOPRINTER /COM /BAUD:115200
```

**3. Use the drives.** At the DOS prompt, `INTERLNK` shows the mapping, for
example `E: equals C:`. From then on, `E:` is the Linux directory:

```
DIR E:
COPY E:\README.TXT C:\
XCOPY C:\WORK\*.* E:\BACKUP\ /S /E /V
```

Files written by DOS appear in the Linux directory about 2 seconds after
the last write. Press `Q` or `Esc` on the server's status screen to quit.

**Drive specifications**

| Form | Meaning |
|---|---|
| `C:=/path/to/dir` | A Linux directory, served as a FAT16 drive. |
| `C:=disk.img` | A volume image, or every DOS volume of a partitioned image (C:, D:, ...). |
| `C:=disk.img#3` | Only the third DOS volume of a partitioned image. |
| `C:=disk.img@512,63,16,100` | An image with its geometry given as DOSBox `imgmount -size` (bytes per sector, sectors, heads, cylinders). |
| `C:=/mnt/cdrom` | A mounted CD-ROM or ISO image (read-only). Mount it first, e.g. `sudo mount -o loop,ro image.iso /mnt/cdrom`. |

Drive letters are the server's own unit letters (A: = unit 0), exactly as
with `INTERSVR C:`. The DOS client chooses which DOS letters they appear as.

## Mounting DOS drives on Linux (client)

**1. Start the server program on DOS.** For example:

```
C:\DOS\INTERSVR.EXE C: /COM /BAUD:115200
```

**2. See what the DOS server offers:**

```sh
pyterlink-client -port /dev/ttyUSB1 -baud 115200 -status
```

**3. Attach a drive and mount it:**

```sh
sudo modprobe nbd
sudo mkdir -p /mnt/interlnk
sudo pyterlink-client -port /dev/ttyUSB1 -baud 115200 -unit 2 -rw -attach /dev/nbd0 &
sudo mount -t msdos -o uid=$(id -u),gid=$(id -g),flush /dev/nbd0 /mnt/interlnk
```

- `-unit 2` selects the DOS server's C: (A: = 0, B: = 1, C: = 2).
- `-rw` allows writes. Without it, Linux mounts the drive write-protected.
- `uid=` and `gid=` make the files belong to you. An `msdos` file system
  has no owners, so without them every file belongs to root.
- `flush` starts sending each file over the link as soon as it is closed.

**4. Unmount before you stop the client.** `umount` waits until every write
has crossed the link:

```sh
sudo umount /mnt/interlnk
sudo pkill -INT -f pyterlink.client
```

Mount the drive as `msdos`, not `vfat`: the DOS programs use 8.3 file
names only.

## Option reference

Options start with `-` and are written in lower case. A value can follow
as the next word or after `=`: `-port /dev/ttyUSB0` and
`-port=/dev/ttyUSB0` mean the same. `-h` shows help and `-version` shows
the version for both programs.

### pyterlink-server

```
pyterlink-server drive:=image [drive:=image ...] [-x drive:[...]]
                 [-com n | -port device] [-baud rate] [-ro] [-free mb]
                 [-pace bytes] [-fat32] [-no-follow-symlinks] [-v] [-trace file]
```

| Option | Meaning | Default |
|---|---|---|
| `drive:=image` | Serve an image, device or directory as this server drive. See [Drive specifications](#sharing-linux-drives-with-dos-server). | -- |
| `-x drive:` | Keep these drives out of the DOS client's mapping. | none |
| `-com n` | Use COM port *n* (`/dev/ttyS(n-1)`). | `-com 1` |
| `-port device` | Use any serial device, e.g. `/dev/ttyUSB0`. | -- |
| `-baud rate` | Highest rate the DOS client may step up to: 9600, 19200, 38400, 57600 or 115200. | 115200 |
| `-ro` | Serve every drive read-only. | read-write |
| `-free mb` | Free space a writable directory drive reports, in MB (limited by the real free space and the 2 GB FAT16 limit). | 64 |
| `-pace bytes` | Start with a cap on sector data, in bytes per second (0 = no cap). | 0 |
| `-fat32` | Send FAT32 volumes unchanged (for DOS 7.1 or later clients only). | FAT16 translation |
| `-no-follow-symlinks` | When DOS writes to a file that is a symlink in a shared directory, replace the link with a regular file instead of writing through it. | follow links |
| `-v` | Show the status screen. | off |
| `-trace file` | Append every byte on the line to *file*. | off |

### pyterlink-client

```
pyterlink-client [-com n | -port device] [-baud rate] [-v]
                 [-unit n] [-rw] [-write n] [-nokeepalive] [-trace file]
                 (-attach /dev/nbdN | -listen host:port | -status)
```

| Option | Meaning | Default |
|---|---|---|
| `-attach /dev/nbdN` | Attach the drive to a kernel NBD device (needs root and `modprobe nbd`). | -- |
| `-listen host:port` | Run an NBD server instead, for `nbd-client`, `nbdkit` or `qemu`. | -- |
| `-status` | Print the drives the DOS server offers, then exit. | -- |
| `-com n` / `-port device` | Serial port, as for the server. | `-com 1` |
| `-baud rate` | Rate to step up to after the 9600 handshake. | 115200 |
| `-unit n` | Server unit to use (A: = 0). | first mapped unit |
| `-rw` | Allow writes. | read-only |
| `-write n` | Largest write request, in sectors (1-127). | 127 |
| `-nokeepalive` | Do not send keepalives. Needs `-baud 9600`: at any higher rate the DOS server falls back to 9600 after 2 s of silence. | keepalives on |
| `-v` | Log progress every 5 seconds. | off |
| `-trace file` | Append every byte on the line to *file*. | off |

Exactly one of `-attach`, `-listen` or `-status` is required.

### pyterlink-nbd

`pyterlink-nbd` uses GNU-style options (`--serial`, `--image`, `--attach`,
...). Run `pyterlink-nbd --help` for the full list.

## Performance

Measured with both USB adapters plugged directly into the computer, at
115200 baud:

| Direction | Throughput |
|---|---|
| Server role, DOS reading or writing | about 11.1 KB/s |
| Client role, Linux writing | about 10.9 KB/s |

115200 baud carries at most about 11.5 KB/s, so both roles run close to the
line rate. Copying a 1 MB file takes about 90 seconds.

## Safety and data integrity

- **Unmount before you stop the client.** `umount` is what waits for the
  last writes. Stopping the client first can leave the DOS disk damaged.
- **Linux before 6.2:** `cp` to the mount returns before the data has
  crossed the link, because older kernels have no per-device limit on
  unwritten data. Mount with `-o flush` and always `umount` before you
  stop the client. The client prints a warning when it detects such a
  kernel.
- **Reading back what you just wrote does not use the link.** Linux and the
  client cache what was written. To read it back over the serial line,
  unmount, restart the client and mount again.
- **Do not serve an image that something else has open read-write**
  (a running DOSBox, or a Linux mount). Each side caches the FAT and they
  overwrite each other. Serve a copy, or use `-ro`.
- **Symlinks in shared directories:** by default the server follows them
  when DOS writes, so a link inside the share can point to a file outside
  it. If the share may contain links you do not trust, use
  `-no-follow-symlinks`.
- **After a sudden stop** (Ctrl-C, `kill`, a crash, a closed terminal)
  during a write, a disk can be left with lost clusters.
  [`docs/abort_recovery.md`](docs/abort_recovery.md) explains which disk to
  check and how (SCANDISK, CHKDSK, NDD, `fsck.fat`).

## Limitations

- **Serial link only.** The parallel-port (LPT) link is not implemented
  yet; `-lpt` is refused.
- **Printer redirection is not implemented.**
- **8.3 file names only** on the DOS side; this is a limit of the DOS
  programs themselves.
- **2 GB limits:** a directory drive or translated FAT32 drive must fit in a
  2 GB FAT16 volume. Linux files larger than 2 GB are not shown to DOS, and
  DOS cannot overwrite them.
- FAT32 volumes translated to FAT16 are read-only.
- **The DOSBox test rig files as shipped** (`configure_rig.sh`, the image
  paths in `dosbox_x/*.conf`, `$PYTERLINK_CONTROL` in the run and stop
  scripts, and the monit template) have not yet been run under DOSBox.

Planned work is listed in [`ROADMAP.md`](ROADMAP.md).

## Troubleshooting

| Symptom | What to check |
|---|---|
| `Permission denied` on the serial port | Your user is not in the `dialout` group, or you did not log out and in after adding it. |
| The DOS client reports no server | Start pyterlink-server **before** DOS loads the client driver. Check the cable is a null-modem cable and that both sides use the same port. |
| `ttyUSB0: input overrun` in `dmesg`, slow or lost transfers | Plug the USB adapter directly into the computer, not into a hub. Run `sudo tools/serial_latency.sh` for FTDI adapters. |
| DOS shows `Abort, Retry, Fail?` on the drive | The server stopped or the link dropped. Restart the server and answer `R` (Retry). |
| `cp` to the mount returns at once but data arrives much later | Linux before 6.2. Mount with `-o flush`; see [Safety and data integrity](#safety-and-data-integrity). |
| An option such as `/PORT=...` is refused | Options changed to `-flag` style in 1.1.0. Use `-port /dev/ttyUSB0`. |

For any failure, the one-line error message names a log file
(`/tmp/pyterlink-<program>-errors.log`) when more detail is available.

## Verification and compatibility

The real DOS client and server programs are the only
reference for pyterlink's behavior. The protocol was decoded from captured
traffic between the two DOS programs and from disassembly of the programs
themselves. It was not taken from pyterlink's own code or notes.

Both roles have been tested live against the real programs, running in
DOSBox over two USB serial adapters, at 9600 to 115200 baud:

- **Server role:** `DIR`, `TYPE`, `COPY` and `XCOPY /V` in both directions;
  `MD`, `REN`, `DEL`, `ATTRIB +R`, `MOVE`; files added on Linux appear on
  DOS; FAT32-as-FAT16 and three-volume partitioned images.
- **Client role:** handshake and rate steps; multi-sector reads; NBD mount
  with file contents checked by MD5; writes of up to 127 sectors; recovery
  after a restart of the DOS server.
- Recorded live sessions replay byte for byte in both roles.

There is no unit-test suite. Changes are checked against recorded traffic
and live runs (the recordings are not part of the distribution; make your
own with `-trace`). The full procedure is in
[`docs/test_procedure.md`](docs/test_procedure.md).


## Documentation

| Document | Contents |
|---|---|
| [`pyterlink/README.md`](pyterlink/README.md) | Detailed program reference. |
| [`docs/interlnk_protocol_spec.md`](docs/interlnk_protocol_spec.md) | The wire protocol: frames, CRC, handshake, rate negotiation, every request. |
| [`docs/abort_recovery.md`](docs/abort_recovery.md) | What to check and repair after an interrupted transfer. |
| [`docs/test_procedure.md`](docs/test_procedure.md) | Test rig and step-by-step live test procedure with DOSBox. |
| [`CHANGELOG.md`](CHANGELOG.md) | Changes in each version. |
| [`dosbox_x/README.md`](dosbox_x/README.md) | The DOSBox test setup and DOS images. |
| [`docs/tools.md`](docs/tools.md) | The analysis and test tools in `tools/`. |
| [`ROADMAP.md`](ROADMAP.md) | Planned work. |

## Project layout

| Path | Contents |
|---|---|
| `pyterlink/` | The Python package: `protocol/` (frames, client and server sessions), `transport/` (serial), `server/`, `client/`, `block.py`, `nbd.py`, `errors.py`. |
| `tools/` | Capture decoder, DOSBox serial-port proxies, FTDI latency setting, nbdkit plugin ([`docs/tools.md`](docs/tools.md)). |
| `docs/` | Protocol specification, procedures, and reverse-engineering notes. |
| `dosbox_x/` | DOSBox test setup: configurations and run scripts (the DOS disk images are not included). |
| `configure_rig.sh` | Writes this computer's image paths, serial ports and control paths into the `dosbox_x/` files. |

## Changes in 1.1.0

- **New option style.** Every option now starts with `-` and is lower case;
  a value follows as the next word or after `=`. The old `/FLAG=value` and
  `/FLAG:value` forms are refused with a clear message. Update scripts that
  call pyterlink.
- **Security hardening.** pyterlink-server no longer stops on a malformed
  request. The client and the NBD server check every length the other side
  sends. Directory and FAT32 tree walks stop at 64 levels. DOS names that
  would become `.` or `..` on Linux are renamed. Control characters in file
  names no longer reach the terminal. The trace file is always closed.
- **New server option `-no-follow-symlinks`.**
- **Smaller package.** Unused modules were removed.

The history of every version is in [`CHANGELOG.md`](CHANGELOG.md).

## License

Copyright (C) 2026 Kris Kirby.

Pyterlink is free software: you can redistribute it and/or modify it under
the terms of the GNU General Public License as published by the Free
Software Foundation, either version 3 of the License, or (at your option)
any later version. See [`LICENSE`](LICENSE) or
<https://www.gnu.org/licenses/>.

MS-DOS, INTERLNK and INTERSVR are products/trademarks/copyrights of Microsoft Corporation.
pyterlink is an independent implementation; the `pyterlink` package
contains no Microsoft code.
