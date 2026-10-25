#!/bin/sh
# Low-latency settings for USB serial adapters (FTDI, PL2303).
#
# LNKCLIENT/LNKSERVER turn the line around on every frame (tag, echo, data,
# ack). USB adapters batch received bytes, which adds up to that batching
# delay per turnaround:
#   FTDI (ftdi_sio)  -- per-port latency timer, default 16 ms, settable
#                       1..255 ms in sysfs: latency_timer.
#   PL2303 (pl2303)  -- no latency timer. The only knob is the tty's
#                       ASYNC_LOW_LATENCY flag (setserial low_latency);
#                       recent kernels accept but do not keep or use it for
#                       USB serial, in which case there is nothing to set and
#                       the script says so.
# Other drivers are reported and left alone.
#
# Usage:
#   tools/serial_latency.sh [set|reset|status|udev] [/dev/ttyUSBn ...]
#     set     FTDI latency_timer=1, PL2303 low_latency   (default)
#     reset   FTDI latency_timer=16, PL2303 ^low_latency
#     status  show driver and current setting per port
#     udev    print a udev rule that applies the FTDI setting at plug-in
#   With no ports given: every /dev/ttyUSB*. set/reset need root.
#
# Settings last until the adapter is unplugged (or the system reboots);
# use the udev rule to make the FTDI setting permanent.

ACTION="${1:-set}"
[ $# -gt 0 ] && shift
FTDI_FAST=1
FTDI_DEFAULT=16

driver_of() {   # /dev/ttyUSBn -> driver name
    n=$(basename "$1")
    d=$(readlink -f "/sys/class/tty/$n/device/driver" 2>/dev/null)
    [ -n "$d" ] && basename "$d"
}

ftdi_timer() {  # /dev/ttyUSBn -> sysfs latency_timer path
    echo "/sys/bus/usb-serial/devices/$(basename "$1")/latency_timer"
}

low_latency_flag() {
    if command -v setserial >/dev/null 2>&1; then
        if setserial -a "$1" 2>/dev/null | grep -q low_latency; then
            echo "low_latency on"
        else
            echo "low_latency off"
        fi
    else
        echo "low_latency unknown (setserial not installed)"
    fi
}

need_setserial() {
    command -v setserial >/dev/null 2>&1 && return 0
    echo "  setserial not installed (e.g. sudo apt install setserial)" >&2
    return 1
}

one() {
    port=$1
    drv=$(driver_of "$port")
    case "$drv" in
        ftdi_sio)
            t=$(ftdi_timer "$port")
            case "$ACTION" in
                set)    echo "$FTDI_FAST" > "$t" ;;
                reset)  echo "$FTDI_DEFAULT" > "$t" ;;
            esac
            echo "$port: FTDI, latency_timer $(cat "$t" 2>/dev/null) ms"
            ;;
        pl2303)
            # A PL2303 has no latency timer; the tty low_latency flag is the
            # only knob, and recent kernels ignore it for USB serial.
            if [ "$ACTION" = set ] || [ "$ACTION" = reset ]; then
                flag=low_latency
                [ "$ACTION" = reset ] && flag=^low_latency
                if need_setserial && ! err=$(setserial "$port" "$flag" 2>&1)
                then
                    echo "$port: setserial $flag failed: $err" >&2
                fi
            fi
            case "$(low_latency_flag "$port")" in
                "low_latency on")
                    echo "$port: PL2303, low latency on (low_latency flag set)" ;;
                "low_latency off")
                    if [ "$ACTION" = reset ]; then
                        echo "$port: PL2303, low_latency flag cleared"
                    else
                        echo "$port: PL2303, no latency setting to change" \
                             "(no latency timer; this kernel ignores the" \
                             "low_latency flag) -- nothing to do"
                    fi ;;
                *)
                    echo "$port: PL2303, $(low_latency_flag "$port")" ;;
            esac
            ;;
        "")
            echo "$port: no such USB serial port" >&2
            ;;
        *)
            echo "$port: driver $drv (not FTDI or PL2303), left alone;" \
                 "$(low_latency_flag "$port")"
            ;;
    esac
}

case "$ACTION" in
    set|reset|status) ;;
    udev)
        cat <<'EOF'
# /etc/udev/rules.d/99-usb-serial-latency.rules
# FTDI adapters: 1 ms latency timer at plug-in (pyterlink / LNKCLIENT).
ACTION=="add", SUBSYSTEM=="usb-serial", DRIVER=="ftdi_sio", ATTR{latency_timer}="1"
# then: sudo udevadm control --reload-rules && sudo udevadm trigger
# (PL2303 has no latency timer, so there is no rule for it.)
EOF
        exit 0
        ;;
    -h|--help|help)
        sed -n "2,26p" "$0" | sed 's/^# \{0,1\}//'
        exit 0
        ;;
    *)
        echo "serial_latency.sh: unknown action '$ACTION' (set|reset|status|udev)" >&2
        exit 2
        ;;
esac

if [ "$ACTION" != status ] && [ "$(id -u)" -ne 0 ]; then
    echo "serial_latency.sh: '$ACTION' needs root (sudo $0 $ACTION $*)" >&2
    exit 1
fi

if [ $# -eq 0 ]; then
    set -- /dev/ttyUSB*
    [ -e "$1" ] || { echo "no /dev/ttyUSB* ports found" >&2; exit 1; }
fi
for p in "$@"; do
    one "$p"
done
