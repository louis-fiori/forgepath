# incident-generator

A deliberately misbehaving Go service. It is ForgePath's **incident fixture**:
its job is to produce a steady, realistic stream of error logs and metrics so
the observability stack, and eventually the **AI Incident Analyzer**, has
something to chew on.

It is a **standing platform service**: ArgoCD deploys it from
`gitops/platform/incident-generator/` into the `incident-generator` namespace and
keeps it running, no PR, no preview lifecycle. Source lives in
[`services/incident-generator/`](https://github.com/louis-fiori/forgepath/tree/dev/services/incident-generator).

## Overview

- **Image**: `incident-generator:dev` (built on the dev host, side-loaded into kind)
- **Namespace**: `incident-generator`
- **Replicas**: `1`
- **Container port**: `8080` (ClusterIP, see "Triggering" for host access)
- **Owner**: `group:default/platform-team`

### What it does

- **Continuously** emits structured JSON logs every `EMIT_INTERVAL` (default
  `5s`). A share `ERROR_RATIO` (default `0.7`) are errors drawn from a
  payments-platform failure catalogue; the rest are healthy INFO noise. Error
  lines carry `error|exception|fatal|panic` markers, so they show up in the
  Grafana **error explorer** with no extra config.
- **On demand**, exposes endpoints to trigger one failure mode at a time.

| Endpoint | Effect |
| --- | --- |
| `GET /` | Endpoint index + list of known error types |
| `GET /boom` | Random catalogue failure, returned as an HTTP error + logged |
| `GET /error/{type}` | Trigger one specific failure by its catalogue key |
| `GET /panic` | Real panic, recovered by middleware → logged 500 |
| `GET /slow?ms=3000` | Sleep then respond (latency incident) |
| `GET /leak?mb=16` | Grow the heap toward the 128Mi limit → OOMKill |
| `GET /crash` | `exit(1)` → CrashLoopBackOff |
| `GET /healthz`, `/readyz` | Liveness / readiness |
| `GET /metrics` | Prometheus metrics (error counters by type) |

### Tuning

Edit the env on the container in `gitops/platform/incident-generator/deployment.yaml`
and commit, ArgoCD reconciles within ~60s:

- `EMIT_INTERVAL`, emitter cadence (Go duration, e.g. `2s`, `500ms`).
- `ERROR_RATIO`, fraction of ticks that are errors (`0.0`–`1.0`). Set `0` to
  silence the background stream without scaling the pod down.
- `PORT`, listen port (keep in sync with the Service `targetPort`).

## Runbook

The Incident Analyzer will read this section first. Be explicit.

### Healthy signals

- Pod is `Running` and `Ready` (Kubernetes tab of this entity).
- A steady mix of INFO and ERROR lines in the logs, errors are **expected**
  here; that is the point of the fixture.
- No `OOMKilled` / `CrashLoopBackOff` unless `/leak` or `/crash` was hit.

### Triggering an incident on purpose

The Service is `ClusterIP`, so port-forward to reach the endpoints from the host:

```bash
kubectl -n incident-generator port-forward svc/incident-generator 8888:80 &

curl localhost:8888/boom                       # random failure
curl localhost:8888/error/payment-gateway-declined
curl "localhost:8888/slow?ms=4000"             # latency spike
curl "localhost:8888/leak?mb=40"               # repeat to force an OOMKill
curl localhost:8888/crash                      # force a restart / CrashLoop
```

### Common failure modes

| Symptom | Likely cause | First check |
| --- | --- | --- |
| `OOMKilled` | `/leak` was called enough to exceed the 128Mi limit | `kubectl describe pod -n incident-generator -l app=incident-generator` |
| `CrashLoopBackOff` | `/crash` was hit, or a bad config | `kubectl logs -n incident-generator -l app=incident-generator --previous` |
| `ImagePullBackOff` | Image not side-loaded into kind | `make incident-generator-load` on the dev host |
| No logs at all | Pod not Ready, or emitter disabled | Check `EMIT_INTERVAL`/`ERROR_RATIO` env and pod status |

### Mitigations

- **Stop the noise**: set `ERROR_RATIO: "0"` (or scale `replicas: 0`) in
  `deployment.yaml` and commit; ArgoCD reconciles within ~60s.
- **Recover from OOM/Crash**: stop calling `/leak` and `/crash`; the pod
  restarts on its own. To free leaked memory immediately,
  `kubectl -n incident-generator rollout restart deploy/incident-generator`.
- **Roll back**: revert the latest commit touching
  `gitops/platform/incident-generator/`.

## Dashboards

All links are pre-filtered to the `incident-generator` namespace.

- [Grafana, pod metrics](http://localhost:3000/d/cluster-pods?var-namespace=incident-generator)
- [Grafana, service logs](http://localhost:3000/d/service-logs?var-namespace=incident-generator)
- [Grafana, error explorer](http://localhost:3000/d/error-explorer?var-namespace=incident-generator)
- [ArgoCD application](http://localhost:8080/applications/incident-generator)

## Contacts

- **Owning team**: `platform-team`
- **Escalation path**: TBD, wire up once the PagerDuty plugin is enabled
  (the `pagerduty.com/integration-key` annotation is already there as a
  placeholder).
