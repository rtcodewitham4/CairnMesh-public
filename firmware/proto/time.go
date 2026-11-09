package proto

// Time is a point in time or a duration in microseconds, counted from boot
// (the board has no RTC that survives a supply cut, spec 4.1).
type Time int64

const (
	Microsecond Time = 1
	Millisecond Time = 1000
	Second      Time = 1000000
	// Never is later than any time the node will see.
	Never Time = 1 << 62
)

// Rand is the source of the protocol's random draws. Uniform returns a
// value in [lo, hi]. The behaviour vectors (spec 16.1, 16.2) replay with
// Midpoint, which the firmware must offer as a switch ("jitter off").
type Rand interface {
	Uniform(lo, hi Time) Time
}

// Midpoint is the jitter-off source: Uniform(lo, hi) = (lo + hi) / 2.
type Midpoint struct{}

func (Midpoint) Uniform(lo, hi Time) Time { return (lo + hi) / 2 }

// scale is d × [lo%, hi%], drawn from r.
func scale(r Rand, d Time, lo, hi Time) Time {
	if d >= Never {
		return Never
	}
	return r.Uniform(d*lo/100, d*hi/100)
}

// after is now + d, saturating at Never.
func after(now, d Time) Time {
	if d >= Never-now {
		return Never
	}
	return now + d
}
