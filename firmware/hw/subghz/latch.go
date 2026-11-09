package subghz

import "sync/atomic"

// Latch carries the radio's interrupt from the handler to the driver's poll.
//
// The radio's IRQ line (EXTI line 44, NVIC 50) is a LEVEL: it stays high until
// the driver clears the radio's interrupt status over SUBGHZSPI. An NVIC input
// that stays high would re-enter the handler for ever, so the handler does two
// things only: Fire (remember it) and mask the NVIC line. The driver's next
// poll sees Take = fired, reads and clears the radio's status, and the poll
// after that sees Take = rearm and unmasks the line. If the radio raised
// another interrupt in between, the line is still high and the handler runs
// again at once; nothing is lost.
//
// Pure Go (atomics only) so the host can test the sequence; the NVIC calls
// are in irq.go.
type Latch struct {
	pending atomic.Bool
	rearm   atomic.Bool
}

// Fire is called by the handler: an interrupt has come and the line is masked.
func (l *Latch) Fire() { l.pending.Store(true) }

// Prime makes the next Take report fired once, to catch an interrupt raised
// before the line was enabled.
func (l *Latch) Prime() { l.pending.Store(true) }

// Take is called by the driver's poll. fired: look at the radio's status now
// (the line stays masked until the status is cleared). rearm: the status was
// cleared by the poll that saw fired, unmask the line. At most one is true.
func (l *Latch) Take() (fired, rearm bool) {
	if l.pending.Swap(false) {
		l.rearm.Store(true)
		return true, false
	}
	if l.rearm.Swap(false) {
		return false, true
	}
	return false, false
}
