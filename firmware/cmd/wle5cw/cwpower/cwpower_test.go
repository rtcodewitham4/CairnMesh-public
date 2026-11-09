package cwpower

import "testing"

func TestParse(t *testing.T) {
	ok := []struct {
		in   string
		want int8
	}{{"", 10}, {"10", 10}, {"0", 0}, {"+5", 5}, {"-9", -9}, {"00010", 10}, {"-0", 0}}
	for _, c := range ok {
		got, err := Parse(c.in)
		if err != nil || got != c.want {
			t.Errorf("Parse(%q) = %d, %v; want %d", c.in, got, err, c.want)
		}
	}
	bad := []struct {
		in   string
		want error
	}{
		{"11", ErrTooHigh}, {"22", ErrTooHigh}, {"127", ErrTooHigh}, {"128", ErrTooHigh},
		{"99999999999999999999", ErrTooHigh}, {"-10", ErrTooLow}, {"-99999999999999999999", ErrTooLow},
		{"abc", ErrNotNumber}, {"1 0", ErrNotNumber}, {"+", ErrNotNumber}, {"-", ErrNotNumber},
		{"10.5", ErrNotNumber}, {" 10", ErrNotNumber},
	}
	for _, c := range bad {
		if got, err := Parse(c.in); err != c.want || got != 0 {
			t.Errorf("Parse(%q) = %d, %v; want refused with %v", c.in, got, err, c.want)
		}
	}
}

// The ceiling is a deliberate design choice; change it only on purpose.
func TestCeiling(t *testing.T) {
	if Max != 10 || Default > Max || Min != -9 {
		t.Fatalf("Max=%d Default=%d Min=%d", Max, Default, Min)
	}
}
