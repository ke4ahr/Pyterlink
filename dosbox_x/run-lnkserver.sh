#!/bin/sh
# Launch the DOS-side LNKSERVER (server) hard-drive image under
# dosbox or dosbox-x. Boots a real MS-DOS 6.22 from
# dosbox_x/images/lnkserver-msdos.img via IMGMOUNT + BOOT, with
# LNKSERVER auto-launched from AUTOEXEC.BAT.
#
# Usage:
#   dosbox_x/run-lnkserver.sh [engine]
#
# engine: "dosbox" (default; the apt dosbox 0.74-3-4, unconfined,
#         opens /dev/ttyUSB0 directly) or "dosbox-x" (the snap;
#         requires `sudo snap connect dosbox-x:raw-usb` first or
#         the real serial port will fail to open -- see
#         dosbox_x/README.md "Snap confinement").
#
# Real hardware setup (per dosbox_x/README.md):
#   /dev/ttyUSB0 and /dev/ttyUSB1 wired together with a null-modem
#   cable. This script opens /dev/ttyUSB0 via the matching conf
#   (lnkserver-msdos.conf). The other end (/dev/ttyUSB1) is for the
#   Linux peer (pyterlink-server, or the dosbox_x/lnkclient-msdos
#   side in a two-dosbox setup).

ENGINE="${1:-dosbox}"

# Resolve paths relative to this script so it works regardless of
# the caller's cwd.
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
CONF="$SCRIPT_DIR/lnkserver-msdos.conf"
cd ${SCRIPT_DIR}

if [ ! -f "$CONF" ]; then
    echo "run-lnkserver.sh: conf not found: $CONF" >&2
    exit 1
fi

case "$ENGINE" in
    dosbox)
        if ! command -v dosbox >/dev/null 2>&1; then
            echo "run-lnkserver.sh: 'dosbox' not found in PATH" >&2
            echo "  install with: sudo apt install dosbox" >&2
            exit 1
        fi
	touch "${PYTERLINK_CONTROL:-$HOME/.control}"/dosbox_lnkserver_started.txt
        exec dosbox -conf "$CONF" 2>&1 | ../tools/klog_tee.py -a lnkserver.log
        ;;
    dosbox-x)
        if ! command -v dosbox-x >/dev/null 2>&1; then
            echo "run-lnkserver.sh: 'dosbox-x' not found in PATH" >&2
            echo "  install with: sudo snap install dosbox-x" >&2
            exit 1
        fi
        echo "run-lnkserver.sh: if /dev/ttyUSB0 fails to open with" >&2
        echo "  'System error 9', run:" >&2
        echo "    sudo snap connect dosbox-x:raw-usb" >&2
        echo "  (see dosbox_x/README.md \"Snap confinement\")" >&2
	touch "${PYTERLINK_CONTROL:-$HOME/.control}"/dosbox_lnkserver_started.txt
        exec dosbox-x -conf "$CONF" 2>&1 | ../tools/klog_tee.py -a lnkserver.log
        ;;
    -h|--help|help)
        echo "Usage: $0 [engine]" >&2
        echo "  engine: dosbox (default) or dosbox-x" >&2
        ;;
    *)
        echo "run-lnkserver.sh: unknown engine: $ENGINE" >&2
        echo "  expected 'dosbox' or 'dosbox-x'" >&2
        exit 2
        ;;
esac
