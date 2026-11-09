//go:build cairn_wle5 && !wle5_allow_high_power

package wle5

// allowHighPower: the default build never sends more than safety.MaxPowerDBm
// (10 dBm). Only the build tag wle5_allow_high_power lifts it.
const allowHighPower = false
