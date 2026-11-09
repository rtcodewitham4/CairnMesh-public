#!/bin/sh
# Check or set up the TinyGo tree against patches/tinygo.lock (TINYGO_LOCK=tinygo-wle5.lock: the WLE5 tree).
#
#   tools/tinygo.sh check TINYGOROOT [TINYGO]   every line of the lock holds (make check-env)
#   tools/tinygo.sh apply TINYGOROOT            a clean clone at the commit: apply the diff, copy the files
#
# apply refuses a tree that is not clean at the locked commit: it never
# touches a working TinyGo (changing ~/src/tinygo needs the user's consent).
set -eu
cd "$(dirname "$0")/../patches"
mode=$1 root=$2 tinygo=${3:-tinygo}
fail() { echo "error: $*" >&2; exit 1; }
lock=${TINYGO_LOCK:-tinygo.lock}
field() { awk -v k="$1" '$1 == k { print $2 }' "$lock"; }
version=$(field version) commit=$(field commit) diff=$(field diff)
[ -d "$root/.git" ] || fail "$root is not a TinyGo git clone"
head=$(git -C "$root" rev-parse HEAD)
case $mode in
check)
	"$tinygo" version | grep -q "tinygo version $version " ||
		fail "tinygo $version expected, found: $("$tinygo" version)"
	[ "$head" = "$commit" ] || fail "$root is at $head, patches/tinygo.lock wants $commit ($lock)"
	git -C "$root" -c core.pager=cat diff --no-color | cmp -s - "$diff" ||
		fail "$root: tracked changes differ from patches/$diff (git -C $root diff | diff - patches/$diff)"
	awk '$1 == "file" { print $2, $3 }' "$lock" | while read -r copy path; do
		cmp -s "$copy" "$root/$path" || fail "$root/$path differs from patches/$copy"
	done
	;;
apply)
	[ "$head" = "$commit" ] || fail "check out $commit in $root first"
	[ -z "$(git -C "$root" status --porcelain --untracked-files=no)" ] || fail "$root has changes: apply needs a clean clone"
	git -C "$root" apply "$PWD/$diff"
	awk '$1 == "file" { print $2, $3 }' "$lock" | while read -r copy path; do
		mkdir -p "$(dirname "$root/$path")"
		cp "$copy" "$root/$path"
	done
	echo "applied patches/$lock to $root; lib/clang still comes from the TinyGo $version release (see README)"
	;;
*) fail "usage: tools/tinygo.sh check|apply TINYGOROOT [TINYGO]" ;;
esac
