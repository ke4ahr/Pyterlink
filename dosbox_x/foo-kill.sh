#!/bin/sh
# foo-kill.sh -- triggered by monit when $PYTERLINK_CONTROL (default ~/.control)/dosbox-stop-lnkserver.txt exists
# Removes trigger files and lock; only valid after foo.sh created lock.
LOCKFILE="${PYTERLINK_CONTROL:-$HOME/.control}/.run.lock"
TRIGGER_RUN="${PYTERLINK_CONTROL:-$HOME/.control}/dosbox-run-lnkserver.txt"
TRIGGER_STOP="${PYTERLINK_CONTROL:-$HOME/.control}/dosbox-stop-lnkserver.txt"

if [ ! -f "$LOCKFILE" ]; then
    echo "No lock file ($LOCKFILE); server not running. Aborting kill." >&2
    exit 2
fi

echo "Stopping server; removing lock and triggers." >> /home/abcd/foo.log 2>/dev/null || true
# Remove lock
rm -f "$LOCKFILE"
# Remove trigger files (as requested: once kill issued, remove dosbox-*lnkserver.txt files)
rm -f "$TRIGGER_RUN" "$TRIGGER_STOP"
# Actual kill command would go here, inheriting env
# Example: DISPLAY="$DISPLAY" XAUTHORITY="$XAUTHORITY" HOME="$HOME" USER="$USER" PATH="$PATH" SHELL="$SHELL" ...
