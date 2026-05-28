# incident-analyzer

The **AI Incident Analyzer** for ForgePath. A FastAPI service that watches the
observability stack, detects incidents, asks **Claude** for a structured
diagnosis (root cause + remediation citing the runbook), and surfaces it as a
**GitHub issue** and a **Backstage in-app notification**.

It consumes what the `incident-generator` produces, closing the loop from raw
errors to a human-actionable diagnosis.

## How it works

```
poll loop (every POLL_INTERVAL) ─┐         on-demand: GET/POST /analyze
                                 ▼            (CLI, or the Backstage template)
   Loki (error counts + samples) + K8s API (pod/OOM/CrashLoop) + Prometheus
                                 ▼
                  detector: incident? (any-of thresholds)
                                 ▼  fingerprint = sha1(ns:error_type:signal_class)
                  dedup cache (skip if filed < REFILE_COOLDOWN ago)
                                 ▼
            masking, scrub PII/secrets from samples & events (MASKING_ENABLED)
                                 ▼
   Claude (claude-sonnet-4-6), cached system+runbook, forced record_diagnosis tool
                                 ▼
        GitHub issue  +  Backstage notification (link → the issue)
```

**Detection (any-of):** error lines ≥ `ERROR_THRESHOLD` in `DETECT_WINDOW`, **or**
a pod `OOMKilled` / `CrashLoopBackOff`, **or** a recovered-panic spike
(increase ≥ `PANIC_THRESHOLD`).

**Masking** (`MASKING_ENABLED`, on by default) redacts emails, phone numbers,
card numbers, IBANs, auth tokens, IPv4/IPv6 addresses and secret key/value pairs
(incl. JSON-quoted) from the log samples and pod
events *before* they reach Claude, so the cardholder data the
`incident-generator` deliberately leaks stays inside the trust boundary.

**Runbook context**, the analyzer fetches the service's TechDocs runbook (from
`raw.githubusercontent.com` on `GITHUB_BRANCH`) and feeds it to Claude, so the
remediation cites your own ops docs.

**Dedup** is in-memory (lost on restart, fine for a local-first demo). The
fingerprint excludes counts/timestamps so an ongoing incident files once, not
every poll. `force=true` bypasses it.

## Endpoints

| Endpoint | Purpose |
| --- | --- |
| `GET /` | Config + capability summary |
| `GET /analyze?namespace=&window=&force=&open_issue=` | Run one cycle now, return the `AnalyzeResult` (`open_issue` overrides issue creation for this run) |
| `POST /analyze` | Same, JSON body `{namespace, window, force, open_issue}` (query string also accepted) |
| `POST /analyze-log` | Analyze a **single log line**: body `{namespace, log_line}` (paste a raw line) or `{namespace, query, window}` (fetch the newest Loki line containing `query`); optional `component`, `open_issue`. No detection thresholds, no dedup, same diagnosis + issue + notification flow |
| `GET /settings` | Read the global issue-creation toggle |
| `POST /settings` | Flip it at runtime, no redeploy, body `{"issues_enabled": true|false}` |
| `GET /healthz`, `/readyz` | Liveness / readiness |
| `GET /metrics` | Prometheus counters (detected, deduped, claude calls, issues, notifications) |

**Auth.** `/analyze`, `/analyze-log` and `/settings` trigger LLM calls and open
GitHub issues, so they require the shared service-to-service bearer token
(`Authorization: Bearer $BACKSTAGE_S2S_TOKEN`) whenever that token is set,
otherwise any in-cluster pod could drive them (ClusterIP only limits exposure to
the cluster). With no token configured the gate is open (local / poll-only dev),
logged as a warning at startup. `/`, `/healthz`, `/readyz` and `/metrics` are
never gated, so probes and Prometheus keep working. The Backstage scaffolder
actions send the token automatically.

## Configuration (env)

In-cluster, most of these flow from the repo's `.env` (see `.env.example`):
`make local-up` bakes them into the `incident-analyzer-secrets` Secret, which
the Deployment reads as optional env vars; `make incident-analyzer-secrets`
re-syncs after a `.env` edit. The in-cluster endpoints and `/aws` file paths are
set in `gitops/platform/incident-analyzer/deployment.yaml`. The advanced
thresholds (`DETECT_WINDOW`, `PANIC_THRESHOLD`, `REFILE_COOLDOWN_SECONDS`,
`MAX_TOKENS`, `LLM_TIMEOUT_SECONDS`, `LLM_MAX_RETRIES`) are **not** wired by
default, their values come from `app/config.py`; to change one, add it as an
env var on the Deployment (the config reads it straight from the environment).

| Var | Default | Meaning |
| --- | --- | --- |
| `WATCH_NAMESPACES` | `incident-generator` | Comma-separated namespaces to poll |
| `POLL_ENABLED` | `true` | Background auto-detection. `false` → manual-only (analyze on demand; no background Claude calls / tokens) |
| `POLL_INTERVAL_SECONDS` | `60` | Background loop cadence (when `POLL_ENABLED=true`) |
| `DETECT_WINDOW` | `10m` | LogQL window for error counts |
| `ERROR_THRESHOLD` | `20` | Error lines in window → incident |
| `PANIC_THRESHOLD` | `1` | Increase in recovered panics → incident |
| `REFILE_COOLDOWN_SECONDS` | `21600` | Don't re-file the same fingerprint within 6h |
| `MASKING_ENABLED` | `true` | Redact emails, phone numbers, card numbers, IBANs, tokens, IPv4/IPv6 addresses and secret key/value pairs (incl. JSON-quoted) from samples/events before the LLM call |
| `LLM_PROVIDER` | `bedrock` | `bedrock` (AWS SigV4) or `anthropic` (direct API key) |
| `MAX_TOKENS` | `2048` | Diagnosis output cap |
| `LLM_TIMEOUT_SECONDS` | `45` | Per-call ceiling on the LLM request. Kept under `POLL_INTERVAL_SECONDS` so a slow call can't outlast a poll cycle |
| `LLM_MAX_RETRIES` | `1` | SDK retries on transient LLM errors (429/5xx/connection). Capped so a Bedrock outage can't stack timeouts and stall the sequential loop |
| `BEDROCK_MODEL_ID` | `us.anthropic.claude-sonnet-4-6` | Model ID when provider=bedrock. The default is a US cross-region inference profile; swap the prefix for another region (e.g. `eu.anthropic.claude-sonnet-4-6`) |
| `AWS_REGION` | `us-east-1` | Bedrock region (Claude must be enabled there) |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_SESSION_TOKEN` |, | Static-key auth for Bedrock. If set, used directly, no `~/.aws` mount needed. Wins over `AWS_PROFILE` |
| `AWS_PROFILE` | `default` | Named profile from the mounted `~/.aws` (needs `bedrock:InvokeModel`); supports assume-role |
| `AWS_CONFIG_FILE` / `AWS_SHARED_CREDENTIALS_FILE` | `/aws/config` / `/aws/credentials` | Where botocore reads the mounted profile (set in the Deployment) |
| `ANALYZER_MODEL` | `claude-sonnet-4-6` | Model ID when provider=anthropic |
| `ANTHROPIC_API_KEY` |, | Secret. Direct-API key. Absent (or no resolvable AWS creds for bedrock) → detection-only |
| `ISSUES_ENABLED` | `true` | Default for opening issues. Override globally at runtime via `POST /settings`, or per-request via `open_issue` on `/analyze` |
| `GITHUB_TOKEN` |, | Secret. Needs **Issues: write** to file issues |
| `GITHUB_OWNER` / `GITHUB_REPO` | `louis-fiori` / `forgepath` | Repo to file issues against |
| `GITHUB_BRANCH` | `dev` | Branch to read TechDocs runbooks from (raw.githubusercontent) |
| `BACKSTAGE_S2S_TOKEN` |, | Secret. Shared service-to-service token: sent outbound to authenticate Backstage notifications, and required inbound on `/analyze`, `/analyze-log`, `/settings`. Unset → notifications skipped **and** those endpoints left open |
| `LOKI_URL` / `PROMETHEUS_URL` / `BACKSTAGE_URL` | in-cluster DNS | Backend endpoints |

> **Bedrock auth, two modes, checked in order.** (1) **Static key**: set
> `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY` (+ `AWS_SESSION_TOKEN` for STS)
>, passed straight to the SDK, no `~/.aws` needed, handy for CI. (2) **Named
> profile**: `scripts/local-up.sh` materializes your local `~/.aws` (config +
> credentials) into the `incident-analyzer-aws` Secret, mounted at `/aws`, and
> botocore resolves `AWS_PROFILE` from there, static-key and assume-role
> profiles both work. SSO profiles work locally but not in-pod (token expiry).
> Running locally (`uvicorn`) needs no mount, it reads your real `~/.aws`.

Each surfacing path degrades independently: no LLM creds → detection
only; no `GITHUB_TOKEN` (or `issues_enabled`/`open_issue` off) → skip issue;
no `BACKSTAGE_S2S_TOKEN` → skip notification.

## Run locally

```bash
pip install -r requirements.txt
LLM_PROVIDER=anthropic ANTHROPIC_API_KEY=sk-... uvicorn app.main:app --port 8080
curl "localhost:8080/analyze?namespace=incident-generator&force=true"
# get the diagnosis without filing an issue:
curl "localhost:8080/analyze?namespace=incident-generator&force=true&open_issue=false"

# analyze a single log line, paste it raw:
curl -X POST localhost:8080/analyze-log -H 'content-type: application/json' \
  -d '{"namespace":"incident-generator","log_line":"{\"severity\":\"error\",\"error_type\":\"db-connection-timeout\",\"msg\":\"...\"}","open_issue":false}'
# ...or let the service fetch the newest matching line from Loki:
curl -X POST localhost:8080/analyze-log -H 'content-type: application/json' \
  -d '{"namespace":"incident-generator","query":"db-connection-timeout","window":"10m"}'
```

## On-demand from Backstage

The `analyze-incident` scaffolder template ("Analyze an incident (AI Incident
Analyzer)") runs one analysis on demand: pick a namespace + lookback window,
toggle whether to open an issue, and the structured diagnosis is rendered back
in the task page. It calls the `incident-analyzer:analyze` custom scaffolder
action (in the Backstage overlay), which hits `POST /analyze`. Lives in
[`platform/backstage/templates/analyze-incident/`](../../platform/backstage/templates/analyze-incident/).

The `analyze-log` template ("Analyze a single log line (AI Incident Analyzer)")
does the same for **one log line**: paste it directly, or give a substring and
the newest matching Loki line is analyzed. It calls the
`incident-analyzer:analyzeLog` action, which hits `POST /analyze-log`. Lives in
[`platform/backstage/templates/analyze-log/`](../../platform/backstage/templates/analyze-log/).

## Build & deploy into ForgePath

```bash
make incident-analyzer-load   # docker build + kind load
```

Standing platform service: ArgoCD deploys it from
`gitops/platform/incident-analyzer/` into the `incident-analyzer` namespace.
Secrets are created by `scripts/local-up.sh` from `.env`, for the default
**Bedrock** path it mounts your `~/.aws` and uses `AWS_PROFILE`; plus
`GITHUB_TOKEN` and `BACKSTAGE_S2S_TOKEN`. (Set `LLM_PROVIDER=anthropic` and
`ANTHROPIC_API_KEY` to use the direct Anthropic API instead.) Backstage needs the
`backend.auth.externalAccess` block (added in `app-config.local.yaml`) and a
`make backstage-reload` to accept the notification token.

Catalog entry + runbook: `platform/backstage/catalog/incident-analyzer/`.
