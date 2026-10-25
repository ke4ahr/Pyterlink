# DOSBox `nullmodem` serial backend -- source analysis

Analysis of DOSBox's TCP/ENet virtual null-modem serial backend
(`CNullModem`, `serial1=nullmodem ...`), read directly from source.
The rig's confs (`dosbox_x/*-msdos.conf`) use both backends:
`directserial` for COM2 (the real USB serial ports) and `nullmodem`
for COM1, COM3 and COM4 (TCP). See "Relevance to this project" below.

## Source provenance

Primary source read for this analysis:

- `https://github.com/dosbox-staging/dosbox-staging/blob/main/src/hardware/serialport/nullmodem.cpp`
  (fetched in full via `raw.githubusercontent.com`, 607 lines, plus
  the companion `nullmodem.h`, 103 lines)

Cross-checked against the version of the same file already vendored
in this repo, which is what the project's actual DOSBox engines
(apt `dosbox 0.74-3-4`, and by lineage `dosbox-x` 2026.01.02) are
built from:

- `dosbox-0.74-3/src/hardware/serialport/nullmodem.cpp` (590 lines)
- `dosbox-0.74-3/src/hardware/serialport/nullmodem.h` (113 lines)

**dosbox-staging is a different, actively-developed fork from the
classic `dosbox`/`dosbox-x` lineage this project uses.** The two
files implement the same feature and the same command-line option
names (confirmed below), but are not the same codebase. Differences
found by diffing them:

| | dosbox-staging (fetched) | vendored dosbox-0.74-3 (this project's engines) |
|---|---|---|
| License header | SPDX tags, GPL-2.0-or-later | Full GPL-2 boilerplate text, same license |
| Compile guard | none | wrapped in `#if C_MODEM ... #endif` (see "Open questions" below) |
| Integer types | `uint8_t`/`uint16_t`/`uint32_t` | legacy `Bit8u`/`Bit16u`/`Bitu` typedefs |
| Transport | TCP **or ENet** (`SocketType`, `sock:` option) | **TCP only** -- no `sock:`/ENet option exists at all |
| Encapsulation | `serversocket`/`clientsocket`/etc. are `private` | same fields are `public` |
| Constructor signature | `CNullModem(uint8_t port_idx, CommandLine*)` | `CNullModem(Bitu id, CommandLine*)` |
| Core protocol (escape byte, RTS/DTR bits, event state machine) | -- | **identical in substance** -- same option names (`usedtr:`, `transparent:`, `telnet:`, `rxdelay:`, `txdelay:`, `port:`, `inhsocket:`, `server:`), same defaults (`rx_retry_max=20`, `tx_gather=12`), same `N_RX_*` state machine, same `0xff`-escape control-line framing |

Conclusion: the option syntax and wire protocol described below are
directly applicable to this project's dosbox 0.74-3 engine (verified
line-for-line against the vendored source). The staging-only
addition is ENet transport (`sock:1`) and some internal code
cleanup; nothing in that diff changes the option names, defaults, or
on-wire framing documented here.

## Where `nullmodem` fits among DOSBox serial backends

DOSBox's `[serial]` section (`serial1=<type> <options>`) supports
several backends; the ones relevant to context:

- `dummy` -- fake port, no I/O, connects to nothing.
- `disabled` -- the port doesn't exist to the guest OS at all.
- `directserial` -- used for **COM2** in this project's confs.
  Passes the emulated UART straight through to a real host serial
  device (`realport:ttyUSB0` / `realport:ttyUSB1`). Nothing is added
  on the wire. On the host side DOSBox opens the port with termios
  PARMRK, so in an strace of its reads a received 0xFF shows as
  `FF FF` and a byte with a framing or parity error as `FF 00 x`
  (a capture made from such an strace must undo this). See
  `dosbox_x/lnkserver-msdos.conf` / `dosbox_x/lnkclient-msdos.conf`.
- `modem` -- emulates a Hayes-compatible AT modem over a TCP socket
  (dial/answer, `ATD`/`ATA` commands). Not analyzed here.
- `nullmodem` -- **the subject of this document**, used for COM1,
  COM3 and COM4 in this project's confs. Emulates a
  null-modem cable between two DOSBox instances over a TCP (or, in
  dosbox-staging, ENet) socket instead of a real cable. Two guest
  OSes, each running in its own DOSBox process (same machine or two
  different machines), see what looks like a real RS-232 null-modem
  link to each other -- but the bytes travel over a network socket
  between the two *host* processes, never touching a real UART.

## Conf/command-line syntax

```
serial1=nullmodem [server:<host>] [port:<n>] [rxdelay:<ms>] [txdelay:<ms>]
                   [usedtr:0|1] [transparent:0|1] [telnet:0|1]
                   [inhsocket:0|1] [sock:0|1]
```

Every option below is read via `getUintFromString("<name>:", ...)`
from the free-text argument string after `nullmodem`; anything not
given falls back to its default. Source: `CNullModem::CNullModem`,
`nullmodem.cpp:12-138` (staging) / `nullmodem.cpp:29-...` (vendored).

- **`server:<hostname>`** -- **presence of this option is what
  makes THIS DOSBox instance the client**, not the server. This is
  the single easiest thing to misread in the whole interface: the
  option is literally named `server:` but it means "connect to a
  server at this hostname," i.e. *be a client*. Omitting `server:`
  entirely makes this instance the server: it opens a listening
  socket on `port:` and waits (`ServerListen()` ->
  `NETServerSocket::NETServerFactory`). Source:
  `nullmodem.cpp:104-129` -- `if (cmd->FindStringBeginCaseSensitive("server:", ...)) { // we are a client ... } else { // we are a server ... }`.

- **`port:<1-65535>`** -- TCP (or ENet) port, used for both roles
  (the server's listen port, or the client's connect-to port).
  Default `23` (the historical telnet port -- relevant if you also
  enable `telnet:1`). Out-of-range values silently reset to 23.

- **`rxdelay:<0-10000>`** (milliseconds) -- how long the emulated
  UART will hold received-but-unconsumed data before declaring a
  receive overrun to the guest, i.e. how many `SERIAL_POLLING_EVENT`
  ticks (1 ms each, see below) the guest's `CanReceiveByte()` is
  allowed to keep returning false before `rx_retry` hits
  `rx_retry_max` and the buffered byte is force-delivered anyway.
  Default `20`. Values `> 10000` silently reset to `50`
  (**not** to the stated default of 20 -- this is what the source
  actually does, confirmed identical in both versions).

- **`txdelay:<0-500>`** (milliseconds) -- how long outgoing bytes
  are gathered into `tx_gather` before being flushed to the socket
  in one write (`SERIAL_TX_REDUCTION` event), i.e. a Nagle-like
  batching knob to cut network overhead when the guest sends bytes
  one at a time. Default `12`. Values `> 500` silently reset to the
  stated default of `12`.

- **`usedtr:0|1`** -- when `1`: (a) a *client*-role instance does
  **not** connect immediately at startup; instead it waits for the
  guest to raise DTR (i.e. until a "modem-aware" DOS program opens
  the port), then connects (`SERIAL_NULLMODEM_DTR_EVENT` handler,
  `nullmodem.cpp:413-424`); (b) received data is only delivered to
  the guest while DTR is on. Setting `usedtr:1` implicitly also sets
  `transparent:true`. Default `0`.

- **`transparent:0|1`** -- when `1`, disables the `0xff`-escape
  control-line framing described below: the socket carries the raw
  data stream with no interleaved RTS/DTR/BREAK signaling bytes, and
  DTR/RTS state is simply asserted as constant (`setCTS`/`setDSR`
  forced true at init). Default `0` (framing enabled).

- **`telnet:0|1`** -- when `1`, implicitly sets `transparent:true`
  and additionally interprets/answers RFC 854 Telnet `IAC` option
  negotiation sequences on the stream (see "Telnet emulation"
  below) instead of treating `0xff` as this backend's own
  control-line escape. Default `0`.

- **`inhsocket:0|1`** -- socket-inheritance mode: instead of
  opening a new connection, adopt an already-open native socket
  handle passed via a `-socket` command-line argument to the whole
  DOSBox process. Requires `NATIVESOCKETS` support to be compiled
  in; a no-op with a log message otherwise. Niche option, not
  relevant to normal use.

- **`sock:0|1`** (**dosbox-staging only -- absent from the vendored
  0.74-3 source used by this project's engines**) -- `1` selects
  ENet transport instead of TCP (`SocketType::Enet`). Confirmed
  absent from `dosbox-0.74-3/src/hardware/serialport/nullmodem.cpp`
  by direct grep (no match for `sock:`, `Enet`, or `enet`) -- do not
  use this option with this project's dosbox/dosbox-x engines.

## Wire protocol: the `0xff`-escape control-line framing

When `transparent:0` (the default) and `telnet:0`, the socket
carries more than raw serial data -- it multiplexes RTS/DTR/BREAK
line-state changes into the same byte stream using `0xff` as an
escape character:

- A literal data byte `0xff` is sent as `0xff 0xff` (escaped) so the
  receiver can tell it apart from a control sequence. Source:
  `CNullModem::transmitByte`, `nullmodem.cpp:454-465`.
- A control-line update is sent as `0xff <status>`, where `<status>`
  bit 0 = RTS, bit 1 = DTR, bit 2 = BREAK active. Source:
  `CNullModem::setRTSDTR`, `nullmodem.cpp:587-601`:
  ```c
  ctrl_lines[0] = 0xff;
  ctrl_lines[1] = 0x0;
  if (xrts) ctrl_lines[1] |= 1;
  if (xdtr) ctrl_lines[1] |= 2;
  if (LCR & LCR_BREAK_MASK) ctrl_lines[1] |= 4;
  ```
- On receive (`CNullModem::readChar`, `nullmodem.cpp:161-183`): a
  `0xff` byte triggers reading one more byte. If that second byte is
  also `0xff`, it's treated as a literal data `0xff`. Otherwise its
  low 3 bits set the local view of the peer's CTS (mirrors the
  peer's RTS), DSR (mirrors the peer's DTR), and, if bit 2 is set,
  synthesizes a BREAK condition to the guest
  (`receiveByteEx(0x0, 0x10)`).
- `setRTSDTR`/`setRTS`/`setDTR` (i.e. whenever the *local* guest's
  UART changes RTS or DTR) immediately send a fresh `0xff <status>`
  frame to the peer -- this is how modem-control-line state
  round-trips between the two DOSBox instances live.

**In `transparent:1` mode none of this framing exists** -- the
socket is a pure, unescaped, byte-for-byte pass-through of the
serial data stream, and CTS/DSR are just forced on at connect time
instead of being tracked live. This is the closer analogue to what
`directserial` does against a real UART.

## Telnet emulation (`telnet:1`)

A separate, optional mini Telnet client
(`CNullModem::TelnetEmulation`, `nullmodem.cpp:467-574`) answers RFC
854 `IAC` (`0xff`) option-negotiation sequences from a real Telnet
server on the other end of the socket -- handles `WILL`/`WONT`/`DO`/
`DONT` (251-254) for options 0 (binary), 1 (echo), and 3 (suppress
go-ahead), replying `WONT`/`DONT` (252/254) to anything else it
doesn't recognize. This exists so a DOS terminal program can dial
into a real Telnet BBS/server through the `nullmodem` backend; it
has no bearing on DOSBox-to-DOSBox links and is not relevant to this
project.

## Event / state-machine internals

- **`SERIAL_POLLING_EVENT`** fires every 1 ms while connected
  (`handleUpperEvent`, `nullmodem.cpp:275-297`, re-arms itself each
  time). It drives `updateMSR()` (a no-op in this backend -- modem
  status is pushed via the `0xff`-frame receive path instead, not
  polled) and the receive state machine.
- **Receive state machine** (`rx_state`, one of `N_RX_IDLE` / `_WAIT`
  / `_BLOCKED` / `_FASTWAIT` / `_DISC`): starts `N_RX_DISC`
  (disconnected). Once connected, cycles `IDLE` -> (byte available
  from `doReceive()`) -> `WAIT` -> back to `IDLE` on the next
  `SERIAL_RX_EVENT`, timed at `bytetime * 0.9` (i.e. paced to the
  guest's configured baud rate, not the network's actual speed). If
  the guest's UART receive buffer is full (`CanReceiveByte()` false),
  it goes to `BLOCKED` and retries once per event, counting
  `rx_retry` up to `rx_retry_max` (`rxdelay:`) before forcing
  delivery anyway (drains everything buffered with a `while
  (doReceive())` loop) -- this is the actual mechanism `rxdelay:`
  controls: guest-side backpressure tolerance, not network latency.
- **`SERIAL_SERVER_POLLING_EVENT`** fires every 50 ms while a server
  instance has no client yet, retrying `ServerConnect()` /
  `Accept()`. Once a client connects, this event is removed for the
  rest of the connection's life.
- **`SERIAL_TX_REDUCTION`** fires once, `tx_gather` ms after the
  first byte in a burst is queued, then flushes the socket send
  buffer (`FlushBuffer()`) and clears the `tx_block` gate --
  a fixed inter-write coalescing window, not adaptive.
- **Disconnect handling** (`Disconnect()`, `nullmodem.cpp:249-270`):
  drops DSR/CTS/CD, frees the client socket, and either re-opens the
  listening socket (server role) or re-arms the DTR-watch event
  (client role with `usedtr:1`) so the link can be re-established
  without restarting DOSBox.

## Relevance to this project

The confs use `nullmodem` on these ports:

| Port | `lnkserver-msdos.conf` | `lnkclient-msdos.conf` |
|---|---|---|
| COM1 | server, TCP 11232, `transparent:1` (DOS console) | server, TCP 11233, `transparent:1` (DOS console, `docs/console_proxy.md`) |
| COM2 | `directserial realport:ttyUSB0` | `directserial realport:ttyUSB1` |
| COM3 | server, TCP 11330, `transparent:1` | client (`server:127.0.0.1`), TCP 11430, `transparent:1` |
| COM4 | server, TCP 11331 | client (`server:127.0.0.1`), TCP 11231 |

(The `127.0.0.1` written without `server:` in the server-role lines
is not an option and is ignored; only `server:` makes an instance a
client.)

- **COM3** joins the two DOSBoxes through `com3_logging_proxy.py`
  (listens on 11430, forwards to 11330). The session-3 captures
  (`logs/com3_traffic_*.log.gz`) of real LNKCLIENT <-> real LNKSERVER
  were taken this way. A TCP nullmodem has no line rate (received
  bytes are only paced to the guest's programmed rate), so the
  captures carry no rate-step timing; the rate negotiation was
  decoded from strace of the `directserial` ports instead.
- **COM2** (`directserial`) is the path pyterlink uses: pyterlink's
  `pyterlink/transport/serial_backend.py` opens a Linux tty
  (`/dev/ttyUSBn`) through pyserial, at the other end of the
  null-modem cable from DOSBox's port. A TCP nullmodem socket has no
  tty node; pyterlink was not run through one.
- A TCP link does not exercise real UART behaviour (baud timing,
  framing and overrun errors, adapter quality), which the real-cable
  runs did surface.

## Open questions / unverified

- **`C_MODEM` in the installed `dosbox 0.74-3-4`.** The vendored
  source wraps this backend in `#if C_MODEM` (`nullmodem.cpp:22`), and
  `config.h.in` leaves it undefined by default. The confs' nullmodem
  ports are in use (the console proxy and the session-3 COM3
  captures), so the engine that ran them has it enabled; which engine
  (`dosbox` or `dosbox-x`) ran each capture is not recorded here.
- **dosbox-x's own `nullmodem` implementation was not sourced or
  read in this session.** dosbox-x is a long-running independent
  fork; while it has historically maintained the same `serial1=`
  option-name lineage as classic dosbox (per its own public
  documentation), its actual current source was not fetched or
  diffed here, so any behavioral claim above should be treated as
  applying to classic `dosbox 0.74-3-4` (verified against vendored
  source) and dosbox-staging (verified against fetched source) only
  -- not confirmed for the `dosbox-x` snap this project also uses.
- A `socat` PTY bridge from a nullmodem TCP port to a pty that
  pyterlink could open was never tried.

## References

- `https://github.com/dosbox-staging/dosbox-staging/blob/main/src/hardware/serialport/nullmodem.cpp`
- `https://github.com/dosbox-staging/dosbox-staging/blob/main/src/hardware/serialport/nullmodem.h`
- `dosbox-0.74-3/src/hardware/serialport/nullmodem.cpp` (vendored in this repo)
- `dosbox-0.74-3/src/hardware/serialport/nullmodem.h` (vendored in this repo)
- `dosbox_x/README.md` -- this project's DOSBox rig (directserial COM2, nullmodem COM1/3/4)
