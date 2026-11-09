#!/bin/sh
# One shot on the bench: build cmd/$CMD (uarthello, wle5check or wle5cw), flash it over OpenOCD, read the serial port, assert.
#
#   tools/wle5-flash-check.sh [-n N] [-e] [-s]
#     -n N   uarthello: at least N consecutive "cairnmesh uarthello <n>" lines (default 5)
#            wle5check: unused; one complete pass must end in "wle5check PASS" with every check PASS
#            wle5cw: the flash STARTS A CARRIER (antenna or 50 ohm load on; each flash needs approval). Passes
#            on "CW on" ... "done"; WLE5_CW_DBM (default 10, at most 10) is built in and checked in the power line
#     -e     uarthello only: also send all 256 byte values and compare the echo (hello lines filtered out)
#     -s     skip build + flash, only read and assert (the board is already running $CMD)
#   env: WLE5_TTY (/dev/ttyUSB0)  WLE5_BAUD (115200)  CMD (uarthello | wle5check | wle5cw)  BENCH_LOCK  BUILD_DIR (build)
#        WLE5_TCXO_MV  WLE5_CW_DBM  WLE5_ANT_OK. The WLE5_* build variables reach `make wle5-build` as they are, whether given as
#        `make wle5-flash-check CMD=wle5cw WLE5_ANT_OK=1 ...` (make exports command-line variables) or in the environment.
#        WLE5_ANT_OK=1 (antenna or 50 ohm load connected) is REQUIRED for wle5cw, which otherwise does not transmit; this
#        script refuses before building. uarthello and wle5check never transmit.
#
# The bench lock is held (flock -n, no waiting) from before the flash until the port is read.
# Flashing goes through tools/wle5-openocd.sh, never JLinkExe. Needs a factory backup in
# backups/e77-factory-*.bin like `make wle5-flash`. Exit 0 = pass; otherwise one "FAIL:" line.
set -eu
cd "$(dirname "$0")/.."

n=5 echo_check= skip=
while getopts n:es opt; do
	case $opt in
	n) n=$OPTARG ;;
	e) echo_check=--echo ;;
	s) skip=1 ;;
	*) echo "usage: $0 [-n N] [-e] [-s]" >&2; exit 2 ;;
	esac
done

tty=${WLE5_TTY:-/dev/ttyUSB0}
baud=${WLE5_BAUD:-115200}
cmd=${CMD:-uarthello}
lock=${BENCH_LOCK:-$HOME/.cache/cairnmesh/bench.lock}
build_dir=${BUILD_DIR:-build} # the Makefile's BUILD_DIR (an environment value reaches make too)
elf=$build_dir/wle5-$cmd.elf
case $cmd in uarthello|wle5check|wle5cw) ;; *) echo "FAIL: CMD=$cmd has no assertion (uarthello, wle5check or wle5cw)" >&2; exit 2 ;; esac

fail() { echo "FAIL: $*" >&2; exit 1; }

[ "$cmd" != wle5cw ] || [ "${WLE5_ANT_OK:-}" = 1 ] || fail "wle5cw transmits a carrier: connect an antenna or 50 ohm load, then set WLE5_ANT_OK=1 (without it the program does not transmit)"

lsusb | grep -q '1366:' || fail "no J-Link (1366:xxxx) in lsusb"
if lsusb | grep -q '1366:0101' || ! lsusb | grep -q '1366:0105'; then
	fail "探针在 BTL 态（1366:0101），请断电重插：先拔目标板电，再拔探针 USB，先插探针再上电目标"
fi
[ -c "$tty" ] || fail "$tty not present"

if [ -z "$skip" ]; then
	ls backups/e77-factory-*.bin >/dev/null 2>&1 || fail "no backups/e77-factory-*.bin: make wle5-backup first"
	make wle5-build CMD="$cmd" >&2 || fail "make wle5-build CMD=$cmd failed"
	[ -f "$elf" ] || fail "$elf missing after build"
fi

mkdir -p "$(dirname "$lock")"
exec 9>"$lock"
flock -n 9 || fail "bench lock $lock is held by someone else"

if [ -z "$skip" ]; then
	sh tools/wle5-openocd.sh flash "$elf" >&2 || fail "OpenOCD flash of $elf failed"
fi

# shellcheck disable=SC2086  # $echo_check is one flag or nothing
python3 -I tools/wle5_uart_check.py -p "$tty" -b "$baud" -n "$n" --cmd "$cmd" $echo_check
