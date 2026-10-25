# dosbox_x/ -- real-hardware DOS rig

Rig for running pyterlink against the real MS-DOS 6.22
`INTERSVR.EXE` / `INTERLNK.EXE` over real serial hardware. This
directory holds the bootable hard-disk images and the dosbox/dosbox-x
configs that boot them. The two DOS binaries are not stored as loose
files here; they are on the images at `C:\DOS\`. The protocol itself
is in `docs/interlnk_protocol_spec.md`; the procedure is in
`docs/test_procedure.md`.

## Hardware and port assignment

`/dev/ttyUSB0` and `/dev/ttyUSB1` are wired together with a null-modem
cable. Each DOSBox exposes one of them to DOS as **COM2**
(`directserial`):

| Conf | DOS program | DOS COM2 = | pyterlink partner (other end of the cable) |
|---|---|---|---|
| `lnkserver-msdos.conf` | INTERSVR | `/dev/ttyUSB0` | pyterlink **client** on `/dev/ttyUSB1` |
| `lnkclient-msdos.conf` | INTERLNK | `/dev/ttyUSB1` | pyterlink **server** on `/dev/ttyUSB0` |

Run one DOSBox at a time against pyterlink: each pyterlink role uses
the port the other DOSBox would open. Both roles have been verified
live at 115200. The user running DOSBox and pyterlink needs access to
the ports: membership of the `dialout` group (`sudo usermod -aG dialout
$USER`, then log out and in).

Plug both adapters straight into the computer. Behind a USB 3.0 hub
the FTDI adapter (`ttyUSB0`) dropped received bytes (dmesg: "ttyUSB0:
N input overrun(s)"), which lost INTERSVR's 8 KB write bursts and
slowed the link; on the computer's own ports there are no overruns and
both roles run at the full 115200 line rate. Run
`sudo ../tools/serial_latency.sh` after every replug (it resets the
FTDI latency timer to 16 ms).

`run-lnkclient.sh`'s comments and error message say it opens
`/dev/ttyUSB0`; its conf opens `/dev/ttyUSB1`. The confs decide (table
above); the scripts are left as they are.

The other serial ports in the confs are DOSBox TCP `nullmodem` ports
(see `docs/dosbox-serial.md`):

| Port | `lnkserver-msdos.conf` | `lnkclient-msdos.conf` |
|---|---|---|
| COM1 | listens on TCP 11232 (console) | listens on TCP 11233 (console; `../docs/console_proxy.md`) |
| COM3 | listens on TCP 11330 | connects to TCP 11430 (`../docs/com3_logging_proxy.md`) |
| COM4 | listens on TCP 11331 | connects to TCP 11231 |

COM3 is the DOSBox-to-DOSBox path used for the session-3 captures
(`logs/com3_traffic_*.log.gz`). A TCP nullmodem has no line rate.

## Files

- `lnkserver-msdos.img`, `lnkclient-msdos.img` (your own; their paths go in
  the confs, see "Paths" below) --
  bootable MS-DOS 6.22 hard-disk images, 51,609,600 bytes each
  (100,800 sectors: 100 cylinders x 16 heads x 63 sectors). One
  partition (type 06, FAT16) starts at sector 63 and is 100,737
  sectors long (byte offset 32256). The images hold MS-DOS 6.22 and are
  **not part of the distribution**: build your own from a licensed copy.
- `lnkserver-msdos.conf`, `lnkclient-msdos.conf` -- the DOSBox configs
  (one per image).
- `run-lnkserver.sh`, `run-lnkclient.sh` -- launch wrappers.
- `stop-lnkserver.sh`, `stop-lnkclient.sh` -- stop the DOSBox a wrapper
  started.
- `lnkclient-CONFIG.SYS`, `lnkclient-AUTOEXEC.BAT`, `lnkserver-CONFIG.SYS`,
  `lnkserver-AUTOEXEC.BAT` -- copies of the startup files on the two
  images (DOS CRLF line endings), for building your own images.
- `monit_abcd_config.rc`, `foo.sh`, `foo-kill.sh` -- optional monit
  trigger-file setup (see "Optional: start and stop DOSBox from monit").

### Startup files

The images are not included. Copies of their startup files are in this
directory (DOS CRLF line endings), for building your own images:

- `lnkclient-CONFIG.SYS` loads `DEVICE=C:\DOS\INTERLNK.EXE /NOPRINTER /COM /BAUD:115200`
  (a `REM`'d line keeps a `/COM:1 /BAUD:115200 /V` variant). `/COM`
  with no number lets INTERLNK search the COM ports for a server.
- `lnkclient-AUTOEXEC.BAT` sets COM1 to 9600 8N1 and ends with `CTTY COM1`,
  so the DOS console is on COM1 (TCP 11233).
- `lnkserver-CONFIG.SYS` is HIMEM/EMM386/SETVER, no INTERLNK.
- `lnkserver-AUTOEXEC.BAT` ends with `C:\DOS\INTERSVR.EXE C: /COM /BAUD:9600`
  (a `REM`'d line keeps a `/COM:1 /V /BAUD:115200` variant).

INTERLNK's `/BAUD` sets the highest rate it steps the line up to. The
handshake is always at 9600; INTERLNK then steps the rate up to its own
`/BAUD` (spec section 4). To change a rate, edit these lines (see "Editing
the images").

## Conf details

Check the `.conf` files for the exact current contents. The key
parts:

```
[cpu]
cycles=fixed 40000

[serial]
serial2=directserial realport:ttyUSB0      (lnkserver; ttyUSB1 in lnkclient)

[autoexec]
mount -u c
mount -u d
imgmount 2 "/path/to/lnkserver-msdos.img" -t hdd -fs none -size 512,63,16,100
boot -l c
```

- **Paths:** the MS-DOS images are not included; keep your own anywhere.
  `../configure_rig.sh` (in the project root) writes their paths, their
  geometry and the serial ports into both confs, and the control and monit
  paths into the scripts and the monit template. Set the variables at its
  top, or give them on the command line; `-n` shows the changes first:

  ```sh
  INTERSVR_IMAGE=$HOME/dos/lnkserver-msdos.img \
  INTERLNK_IMAGE=$HOME/dos/lnkclient-msdos.img ./configure_rig.sh -n
  ```

  DOSBox runs in `dosbox_x/` (the run scripts change to it first), so a
  relative image path is read from there.
- **Hard disk:** the image is attached by BIOS drive number (`2` =
  first hard disk) with explicit geometry (`-size 512,63,16,100`),
  then booted with `boot -l c`. Booting a real image means
  `CONFIG.SYS` is processed by the DOS kernel, which INTERLNK needs:
  it is a device driver and must load from `CONFIG.SYS`.
- **Floppy for INTERSVR's A:** attach it by BIOS drive number before
  the `boot` line, not by drive letter:

  ```
  imgmount 0 /abs/path/floppy.img -t floppy -fs none
  ```

- **CPU cycles:** the confs have `cycles=fixed 40000` (both). At
  115200, 5000 caused line errors; 20000, 30000 and 40000 all work.

## Running

The full procedure for a test session -- which program to start first,
the pyterlink command lines with `-trace`, mounting, and saving the
logs -- is in `../docs/test_procedure.md`. In short:

```bash
# Real INTERSVR on ttyUSB0; then the pyterlink client on ttyUSB1:
./dosbox_x/run-lnkserver.sh
python3 -m pyterlink.client.cli -port /dev/ttyUSB1 -baud 115200 -status

# Real INTERLNK on ttyUSB1; start the pyterlink server on ttyUSB0 first:
python3 -m pyterlink.server.cli C:=/tmp/c-drive -port /dev/ttyUSB0 -baud 115200 -v
./dosbox_x/run-lnkclient.sh
```

The client can also attach the server drive as a Linux block device
(`-attach /dev/nbd0`, as root, after `sudo modprobe nbd`) and Linux mounts
it with `sudo mount -t msdos -o uid=$(id -u),gid=$(id -g),flush /dev/nbd0
/mnt/lnkclient`. See `pyterlink/README.md` for all options. Do not serve an
image that a running DOSBox has open read-write; serve a copy, or use
`-ro`.

The wrappers take an optional engine argument (`dosbox`, the default,
or `dosbox-x`), `cd` to this directory, and pipe the engine's output
through `../tools/klog_tee.py -a` into `lnkserver.log` / `lnkclient.log` here.
They also touch a start marker in a control directory (see the next
section). The confs can also be run directly from this directory:
`cd dosbox_x && dosbox -conf lnkserver-msdos.conf`.

### Optional: start and stop DOSBox from monit

The rig can start and stop the INTERSVR DOSBox when a trigger file
appears, so a script or another account can drive it without a
terminal. [monit](https://mmonit.com/monit/) watches a shared control
directory (called `CONTROL` below), readable and writable by a group that
every account using it belongs to. The scripts use `$PYTERLINK_CONTROL`
when it is set and `~/.control` otherwise:

| Trigger file | monit runs |
|---|---|
| `CONTROL/dosbox-run-lnkserver.txt` | `dosbox_x/run-lnkserver.sh` (boots the INTERSVR image, then touches `CONTROL/dosbox_lnkserver_started.txt`) |
| `CONTROL/dosbox-stop-lnkserver.txt` | `dosbox_x/stop-lnkserver.sh` (finds the DOSBox started with `lnkserver-msdos.conf`, sends it SIGTERM with `sudo`, and removes the trigger and marker files) |

`monit_abcd_config.rc` is a template for this: a 30-second cycle (first
check after 30 s), a log file, and monit's web interface on
`localhost:2812`. To use it:

1. Run `../configure_rig.sh` with `CONTROL_DIR` (the control directory)
   and `MONIT_DIR` (where monit keeps its control file, log, id and state
   files; default `~/monit`) set, or replace the example paths in
   `monit_abcd_config.rc` (`/home/abcd/...`) by hand. monit does not
   expand variables in its configuration, and the programs it starts may
   have no `$HOME`, so the template passes the control directory to the
   scripts with `/usr/bin/env PYTERLINK_CONTROL=...`.
2. Copy the template to `MONIT_DIR/monitrc`; monit requires mode 600
   (`chmod 600 ~/monit/monitrc`).
3. Create the control directory with group read and write access, and
   add each account that uses it to that group.
4. Start monit inside the graphical session, so the DOSBox window can
   open:

   ```sh
   DISPLAY=:0 XAUTHORITY=$HOME/.Xauthority monit -c $HOME/monit/monitrc -I
   ```

5. Start and stop DOSBox by creating the trigger files:
   `touch CONTROL/dosbox-run-lnkserver.txt`.

`foo.sh` and `foo-kill.sh` are examples of a lock-file variant that
allows a stop only after a start: `foo.sh` refuses to run while
`CONTROL/.run.lock` exists and otherwise creates it; `foo-kill.sh`
refuses unless the lock exists, then removes it and both trigger files.
The DOSBox calls in them are stubs, and the monit template does not use
them.

If DOSBox's DOS console is connected to a TCP serial port (see "What the
images run"), DOSBox gives that port to the **first** connection only.
Do not probe it (for example with `nc -z`); connect through
`tools/console_proxy.py` instead.

On the INTERLNK side, running `INTERLNK` at the DOS prompt shows the
"This Computer / Other Computer" drive table once a server is
connected.

Plain `dosbox` (apt, 0.74-3) has none of dosbox-x's `-exit` /
`-fastlaunch` / `-nogui` / `-time-limit` flags; close the window or
type `exit` when done.

## Snap confinement (dosbox-x)

The `dosbox-x` snap is strictly confined. Until its `raw-usb` plug is
connected, opening `/dev/ttyUSBn` with `directserial` fails with:

```
Serial2: Serial Port "ttyUSB0" could not be opened.
System error 9.
```

Fix (once):

```bash
sudo snap connect dosbox-x:raw-usb
```

The `serial-port` plug has no slot for a generic USB-serial adapter,
so `snap connect dosbox-x:serial-port` fails; it is not needed.
Classic `/usr/bin/dosbox` is not sandboxed and needs none of this.

## Editing the images

mtools reads the FAT partition at byte offset 32256 (sector 63); below,
`INTERSVR_IMAGE` and `INTERLNK_IMAGE` hold the paths of your images, as
for `configure_rig.sh`:

```bash
mtype -i "$INTERLNK_IMAGE"@@32256 ::/config.sys
mtype -i "$INTERSVR_IMAGE"@@32256 ::/autoexec.bat

# Write a file back with DOS line endings, e.g. INTERSVR at 115200:
printf '...\r\nC:\\DOS\\INTERSVR.EXE C: /COM /BAUD:115200\r\n' \
  | mcopy -i "$INTERSVR_IMAGE"@@32256 -o - ::/autoexec.bat
```

Do this only while no DOSBox has the image open.

To build your own images: install MS-DOS 6.22 on a DOSBox hard-disk image
(100 cylinders x 16 heads x 63 sectors, one FAT16 partition from sector
63; `INTERLNK.EXE` and `INTERSVR.EXE` are in `C:\DOS\` after a standard
install), make two copies, and write the startup files from this
directory onto them:

```bash
mcopy -i "$INTERLNK_IMAGE"@@32256 -o lnkclient-CONFIG.SYS ::/CONFIG.SYS
mcopy -i "$INTERLNK_IMAGE"@@32256 -o lnkclient-AUTOEXEC.BAT ::/AUTOEXEC.BAT
mcopy -i "$INTERSVR_IMAGE"@@32256 -o lnkserver-CONFIG.SYS ::/CONFIG.SYS
mcopy -i "$INTERSVR_IMAGE"@@32256 -o lnkserver-AUTOEXEC.BAT ::/AUTOEXEC.BAT
```

## Parallel port: not in scope

INTERSVR and INTERLNK also support LPT links. This rig has no LPT
ports, and pyterlink's `pyterlink/transport/parallel_backend.py` is a
stub (D-047). The confs and images here are serial-only.

## Audio

Sound Blaster, Gravis Ultrasound and MIDI are disabled in both confs;
only the PC speaker is on. This also silences the ALSA/fluidsynth
warnings on every launch.
