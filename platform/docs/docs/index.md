# ForgePath

Internal developer platform demo, packaged as a local kind cluster.

The goal: show how a small platform team can give application developers a
clean self-service experience, open a PR from Backstage, see your service
running in seconds, close the PR to tear it down, while keeping the cluster
state fully described in Git and reconciled by ArgoCD.

## Components

- **Backstage**, the front door. Service catalog, scaffolder templates for
  deploy/destroy, embedded Kubernetes view, this documentation site.
- **ArgoCD**, the GitOps engine. Watches `gitops/` on GitHub, syncs the
  platform manifests and every preview environment.
- **Prometheus + Grafana + Loki**, always-on observability. Prometheus
  scrapes every pod metric for free; Promtail forwards every pod's logs
  into Loki; the `Cluster pods`, `Service logs` and `Logs · Error explorer`
  dashboards are provisioned by default.
- **incident-generator**, a Go chaos fixture that emits errors / OOMKills /
  panics on a loop and on demand, so there's always something to detect.
- **incident-analyzer**, a Python/FastAPI service that watches Loki,
  Prometheus and the Kubernetes API; on an error spike / OOMKill / CrashLoop it
  asks Claude (via Bedrock or the direct Anthropic API) for a root cause +
  remediation, files a GitHub issue and a Backstage notification, and runs on a
  poll loop or on demand. Sensitive data (emails, cards, IBANs, tokens, IPs,
  phone numbers) is masked before anything reaches the LLM.
- **kind cluster**, `forgepath` single-node cluster, mapped to a handful
  of localhost ports for browser access.

## Where to go next

- [Architecture](architecture.md), what runs where, how the pieces
  communicate, who owns what.
- [Deploy & destroy flow](deploy-flow.md), walk through a complete
  scaffold cycle: form submit, PR open, ArgoCD sync, PR close, cleanup.
- [Operations](ops.md), how to bootstrap from scratch, rotate secrets,
  upgrade ArgoCD, debug a stuck preview.
