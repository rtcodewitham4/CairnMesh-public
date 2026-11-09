#!/bin/sh
# Flash or read an STM32WLE5 over SWD with OpenOCD and a J-Link.
#
#   tools/wle5-openocd.sh flash FILE.elf     erase + write + verify, then reset
#   tools/wle5-openocd.sh dump FILE.bin      read the 256 KB flash (a backup)
#
# The E77's factory firmware turns SWD off soon after it starts, so a plain
# connect fails ("cannot read IDR"). This holds NRST low, connects, sets
# C_DEBUGEN and the reset vector catch (DEMCR.VC_CORERESET), releases NRST and
# the core stops at the reset vector; after the work the catch is cleared and
# NRST is pulsed so the program runs. NRST must be wired to the probe.
# Wrap it in the bench lock: flock -n ~/.cache/cairnmesh/bench.lock tools/wle5-openocd.sh ...
set -eu
mode=${1:-} file=${2:-}
[ -n "$file" ] || { echo "usage: $0 flash FILE.elf | dump FILE.bin" >&2; exit 2; }
hold='-c "mww 0xE000EDF0 0xA05F0001" -c "mww 0xE000EDFC 0x01000001" -c "adapter deassert srst" -c "sleep 300"'
release='-c "mww 0xE000EDFC 0" -c "mww 0xE000EDF0 0xA05F0000" -c "adapter assert srst" -c "sleep 100" -c "adapter deassert srst" -c shutdown'
base='openocd -f interface/jlink.cfg -c "transport select swd" -c "adapter speed 2000" -c "reset_config srst_only srst_nogate srst_open_drain connect_assert_srst" -f target/stm32wlx.cfg -c init'
case $mode in
flash)
	[ -f "$file" ] || { echo "error: $file not found" >&2; exit 1; }
	eval "$base $hold -c \"flash write_image erase $file\" -c \"verify_image $file\" $release"
	;;
dump)
	[ ! -e "$file" ] || { echo "error: $file exists; pick another name" >&2; exit 1; }
	eval "$base $hold -c \"dump_image $file 0x08000000 0x40000\" $release"
	chmod a-w "$file"
	;;
*) echo "usage: $0 flash FILE.elf | dump FILE.bin" >&2; exit 2 ;;
esac
