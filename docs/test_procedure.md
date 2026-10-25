# Test procedure: pyterlink against the real LNKSERVER / LNKCLIENT

How pyterlink is checked against the original MS-DOS 6.22 `LNKSERVER`
and `LNKCLIENT` running in DOSBox: the test rig, the step-by-step
procedure for a live session, what to check, and how traffic is captured.
The protocol is in [`interlnk_protocol_spec.md`](interlnk_protocol_spec.md);
the DOSBox configuration is in [`../dosbox_x/README.md`](../dosbox_x/README.md).

**Not included in the distribution:** the MS-DOS 6.22 disk images
(`dosbox_x/images/`) and the recorded traffic (`logs/`). To run the live
tests you need your own licensed copy of MS-DOS 6.22 installed on DOSBox
disk images. Recordings of your own sessions are made with `-trace`.

## Status

Both roles have been run live against the real programs, at 9600 up to
115200 baud:

| Check | Status |
|---|---|
| Byte-exact replay of recorded real LNKCLIENT <-> LNKSERVER sessions, both roles | passes |
| pyterlink client <-> pyterlink server, end to end, no DOS | passes |
| Directory drives: DOS-style changes checked against the Linux tree | passes |
| pyterlink client <-> real LNKSERVER (DOSBox) | verified live at 115200 |
| pyterlink server <-> real LNKCLIENT (DOSBox) | verified live at 115200 |

The first three checks use development tools that are not part of the
distribution.

There is no unit-test suite: behavior is checked against the real
programs.

## The rig

- Two USB serial adapters joined by a null-modem cable: `/dev/ttyUSB0`
  (FTDI) and `/dev/ttyUSB1` (PL2303). Plug them **straight into the
  computer, not into a hub**: behind a USB 3.0 hub the FTDI adapter lost
  bytes ("ttyUSB0: input overrun" in `dmesg`) and the link ran slower; on
  the computer's own ports both roles run at the full 115200 line rate with
  no overruns.
- Two DOSBox setups in `dosbox_x/`. Each gives DOS one adapter as COM2:

  | DOSBox | runs | its COM2 | pyterlink on the other end |
  |---|---|---|---|
  | `dosbox_x/run-lnkserver.sh` | LNKSERVER | `/dev/ttyUSB0` | **client** on `/dev/ttyUSB1` |
  | `dosbox_x/run-lnkclient.sh` | LNKCLIENT | `/dev/ttyUSB1` | **server** on `/dev/ttyUSB0` |

  Run one role at a time: each pyterlink role uses the port the other
  DOSBox would open.

## Prerequisites (once)

```sh
cd pyterlink                             # all commands below run from here
pip install -e .
sudo usermod -aG dialout $USER           # serial port access; log out and in
sudo mkdir -p /mnt/lnkclient
```

- `dosbox` (apt, 0.74-3) or the `dosbox-x` snap with raw USB access
  (`sudo snap connect dosbox-x:raw-usb`), and `mtools` to look at or edit
  the DOS images.
- Your own MS-DOS 6.22 images (not included): run `./configure_rig.sh`
  with `LNKSERVER_IMAGE` and `LNKCLIENT_IMAGE` set to write their paths,
  geometry and the serial ports into `dosbox_x/*.conf` (`-n` shows the
  changes first; see `dosbox_x/README.md`). The confs run at
  `cycles=fixed 40000` (5000 caused line errors at 115200).
- The startup files in `dosbox_x/` start LNKSERVER with `/BAUD:9600` and
  LNKCLIENT with `/BAUD:115200`. To change a rate, edit `AUTOEXEC.BAT`
  (LNKSERVER) or `CONFIG.SYS` (LNKCLIENT);
  `dosbox_x/README.md`, "Editing the images", shows how with mtools.
  Check yours:
  `mtype -i "$LNKCLIENT_IMAGE"@@32256 ::/config.sys`.
- Root for the client's `-attach` (after `sudo modprobe nbd`), for `mount`
  and `umount`, and for `tools/serial_latency.sh`.

## Before each session

```sh
git pull                                  # the version to test
python3 -m pyterlink.server.cli -version  # and check it
sudo tools/serial_latency.sh              # FTDI: 1 ms latency; after every replug
dmesg -w | grep -i ttyUSB                 # in another terminal: overruns?
```

The FTDI default latency of 16 ms costs about 15 ms per turnaround at
115200.

## Server role: pyterlink-server <-> the real LNKCLIENT

LNKCLIENT's DOSBox has COM2 = `/dev/ttyUSB1`; the server uses
`/dev/ttyUSB0`. LNKCLIENT is a `CONFIG.SYS` device driver, so **start the
server first**: LNKCLIENT looks for its server while DOS boots (or run
`INTERLNK` at the DOS prompt afterwards to reconnect).

### A disk image

```sh
cp some-fat16.img /tmp/served.img     # never serve an image a DOSBox has open
python3 -m pyterlink.server.cli C:=/tmp/served.img -port /dev/ttyUSB0 -baud 115200 -v
```

In another terminal: `dosbox_x/run-lnkclient.sh`. Expected: LNKCLIENT's boot
message and the `INTERLNK` status table list a drive redirected to the
server's C:; `DIR` on that drive lists the image's root directory. The
line runs at LNKCLIENT's `/BAUD:` rate after the 9600 handshake.

### A Linux directory (read-write)

```sh
mkdir -p /tmp/c-drive && cp rand.bin /tmp/c-drive/      # what DOS will see
python3 -m pyterlink.server.cli C:=/tmp/c-drive -port /dev/ttyUSB0 -baud 115200 -v -trace logs/live_server.log
```

In another terminal: `dosbox_x/run-lnkclient.sh`. When DOS has booted,
`INTERLNK` at the prompt shows the redirected drive (e.g. `D: equals C:`).
Then, for example:

```
COPY D:\RAND.BIN C:\TEMP
XCOPY D:\*.BIN C: /A/S/E/Y
CD \TEMP
XCOPY *.BIN D:\TEMP /A/S/Y/V
```

Expected:

- `DIR` on the redirected drive lists the directory, long and lower-case
  Linux names as their 8.3 short names; `TYPE` shows contents.
- On the DOS side, `COPY` a file in, `MD` a directory and copy into it,
  `REN`, `DEL`, `ATTRIB +R`, `MOVE` a file between directories, and `COPY`
  over an existing file. About 2 s after the last write, `ls -l
  /tmp/c-drive` shows each change: new and renamed files under DOS's
  upper-case 8.3 names, modified files under their Linux names, DOS dates
  as mtimes, read-only as no write permission. Compare file contents with
  `md5sum`.
- The server logs each file it writes (`-v`). Nothing named
  `.pyterlink-*` is left in the directory after the server stops.
- The other direction: add, delete and change files in `/tmp/c-drive` on
  Linux. Within about a second (with the DOS side idle) the server logs
  "changed on the server side: n added, ..." and the next `DIR` on DOS
  shows the new state (the server answers DOS's media check "don't know",
  so DOS re-reads the FAT and directories).

Use `-ro` for a read-only drive. The server's screen shows the drives, the
file being read or written and the log; `Q` or `Esc` quits.

## Client role: pyterlink-client <-> the real LNKSERVER

LNKSERVER's DOSBox has COM2 = `/dev/ttyUSB0`; the client uses
`/dev/ttyUSB1`. **Start LNKSERVER first**: `dosbox_x/run-lnkserver.sh` (its
`AUTOEXEC.BAT` starts `INTERSVR C:`). To also offer a floppy as A:, add
`imgmount 0 /abs/path/floppy.img -t floppy -fs none` before `boot -l c` in
`lnkserver-msdos.conf`. Then:

```sh
python3 -m pyterlink.client.cli -port /dev/ttyUSB1 -baud 115200 -status   # what it offers
sudo modprobe nbd
sudo python3 -m pyterlink.client.cli -port /dev/ttyUSB1 -baud 115200 -unit 2 -rw -attach /dev/nbd0 -v -trace logs/live_attempt.log &
sudo mount -t msdos -o uid=$(id -u),gid=$(id -g),flush /dev/nbd0 /mnt/lnkclient
```

- `-status` prints the drive map LNKSERVER offers; LNKSERVER's screen shows
  the connection.
- `-unit 2` is LNKSERVER's C:; `-rw` allows writes. Without `-rw`, mount
  with `-o ro`.
- `uid=`/`gid=` make the files yours: an msdos file system has no owners,
  so without them everything belongs to root and copying *into* the mount
  needs `sudo`. Unmounting still needs `sudo`.
- `flush` starts writing each file to the link as soon as it is closed. On
  Linux before 6.2 (e.g. Ubuntu 22.04's 5.15) `cp` returns before the data
  has crossed the link, and without `flush` the data can sit in memory
  ~30 s before it starts; the client warns about this at attach.

```sh
cp 128kb.bin 1mb.bin /mnt/lnkclient/temp/          # writes over the link
rsync -av /mnt/lnkclient/dos/mwbackup.hlp /tmp/     # reads over the link
sudo umount /mnt/lnkclient                         # waits until every write is done
sudo pkill -INT -f pyterlink.client.cli           # then stop the client
```

**Reading back what you just wrote does not use the link.** Linux keeps
the data it just wrote in memory and the client keeps the sectors it sent,
so `rsync`/`cp` of those files out of the mount finishes at once. To read
them over the serial link, unmount, stop the client, start it again and
mount again; then read them.

Always `umount` before stopping the client: the umount is what waits for
the last writes. **If pyterlink-server or pyterlink-client stops
suddenly** (Ctrl-C, `kill`, a crash, a closed terminal) while something is
being written, a disk may be left with lost chains or other damage: which
disk to check, and how (SCANDISK, CHKDSK, NDD, `fsck.fat`), is in
[`abort_recovery.md`](abort_recovery.md), section 0. In short: an image the
server was serving, or LNKSERVER's disk when the client stopped before
`umount` finished.

## After the session: the recordings

`-trace` appends every byte on the wire to the file named, with a header
line giving the pyterlink version and the command line. Compress the trace
under a name with the UTC time:

```sh
gzip -c logs/live_server.log > logs/live_server_$(date -u +%Y-%m-%dT%HH%MTZ00H00).log.gz && rm logs/live_server.log
```

To look at a run: `python3 tools/decode_pyterlink_capture.py
logs/<file>.log.gz` lists every request and reply; `python3
tools/capture_frames.py logs/<file>.log.gz` lists every frame with its time.

## Capturing traffic

- `-trace file` on either pyterlink program appends every wire byte to
  `file` in the capture format.
- DOSBox to DOSBox over a TCP null-modem: `tools/com3_logging_proxy.py`
  ([`com3_logging_proxy.md`](com3_logging_proxy.md)). That path has no line
  rate, so no rate steps appear in it.
- The DOS console of a DOSBox can be driven over its serial console port
  through `tools/console_proxy.py`
  ([`console_proxy.md`](console_proxy.md)). DOSBox gives the console's TCP
  port to the **first** connection only: connect only through the proxy,
  and never probe the console port itself (for example with `nc -z`), or
  the proxy loses the console.

Decode captures with `tools/decode_pyterlink_capture.py` and
`tools/capture_frames.py`.

### Reference session: real LNKCLIENT <-> real LNKSERVER

The reference captures of the two DOS programs talking to each other were
made with both proxies running:

- COM3 logging proxy (TCP 11430 -> 11330): the handshake and the
  installation check on COM3; written to `logs/com3_traffic.log`.
- Console proxy (TCP 11234 -> 11233): every file operation on `D:\`, run
  through the console connection; written to `logs/console_traffic.log`.

Commands run on `D:\` (the redirected drive) through the console proxy,
all completed:

| Command |
|---|
| `DIR D:\TEMP` |
| `ATTRIB D:\TEMP\*` |
| `ATTRIB D:\TEMP\HELP.COM -A +H` |
| `ATTRIB D:\TEMP\NE* -A +H` |
| `COPY D:\TEMP\HELP.COM D:\TEMP\NEWNAME.COM` |
| `COPY D:\TEMP\HELP.COM D:\TEMP\NEWNAME` |
| `ATTRIB D:\TEMP\NEWNAME.COM` (8.3 name) |
| `COPY D:\TEMP\HELP.COM D:\TEMP\NEWN` |
| `ATTRIB D:\TEMP\NE*` |
| `ATTRIB D:\TEMP\NEWN -A +H` |
| `COPY D:\TEMP\NEWN D:\TEMP\COPYTEST` |
| `DIR D:\TEMP` (check) |
| `REN D:\TEMP\COPYTEST RENAMED` |
| `DIR D:\TEMP` (check the rename) |
| `DEL D:\TEMP\RENAMED` |
| `TYPE D:\TEMP\HELP.COM` |
| `MD D:\TEMP\NEWDIR`, then `RD D:\TEMP\NEWDIR` |
| `COPY D:\TEMP\HELP.COM D:\TEMP\NEWNAME.COM` |
| `REN D:\TEMP\COPYTEST RENAMED` |
| `DIR D:\TEMP` (check) |
| `DEL D:\TEMP\RENAMED` |
| `TYPE D:\TEMP\HELP.COM` (binary file) |

## What "wire compatible" means here

1. pyterlink's client completes the handshake, rate steps, install, drive
   map, drive info, media check and build BPB with the real LNKSERVER, and
   reads (and with `-rw` writes) sectors.
2. The real LNKCLIENT does the same against pyterlink's server, and DOS on
   the LNKCLIENT side can list, read and write the served drive.
3. Every frame in a capture of the session passes the CRC-16/GENIBUS
   check, and replaying the capture through pyterlink reproduces the
   captured bytes in both roles.

## Known limits

- With both adapters on the computer's own ports, both roles run at the
  full 115200 line rate (~11 KB/s) with 127-sector requests. Behind a USB
  hub the FTDI adapter overran and 8 KB bursts were lost; the client then
  falls back to smaller writes and the server slows its sending (spec
  section 9).
- On Linux before 6.2 a `cp` to an `-attach` mount returns before the data
  has crossed the link (mount with `flush`; `umount` waits).
- Frame retransmission (tag bit 2), accepting LNKSERVER's rate probe and
  printer redirection were never seen in a capture (spec sections 6.12 and
  10).
