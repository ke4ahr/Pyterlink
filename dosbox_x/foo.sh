#!/bin/sh
# foo.sh -- triggered by monit when $PYTERLINK_CONTROL (default ~/.control)/dosbox-run-lnkserver.txt exists
# Runs under abcd (example) in graphical session; inherits DISPLAY, XAUTHORITY, HOME, USER, PATH, SHELL
set -e
LOCKFILE="${PYTERLINK_CONTROL:-$HOME/.control}/.run.lock"
TRIGGER_RUN="${PYTERLINK_CONTROL:-$HOME/.control}/dosbox-run-lnkserver.txt"
TRIGGER_STOP="${PYTERLINK_CONTROL:-$HOME/.control}/dosbox-stop-lnkserver.txt"

# Create lock (exclusive mechanism)
if [ -f "$LOCKFILE" ]; then
    echo "Lock already exists ($LOCKFILE); server may already be running. Aborting." >&2
    exit 2
fi
echo "Starting server (run trigger found: $TRIGGER_RUN)" >> /home/abcd/foo.log 2>/dev/null || true
touch "$LOCKFILE"
# Actual work would call the dosbox_x server script here, inheriting env
# Example: DISPLAY="$DISPLAY" XAUTHORITY="$XAUTHORITY" HOME="$HOME" USER="$USER" PATH="$PATH" SHELL="$SHELL" exec /path/to/Pyterlink/dosbox_x/run-lnkserver.sh
