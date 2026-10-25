# klog_tee

A `tee(1)`-like utility that prepends kernel-style monotonic timestamps to each line of input.

## Synopsis

```sh
klog_tee [OPTION]... [FILE]...
```

## Description

Reads from standard input, writes to standard output and any number of files, with each line prefixed by a timestamp in the format used by the Linux kernel logger:

```
[   12.345678] message text
```

The timestamp is seconds since klog_tee started. klog_tee reads only its standard input, so a piped command's stdout and stderr (`2>&1`) are not told apart. An asterisk after the closing bracket marks klog_tee's own diagnostics, which it writes to its stderr:

```
[    0.000412]* cannot open log.txt: ...
```

## Options

| Option | Description |
|--------|-------------|
| `-a`, `--append` | Append to the given FILEs, do not overwrite |
| `-i`, `--ignore-interrupts` | Ignore SIGINT (SIGTERM and SIGHUP still cause clean shutdown) |
| `-p` | Diagnose errors writing to non-pipes (alias for `--output-error=warn`) |
| `--output-error MODE` | Set behavior on write error: `warn` (default), `exit`, or `exit-nopipe` |
| `--flush-interval SECONDS` | Periodic flush interval in seconds (default: 60) |
| `-0`, `--null`, `--print0` | Input records are NUL-terminated (like `find -print0`) |
| `--help` | Display help and exit |
| `--version` | Output version information and exit |

## Files

Output FILEs receive the same timestamped output as stdout. When `-a/--append` is used, each run prepends a banner showing the wall-clock start time and the monotonic time base, allowing correlation of timestamps to absolute time:

```
=== klog_tee started at 2026-09-07 17:43:13 CDT (monotonic base: 765999.017746) ===
```

## Signals

- **SIGTERM**: Clean shutdown (flushes buffers, closes files)
- **SIGHUP**: Clean shutdown
- **SIGINT**: Clean shutdown (ignored if `--ignore-interrupts` is set)

## Examples

```sh
# Basic usage
command | python3 tools/klog_tee.py log.txt

# Append with timestamps
command | python3 tools/klog_tee.py -a log.txt

# Capture both stdout and stderr
command 2>&1 | python3 tools/klog_tee.py log.txt

# Ignore Ctrl-C, only SIGTERM/SIGHUP stop it
command | python3 tools/klog_tee.py -i log.txt

# Exit on any write error
command | python3 tools/klog_tee.py --output-error=exit log.txt

# Null-terminated input (like find -print0)
find . -print0 | python3 tools/klog_tee.py -0 log.txt

# Custom flush interval (every 10 seconds)
command | python3 tools/klog_tee.py --flush-interval=10 log.txt
```

## Use with DOSBox

The project includes wrapper scripts for DOSBox that automatically pipe output through klog_tee:

```sh
# DOS-side LNKSERVER (server)
dosbox_x/run-lnkserver.sh dosbox

# DOS-side LNKCLIENT (client)
dosbox_x/run-lnkclient.sh dosbox
```

These run `dosbox ... 2>&1 | ../tools/klog_tee.py -a` from `dosbox_x/` and append to `dosbox_x/lnkserver.log` and `dosbox_x/lnkclient.log`, so DOSBox's own messages (serial port open errors and the like) get timestamps. DOSBox does not log the serial bytes there; for those see `docs/test_procedure.md` "Capturing traffic".

## Timestamp Format

The timestamp is `time.monotonic()` minus the value at klog_tee's start, i.e. seconds since klog_tee started, formatted as `[%12.6f]`. The layout matches the Linux kernel's `printk` time format, but the values are not kernel uptime.

When appending, the banner gives the wall-clock start time (and the raw `time.monotonic()` at that moment), so a timestamp `T` converts to wall-clock as:

```
wall_clock ~= banner_time + T
```

## Exit Status

- `0` on success
- `1` on error (file open failure with `--output-error=exit`, or write error with `--output-error=exit`/`exit-nopipe`)
- `2` on invalid arguments

## Requirements

- Python 3.6+
- Standard library only (no external dependencies)