# INTERLNK / INTERSVR Serial Protocol Specification

MS-DOS 6.22 `INTERLNK.EXE` (client, a block-device driver) and `INTERSVR.EXE`
(server), serial link. Status: **reverse-engineered, implemented and verified**
(pyterlink, sessions s53-s57).

## 0. Sources and confidence

Everything here comes from the two programs themselves (D-036): wire captures
and Ghidra. pyterlink's code is a *product* of this document and is not
evidence for it.

| Source | What it shows |
|---|---|
| `logs/com3_traffic_*.log.gz` (session 3) | Full INTERLNK<->INTERSVR traffic through a TCP proxy at a fixed rate: establishment, DIR, COPY, ATTRIB; reads and writes |
| `logs/strage_intersvr.log.gz`, `logs/strace_interlnk*.log.gz` | strace of both DOSBox processes' serial syscalls: rate changes (termios) and every byte, with INTERLNK's /BAUD, cycles, power and cable varied |
| `logs/live_*.log.gz` | pyterlink client against real INTERSVR, and pyterlink server against real INTERLNK, at 9600-115200 |
| Ghidra project `InterLinkRE` | Frame receive `LinkProtoHandler` 1000:7257 (seq check, per-byte timeout), CRC init (`MOV CX,0xffff` at 1000:728f), echo formula `NOT AH; AND 0xFB` at 1000:7289 |

Confidence tags used below:
- **[V]** verified: replayed byte-for-byte in both roles through pyterlink, and/or run live against the real program.
- **[O]** observed in captures, but its meaning or derivation is not traced.
- **[I]** inferred from the evidence, not directly confirmed.

## 1. Model

INTERLNK is a **remote block device**. The client machine's DOS sees extra
drive letters, and INTERLNK forwards that DOS's device-driver requests to
INTERSVR: media check, build BPB, read sectors, write sectors and generic IOCTL.
INTERSVR performs each request on its own drive and returns the result. The
client's DOS runs the file system (FAT, directories), so **files, names and
directories never appear on the wire, only sectors** [V]. Consequences:

- Long file names are invisible to the protocol. A reader of the sectors sees
  whatever the volume holds.
- A drive whose file system the client's DOS can't read cannot usefully be
  served raw. For MS-DOS 6.22 that means FAT32, ISO 9660 and anything else
  that isn't FAT12/FAT16.
- INTERSVR does not redirect network or CD-ROM (MSCDEX) drives. Its drive scan
  flags redirected drives through DOS's IOCTL "is the drive remote?" query
  [O, Ghidra triage].

## 2. Physical layer

- Asynchronous serial, 8N1, no hardware flow control [V].
- INTERSVR asserts DTR+RTS statically at UART init (MCR=3 via UartMcrInit3 at
  1000:6b8d); lines are never toggled for flow control [Ghidra, s77]. All MCR/MSR
  operations in INTERSVR (UartMcrWaitAck 1000:6979, UartSpeedNegotiate 1000:6b96)
  are UART internal loopback (MCR bit4=LOOP): speed calibration only, not external
  line control [Ghidra, s77].
- INTERLNK has no MSR reads in the serial data path; UartMsrDebounce (1000:1f08)
  is called only from LPT parallel-port functions [Ghidra, s77]. Neither side uses
  RTS/CTS for flow gating. pyterlink does not assert RTS/DTR (D-022).
- Rates: 9600 (handshake), then 19200, 38400, 57600 and 115200 by negotiation (section 4) [V].

DOSBox note (not part of the protocol): DOSBox's `directserial` opens the host
port with termios PARMRK. A received 0xFF therefore reads as `FF FF`, and a
byte with a framing or parity error as `FF 00 x`; a capture taken from an
strace of DOSBox must undo this.

## 3. Link establishment: handshake

INTERLNK sends one byte and waits for each answer [V]:

| INTERLNK | INTERSVR |
|---|---|
| `AA` | `00` |
| `55` | `FF` |
| `5A` | `11` |

- **Always at 9600**, whatever `/BAUD` either side was given. INTERSVR answers `AA` at no other rate [V, live probe at each rate].
- After a handshake the frame sequence number (section 5.2) restarts at 0 [V].
- A handshake at any time abandons the previous session state and starts a new one [V].
- INTERSVR does not search ports. It answers on the port it listens on, and INTERLNK is the one that searches [user, V].

## 4. Line-speed negotiation

Decoded from the strace captures; verified live at 19200-115200 in both roles [V].

### 4.1 Rate steps (INTERLNK-driven)

Immediately after the handshake, INTERLNK raises the rate one step at a time,
from code `04`, until it reaches its own `/BAUD`:

| Code | Rate |
|---|---|
| `04` | 19200 |
| `05` | 38400 |
| `06` | 57600 |
| `07` | 115200 |

The code table is INTERSVR's own: its `/BAUD` parser `CliParseBaud`
(1000:0f92) maps 9600 -> 3, 19200 -> 4, 38400 -> 5, 57600 -> 6 and 115200 -> 7
[O, decompile of 1000:0f92]. The speed names it displays (`"9600 ", "19200 ", "38400 ",
"57600 ", "115200"`, file offset 0x7801) follow the same order. So 9600 is
code `03`, the rate every session starts at.

One step:

```
at the current rate:   L: FF        S: 00        L: <code>
both sides reprogram the UART to the new rate
at the new rate:       S: code<<4   L: code<<4   S: ~(code<<4)
                       then 15 test pairs, L sends / S answers:
```

| Pair | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12 | 13 | 14 | 15 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| L | FF | 00 | 5A | 55 | AA | F0 | 0F | E7 | 7E | C3 | 3C | 81 | 18 | 00 | FF |
| S | 00 | FF | A5 | AA | 55 | F0 | 0F | FF | 00 | 18 | 81 | 3C | C3 | 7E | E7 |

After the last pair, INTERLNK either sends `FF` again (the next step) or sends
the first frame's tag (`80`, the install request), at the new rate. A step takes
about 50-65 ms.

Robustness details [O]:
- INTERSVR repeats `code<<4` about every 7 ms until it is echoed. INTERLNK skips the extra copies. It also skips bytes garbled by the rate switch before the first copy.
- If the byte after `FF`/`00` is not a valid code (garbled, or received with a framing error), INTERSVR makes no step and processes that byte as usual at the current rate. In the capture the next byte was the install frame at 9600.

### 4.2 INTERSVR rate probe

If INTERLNK stopped below INTERSVR's own `/BAUD`, INTERSVR offers the next
code at the client's **first yield** (section 5.4, after the install request) [V]:

```
L: <seq>          (yield)
S: ~<seq>         (e.g. FD for seq 2)  instead of the reply tag
L: <seq>          (yield repeated)
S: <next code>    and INTERSVR switches to that rate
   ... ~0.45 s (8 ticks) of silence: INTERLNK did not follow
INTERSVR switches back and sends the install reply normally
```

A client that never steps up (a 9600-only client) sees this probe with code
`04`.

**INTERLNK probe acceptance -- Ghidra-verified [V, s80]:**
`LinkRecvDispatch` (1000:1b45) exits its read loop when it receives `~seq`
(the probe signal), because `~(~seq & 3) & 3 == seq`. It then calls
`LinkRecvFrame` (1000:1a02) with BH = `~seq` (the full probe byte).

Inside `LinkRecvFrame`:
1. Computes `BL = ~BH = seq`.
2. Transmits `BL` (`seq`) via `LinkCrc16Out` -- this is the **yield repeat**.
3. Reads INTERSVR's offered rate code via the state machine (AL = offered code).
4. Discards the offered code (`MOV AL, BL` at 1000:1a38 overwrites it).
5. Switches to INTERLNK's own max speed code in RAM:0x8ee via `UartMcrBringup`.
6. Sends 16 test pairs at the new rate; waits for echoes within a 6-tick window.

Since INTERSVR offers one step above where INTERLNK stopped, the offered code
is always one above RAM:0x8ee. INTERSVR switches to the offered rate;
INTERLNK switches to its own max (one lower). Baud mismatch -- test pairs
fail -- both sides revert to the pre-probe rate. Effective behavior: always
declines the probe. Capturing a successful acceptance would require both sides
to have the same `/BAUD` and for INTERLNK to have stopped one step short (a
corner case that has not occurred in any captured session).

**pyterlink:** the current `_yield_line()` behavior (read code, stay, wait for
INTERSVR to revert) is functionally equivalent and correct.

### 4.3 Idle fallback

At any rate above 9600, INTERSVR returns to 9600 after **2.0 s** without a
received byte, and then waits for a handshake [V]. The idle keepalive (section 5.5)
prevents this during a session. INTERLNK re-handshakes after a lost link.

INTERSVR's console shows `Speed=` with the rate name, or `Variable`. The
`Variable` string is at file offset 0x88df. Most likely `Variable` means a
session that never stepped the rate [I, unconfirmed].

## 5. Frame layer

### 5.1 Frame format [V]

```
tag (1) | count (1 if tag bit 7 set, else 2, little-endian) | data (count) | crc (2)
```

**CRC**: CRC-16/GENIBUS over `count + data`:
- polynomial 0x1021, initial value 0xFFFF, no bit reflection;
- the transmitted value is the CRC inverted, high byte first;
- check value for `123456789`: 0xD64E;
- a receiver running the same CRC over `count + data + crc` gets the residue **0x1D0F** when the frame is good.

Example, the install pre-frame: `80 02 00 00 5D 03`.

### 5.2 Tag byte

| Bit(s) | Meaning |
|---|---|
| 0-1 | sequence number, mod 4, shared by both directions; each frame (either way) uses the next value [V] |
| 2 | retransmission signal; semantics differ by direction (section 5.6) [V, Ghidra s79] |
| 3-5 | always 0 [V] |
| 6 | sector-data chunk with **another chunk after it**; 16-bit count [V] |
| 7 | 8-bit count; the data is <= 255 bytes [V] |

Seen in practice:
- requests and short replies use `0x80|seq`;
- sector-data chunks use `0x40|seq` (more to follow) or the bare `seq` (last chunk).

### 5.3 One frame exchange [V]

```
sender:   tag
receiver: ~tag & 0xFB          (echo; INTERSVR always clears bit 2; INTERLNK may set it -- see section 5.6)
sender:   count, data, crc     (one burst)
receiver: tag                  (ack; a CRC failure gets no ack)
```

### 5.4 Line ownership and turnaround [V]

After the ack, the **sender still owns the line**, and its next byte says what
happens:

- **a tag with bit 7 set:** another short frame follows (e.g. Get Media ID: status frame, then data frame).
- **a tag `0x40|seq` or a bare `seq` where sector data is due** (read reply, write request): the next data chunk.
- **a bare `seq` otherwise:** the sender **yields**. The peer now owns the line, and its next frame's tag uses that same `seq`.

A request/reply therefore looks like this:

```
client: request frame(s) ... yield (bare seq)
server: reply frame(s)   ... yield (bare seq)
```

### 5.5 Idle keepalive [V]

The line owner, when idle, sends `0xFC | (next_seq ^ 1)` about once per BIOS
tick (55-100 ms), and the peer echoes the byte unchanged. The pairs are
`FD`<->seq 0, `FC`<->1, `FF`<->2, `FE`<->3. INTERLNK sends keepalives without waiting
for the previous echo, so an echo can arrive after the next frame's tag.

### 5.6 Timeouts and retransmission

- INTERSVR's per-byte receive timeout is 18 ticks, about 1 s (`LinkProtoHandler` 1000:7257). If it expires, the frame is abandoned [V, Ghidra].
**Retransmission -- Ghidra-verified [V, s79]:**

*INTERSVR receiving a request with bit 2 (`LinkProtoHandler` 1000:7257):*
- Seq check is `tag & 3` only; bit 2 is not tested.
- Full tag stored at RAM:0x1d6 but bit 2 has no effect on processing.
- Echo is always `~tag & 0xfb` (bit 2 cleared).
- A retransmit from INTERLNK carries the original seq bits (e.g. seq=0); by the time
  INTERLNK retransmits, INTERSVR has already processed the first attempt and advanced
  its counter to seq=1. The seq loop never matches; the link dies.
- There is no recovery path on the server side. Prevention is the only remedy: the
  server must send its echo before INTERLNK's retransmit timer fires (pyterlink 1.0.4
  bulk-read fix).

*INTERLNK receiving a reply with bit 2 (`LinkRecvDispatch` 1000:1b45):*
- INTERLNK extracts `bVar4 = received_tag & 4`.
- Error conditions at RAM:0x45a, 0xa3, or 0x8e2 nonzero force `bVar4 = 4` regardless
  of the received tag.
- Echo = `(~received_tag & 0xfb) | bVar4`: mirrors bit 2 from the server's reply, or
  sets it when INTERLNK has internal errors; this signals INTERSVR to retransmit.
- When bit 2 is set, INTERLNK uses `LinkXmitByte` directly for data reads instead of
  the normal indirect-call path; the underlying data volume and CRC check are unchanged.
- Whether INTERSVR ever sets bit 2 in its own reply tag is not confirmed (send path
  not located) [I].

## 6. Requests and replies

Byte 1 of a request is the **server unit**: 0 = A:, 1 = B:, 2 = C:, and so on,
on the INTERSVR machine. A reply to a block operation starts with the DOS
device-driver status word, 16-bit little-endian: bit 15 error, bit 8 done, low
byte the error code.

### 6.1 `00` Install [V]

The client sends two frames, then yields:

```
[00 00]                                                     (pre-frame)
[00 00][00 00 00 00 00 01 01 00 00 01 00 20][00 x12][client cookie 4][server cookie 4]
```

Reply (33 bytes):

```
[00][00 00 00 00 00 0A 01 01 00 01 00 20][00 x12][previous client's cookie 4][server cookie 4]
```

Cookies:
- The client's cookie is random per INTERLNK load. In the request, the server-cookie field holds the server cookie the client last saw (0 on a first connect).
- INTERSVR's cookie is constant for its lifetime. Its reply carries the cookie of the **previous client it served** (0 after INTERSVR starts), then its own.

The 12-byte info fields are constant in every capture [O].

### 6.2 `01` Drive map [V]

```
request: [01 01 00 16 06 03 03][map 29]
reply:   [01 0A 16 06 n_units][map 29][0x08C2 x n_units, LE][00 ...]   (87 bytes)
```

- `16 06` is the DOS version, 6.22. The other header bytes are constant [O].
- `map[i]` is client drive letter i (A: = 0; the entries past Z: are always `FE`):
  - `FF`: a local drive;
  - `FE`: unassigned;
  - any other value: the server unit that letter redirects to.
- On a first connect the client sends only its local drives (`FF`). The server fills the first free letters with the units it offers, in order.
- On a reconnect, INTERLNK sends the mapping it already has (e.g. `FF FF FF 02` = D: is server unit 2), and the server keeps it [V].
- Unit attribute `0x08C2`, per unit [O].

### 6.3 `1C` Drive info [V]

```
request: [1C unit]
reply:   [size text 10][volume label 13]          (23 bytes)
```

- **Size text:** e.g. `"(51Mb)"` or `"(106Mb)"`, computed as `total sectors x 512 // 1,000,000`, NUL-terminated.
- **Label:** the volume label, NUL-terminated.
- **Padding:** the bytes after each NUL are leftover INTERSVR memory and vary [O].
- **Floppy:** a 1.44 MB floppy (media F0) gives an empty size text and label. The first byte is 00 and the rest is leftover memory [V].

### 6.4 `02` Media check [V]

```
request: [02 unit media]
reply:   [status 2][changed 1][volume label 12][serial 4]       (19 bytes)
```

- **Label:** taken from the **root-directory volume-label entry**, not the BPB. A fresh `mkfs` floppy whose BPB says `NO NAME` reports an empty label.
- **Fixed disk (F8):** status `02 01`, changed `01` (unchanged).
- **1.44 MB floppy (F0):** status low byte varies from run to run (`13`, `14`) [O], changed `00` (don't know).
- **`FF 01`:** returned for an empty floppy drive (session 3), and right after the image in the drive was replaced. It cannot yet be told apart from "media changed" [O].

### 6.5 `03` Build BPB [V]

```
request: [03 unit media]
reply:   [status 2][BPB 25 = boot sector 0x0B..0x23][trailer 6]     (33 bytes)
```

| Media | Status | Trailer |
|---|---|---|
| Fixed disk (F8) | `02 01` | `40 00 00 05 21 00` (same on two disk sizes) [O] |
| 1.44 MB floppy (F0) | `09 01` | `00 00 00 07 30 00` [O] |
| Empty floppy drive | `0C 81` (general failure) | -- [V] |

### 6.6 `04` Read sectors [V]

```
request: [04 unit media][count 2][FF FF][lba 4]
reply:   [status 2][count 2]   then the data as chunks (section 6.8)
```

- `count` is 1-127 sectors. Captured counts: 1, 3, 5, 8, 9, 16, 32 and 112.
- `FF FF` in the 16-bit start-sector field selects the 32-bit `lba` (DOS 4+ huge-sector convention).
- Success status is `00 01`.

### 6.7 `05` Write sectors [V]

```
request: [05 unit media][count 2][FF FF][lba 4]   then the data as chunks (section 6.8)
reply:   [status 2][count 2]
```

Counts acknowledged in captures: 1, 2 and 8. Requests for 16 and 127 sectors
were captured too, but each one failed when the data was lost in DOSBox (section 9).

### 6.7a `06` Write sectors with verify [V]

DOS device command 9 (write with verify), sent when DOS VERIFY is on (e.g.
`XCOPY /V`). Same request layout, data chunks and reply as `05`; only the
opcode differs. INTERSVR runs both through one handler (1000:1c67), which
passes command 8 for `05` and 9 for `06` to its own disk driver. Captured
live 2026-10-08 (`live_server_2026-10-08T00H32`, XCOPY /A/E/S/Y/V):
`06 02 F8 01 00 FF FF 43 02 00 00`, then the data chunk `03`.

### 6.8 Sector data chunks [V]

Data moves in frames of at most **8192 bytes** (16 sectors), 16-bit count:
- every chunk except the last has tag `0x40|seq`;
- the last chunk has the bare `seq`;
- after the last chunk is acked, the sender yields.

Example, a 32-sector read reply: status frame `82` (`00 01 20 00`), chunk `43`
(8192 bytes), chunk `00` (8192 bytes), yield `01`.

### 6.9 `1D` Generic IOCTL: Get Media ID [V]

```
request: [1D unit 08 66]
reply:   status frame [00 01]  (tag bit 7: the line is kept)
         then data frame [1D 60][serial 4][label 11][file system type 8]   (25 bytes)
```

- The label and file-system type come from the boot sector.
- The first word (`1D 60`) is constant [O].
- On an empty drive: status `0C 81` only.

### 6.10 Media byte sequencing [V]

On a unit's first media check and build BPB, INTERLNK sends media `00` ("not
known yet"). Every later request carries the BPB's media byte (`F8` fixed disk,
`F0` 1.44 MB floppy).

### 6.11 Units INTERLNK addresses [V]

INTERLNK only sends requests for units the drive map gave it. A request for
another unit still reaches the server's DOS. For example, B: on a one-floppy
machine makes MS-DOS display "Insert diskette for drive B:", which blocks
INTERSVR until someone answers it.

### 6.12 INTERSVR's request table [V: Ghidra]

INTERSVR's request dispatcher (1000:061c) takes the opcode at
DS:0x27a3; below `1E` it calls the near handler at DS:0x2c8 + 2 x op, with a
class byte at DS:0x304 + op (1 = block request, 2 = block request with data
pushed after it, 3 = printer-port unit). The tables, read from the EXEPACK-
unpacked image (EXEPACK removed; table at image offset 0x195b8):

| Op | Handler | What it does | Captured |
|---|---|---|---|
| `00` | 1626 | Install | yes |
| `01` | 16f5 | Drive map | yes |
| `02` | 188c | Media check | yes |
| `03` | 19fe | Build BPB | yes |
| `04` | 1b53 | Read (driver command 4) | yes |
| `05` | 1c67 | Write (driver command 8) | yes |
| `06` | 1c67 | Write with verify (driver command 9) | yes (2026-10-08) |
| `07` | 1d88 ReqHandlerReturnOne | returns 1; bare yield only, no reply frame | no |
| `08` `09` `0C` `0D` `0F` `13` | 16f2 ReqHandlerReturnZero | returns 0; bare yield only, no reply frame | no |
| `0A` `0B` `14` `15` | 28c2 ReqHandlerPrinterOutput | printer output to LPT1-3; req[1]=port, req[2:4]=count; reply: status(2) + count(2); count < 2: no push; count >= 2: data pushed [V s73] | no |
| `0E` `10` `11` `12` | 2988 ReqHandlerPrinterCtrl | printer ctrl (output status, flush, open, close); reply: status(2); 0x0100 ok, 0x8101 no port [V s73] | no |
| `16` `17` `18` | 1d8c ReqHandlerBlockCtrl | block device open (16) / close (17, 18); reply: status(2) [V s73] | no |
| `19` `1A` `1B` | 2a85 ReqHandlerIntPrinter | INT 17h AH=0/1/2 (print char/init/status) on LPT unit; reply: 1-byte printer status; 0x19 = no printer [V s73] | no |
| `1C` | 3198 | Drive info | yes |
| `1D` | 1df9 | Generic IOCTL (driver command 13h): minor `66` Get Media ID returns data; minor `46` Set Media ID takes pushed data; others pass to the driver | `66` only |

Handlers verified by live Ghidra trace s73 (InterLinkRE, INTERSVR.EXE). Function
names in the Ghidra project updated same session: 1d88->ReqHandlerReturnOne,
16f2->ReqHandlerReturnZero, 2a85->ReqHandlerIntPrinter. pyterlink-server 1.0.7
implements all of these: bare-yield ops (07/08-0D/0F/13) send no reply frame;
printer output (0A/0B/14/15) returns `0x0100` + count(2); printer ctrl
(0E/10-12) and block ops (16-18) return `0x0100`; INT 17h (19-1B) returns
printer status `0x19` (no printer); IOCTL Set Media ID (1D minor 46) accepts
the pushed 25-byte MID structure and returns `0x0100`.

## 7. Status words

| Word (LE bytes) | Meaning | Seen |
|---|---|---|
| `0x0100` (`00 01`) | done: read, write, IOCTL | [V] |
| `0x0102` (`02 01`) | done: fixed-disk media check, build BPB | [V] |
| `0x0113`, `0x0114` | done: floppy media check (low byte varies) | [O] |
| `0x0109` | done: floppy build BPB | [O] |
| `0x01FF` | media check: empty drive, or media changed | [O] |
| `0x810C` | error 0C, general failure (e.g. BPB on an empty floppy drive) | [V] |
| `0x8100` | error 00, write protect | DOS standard; pyterlink's read-only drives |

The other DOS device-driver error codes (01 unknown unit, 02 not ready, 03
unknown command, 08 sector not found, ...) follow the DOS convention. None of
them was captured.

## 8. Session lifecycle (INTERLNK) [V]

1. Handshake at 9600 (section 3).
2. Rate steps up to `/BAUD` (section 4.1). Expect INTERSVR's probe at the first yield if INTERLNK stopped below INTERSVR's own rate (section 4.2).
3. Install (section 6.1).
4. Drive map (section 6.2).
5. Drive info for each mapped unit (section 6.3).
6. On first use of a unit: media check and build BPB with media `00` (section 6.10).
7. Then reads, writes, media checks and IOCTLs as the client's DOS asks.
8. Keepalives whenever idle (section 5.5).
9. After a lost link (timeout, restarted INTERSVR): handshake again, step up again, install with the saved cookies, drive map, then continue.

## 9. Practical limits (not protocol)

- **DOSBox serial throughput.** A DOSBox INTERSVR or INTERLNK at 115200 drains its port at about 5 KB/s, through the host's 4096-byte tty buffer, with no flow control. An 8 KB data burst sent at the full line rate into a DOSBox loses bytes, and the receiver then never acks. It happened at 57600 too.
  - The real INTERLNK hits the same limit [O]: in the s57 strace captures (`strace_interlnk2`, `strage_intersvr`, 18:26-18:28) its 127-sector writes to a DOSBox INTERSVR go out as 8 KB chunks (tag `0x40|seq`, as in section 7), the first chunk is never acked, and INTERLNK re-installs and resends the whole request -- three times, then gives up (the next requests are single-sector writes); it never tries a smaller request. The 8 KB write chunk is therefore the protocol, not a pyterlink choice; the loss is on the DOSBox receive side.
  - The limit does not move with DOSBox's CPU setting: live runs at 20000, 30000 and 40000 cycles (s58, `live_attempt_2026-10-07T23H13/23H23/23H32`) all lost the 16-sector (8 KB) write and wrote 8-sector requests at the same ~4.5 KB/s.
  - pyterlink's client starts writes at 127 sectors, as INTERLNK does; a lost write is resent at half the size, which stays the limit for the run. Its server sends unpaced and halves its rate each time INTERLNK misses a burst.
- **USB serial latency.** With an FTDI adapter's default 16 ms latency timer, every turnaround costs about 15 ms. A setting of 1 ms helps (`/sys/bus/usb-serial/devices/ttyUSBn/latency_timer`).

## 10. Not captured / open

- Frame retransmission (tag bit 2): the complete exchange (section 5.6).
- Accepting INTERSVR's rate probe (section 4.2).
- Printer redirection (INTERLNK's LPT sharing): no printer traffic captured.
- The parallel-port (LPT) link: not examined (D-047).
- The meaning of the constant install/map/IOCTL fields, the BPB trailers and the status low bytes (section 6).
- Telling "media changed" from "no disk" (`FF 01`).
- Error replies other than `0x810C`.
- The meaning of `Speed=Variable`.

## Appendix A: annotated establishment at 115200

From `logs/strace_interlnk2.log.gz` (18:24:41, INTERLNK `/BAUD:115200`), bytes in time order:

```
L AA  S 00  L 55  S FF  L 5A  S 11                       handshake @9600
L FF  S 00  L 04                                          step to 19200
S 40  L 40  S BF  L FF S 00 ... L FF S E7                 announce + 15 pairs @19200
L FF  S 00  L 05  ...                                     38400
L FF  S 00  L 06  ...                                     57600
L FF  S 00  L 07  S 70 L 70 S 8F ... L FF S E7            115200
L 80  S 7B  L 02 00 00 5D 03  S 80                        frame 0: install pre-frame [00 00]
L 81  S 7A  L 22 <34 bytes> <crc>  S 81                   frame 1: install request
L 02                                                      yield (seq 2)
S 82  L 79  S 21 <33 bytes> <crc>  L 82                   frame 2: install reply
S 03                                                      server yields (seq 3)
L 83  S 78  L 24 01 01 00 16 06 03 03 <map 29> <crc>  S 83   frame 3: drive map request
L 00                                                      yield (seq 0)
S 80  L 7B  S 57 01 0A 16 06 03 <map 29> C2 08 x3 00... <crc>  L 80   frame 4: drive map reply
...
```

## Appendix B: implementation

- **pyterlink.**
  - `pyterlink/protocol/framing.py`: CRC and frame encoding.
  - `pyterlink/protocol/session.py`: client role.
  - `pyterlink/protocol/server.py`: server role.
- **Tools:**
  - `tools/decode_pyterlink_capture.py`: requests and replies in a capture.
  - `tools/capture_frames.py`: CRC-checked frames in a capture.
  - `tools/com3_logging_proxy.py`: records DOSBox-to-DOSBox traffic.
