#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# pyterlink
# Copyright (C) 2026 Kris Kirby
# https://github.com/ke4ahr/Pyterlink/
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
#
# configure_rig.sh -- fill in the paths of this computer in the DOSBox
# test rig files under dosbox_x/.
#
# The MS-DOS disk images are not part of pyterlink. Put your own images
# anywhere, set the variables below (or give them on the command line),
# and run this script once. Running it again with new values is safe:
# it finds each line by its pattern, not by its old value.
#
#   ./configure_rig.sh            change the files
#   ./configure_rig.sh -n         only show what would change
#
#   LNKSERVER_IMAGE=~/dos/lnkserver.img LNKCLIENT_IMAGE=~/dos/lnkclient.img \
#       ./configure_rig.sh
#
# Files it changes:
#   dosbox_x/lnkserver-msdos.conf, dosbox_x/lnkclient-msdos.conf
#       the imgmount line (image path and geometry) and the serial port
#   dosbox_x/run-*.sh, stop-*.sh, foo.sh, foo-kill.sh
#       the default control directory for trigger files
#   dosbox_x/monit_abcd_config.rc
#       the control directory, the checkout, and where monit keeps its
#       control file, log, id and state files

set -eu

# ---- Settings: change these, or set them in the environment -----------

# Where this checkout is (default: the directory of this script).
CHECKOUT="${CHECKOUT:-$(cd "$(dirname "$0")" && pwd)}"

# Your MS-DOS 6.22 hard-disk images. DOSBox runs in dosbox_x/, so a
# relative path is read from there; an absolute path is safest.
LNKSERVER_IMAGE="${LNKSERVER_IMAGE:-}"
LNKCLIENT_IMAGE="${LNKCLIENT_IMAGE:-}"

# Image geometry, as DOSBox imgmount -size: bytes per sector, sectors
# per track, heads, cylinders.
LNKSERVER_SIZE="${LNKSERVER_SIZE:-512,63,16,100}"
LNKCLIENT_SIZE="${LNKCLIENT_SIZE:-512,63,16,100}"

# The serial port each DOSBox gives DOS as COM2 (a name under /dev).
LNKSERVER_PORT="${LNKSERVER_PORT:-ttyUSB0}"
LNKCLIENT_PORT="${LNKCLIENT_PORT:-ttyUSB1}"

# Directory watched for trigger files (monit setup).
CONTROL_DIR="${CONTROL_DIR:-$HOME/.control}"

# Directory for monit's own files: its control file (monitrc), log, id
# and state files. The X authority file lets monit open DOSBox's window.
MONIT_DIR="${MONIT_DIR:-$HOME/monit}"
XAUTH_FILE="${XAUTH_FILE:-$HOME/.Xauthority}"

# ---- End of settings ---------------------------------------------------

DRY_RUN=0
case "${1:-}" in
    -n|--dry-run) DRY_RUN=1 ;;
    -h|--help) sed -n '/^# configure_rig.sh --/,/^set -eu/p' "$0" | grep '^#' | sed 's/^# \{0,1\}//'; exit 0 ;;
    "") ;;
    *) echo "configure_rig.sh: unknown option: $1 (use -n or -h)" >&2; exit 2 ;;
esac

die() { echo "configure_rig.sh: $*" >&2; exit 1; }

[ -n "$LNKSERVER_IMAGE" ] || die "set LNKSERVER_IMAGE to your LNKSERVER disk image"
[ -n "$LNKCLIENT_IMAGE" ] || die "set LNKCLIENT_IMAGE to your LNKCLIENT disk image"

# The values go into sed commands and quoted config lines: refuse
# characters that would break them.
for name in CHECKOUT LNKSERVER_IMAGE LNKCLIENT_IMAGE LNKSERVER_SIZE \
            LNKCLIENT_SIZE LNKSERVER_PORT LNKCLIENT_PORT CONTROL_DIR MONIT_DIR \
            XAUTH_FILE; do
    value="${!name}"
    case "$value" in
        *'#'*|*'&'*|*'\'*|*'"'*|*$'\n'*)
            die "$name contains one of  # & \\ \"  -- not allowed: $value" ;;
    esac
done
for name in LNKSERVER_SIZE LNKCLIENT_SIZE; do
    [[ "${!name}" =~ ^[0-9]+,[0-9]+,[0-9]+,[0-9]+$ ]] ||
        die "$name must look like 512,63,16,100: ${!name}"
done

DOSBOX_DIR="$CHECKOUT/dosbox_x"
[ -d "$DOSBOX_DIR" ] || die "no dosbox_x/ directory in $CHECKOUT"

for image in "$LNKSERVER_IMAGE" "$LNKCLIENT_IMAGE"; do
    case "$image" in /*) full="$image" ;; *) full="$DOSBOX_DIR/$image" ;; esac
    [ -f "$full" ] || echo "configure_rig.sh: warning: image not found (yet): $full" >&2
done

changed=0

# Apply sed expressions to one file. With -n, show a diff only.
update() {
    file="$DOSBOX_DIR/$1"; shift
    [ -f "$file" ] || { echo "configure_rig.sh: skipped (missing): $file" >&2; return; }
    temp=$(mktemp)
    sed -E "$@" "$file" > "$temp"
    if cmp -s "$file" "$temp"; then
        rm -f "$temp"
        return
    fi
    changed=$((changed + 1))
    if [ "$DRY_RUN" = 1 ]; then
        diff -u "$file" "$temp" | sed "1,2s#$temp#$file (new)#" || true
        rm -f "$temp"
    else
        cat "$temp" > "$file"      # keeps the file's owner and mode
        rm -f "$temp"
        echo "updated $file"
    fi
}

update lnkserver-msdos.conf \
    -e "s#^imgmount 2 \"[^\"]*\".*#imgmount 2 \"$LNKSERVER_IMAGE\" -t hdd -fs none -size $LNKSERVER_SIZE#" \
    -e "s#^serial2=directserial realport:.*#serial2=directserial realport:$LNKSERVER_PORT#"
update lnkclient-msdos.conf \
    -e "s#^imgmount 2 \"[^\"]*\".*#imgmount 2 \"$LNKCLIENT_IMAGE\" -t hdd -fs none -size $LNKCLIENT_SIZE#" \
    -e "s#^serial2=directserial realport:.*#serial2=directserial realport:$LNKCLIENT_PORT#"

for script in run-lnkserver.sh run-lnkclient.sh stop-lnkserver.sh stop-lnkclient.sh \
              foo.sh foo-kill.sh; do
    update "$script" \
        -e "s#\\\$\{PYTERLINK_CONTROL:-[^}]*\}#\${PYTERLINK_CONTROL:-$CONTROL_DIR}#g" \
        -e "s#\(default [^)]*\)#(default $CONTROL_DIR)#" \
        -e "s#exec [^ ]*/dosbox_x/run-lnkserver\.sh#exec $DOSBOX_DIR/run-lnkserver.sh#"
done

update monit_abcd_config.rc \
    -e "s#^set logfile .*#set logfile $MONIT_DIR/monit.log#" \
    -e "s#^set idfile .*#set idfile $MONIT_DIR/monit.id#" \
    -e "s#^set statefile .*#set statefile $MONIT_DIR/monit.state#" \
    -e "s#path \"[^\"]*/dosbox-(run|stop)-lnkserver\.txt\"#path \"$CONTROL_DIR/dosbox-\1-lnkserver.txt\"#" \
    -e "s#exec \"[^\"]*/dosbox_x/(run|stop)-lnkserver\.sh\"#exec \"/usr/bin/env PYTERLINK_CONTROL=$CONTROL_DIR $DOSBOX_DIR/\1-lnkserver.sh\"#" \
    -e "s#^(\# Copy this file to:).*#\1 $MONIT_DIR/monitrc  (mode 600: chmod 600 $MONIT_DIR/monitrc)#" \
    -e "s#^(\# Then start with:).*#\1  DISPLAY=:0 XAUTHORITY=$XAUTH_FILE monit -c $MONIT_DIR/monitrc -I#"

if [ "$DRY_RUN" = 1 ]; then
    echo "configure_rig.sh: $changed file(s) would change (dry run; nothing written)"
else
    echo "configure_rig.sh: $changed file(s) changed"
    [ -d "$CONTROL_DIR" ] || echo "configure_rig.sh: note: control directory $CONTROL_DIR does not exist; create it if you use the monit setup" >&2
fi
