package main

import (
	"regexp"
	"strings"
	"testing"
	"time"
)

// luhnValid mirrors the checksum the analyzer uses to gate card-number
// redaction; fakeCard() must satisfy it or the card rule wouldn't fire.
func luhnValid(digits string) bool {
	sum, alt := 0, false
	for i := len(digits) - 1; i >= 0; i-- {
		d := int(digits[i] - '0')
		if alt {
			if d *= 2; d > 9 {
				d -= 9
			}
		}
		sum += d
		alt = !alt
	}
	return sum%10 == 0
}

func TestFakeCardIsLuhnValid16Digits(t *testing.T) {
	for i := 0; i < 1000; i++ {
		c := fakeCard()
		if len(c) != 16 {
			t.Fatalf("fakeCard() = %q, want 16 digits, got %d", c, len(c))
		}
		if c[0] != '4' {
			t.Fatalf("fakeCard() = %q, want Visa-style leading 4", c)
		}
		if !luhnValid(c) {
			t.Fatalf("fakeCard() = %q, fails Luhn, analyzer's card rule would skip it", c)
		}
	}
}

// The fake-PII generators must match the shapes the analyzer's masking regexes
// key on; drift here silently stops the fixture from covering them.
func TestFakePIIShapesMatchAnalyzerRules(t *testing.T) {
	cases := []struct {
		name    string
		got     string
		pattern string
	}{
		{"email", fakeEmail(), `^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$`},
		{"iban", fakeIBAN(), `^[A-Z]{2}\d{2}[A-Z0-9]{11,30}$`},
		{"phone", fakePhone(), `^\+\d{8,15}$`},
		{"ip", fakeIP(), `^(?:\d{1,3}\.){3}\d{1,3}$`},
		{"jwt", fakeJWT(), `^eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$`},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if !regexp.MustCompile(c.pattern).MatchString(c.got) {
				t.Fatalf("%s() = %q, does not match analyzer rule %s", c.name, c.got, c.pattern)
			}
		})
	}
}

func TestCatalogueKeysAreUniqueKebab(t *testing.T) {
	seen := map[string]bool{}
	kebab := regexp.MustCompile(`^[a-z0-9]+(-[a-z0-9]+)*$`)
	for _, s := range catalogue {
		if seen[s.typ] {
			t.Errorf("duplicate scenario type %q", s.typ)
		}
		seen[s.typ] = true
		if !kebab.MatchString(s.typ) {
			t.Errorf("scenario type %q is not kebab-case", s.typ)
		}
		if s.message == "" {
			t.Errorf("scenario %q has an empty message", s.typ)
		}
		if s.httpStatus < 100 || s.httpStatus > 599 {
			t.Errorf("scenario %q has implausible httpStatus %d", s.typ, s.httpStatus)
		}
	}
	if len(catalogue) == 0 {
		t.Fatal("catalogue is empty")
	}
}

func TestScenarioByTypeCoversCatalogue(t *testing.T) {
	if len(scenarioByType) != len(catalogue) {
		t.Fatalf("scenarioByType has %d entries, catalogue has %d", len(scenarioByType), len(catalogue))
	}
	for _, s := range catalogue {
		if got, ok := scenarioByType[s.typ]; !ok || got.typ != s.typ {
			t.Errorf("scenarioByType[%q] missing or mismatched", s.typ)
		}
	}
}

// The analyzer keys panic detection on the "nil-pointer-panic" error_type that
// the recover middleware logs, so the catalogue must stay in sync.
func TestPanicScenarioPresent(t *testing.T) {
	if _, ok := scenarioByType["nil-pointer-panic"]; !ok {
		t.Fatal("catalogue must contain nil-pointer-panic (the analyzer's panic detector keys on it)")
	}
}

func TestKnownTypesSorted(t *testing.T) {
	got := knownTypes()
	if len(got) != len(catalogue) {
		t.Fatalf("knownTypes() returned %d, want %d", len(got), len(catalogue))
	}
	for i := 1; i < len(got); i++ {
		if got[i-1] > got[i] {
			t.Errorf("knownTypes() not sorted at %d: %q > %q", i, got[i-1], got[i])
		}
	}
}

func TestMetricsRenderExpositionFormat(t *testing.T) {
	m := newMetrics()
	m.inc("incidentgen_logs_emitted_total", `severity="error",type="db-connection-timeout"`)
	m.inc("incidentgen_logs_emitted_total", `severity="error",type="db-connection-timeout"`)
	m.set("incidentgen_leaked_bytes", "", 1048576)

	out := m.render()

	if !strings.Contains(out, "# HELP incidentgen_logs_emitted_total ") {
		t.Errorf("missing HELP line for counter:\n%s", out)
	}
	if !strings.Contains(out, "# TYPE incidentgen_logs_emitted_total counter") {
		t.Errorf("missing TYPE line for counter:\n%s", out)
	}
	if !strings.Contains(out, `incidentgen_logs_emitted_total{severity="error",type="db-connection-timeout"} 2`) {
		t.Errorf("counter value wrong (want 2):\n%s", out)
	}
	if !strings.Contains(out, "incidentgen_leaked_bytes 1.048576e+06") {
		t.Errorf("gauge value missing/misformatted:\n%s", out)
	}
	// HELP/TYPE preamble must be emitted once per metric name, not per series.
	if n := strings.Count(out, "# TYPE incidentgen_logs_emitted_total"); n != 1 {
		t.Errorf("TYPE preamble emitted %d times, want 1:\n%s", n, out)
	}
}

func TestSeriesKey(t *testing.T) {
	if got := seriesKey("m", ""); got != "m" {
		t.Errorf("seriesKey(m, \"\") = %q, want m", got)
	}
	if got := seriesKey("m", `a="b"`); got != `m{a="b"}` {
		t.Errorf(`seriesKey(m, a="b") = %q, want m{a="b"}`, got)
	}
}

func TestEnvHelpers(t *testing.T) {
	t.Run("int", func(t *testing.T) {
		if got := envInt("FORGEPATH_TEST_UNSET", 8080); got != 8080 {
			t.Errorf("envInt default = %d, want 8080", got)
		}
		t.Setenv("FORGEPATH_TEST_INT", "9090")
		if got := envInt("FORGEPATH_TEST_INT", 8080); got != 9090 {
			t.Errorf("envInt = %d, want 9090", got)
		}
		t.Setenv("FORGEPATH_TEST_INT", "not-a-number")
		if got := envInt("FORGEPATH_TEST_INT", 8080); got != 8080 {
			t.Errorf("envInt with garbage = %d, want fallback 8080", got)
		}
	})
	t.Run("float", func(t *testing.T) {
		t.Setenv("FORGEPATH_TEST_FLOAT", "0.9")
		if got := envFloat("FORGEPATH_TEST_FLOAT", 0.7); got != 0.9 {
			t.Errorf("envFloat = %v, want 0.9", got)
		}
	})
	t.Run("duration", func(t *testing.T) {
		t.Setenv("FORGEPATH_TEST_DUR", "2s")
		if got := envDuration("FORGEPATH_TEST_DUR", time.Second); got != 2*time.Second {
			t.Errorf("envDuration = %v, want 2s", got)
		}
		t.Setenv("FORGEPATH_TEST_DUR", "garbage")
		if got := envDuration("FORGEPATH_TEST_DUR", time.Second); got != time.Second {
			t.Errorf("envDuration with garbage = %v, want fallback 1s", got)
		}
	})
	t.Run("str", func(t *testing.T) {
		if got := envStr("FORGEPATH_TEST_UNSET", "def"); got != "def" {
			t.Errorf("envStr default = %q, want def", got)
		}
	})
}
