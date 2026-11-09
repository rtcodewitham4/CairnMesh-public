package buildbounds

import (
	"encoding/json"
	"os"
	"os/exec"
	"slices"
	"strings"
	"testing"
)

type listedPackage struct {
	GoFiles        []string
	IgnoredGoFiles []string
	Deps           []string
}

func list(t *testing.T, tags, path string) listedPackage {
	t.Helper()
	args := []string{"list", "-json"}
	if tags != "" {
		args = append(args, "-tags", tags)
	}
	args = append(args, path)
	cmd := exec.Command("go", args...)
	cmd.Dir = "../.." // firmware/
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("go %s: %v\n%s", strings.Join(args, " "), err, out)
	}
	var p listedPackage
	if err := json.Unmarshal(out, &p); err != nil {
		t.Fatalf("decode go list output: %v\n%s", err, out)
	}
	return p
}

func requireFile(t *testing.T, p listedPackage, name string, want bool) {
	t.Helper()
	got := slices.Contains(p.GoFiles, name)
	if got != want {
		t.Fatalf("%s included = %v, want %v (files: %v)", name, got, want, p.GoFiles)
	}
}

func requireDep(t *testing.T, p listedPackage, name string, want bool) {
	t.Helper()
	got := slices.Contains(p.Deps, name)
	if got != want {
		t.Fatalf("%s dependency = %v, want %v", name, got, want)
	}
}

func TestGatewayTestbedFaultIsBuildTagged(t *testing.T) {
	prod := list(t, "", "./cmd/gateway")
	requireFile(t, prod, "testbed_off.go", true)
	requireFile(t, prod, "testbed_on.go", false)

	testbed := list(t, "cairn_testbed", "./cmd/gateway")
	requireFile(t, testbed, "testbed_off.go", false)
	requireFile(t, testbed, "testbed_on.go", true)
}

func TestWLE5BenchActionsAreBuildTagged(t *testing.T) {
	prod := list(t, "cairn_wle5", "./cmd/wle5node")
	requireFile(t, prod, "testbed_actions_off.go", true)
	requireFile(t, prod, "testbed_actions_on.go", false)
	if slices.Contains(prod.GoFiles, "testfilter_on.go") {
		t.Fatal("production wle5node unexpectedly has the receive-test-filter file")
	}

	testbed := list(t, "cairn_wle5 cairn_testbed", "./cmd/wle5node")
	requireFile(t, testbed, "testbed_actions_off.go", false)
	requireFile(t, testbed, "testbed_actions_on.go", true)
	requireDep(t, prod, "cairnmesh/firmware/testbed/benchactions", false)
	requireDep(t, testbed, "cairnmesh/firmware/testbed/benchactions", true)
	if slices.Contains(testbed.GoFiles, "testfilter_on.go") {
		t.Fatal("WLE5_BENCH_ACTIONS changed the semantics of WLE5_TESTFILTER")
	}

	filterOnly := list(t, "cairn_wle5 cairn_testfilter", "./cmd/wle5node")
	requireFile(t, filterOnly, "testfilter_on.go", true)
	requireFile(t, filterOnly, "testbed_actions_on.go", false)
	requireDep(t, filterOnly, "cairnmesh/firmware/testbed/benchactions", false)
}

func TestGatewayFaultIsRejectedForWLE5Node(t *testing.T) {
	cmd := exec.Command("make", "-n", "wle5-build", "CMD=wle5node", "GATEWAY_BENCH_FAULT=radio1")
	cmd.Dir = "../.."
	cmd.Env = append(os.Environ(), "MAKEFLAGS=")
	out, err := cmd.CombinedOutput()
	if err == nil {
		t.Fatal("GATEWAY_BENCH_FAULT with CMD=wle5node did not fail")
	}
	if !strings.Contains(string(out), "GATEWAY_BENCH_FAULT is only for CMD=gateway") {
		t.Fatalf("wrong error:\n%s", out)
	}
}

func TestGatewayImageAssertion(t *testing.T) {
	for _, c := range []struct {
		name, mode, content string
		ok                  bool
	}{
		{"production clean", "prod", "gateway without testbed code", true},
		{"production leak", "prod", "main.gatewayBenchFault", false},
		{"testbed marker", "testbed", "main.gatewayBenchFault", true},
		{"testbed empty", "testbed", "gateway without testbed code", false},
	} {
		t.Run(c.name, func(t *testing.T) {
			f, err := os.CreateTemp(t.TempDir(), "gateway-image-*")
			if err != nil {
				t.Fatal(err)
			}
			if _, err := f.WriteString(c.content); err != nil {
				t.Fatal(err)
			}
			if err := f.Close(); err != nil {
				t.Fatal(err)
			}
			cmd := exec.Command("sh", "tools/gateway-assert-prod.sh", c.mode, f.Name())
			cmd.Dir = "../.."
			out, err := cmd.CombinedOutput()
			if (err == nil) != c.ok {
				t.Fatalf("exit %v, want ok=%v:\n%s", err, c.ok, out)
			}
		})
	}
}
