//go:build !cairn_testfilter

package node

// The receive test filter (rxhook_on.go, rxfilter) is not in this build:
// rxDrop never drops, the compiler removes the call, and Stack carries
// nothing extra. This is every default and production build.

// rxHook is the filter's state in Stack: nothing.
type rxHook struct{}

func (s *Stack) rxDrop([]byte) bool { return false }
