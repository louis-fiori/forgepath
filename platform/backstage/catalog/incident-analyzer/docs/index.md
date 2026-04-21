# incident-analyzer

The **AI Incident Analyzer**, ForgePath's AIOps brick. It watches the
observability stack, detects incidents, asks **Claude** for a structured
diagnosis, and surfaces it where developers already work: a **GitHub issue** and
a **Backstage notification**.

Standing platform service: ArgoCD deploys it from
`gitops/platform/incident-analyzer/` into the `incident-analyzer` namespace.
Source: [`services/incident-analyzer/`](https://github.com/louis-fiori/forgepath/tree/dev/services/incident-analyzer).

## Pipeline

```
poll loop (60s) ─┐                       on-demand: GET/POST /analyze
                 ▼
  Loki (errors) + K8s API (OOM/CrashLoop) + Prometheus (panics, 5xx)
                 ▼
  detector → incident? (any-of thresholds) → fingerprint + dedup cache
                 ▼
  Claude claude-sonnet-4-6 (cached system+runbook, forced record_diagnosis tool)
                 ▼
  GitHub issue  +  Backstage notification (link → the issue)
```

- **Detection (any-of):** error lines ≥ `ERROR_THRESHOLD` in `DETECT_WINDOW`,
  **or** a pod `OOMKilled` / `CrashLoopBackOff`, **or** a recovered-panic spike.
- **Dedup:** in-memory fingerprint `sha1(namespace:error_type:signal_class)`,
  excluding counts/timestamps so an ongoing incident files once (not every poll).
  Re-files only after `REFILE_COOLDOWN` (6h). **The cache is in-memory and lost on
  restart**, a restart can re-file an active incident once.

## Configuration

Env on the container in `gitops/platform/incident-analyzer/deployment.yaml`
(commit to change; ArgoCD reconciles in ~60s):

| Var | Default | Meaning |
| --- | --- | --- |
| `WATCH_NAMESPACES` | `incident-generator` | Namespaces to poll (comma-separated) |
| `POLL_ENABLED` | `true` | `false` = manual-only (analyze on demand only; no background token spend) |
| `POLL_INTERVAL_SECONDS` | `60` | Background loop cadence (when `POLL_ENABLED=true`) |
| `ERROR_THRESHOLD` | `20` | Error lines in window → incident |
| `LLM_PROVIDER` | `bedrock` | `bedrock` (AWS) or `anthropic` (direct API key) |
| `BEDROCK_MODEL_ID` / `AWS_REGION` | `us.anthropic.claude-sonnet-4-6` / `us-east-1` | Bedrock model (US cross-region inference profile) + region |

Secrets (from `.env` via `scripts/local-up.sh`): for Bedrock, your local
`~/.aws` is mounted as a Secret and `AWS_PROFILE` selects the profile (static-key
or assume-role); or `ANTHROPIC_API_KEY` for the direct path. Plus `GITHUB_TOKEN`
(needs **Issues: write**) and `BACKSTAGE_S2S_TOKEN`. Each surfacing path degrades
independently if its secret is absent.

## Runbook

### Healthy signals

- Pod `Running`/`Ready`; `/healthz` and `/readyz` return 200.
- Logs show periodic `cycle ... no incident` lines while the watched service is calm.
- `incidentanalyzer_polls_total` increases steadily (Prometheus).

### Trigger an analysis on purpose

**From Backstage (no terminal):** open the **"Analyze an incident"** template
(`/create` → Analyze an incident, or the "Analyze an incident now" link on this
component). Pick the namespace, tick/untick **Open a GitHub issue**, submit, the
diagnosis is shown back in the scaffolder result.

**From the CLI:**
```bash
kubectl -n incident-analyzer port-forward svc/incident-analyzer 8081:80 &
# /analyze, /analyze-log and /settings require the shared S2S bearer token
# (when one is configured). Pull it from the secret:
TOKEN=$(kubectl -n backstage get secret backstage-s2s-token -o jsonpath='{.data.token}' | base64 -d)
AUTH=(-H "Authorization: Bearer $TOKEN")
# Force a fresh analysis even if recently filed:
curl "${AUTH[@]}" "localhost:8081/analyze?namespace=incident-generator&force=true"
# Analyze WITHOUT opening an issue (per-request override):
curl "${AUTH[@]}" "localhost:8081/analyze?namespace=incident-generator&force=true&open_issue=false"
curl localhost:8081/metrics   # /metrics, /healthz, /readyz and / are not gated
```

### Enable / disable issue creation

- **Per run:** the `open_issue` param on `/analyze` (or the checkbox in the
  Backstage template).
- **Globally at runtime** (instant, no redeploy):
  ```bash
  curl "${AUTH[@]}" -XPOST localhost:8081/settings -d '{"issues_enabled": false}'  # turn off
  curl "${AUTH[@]}" -XPOST localhost:8081/settings -d '{"issues_enabled": true}'   # back on
  curl "${AUTH[@]}" localhost:8081/settings                                        # check
  ```
- **Global default (persisted):** `ISSUES_ENABLED` env in the Deployment
  (commit → ArgoCD reconciles).

To create something worth analysing, drive the incident-generator:
```bash
kubectl -n incident-generator port-forward svc/incident-generator 8888:80 &
curl "localhost:8888/leak?mb=40"   # OOMKill   (repeat)
curl localhost:8888/crash          # CrashLoop (repeat)
```

### Common failure modes

| Symptom | Likely cause | First check |
| --- | --- | --- |
| Detects but never files | `ANTHROPIC_API_KEY` empty | `kubectl -n incident-analyzer logs deploy/incident-analyzer` → "detection only" |
| Issue POST 403 | PAT lacks Issues: write | Add Issues:write to the GitHub PAT |
| Notification 401/403 | `BACKSTAGE_S2S_TOKEN` mismatch | Same token in `incident-analyzer-secrets` and `backstage-s2s-token`; `make backstage-reload` |
| `/analyze*` or `/settings` → 401 | Missing/stale S2S bearer token | Pass `Authorization: Bearer $(kubectl -n backstage get secret backstage-s2s-token -o jsonpath='{.data.token}' \| base64 -d)`; Backstage actions send it automatically |
| No incidents ever | Threshold too high / nothing failing | Lower `ERROR_THRESHOLD`, or trigger the generator |

### Mitigations

- **Pause:** scale `replicas: 0` in the Deployment and commit.
- **Tune sensitivity:** adjust `ERROR_THRESHOLD` / `POLL_INTERVAL_SECONDS`.
- **Stop duplicate issues:** raise `REFILE_COOLDOWN_SECONDS`.

## Contacts

- **Owning team:** `platform-team`
- **Escalation:** TBD, PagerDuty annotation placeholder present.
