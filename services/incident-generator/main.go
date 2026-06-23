// Command incident-generator is a ForgePath chaos/incident-fixture service that
// feeds error material to the observability stack and AI Incident Analyzer. A
// background goroutine emits mixed healthy/error JSON logs; HTTP endpoints
// trigger specific failures on demand. Stdlib only, so the image stays tiny and
// builds offline.
package main

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"math/rand"
	"net/http"
	"os"
	"os/signal"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
)

// scenario is one named failure mode, shared by the continuous emitter and the
// /error/{type} endpoint so background and on-demand errors look identical.
type scenario struct {
	// typ is the stable kebab-case machine key (logs, metric labels, /error/{type} path).
	typ        string
	severity   slog.Level
	httpStatus int
	// message carries an error-family keyword so the error-explorer regex catches it.
	message string
	// fields are extra structured attributes an incident analyzer would correlate on.
	fields map[string]any
	// sensitive, when non-nil, builds the logged message with fresh fake PII to
	// model a data-leaking code path and exercise the analyzer's masking; the
	// HTTP response still uses the clean static message.
	sensitive func() string
}

// catalogue is the set of failure modes, biased toward a payments platform.
var catalogue = []scenario{
	{
		typ:        "db-connection-timeout",
		severity:   slog.LevelError,
		httpStatus: http.StatusServiceUnavailable,
		message:    "database connection pool exhausted: timeout acquiring connection after 5s",
		fields:     map[string]any{"component": "ledger-db", "pool_size": 20, "wait_ms": 5000},
	},
	{
		typ:        "payment-gateway-declined",
		severity:   slog.LevelError,
		httpStatus: http.StatusBadGateway,
		message:    "payment authorization failed: upstream gateway returned decline code 51 (insufficient funds)",
		fields:     map[string]any{"component": "acquirer-adapter", "gateway": "stripe", "decline_code": "51"},
	},
	{
		typ:        "upstream-502",
		severity:   slog.LevelError,
		httpStatus: http.StatusBadGateway,
		message:    "exception calling fx-rates service: received HTTP 502 Bad Gateway from upstream",
		fields:     map[string]any{"component": "fx-rates-client", "upstream": "fx-rates.internal", "status": 502},
	},
	{
		typ:        "auth-token-invalid",
		severity:   slog.LevelWarn,
		httpStatus: http.StatusUnauthorized,
		message:    "authentication error: JWT signature verification failed, rejecting request",
		fields:     map[string]any{"component": "auth-middleware", "reason": "signature_mismatch"},
	},
	{
		typ:        "rate-limit-exceeded",
		severity:   slog.LevelWarn,
		httpStatus: http.StatusTooManyRequests,
		message:    "rate limit exceeded: client breached 100 req/s quota, requests are being shed",
		fields:     map[string]any{"component": "api-gateway", "limit_rps": 100},
	},
	{
		typ:        "nil-pointer-panic",
		severity:   slog.LevelError,
		httpStatus: http.StatusInternalServerError,
		message:    "recovered panic: runtime error: invalid memory address or nil pointer dereference",
		fields:     map[string]any{"component": "settlement-worker", "goroutine": "settle-batch"},
	},
	{
		typ:        "kafka-rebalance",
		severity:   slog.LevelError,
		httpStatus: http.StatusServiceUnavailable,
		message:    "error consuming events: kafka consumer group rebalance in progress, partitions revoked",
		fields:     map[string]any{"component": "event-consumer", "topic": "transactions", "group": "settlement"},
	},
	{
		typ:        "disk-pressure",
		severity:   slog.LevelError,
		httpStatus: http.StatusInternalServerError,
		message:    "fatal: failed to write audit log, no space left on device (disk usage 98%)",
		fields:     map[string]any{"component": "audit-writer", "disk_used_pct": 98},
	},

	// --- PII-bearing scenarios -------------------------------------------
	// Model code paths that accidentally log raw customer data: the sensitive
	// hook regenerates fresh PII per emission to exercise the analyzer's masking,
	// while the static message (the HTTP response) stays PII-free.
	{
		typ:        "payment-declined-pii",
		severity:   slog.LevelError,
		httpStatus: http.StatusBadGateway,
		message:    "payment authorization failed: decline code 51 (insufficient funds)",
		fields:     map[string]any{"component": "acquirer-adapter", "gateway": "stripe"},
		sensitive: func() string {
			return fmt.Sprintf(
				"payment authorization failed for cardholder %s using card %s from %s: decline code 51 (insufficient funds)",
				fakeEmail(), fakeCard(), fakeIP())
		},
	},
	{
		typ:        "kyc-validation-failed",
		severity:   slog.LevelWarn,
		httpStatus: http.StatusUnprocessableEntity,
		message:    "KYC validation failed: beneficiary document mismatch, rejecting onboarding",
		fields:     map[string]any{"component": "kyc-service", "reason": "document_mismatch"},
		sensitive: func() string {
			return fmt.Sprintf(
				"KYC validation failed for customer %s (phone %s): beneficiary IBAN %s document mismatch",
				fakeEmail(), fakePhone(), fakeIBAN())
		},
	},
	{
		typ:        "payout-iban-rejected",
		severity:   slog.LevelError,
		httpStatus: http.StatusServiceUnavailable,
		message:    "payout to beneficiary failed: upstream bank timeout on SEPA rail",
		fields:     map[string]any{"component": "payout-worker", "rail": "sepa"},
		sensitive: func() string {
			return fmt.Sprintf(
				"payout to beneficiary IBAN %s (contact %s) failed: upstream bank timeout on SEPA rail",
				fakeIBAN(), fakeEmail())
		},
	},
	{
		typ:        "webhook-auth-leak",
		severity:   slog.LevelWarn,
		httpStatus: http.StatusUnauthorized,
		message:    "webhook signature verification failed, rejecting delivery",
		fields:     map[string]any{"component": "webhook-receiver"},
		sensitive: func() string {
			return fmt.Sprintf(
				"webhook signature verification failed: Authorization: Bearer %s from client %s",
				fakeJWT(), fakeIP())
		},
	},
}

// scenarioByType lets /error/{type} look up a scenario in O(1).
var scenarioByType = func() map[string]scenario {
	m := make(map[string]scenario, len(catalogue))
	for _, s := range catalogue {
		m[s.typ] = s
	}
	return m
}()

// healthyMessages are INFO lines mixed in so the stream isn't 100% errors.
var healthyMessages = []string{
	"processed payment intent successfully",
	"settlement batch committed to ledger",
	"fx rate cache refreshed",
	"health check ok, all dependencies reachable",
	"webhook delivered to merchant endpoint",
}

// ---- Fake PII generators ----
//
// Synthetic, realistically-shaped customer data for the PII-bearing scenarios.
// Card numbers carry a valid Luhn digit so the analyzer's Luhn-gated card rule fires.

func randDigits(n int) string {
	b := make([]byte, n)
	for i := range b {
		b[i] = byte('0' + rand.Intn(10))
	}
	return string(b)
}

// fakeCard returns a 16-digit Visa-style PAN with a valid Luhn check digit.
func fakeCard() string {
	body := "4" + randDigits(14) // 15 digits incl. the leading 4
	sum, dbl := 0, true          // rightmost body digit is doubled
	for i := len(body) - 1; i >= 0; i-- {
		d := int(body[i] - '0')
		if dbl {
			if d *= 2; d > 9 {
				d -= 9
			}
		}
		sum += d
		dbl = !dbl
	}
	return fmt.Sprintf("%s%d", body, (10-sum%10)%10)
}

func fakeEmail() string {
	names := []string{"alice", "bob", "carol", "dave", "erin", "frank"}
	return fmt.Sprintf("%s.%s@example.com", names[rand.Intn(len(names))], randDigits(3))
}

func fakeIBAN() string  { return "FR76" + randDigits(18) }
func fakePhone() string { return "+336" + randDigits(8) }

func fakeIP() string {
	return fmt.Sprintf("%d.%d.%d.%d", rand.Intn(223)+1, rand.Intn(256), rand.Intn(256), rand.Intn(254)+1)
}

// fakeJWT returns a header.payload.signature shaped token to match the
// analyzer's JWT rule; it is not a real signed token.
func fakeJWT() string {
	return "eyJ" + randDigits(12) + "." + randDigits(24) + "." + randDigits(16)
}

func main() {
	cfg := loadConfig()

	logger := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: slog.LevelDebug}))
	logger = logger.With("service", cfg.serviceName)
	slog.SetDefault(logger)

	m := newMetrics()

	// Root context cancelled on SIGTERM/SIGINT for a clean shutdown of the
	// HTTP server and background emitter when Kubernetes stops the pod.
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()

	go emitLoop(ctx, logger, m, cfg)

	srv := &http.Server{
		Addr:              ":" + strconv.Itoa(cfg.port),
		Handler:           newMux(logger, m, cfg),
		ReadHeaderTimeout: 5 * time.Second,
	}

	go func() {
		logger.Info("incident-generator listening",
			"port", cfg.port,
			"emit_interval", cfg.emitInterval.String(),
			"error_ratio", cfg.errorRatio,
		)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			logger.Error("http server failed", "error", err.Error())
			os.Exit(1)
		}
	}()

	<-ctx.Done()
	logger.Info("shutdown signal received, draining")
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	_ = srv.Shutdown(shutdownCtx)
}

// config holds the runtime knobs, all overridable by env var.
type config struct {
	port         int
	emitInterval time.Duration
	errorRatio   float64
	serviceName  string
}

func loadConfig() config {
	return config{
		port:         envInt("PORT", 8080),
		emitInterval: envDuration("EMIT_INTERVAL", 5*time.Second),
		errorRatio:   envFloat("ERROR_RATIO", 0.7),
		serviceName:  envStr("SERVICE_NAME", "incident-generator"),
	}
}

// emitLoop is the continuous background stream: each tick logs a healthy INFO
// line or, with probability errorRatio, a random failure from the catalogue.
func emitLoop(ctx context.Context, logger *slog.Logger, m *metrics, cfg config) {
	ticker := time.NewTicker(cfg.emitInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			if rand.Float64() < cfg.errorRatio {
				emitScenario(logger, m, catalogue[rand.Intn(len(catalogue))], "background")
			} else {
				logger.Info(healthyMessages[rand.Intn(len(healthyMessages))],
					"severity", "info",
					"source", "background",
					"trace_id", traceID(),
				)
				m.inc("incidentgen_logs_emitted_total", `severity="info",type="healthy"`)
			}
		}
	}
}

// emitScenario logs a single catalogue entry and records the matching metric.
// source distinguishes background noise from on-demand HTTP triggers.
func emitScenario(logger *slog.Logger, m *metrics, s scenario, source string) {
	attrs := []any{
		"severity", strings.ToLower(s.severity.String()),
		"error_type", s.typ,
		"source", source,
		"trace_id", traceID(),
	}
	for k, v := range s.fields {
		attrs = append(attrs, k, v)
	}
	// PII scenarios build the logged message fresh; s.message stays the clean HTTP response.
	msg := s.message
	if s.sensitive != nil {
		msg = s.sensitive()
	}
	logger.Log(context.Background(), s.severity, msg, attrs...)
	m.inc("incidentgen_logs_emitted_total",
		fmt.Sprintf(`severity=%q,type=%q`, strings.ToLower(s.severity.String()), s.typ))
}

// ---- HTTP layer ----

func newMux(logger *slog.Logger, m *metrics, cfg config) http.Handler {
	mux := http.NewServeMux()

	// Liveness: always 200 unless the process is wedged.
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		writeText(w, http.StatusOK, "ok")
	})
	// Readiness: kept separate from liveness so it can fail without triggering restarts.
	mux.HandleFunc("GET /readyz", func(w http.ResponseWriter, _ *http.Request) {
		writeText(w, http.StatusOK, "ready")
	})

	mux.HandleFunc("GET /metrics", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/plain; version=0.0.4")
		_, _ = w.Write([]byte(m.render()))
	})

	// /boom, return a random error scenario as an HTTP failure, and log it.
	mux.HandleFunc("GET /boom", func(w http.ResponseWriter, r *http.Request) {
		s := catalogue[rand.Intn(len(catalogue))]
		emitScenario(logger, m, s, "http:/boom")
		m.inc("incidentgen_http_requests_total", fmt.Sprintf(`path="/boom",status="%d"`, s.httpStatus))
		writeText(w, s.httpStatus, "boom: "+s.message+"\n"+fakeStackTrace(s.typ))
	})

	// /error/{type}, trigger one specific scenario by its catalogue key.
	mux.HandleFunc("GET /error/{type}", func(w http.ResponseWriter, r *http.Request) {
		typ := r.PathValue("type")
		s, ok := scenarioByType[typ]
		if !ok {
			m.inc("incidentgen_http_requests_total", `path="/error",status="404"`)
			writeText(w, http.StatusNotFound,
				"unknown error type "+strconv.Quote(typ)+"\nknown types:\n  "+strings.Join(knownTypes(), "\n  "))
			return
		}
		emitScenario(logger, m, s, "http:/error")
		m.inc("incidentgen_http_requests_total", fmt.Sprintf(`path="/error",status="%d"`, s.httpStatus))
		writeText(w, s.httpStatus, s.message)
	})

	// /panic, trigger a real panic, recovered by middleware into a logged 500.
	mux.HandleFunc("GET /panic", func(w http.ResponseWriter, r *http.Request) {
		panic("simulated panic from /panic: nil pointer dereference in settlement worker")
	})

	// /slow, sleep then respond, to simulate a latency incident. ?ms=3000.
	mux.HandleFunc("GET /slow", func(w http.ResponseWriter, r *http.Request) {
		ms := 3000
		if v := r.URL.Query().Get("ms"); v != "" {
			if parsed, err := strconv.Atoi(v); err == nil && parsed >= 0 {
				ms = parsed
			}
		}
		if ms > 30000 {
			ms = 30000 // cap so a stray request can't pin the handler forever
		}
		start := time.Now()
		select {
		case <-time.After(time.Duration(ms) * time.Millisecond):
		case <-r.Context().Done():
			return
		}
		logger.Warn("slow request served: latency budget exceeded",
			"severity", "warn",
			"error_type", "high-latency",
			"source", "http:/slow",
			"latency_ms", time.Since(start).Milliseconds(),
			"trace_id", traceID(),
		)
		m.inc("incidentgen_http_requests_total", `path="/slow",status="200"`)
		writeText(w, http.StatusOK, fmt.Sprintf("served after %dms\n", ms))
	})

	// /leak, grow a global buffer; repeated calls push the pod toward an OOMKill.
	mux.HandleFunc("GET /leak", func(w http.ResponseWriter, r *http.Request) {
		mb := 16
		if v := r.URL.Query().Get("mb"); v != "" {
			if parsed, err := strconv.Atoi(v); err == nil && parsed > 0 {
				mb = parsed
			}
		}
		leak(mb)
		total := leakedMB()
		logger.Error("memory leak simulated: heap deliberately grown, OOMKill imminent",
			"severity", "error",
			"error_type", "memory-leak",
			"source", "http:/leak",
			"leaked_mb", total,
			"trace_id", traceID(),
		)
		m.set("incidentgen_leaked_bytes", "", float64(total)*1024*1024)
		m.inc("incidentgen_http_requests_total", `path="/leak",status="200"`)
		writeText(w, http.StatusOK, fmt.Sprintf("leaked %dMB (total ~%dMB held)\n", mb, total))
	})

	// /crash, exit the process; repeat to produce a CrashLoopBackOff.
	mux.HandleFunc("GET /crash", func(w http.ResponseWriter, r *http.Request) {
		logger.Error("fatal: simulated hard crash via /crash, process exiting with code 1",
			"severity", "fatal",
			"error_type", "hard-crash",
			"source", "http:/crash",
			"trace_id", traceID(),
		)
		m.inc("incidentgen_http_requests_total", `path="/crash",status="500"`)
		writeText(w, http.StatusInternalServerError, "crashing now\n")
		// Give the response a moment to flush before the process dies.
		go func() {
			time.Sleep(100 * time.Millisecond)
			os.Exit(1)
		}()
	})

	// Index: a human-readable map of what this service can do.
	mux.HandleFunc("GET /", func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/" {
			writeText(w, http.StatusNotFound, "not found\n")
			return
		}
		writeText(w, http.StatusOK, indexPage(cfg))
	})

	return recoverMiddleware(logger, m, mux)
}

// recoverMiddleware turns a panic into a logged 500 instead of crashing the server.
func recoverMiddleware(logger *slog.Logger, m *metrics, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		defer func() {
			if rec := recover(); rec != nil {
				buf := make([]byte, 4096)
				n := runtime.Stack(buf, false)
				logger.Error(fmt.Sprintf("recovered panic serving %s: %v", r.URL.Path, rec),
					"severity", "error",
					"error_type", "nil-pointer-panic",
					"source", "http:recover",
					"trace_id", traceID(),
					"stack", string(buf[:n]),
				)
				m.inc("incidentgen_panics_recovered_total", "")
				m.inc("incidentgen_http_requests_total", fmt.Sprintf(`path=%q,status="500"`, r.URL.Path))
				writeText(w, http.StatusInternalServerError, "internal server error (panic recovered)\n")
			}
		}()
		next.ServeHTTP(w, r)
	})
}

// ---- memory leak helpers ----

var (
	leakMu  sync.Mutex
	leakBuf [][]byte
)

func leak(mb int) {
	leakMu.Lock()
	defer leakMu.Unlock()
	for i := 0; i < mb; i++ {
		chunk := make([]byte, 1024*1024)
		for j := range chunk {
			chunk[j] = byte(j) // touch every page so the RSS actually grows
		}
		leakBuf = append(leakBuf, chunk)
	}
}

func leakedMB() int {
	leakMu.Lock()
	defer leakMu.Unlock()
	return len(leakBuf)
}

// ---- metrics ----

// metrics is a minimal, concurrency-safe Prometheus text-format registry. Keys
// are pre-rendered name{labels} strings; values are float64 counters/gauges.
type metrics struct {
	mu     sync.Mutex
	values map[string]float64
	// meta maps a metric name to its (type, help) for the # HELP / # TYPE preamble.
	meta map[string][2]string
}

func newMetrics() *metrics {
	return &metrics{
		values: map[string]float64{},
		meta: map[string][2]string{
			"incidentgen_logs_emitted_total":     {"counter", "Log lines emitted, by severity and error type."},
			"incidentgen_http_requests_total":    {"counter", "HTTP requests handled, by path and status."},
			"incidentgen_panics_recovered_total": {"counter", "Panics caught by the recover middleware."},
			"incidentgen_leaked_bytes":           {"gauge", "Bytes deliberately held by the /leak endpoint."},
		},
	}
}

func seriesKey(name, labels string) string {
	if labels == "" {
		return name
	}
	return name + "{" + labels + "}"
}

func (m *metrics) inc(name, labels string) {
	m.mu.Lock()
	m.values[seriesKey(name, labels)]++
	m.mu.Unlock()
}

func (m *metrics) set(name, labels string, v float64) {
	m.mu.Lock()
	m.values[seriesKey(name, labels)] = v
	m.mu.Unlock()
}

// render produces the Prometheus exposition text, grouping series by metric
// name (sorted) with a single HELP/TYPE block each.
func (m *metrics) render() string {
	m.mu.Lock()
	defer m.mu.Unlock()

	keys := make([]string, 0, len(m.values))
	for k := range m.values {
		keys = append(keys, k)
	}
	sort.Strings(keys)

	var b strings.Builder
	emittedMeta := map[string]bool{}
	for _, k := range keys {
		name := k
		if i := strings.IndexByte(k, '{'); i >= 0 {
			name = k[:i]
		}
		if !emittedMeta[name] {
			if meta, ok := m.meta[name]; ok {
				fmt.Fprintf(&b, "# HELP %s %s\n# TYPE %s %s\n", name, meta[1], name, meta[0])
			}
			emittedMeta[name] = true
		}
		fmt.Fprintf(&b, "%s %g\n", k, m.values[k])
	}
	return b.String()
}

// ---- small helpers ----

func knownTypes() []string {
	out := make([]string, 0, len(catalogue))
	for _, s := range catalogue {
		out = append(out, s.typ)
	}
	sort.Strings(out)
	return out
}

func fakeStackTrace(typ string) string {
	return strings.Join([]string{
		"goroutine 42 [running]:",
		"github.com/fipto/payments/" + strings.ReplaceAll(typ, "-", "") + ".handle(0xc000123456)",
		"\t/app/internal/" + strings.ReplaceAll(typ, "-", "") + "/handler.go:118 +0x1a4",
		"github.com/fipto/payments/server.(*Server).serve(0xc0000aa000)",
		"\t/app/server/server.go:204 +0x2cc",
	}, "\n")
}

func indexPage(cfg config) string {
	return strings.Join([]string{
		"incident-generator, ForgePath incident fixture",
		"",
		"Continuously emits structured JSON error logs (every " + cfg.emitInterval.String() +
			", error ratio " + strconv.FormatFloat(cfg.errorRatio, 'f', -1, 64) + ").",
		"",
		"On-demand endpoints:",
		"  GET /boom            random failure as an HTTP error + log line",
		"  GET /error/{type}    trigger one specific failure (see types below)",
		"  GET /panic           real panic, recovered to a logged 500",
		"  GET /slow?ms=3000    sleep then respond (latency incident)",
		"  GET /leak?mb=16      grow heap toward the memory limit (OOMKill)",
		"  GET /crash           exit(1) -> CrashLoopBackOff",
		"  GET /healthz         liveness",
		"  GET /readyz          readiness",
		"  GET /metrics         Prometheus metrics",
		"",
		"Known error types for /error/{type}:",
		"  " + strings.Join(knownTypes(), "\n  "),
		"",
	}, "\n")
}

func writeText(w http.ResponseWriter, status int, body string) {
	w.Header().Set("Content-Type", "text/plain; charset=utf-8")
	w.WriteHeader(status)
	_, _ = w.Write([]byte(body))
}

const hexdigits = "0123456789abcdef"

func traceID() string {
	b := make([]byte, 16)
	for i := range b {
		b[i] = hexdigits[rand.Intn(16)]
	}
	return string(b)
}

func envStr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func envInt(key string, def int) int {
	if v := os.Getenv(key); v != "" {
		if parsed, err := strconv.Atoi(v); err == nil {
			return parsed
		}
	}
	return def
}

func envFloat(key string, def float64) float64 {
	if v := os.Getenv(key); v != "" {
		if parsed, err := strconv.ParseFloat(v, 64); err == nil {
			return parsed
		}
	}
	return def
}

func envDuration(key string, def time.Duration) time.Duration {
	if v := os.Getenv(key); v != "" {
		if parsed, err := time.ParseDuration(v); err == nil {
			return parsed
		}
	}
	return def
}
