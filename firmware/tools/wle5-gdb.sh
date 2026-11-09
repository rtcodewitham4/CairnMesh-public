#!/bin/sh
# OpenOCD gdb server for an E77 (STM32WLE5) over the J-Link, same connect as wle5-openocd.sh
# (NRST held low, connect, reset vector catch, release NRST: the core waits at the reset vector).
#
#   tools/wle5-gdb.sh [server] [FILE.elf]   run the gdb server (Ctrl-C stops it and lets the program run)
#   tools/wle5-gdb.sh smoke [FILE.elf]      server + gdb batch with tools/wle5-gdb-smoke.gdb, then release
#   env: GDB_PORT (3333)  BENCH_LOCK
# FILE.elf defaults to $BUILD_DIR/wle5-uarthello.elf (build/ as in the Makefile). Debug info is on by default in TinyGo; see
# `make wle5-build WLE5_OPT=1` (less optimised, easier to step) in the Makefile.
set -eu
cd "$(dirname "$0")/.."

mode=server
case ${1:-} in server | smoke) mode=$1; shift ;; esac
elf=${1:-${BUILD_DIR:-build}/wle5-uarthello.elf}
port=${GDB_PORT:-3333}
lock=${BENCH_LOCK:-$HOME/.cache/cairnmesh/bench.lock}

[ -f "$elf" ] || { echo "error: $elf not found: make wle5-build CMD=uarthello" >&2; exit 1; }
lsusb | grep -q '1366:' || { echo "error: no J-Link (1366:xxxx) in lsusb" >&2; exit 1; }
if command -v arm-none-eabi-gdb >/dev/null 2>&1; then gdb=arm-none-eabi-gdb
elif command -v gdb-multiarch >/dev/null 2>&1; then gdb=gdb-multiarch
else echo "error: neither arm-none-eabi-gdb nor gdb-multiarch in PATH" >&2; exit 1; fi
readelf -S "$elf" 2>/dev/null | grep -q '\.debug_info' ||
	echo "warning: $elf has no .debug_info (built with -no-debug?); rebuild with make wle5-build" >&2

mkdir -p "$(dirname "$lock")"
exec 9>"$lock"
flock -n 9 || { echo "error: bench lock $lock is held by someone else" >&2; exit 1; }

base='openocd -f interface/jlink.cfg -c "transport select swd" -c "adapter speed 2000" -c "reset_config srst_only srst_nogate srst_open_drain connect_assert_srst" -c "bindto 127.0.0.1" -c "gdb_port '$port'" -f target/stm32wlx.cfg -c init'
hold='-c "mww 0xE000EDF0 0xA05F0001" -c "mww 0xE000EDFC 0x01000001" -c "adapter deassert srst" -c "sleep 300"'
release='-c "mww 0xE000EDFC 0" -c "mww 0xE000EDF0 0xA05F0000" -c "adapter assert srst" -c "sleep 100" -c "adapter deassert srst" -c shutdown'

pid=
cleanup() {
	trap '' EXIT INT TERM
	[ -z "$pid" ] || kill "$pid" 2>/dev/null || true
	[ -z "$pid" ] || wait "$pid" 2>/dev/null || true
	# Clear the catch and pulse NRST so the board runs the program again.
	eval "$base $hold $release" >/dev/null 2>&1 || true
}
trap cleanup EXIT
trap 'exit 130' INT TERM

echo "gdb server on 127.0.0.1:$port; connect with:" >&2
echo "  $gdb -ex 'target extended-remote :$port' $elf" >&2
echo "  $gdb -batch -ex 'target extended-remote :$port' -x tools/wle5-gdb-smoke.gdb $elf" >&2

eval "exec $base $hold" &
pid=$!

case $mode in
server) wait "$pid" ;;
smoke)
	sleep 3
	kill -0 "$pid" 2>/dev/null || { echo "FAIL: OpenOCD exited before gdb connected" >&2; exit 1; }
	out=$("$gdb" -batch -ex "target extended-remote :$port" -x tools/wle5-gdb-smoke.gdb "$elf" 2>&1) || { echo "$out" >&2; echo "FAIL: gdb exited non-zero" >&2; exit 1; }
	echo "$out"
	echo "$out" | grep -q 'main\.main' || { echo "FAIL: no main.main in the gdb output (breakpoint not hit)" >&2; exit 1; }
	echo "PASS: halted in main.main" ;;
esac
