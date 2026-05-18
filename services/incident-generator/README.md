# incident-generator

A small, dependency-free Go service whose only job is to **misbehave on
purpose**. It is ForgePath's incident fixture: it produces a realistic stream
of error logs and metrics so the observability stack (Promtail → Loki →
Grafana), and the **AI Incident Analyzer** that consumes it, has real
material to analyse.

It misbehaves in two ways:

- **Continuously**, a background goroutine emits structured JSON logs every
  `EMIT_INTERVAL`, a mix of healthy INFO lines and errors from a
  payments-platform failure catalogue. Error lines carry the
  `error|exception|fatal|panic` markers the Grafana `error-explorer`
  dashboard matches on.
- **On demand**, HTTP endpoints trigger one specific failure at a time.

A subset of the catalogue (`payment-declined-pii`, `kyc-validation-failed`,
`payout-iban-rejected`, `webhook-auth-leak`) deliberately **logs fake customer
data**, emails, Luhn-valid card numbers, IBANs, phone numbers, IPs, bearer
tokens, regenerated on every emission. The HTTP response stays PII-free; only
the log line leaks, mirroring a sanitized API over a leaky log. This is the
fixture for the **AI Incident Analyzer's data masking**, which scrubs these
shapes before any line reaches the LLM (`MASKING_ENABLED`).

## Endpoints

| Endpoint | Effect |
| --- | --- |
| `GET /` | Endpoint index + known error types |
| `GET /boom` | Random catalogue failure as an HTTP error + log line |
| `GET /error/{type}` | Trigger one specific failure (see `/` for the list) |
| `GET /panic` | Real panic, recovered by middleware → logged 500 |
| `GET /slow?ms=3000` | Sleep then respond (latency incident) |
| `GET /leak?mb=16` | Grow the heap (push toward the memory limit → OOMKill) |
| `GET /crash` | `exit(1)` → CrashLoopBackOff under Kubernetes |
| `GET /healthz`, `/readyz` | Liveness / readiness |
| `GET /metrics` | Prometheus metrics (error counters by type) |

## Configuration (env vars)

| Var | Default | Meaning |
| --- | --- | --- |
| `PORT` | `8080` | HTTP listen port |
| `EMIT_INTERVAL` | `5s` | Continuous emitter cadence (Go duration) |
| `ERROR_RATIO` | `0.7` | Fraction of ticks that are errors (`0`–`1`) |
| `SERVICE_NAME` | `incident-generator` | `service` field on every log line |

## Run locally

```bash
go run .                      # listens on :8080
# or
EMIT_INTERVAL=1s ERROR_RATIO=0.9 go run .

curl localhost:8080/          # endpoint index
curl localhost:8080/boom
curl localhost:8080/metrics
```

## Build & deploy into ForgePath

The incident-generator is a **standing platform service**: ArgoCD deploys it from
`gitops/platform/incident-generator/` into the `incident-generator` namespace and
keeps it running, no PR, no preview lifecycle.

The image is built on the dev host and **side-loaded into kind** (never pushed
to a registry); the manifests reference `incident-generator:dev` with
`imagePullPolicy: IfNotPresent`.

```bash
make incident-generator-load     # docker build + kind load docker-image
```

After the image is in the node cache, ArgoCD reconciles the Deployment on its
own (within ~60s). On a fresh cluster, `make local-up` side-loads the image
for you if it's already been built.

## Triggering an incident

The Service is a **NodePort** mapped to **<http://localhost:8889>**, so the
on-demand endpoints are reachable from the host with no port-forward. The
quickest way is the wrapper:

```bash
make incident TYPE=panic                       # one panic
make incident TYPE=leak ARGS="--mb 32 --count 5"   # push the pod toward an OOMKill
make incident TYPE=db-connection-timeout ARGS="--count 30"  # cross the analyzer's error threshold
make incident TYPE=list                        # list the known catalogue error types
```

`make incident` shells out to [`scripts/trigger-incident.sh`](../../scripts/trigger-incident.sh)
(`TYPE` defaults to `boom`). The script targets `http://localhost:8889` and, if
that host port isn't mapped yet, **falls back to an ephemeral `kubectl
port-forward`**, so it works whether or not the cluster was (re)created with
the port mapping. You can also just curl it directly:

```bash
curl localhost:8889/ ; curl localhost:8889/boom
```

> The `localhost:8889` mapping is set at kind-cluster creation
> (`local/kind-config.yaml`). On a cluster created **before** this mapping
> existed, run `make local-down && make local-up` once to pick it up, or rely
> on the script's port-forward fallback in the meantime. On docker-desktop the
> NodePort is reachable at `localhost:30081` directly.

The Backstage catalog entry, TechDocs runbook, and Grafana dashboard links
live in [`platform/backstage/catalog/incident-generator/`](../../platform/backstage/catalog/incident-generator/).
