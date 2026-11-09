#!/bin/sh
# Assert which test-only features a wle5node image contains.
#
#   tools/wle5-assert-prod.sh prod FILE              neither feature; exit 0, else exit 1
#   tools/wle5-assert-prod.sh testfilter FILE        receive filter only
#   tools/wle5-assert-prod.sh bench FILE             actions 0x02-0x05 only
#   tools/wle5-assert-prod.sh testfilter-bench FILE  both
#
# The feature files are selected by build tags (cairn_testfilter and
# cairn_testbed), so excluding a file excludes its code and markers. Production
# must contain neither the receive filter nor the bench action module.
set -eu

[ $# -eq 2 ] || { echo "usage: $0 prod|testfilter|bench|testfilter-bench FILE" >&2; exit 2; }
mode=$1 file=$2
[ -f "$file" ] || { echo "FAIL: $file not found" >&2; exit 2; }

# Case-sensitive on purpose: the default build says "testfilter none" in status.
filter_marks=$(grep -a -o -e 'TESTFILTER' -e 'firmware/rxfilter' -e 'RxFilter' -e 'rxhook_on' "$file" | sort -u | tr '\n' ' ' || true)
bench_marks=$(grep -a -o -e 'BENCHACTIONS' -e 'firmware/testbed/benchactions' "$file" | sort -u | tr '\n' ' ' || true)

case $mode in
prod)
	[ -z "$filter_marks" ] || { echo "FAIL: $file contains the receive test filter ($filter_marks): a production image must not" >&2; exit 1; }
	[ -z "$bench_marks" ] || { echo "FAIL: $file contains bench actions ($bench_marks): a production image must not" >&2; exit 1; }
	echo "ok: $file has neither the receive test filter nor bench actions"
	;;
testfilter)
	[ -n "$filter_marks" ] || { echo "FAIL: $file (a WLE5_TESTFILTER=1 build) has none of the filter's marks" >&2; exit 1; }
	[ -z "$bench_marks" ] || { echo "FAIL: $file (WLE5_TESTFILTER=1) unexpectedly contains bench actions ($bench_marks)" >&2; exit 1; }
	echo "ok: $file is a testfilter build (marks: $filter_marks)"
	;;
bench)
	[ -n "$bench_marks" ] || { echo "FAIL: $file (a WLE5_BENCH_ACTIONS=1 build) has none of the bench-action marks" >&2; exit 1; }
	[ -z "$filter_marks" ] || { echo "FAIL: $file (WLE5_BENCH_ACTIONS=1) unexpectedly contains the receive test filter ($filter_marks)" >&2; exit 1; }
	echo "ok: $file is a bench-action build (marks: $bench_marks)"
	;;
testfilter-bench)
	[ -n "$filter_marks" ] || { echo "FAIL: $file has none of the receive-filter marks" >&2; exit 1; }
	[ -n "$bench_marks" ] || { echo "FAIL: $file has none of the bench-action marks" >&2; exit 1; }
	echo "ok: $file is a combined test build (filter: $filter_marks; bench: $bench_marks)"
	;;
*) echo "usage: $0 prod|testfilter|bench|testfilter-bench FILE" >&2; exit 2 ;;
esac
