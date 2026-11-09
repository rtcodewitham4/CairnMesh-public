#!/bin/sh
# Assert that the gateway fault-injection code is present only in testbed images.
#
#   tools/gateway-assert-prod.sh prod FILE     no gatewayBenchFault marker
#   tools/gateway-assert-prod.sh testbed FILE  gatewayBenchFault marker present
set -eu

[ $# -eq 2 ] || { echo "usage: $0 prod|testbed FILE" >&2; exit 2; }
mode=$1 file=$2
[ -f "$file" ] || { echo "FAIL: $file not found" >&2; exit 2; }

marks=$(grep -a -o 'gatewayBenchFault' "$file" | sort -u | tr '\n' ' ' || true)
case $mode in
prod)
	[ -z "$marks" ] || { echo "FAIL: $file contains gatewayBenchFault ($marks): a production image must not" >&2; exit 1; }
	echo "ok: $file has no gatewayBenchFault marker"
	;;
testbed)
	[ -n "$marks" ] || { echo "FAIL: $file has no gatewayBenchFault marker: the production check is vacuous" >&2; exit 1; }
	echo "ok: $file is a gateway testbed image (marks: $marks)"
	;;
*) echo "usage: $0 prod|testbed FILE" >&2; exit 2 ;;
esac
