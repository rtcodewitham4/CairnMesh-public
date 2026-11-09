package ext

import "cairnmesh/firmware/proto"

// Detector decides from the samples when a condition begins. Detect
// returns true once per beginning; Reset forgets the history (the next
// sample sets the baseline again).
type Detector interface {
	Detect(sample int32, now proto.Time) bool
	Reset()
}

// Edge watches a level (0 or not 0): it fires when the level has changed
// and held the new value for Debounce, on the edges asked for. The first
// sample is the baseline and never fires.
type Edge struct {
	Rising, Falling bool
	Debounce        proto.Time

	primed, stable, candidate bool
	since                     proto.Time
}

func (e *Edge) Detect(sample int32, now proto.Time) bool {
	level := sample != 0
	if !e.primed {
		e.primed, e.stable, e.candidate = true, level, level
		return false
	}
	if level != e.candidate {
		e.candidate, e.since = level, now
	}
	if e.candidate == e.stable || now-e.since < e.Debounce {
		return false
	}
	e.stable = e.candidate
	return e.stable && e.Rising || !e.stable && e.Falling
}

func (e *Edge) Reset() { e.primed = false }

// Threshold fires when the sample reaches Level (from below, or from above
// with Below) and re-arms once it is back past Level by Hysteresis. A
// sample already past Level at the start fires: the condition holds.
type Threshold struct {
	Level, Hysteresis int32
	Below             bool

	tripped bool
}

func (t *Threshold) Detect(sample int32, _ proto.Time) bool {
	past, back := sample >= t.Level, sample < t.Level-t.Hysteresis
	if t.Below {
		past, back = sample <= t.Level, sample > t.Level+t.Hysteresis
	}
	switch {
	case !t.tripped && past:
		t.tripped = true
		return true
	case t.tripped && back:
		t.tripped = false
	}
	return false
}

func (t *Threshold) Reset() { t.tripped = false }

// Change fires when the sample has moved by at least Delta from the last
// one it fired on (the first sample fires): telemetry that speaks only
// when something changed.
type Change struct {
	Delta int32

	primed bool
	last   int32
}

func (c *Change) Detect(sample int32, _ proto.Time) bool {
	d := sample - c.last
	if d < 0 {
		d = -d
	}
	if c.primed && d < c.Delta {
		return false
	}
	c.primed, c.last = true, sample
	return true
}

func (c *Change) Reset() { c.primed = false }

// Nonzero fires on every sample that is not 0: with a Latch, on every
// batch of interrupts.
type Nonzero struct{}

func (Nonzero) Detect(sample int32, _ proto.Time) bool { return sample != 0 }
func (Nonzero) Reset()                                 {}

// Always fires on every sample: with Watch.Period, a periodic task.
type Always struct{}

func (Always) Detect(int32, proto.Time) bool { return true }
func (Always) Reset()                        {}
