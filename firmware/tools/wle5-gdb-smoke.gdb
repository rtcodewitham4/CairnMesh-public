# gdb smoke test for an E77 running a TinyGo program, through the OpenOCD server of
# tools/wle5-gdb.sh (which passes the port, GDB_PORT, in the first -ex):
#   gdb-multiarch -batch -ex 'target extended-remote :3333' -x tools/wle5-gdb-smoke.gdb build/wle5-uarthello.elf
set pagination off
set confirm off
# The caller connects first: -ex 'target extended-remote :<port>' (the port is not fixed here).
# TinyGo's entry is main.main (nm: "t main.main"); flash breakpoints are hardware ones.
hbreak main.main
monitor reset halt
continue
info registers pc
bt
delete breakpoints
# Clear the reset vector catch the server set (DEMCR.VC_CORERESET) so the next reset runs freely.
monitor mww 0xE000EDFC 0
monitor resume
detach
quit
