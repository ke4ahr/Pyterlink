# Aborted transfers: what can be left damaged, and how to repair it

**If pyterlink-server or pyterlink-client exits suddenly**, start with the
first section below: it says what to check for lost chains and other disk
damage, where, and with which command. "Exits suddenly" means:
- Ctrl-C, `kill`, a crash, a closed terminal or SSH session;
- the machine running it stops.

The later sections give the reasons, and cover the other ways a transfer
can be cut off: a pulled cable, LNKSERVER quit with Alt+F4, *Abort* at DOS's
"Abort, Retry, Fail" prompt, or a DOS machine or DOSBox reset.

Evidence marks:
- **[V]** verified in pyterlink's code or a live trace;
- **[D]** documented in the MS-DOS 6.22 help files in this directory;
- **[I]** inferred, not tested.

## 0. pyterlink exited suddenly: what to check

**Rule of thumb:** a disk needs checking only if something was being
**written** to it when pyterlink stopped. Reads never damage anything.

### pyterlink-server (DOS runs LNKCLIENT and writes to your drive)

| It was serving... | and DOS was... | Check | For |
|---|---|---|---|
| an image (`C:=disk.img`) | writing (COPY, XCOPY, DEL, REN, MD, MOVE onto the drive) | **the image file** | lost chains (lost clusters), files whose size does not match their chain, cross-links, FAT 1 and FAT 2 differing |
| an image | only reading | nothing | |
| a Linux directory (`C:=/path/dir`) | writing | **the directory**, by eye: no disk check applies (its FAT is virtual and rebuilt at the next start) | files from the last ~2 s missing, empty or short; after a hard kill, leftover `.pyterlink-XXXX` temporary files and a `.pyterlink-trash-*` directory (replaced or deleted files from the last minute: look before deleting) |
| a Linux directory | only reading | nothing | |
| anything | copying **from** the drive to its own disk | DOS's own disk only if the DOS machine was then reset or its DOSBox closed; otherwise just delete the partly copied file | lost chains on the DOS disk |

Checking a served **image**: stop pyterlink-server first, then either
- boot the image in DOSBox (LNKCLIENT not loaded) and run `SCANDISK C:`
  (or `CHKDSK C: /F`, or NDD); or
- on Linux:

  ```sh
  sudo losetup -fP --show disk.img    # prints /dev/loopN
  sudo fsck.fat -n /dev/loopNp1       # report (an unpartitioned volume: fsck.fat -n disk.img)
  sudo fsck.fat -a -f /dev/loopNp1    # repair; -f keeps lost chains as files
  sudo losetup -d /dev/loopN
  ```

  With `-f`, `fsck.fat` saves lost chains as `FSCK0000.REC` files in the
  root directory; without it they may just be freed. CHKDSK /F (answer Y to
  "Convert lost chains to files?") and SCANDISK save them as
  `FILE0000.CHK`. Look inside before deleting them.

After Ctrl-C or Q (a clean stop), a directory drive writes back every
complete change first and names anything it could not write in the
warning "closed with changes not written back (client mid-update)" [V].
After `kill -9` or a crash, nothing from the last ~2 s is written back.

### pyterlink-client (Linux has LNKSERVER's drive mounted)

| The mount was... | when the client stopped... | Check | For |
|---|---|---|---|
| read-only (`-o ro`), or nothing written since it was mounted | at any time | nothing; `sudo umount -l /mnt/lnkclient` | |
| read-write | after a completed `umount` | nothing | |
| read-write | with no `umount` after the last write: `cp` had returned, but Linux had not finished sending (on Linux before 6.2 that can be many seconds), or `umount` was still running | **LNKSERVER's disk** | lost chains, sizes that do not match, FAT 1 and FAT 2 differing, the "not properly unmounted" flag; plus files whose data never arrived, which only a checksum finds |

**Ctrl-C of the client counts as a sudden stop.** It disconnects the NBD
device at once, and data Linux had not sent yet is lost [V: `serve_attach`].
Always `umount` first.

Checking **LNKSERVER's disk**, any of:
- quit LNKSERVER (Alt+F4) and run `SCANDISK C:` (or `CHKDSK C: /F`, or NDD)
  on that DOS machine; reboot it first if it is going to write to the disk
  itself (section 4);
- start the client again with `/RW` and **do not mount**:
  `sudo fsck.fat -n /dev/nbd0`, then `sudo fsck.fat -a -f /dev/nbd0`. This
  works over the link: slow, but nothing to move;
- for the rig, with its DOSBox closed: `fsck.fat` on
  `dosbox_x/images/lnkserver-msdos.img` with `losetup -fP` as above.

Then compare checksums of the files that were being copied (`md5sum` on
Linux, `FC /B` on DOS), and recopy any that differ.

## 1. Why aborts damage FAT disks at all

LNKCLIENT moves **sectors**, not files (spec section 1). Copying one file
means three separate kinds of write to the disk that holds it:

1. **the data clusters;**
2. **the FAT**, which records which clusters belong to the file;
3. **the directory entry**, which holds the name, size and first cluster.

DOS and Linux both write these at different moments, in their own order.
Linux also writes the two FAT copies in separate requests (sectors 16 and
220 on the rig's images) [V: live_attempt traces]. A transfer stopped
between those writes leaves a disk on which they disagree:

| Symptom | What it means | Found by |
|---|---|---|
| lost clusters / lost allocation units | FAT marks clusters used, no directory entry owns them | CHKDSK, SCANDISK, NDD, fsck.fat |
| file size does not match its cluster chain | entry written before or after its FAT chain | same |
| cross-linked clusters | two chains share a cluster (rare; a FAT sector from an older state) | same |
| the two FAT copies differ | stopped between FAT 1 and FAT 2 | SCANDISK, NDD, fsck.fat (whether CHKDSK compares them is not checked) |
| a file with the right name and size but wrong contents | entry and FAT written, data clusters not | **nothing**: no disk check sees it; compare checksums |
| "volume was not properly unmounted" on the next Linux mount | the dirty byte (boot sector offset 0x25) is still 1 | Linux at mount; `fsck.fat` clears it |

A single request is never half applied: a write request's data is
CRC-checked as a whole and only then written. pyterlink-server receives
every chunk before it writes [V]. LNKSERVER does too, as far as its code
reads: its dispatcher (1000:061c) collects the pushed data before calling
the write handler (1000:1c67) [I: Ghidra, not traced at runtime]. So the damage is always **between**
requests, never inside a sector.

## 2. Where each repair tool can run

**Drives redirected by LNKCLIENT cannot be checked through the link.**

| Tool | Works on | Not on |
|---|---|---|
| CHKDSK | the DOS machine's own drives | LNKCLIENT drives: "The following commands do not work with the Interlnk server: CHKDSK, DEFRAG, DISKCOMP, DISKCOPY, FDISK, FORMAT, MIRROR, SYS, UNDELETE, UNFORMAT" [D: LNKSERVER help] |
| SCANDISK (MS-DOS 6.22) | the DOS machine's own drives | LNKCLIENT drives: explicitly refused -- "You cannot check or repair drive D because it was created by using INTERLNK." [verified live s71 B3] |
| NDD (Norton Utilities) | the DOS machine's own drives | LNKCLIENT drives: not tested (NDD absent from test image [s71 B3]) |
| `fsck.fat` (`dosfstools`) | an image file, a partition, or `/dev/nbdN` while attached and **not mounted** | a mounted file system |

So repair is done **on the machine that owns the disk**, with that disk not
in use by LNKCLIENT/LNKSERVER or a Linux mount:

- **A DOSBox image**, either the DOSBox's own C: or an image pyterlink-server
  serves. There are two ways to check it:
  - Boot it in DOSBox (with LNKCLIENT/LNKSERVER not running) and run
    `SCANDISK C:` (or `CHKDSK C: /F`, or NDD).
  - On Linux, with no DOSBox and no pyterlink using it:

    ```sh
    sudo losetup -fP --show image.img    # prints /dev/loopN; the partition is /dev/loopNp1
    sudo fsck.fat -n /dev/loopNp1        # report only
    sudo fsck.fat -a -f /dev/loopNp1     # repair, lost chains kept as files (or -r to be asked)
    sudo losetup -d /dev/loopN
    ```

    A floppy image or an unpartitioned volume takes `fsck.fat` directly.
- **LNKSERVER's drive, seen from Linux in client role.** With the client
  attached `/RW` and the drive **unmounted**, run `sudo fsck.fat -n
  /dev/nbd0` (or `-a`). It reads and writes LNKSERVER's disk over the link,
  which is slow but works. Alternatively quit LNKSERVER and run SCANDISK on
  that DOS machine.
- **A Linux directory served by pyterlink-server** has no FAT to repair. The
  FAT is virtual and is rebuilt from the directory each time the server
  starts (section 3.2).

## 3. Server role: pyterlink-server <-> the real LNKCLIENT

### 3.1 Serving an image (`C:=disk.img`)

pyterlink writes each request to the image file as it arrives [V:
`Unit.write`]. Killing pyterlink does not lose what was already written,
because the data is in Linux's cache. Only a crash or power loss of the
Linux machine can lose it.

| Abort while DOS was... | Left on the image | Repair |
|---|---|---|
| reading (DIR, TYPE, COPY from the drive) | nothing | none |
| writing a file (COPY, XCOPY onto the drive) | a partial file: lost clusters, or an entry with size 0 or the wrong size; possibly FAT copies differing | stop the server, then SCANDISK/CHKDSK /F/NDD in a DOSBox that boots that image, or `fsck.fat` on Linux (section 2); delete and recopy the file |
| deleting, renaming, MD/RD, MOVE | half-done: an entry gone but its clusters still allocated (lost clusters), or an entry pointing to freed clusters | same |
| answering *Abort* at "Abort, Retry, Fail" | DOS abandons the operation where it stood: as the rows above | same |
| answering *Fail* / *Ignore* | DOS goes on without that sector: the file or the FAT can be wrong where it was meant to be written | same; check the file's contents |

The image may also be open in a DOSBox, or mounted on Linux, while
pyterlink serves it. Two writers corrupt it whether or not anything is
aborted. Never do this: serve a copy, or use `/RO`.

### 3.2 Serving a Linux directory (`C:=/path/dir`)

DOS's writes go into an overlay: memory plus a temporary file that is
deleted when the server exits. After 2 s with no writes, the server writes
the result back to the directory. It never writes back a state that is
half done: a broken or shared cluster chain, or a chain too short for its
file. Such entries stay as they were on Linux [V: dirsync.py].

| Abort | Left behind | Repair |
|---|---|---|
| link lost or DOS reset/killed mid-write; server still running | the overlay holds DOS's half-done state; consistent parts are written back, half-done files stay as they were on Linux. Lost clusters in the overlay are harmless and disappear when the server restarts | none on Linux. If DOS is still running, check the files with DIR; recopy anything that was being written. A file DOS had created but not finished may appear on Linux empty or short: delete or recopy it |
| pyterlink-server quit cleanly (Q, Esc, Ctrl-C) mid-write | it writes back what is consistent and logs "closed with changes not written back (client mid-update)" for the rest [V: `close()`] | recopy what that warning covers |
| pyterlink-server killed hard (`kill -9`, crash) | DOS's changes from the last ~2 s, and any half-done ones, are lost. A rewrite in progress can leave a `.pyterlink-XXXX` temporary file, and a `.pyterlink-trash-*` directory may remain (files replaced or deleted in the last minute) [V] | no disk check. With the server stopped, delete `.pyterlink-*` leftovers by hand (look inside the trash directory first: it holds replaced/deleted files). The server skips such names and warns about them meanwhile |
| Linux machine crashes | its own file system (ext4, ...) is checked at boot as usual | the host's `fsck`, automatically |

**After a server restart, DOS may still hold old FAT and directory sectors
for the drive.** pyterlink rebuilds the drive from the directory, so cluster
numbers can change. Whether LNKCLIENT makes DOS discard its buffers when it
reconnects to a new server: **confirmed** -- LNKCLIENT discards its DOS directory
buffers on reconnect; files added while the server was down are visible
immediately after reconnecting [verified live s71 B1]. Run `INTERLNK` and
then `DIR` on the drive after reconnecting to ensure the fresh layout is seen
before writing to it again. Writing from stale
buffers would put DOS's idea of the FAT into the overlay; that state could
then be written back as wrong files.

### 3.3 The LNKCLIENT machine's own disks

Copies from the served drive **to the DOS machine's own disk** (`XCOPY
D:\*.BIN C:`) behave like any interrupted DOS copy:

- **Abort answered at the DOS prompt:** the program ends and DOS closes its
  files. The last file is left short (FAT consistent); delete it.
- **DOS machine reset or powered off, or its DOSBox killed:** DOS's
  unwritten buffers (BUFFERS=, and SMARTDRV write-behind if loaded) are
  lost. That leaves lost clusters or a wrong size on the local disk.
  Repair: SCANDISK C: (or CHKDSK C: /F, or NDD) on that machine. For the
  rig's DOSBox: boot it and run SCANDISK, or `fsck.fat` its image as in
  section 2.

## 4. Client role: pyterlink-client <-> the real LNKSERVER

Linux mounts the LNKSERVER machine's drive as `msdos`. Linux decides what to
write and when: data and metadata wait in its page cache. On Linux before
6.2 that can be many seconds after `cp` returned. `umount` writes
everything out and clears the dirty byte [V: live trace, boot sector 0x25
set at mount, cleared at umount].

pyterlink-client by itself is robust to short interruptions. If the link
drops or LNKSERVER is restarted, it re-establishes the link and resends the
request [V]; nothing is lost while it can reconnect. Damage happens when
the writes **stop for good** before `umount` finishes:

| Abort | Left on LNKSERVER's disk | Repair |
|---|---|---|
| reading only (`-o ro` mount, or nothing written) | nothing | none |
| client killed, cable pulled for good, LNKSERVER quit (Alt+F4) or its DOSBox closed, with writes not yet out (`cp` done but no `umount`, or during `umount`) | whatever requests had arrived: data without FAT or directory (lost clusters), FAT 1 without FAT 2 (FAT copies differ), entries with wrong sizes, files with unwritten data; the dirty byte still set | quit LNKSERVER, then `SCANDISK C:` on that machine (it also compares the FAT copies). Or keep LNKSERVER running, attach the client `/RW` without mounting, and `sudo fsck.fat -a -f /dev/nbd0`. Or `fsck.fat` the DOSBox image (section 2). Check the copied files' checksums: a file whose data never arrived can pass every disk check |
| the LNKSERVER machine reset or powered off, or its DOSBox killed | as above, plus the DOS machine's own cache if any (SMARTDRV write-behind) | as above |
| Linux crashes or the client is killed during a write | as above; Linux's own disks are not involved | as above |

On the Linux side:
- **A dead NBD device:** after a hard abort the mount stays on a device that
  no longer answers. Its requests fail with I/O errors once the NBD
  timeout runs out (600 s, `ATTACH_TIMEOUT`), or at once if the client
  process has exited. Run `sudo umount -l /mnt/lnkclient` to drop the mount;
  a normal `umount` may wait for the dead device. Nothing on the Linux machine's own disks needs checking.
- **The next mount after an abort:** the dirty byte is still set, so Linux
  warns that the volume was not properly unmounted. MS-DOS 6.22 ignores the
  byte. `fsck.fat` clears it.

**After LNKSERVER quits, the LNKSERVER machine's DOS may still hold old copies
of FAT and directory sectors** from before the session. LNKSERVER writes
through the disk driver, not through DOS's buffers [V: 1000:1c67 builds a
driver request], so DOS does not see the changes Linux made. Whether
LNKSERVER flushes DOS's buffers when it exits: **confirmed** -- the DOS machine
immediately shows "Not ready reading drive D / Abort, Retry, Fail?" when
LNKSERVER exits while LNKCLIENT is active [verified live s71 B2].
Reboot the LNKSERVER machine before writing to the disk from DOS itself.
This applies even without an abort; after an abort it matters more, because
SCANDISK/CHKDSK must read the disk as it really is.

## 5. Quick reference

| You stopped... | while... | Check |
|---|---|---|
| anything | only reading | nothing |
| pyterlink-server (image) | DOS was writing | the served image: SCANDISK in DOSBox or `fsck.fat` (stop the server first) |
| pyterlink-server (directory) | DOS was writing | no disk check; recopy the files in progress; remove `.pyterlink-*` leftovers after a hard kill |
| the LNKCLIENT DOS machine / its DOSBox | DOS was writing to its own disk | that disk: SCANDISK / CHKDSK /F / NDD |
| the LNKCLIENT DOS machine / its DOSBox | DOS was writing to a served image | the served image, as above |
| pyterlink-client, the cable, LNKSERVER | Linux had unwritten data (before `umount` completed) | LNKSERVER's disk: SCANDISK there, or `fsck.fat` on `/dev/nbd0` (unmounted) or on its image; verify checksums |
| the Linux machine (crash) | anything | its own disks (automatic), plus the row above for whatever role was running |

## 6. Avoiding all of it

- Client role:
  - `umount` before stopping the client, and let it finish.
  - Mount with `-o flush` on kernels before 6.2, so writes start at once.
  - Run `sync` before anything risky.
- Server role:
  - Let DOS's command finish, and wait 2 s, before stopping pyterlink-server.
  - Quit it with Q, not `kill`.
- Answer *Retry* rather than *Abort* at "Abort, Retry, Fail" when the link
  can come back: LNKCLIENT and pyterlink re-establish it.
- Verify important copies with checksums (`md5sum` on Linux; `FC /B` on
  DOS). No disk check can see a file whose data never arrived.
