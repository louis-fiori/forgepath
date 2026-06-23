# ForgePath ⚒️

**An open-source starter kit for an AI-ready Internal Developer Platform, local-first, GitOps-driven, observable by default.**

ForgePath shows how a small platform team can give application developers a clean self-service experience: open a PR from Backstage, see your service running in a preview namespace seconds later, close the PR to tear it down. Cluster state stays fully described in Git; ArgoCD reconciles; Prometheus and Loki give you metrics and logs without per-service wiring. An AI Incident Analyzer watches the same observability stack, asks Claude to diagnose what broke, and surfaces the result as a GitHub issue and a Backstage notification.

> 🚧 **Status:** Work in progress
> 🎯 **Scope:** Reference implementation / portfolio project / learning-oriented starter kit
> ⚠️ **Not production-ready**, emptyDir storage, demo-grade limits, no HA, no real auth.

---

## ✨ What it does

- **Self-service deploys**, fill a Backstage form, get a PR with rendered K8s manifests
- **Auto preview environments**, labeled PRs are deployed into `preview-scaffold-<name>` namespaces by ArgoCD
- **Observability by default**, Prometheus scrapes every pod for CPU/memory, Promtail forwards every log line into Loki, three Grafana dashboards (`Cluster pods` + `Service logs` + `Logs · Error explorer`) are auto-provisioned and deep-linked from each service's catalog entry
- **TechDocs runbooks**, each scaffolded service ships with an editable mkdocs runbook, served straight in Backstage
- **AI incident detection**, the `incident-analyzer` watches Loki, Prometheus and the K8s API; on an error spike / OOMKill / CrashLoop it asks Claude (via Bedrock or the direct Anthropic API) for a root cause + remediation, then files a GitHub issue and a Backstage notification. Sensitive data is masked before it ever reaches the LLM. Run it on a poll loop or on demand from a Backstage form.
- **Closing the PR cleans up**, ArgoCD ApplicationSet prunes the namespace, Backstage marks the catalog entity orphan once the branch is gone

---

## 🚀 Quickstart

```bash
git clone https://github.com/louis-fiori/forgepath.git && cd forgepath
cp .env.example .env  # fill in GITHUB_TOKEN (fine-grained PAT, see .env.example)

make backstage-init   # scaffolds local/backstage/ (~5 min, one-time)
make backstage-build  # builds the Backstage Docker image (~5 min, one-time)
make local-up         # creates the kind cluster + applies the platform
```

After `make local-up`:

| Service        | URL                       | Credentials                          |
|----------------|---------------------------|--------------------------------------|
| Backstage      | http://localhost:7007     | guest auth                           |
| ArgoCD UI      | http://localhost:8080     | admin / `make argocd-pw`             |
| Grafana        | http://localhost:3000     | admin / `make grafana-pw`            |
| Preview demo slot | http://localhost:8888  | n/a (any preview with `exposeOnLocalhost: true`) |
| incident-generator | http://localhost:8889  | n/a (`make incident TYPE=panic` to trigger one) |

Full walkthrough, including prerequisites and platform-specific notes, in [docs/quickstart.md](docs/quickstart.md).

---

## 🧩 What's in the box

| Component            | Role                                                 | Where it lives                      |
|----------------------|------------------------------------------------------|-------------------------------------|
| **Backstage**        | Developer portal, catalog, scaffolder, Kubernetes UI | `platform/backstage/`               |
| **ArgoCD**          | GitOps engine, syncs `gitops/` to the cluster        | `platform/argocd/`, `gitops/platform/`  |
| **Prometheus**       | Pod metrics via kubelet/cAdvisor + annotation-based scraping | `gitops/platform/prometheus/`       |
| **Loki + Promtail**  | Cluster-wide log ingestion (every pod, no wiring)    | `gitops/platform/loki/`             |
| **Grafana**          | Dashboards + datasources, auto-provisioned           | `gitops/platform/grafana/`          |
| **incident-analyzer**| AI incident detector, Loki/Prometheus/K8s → Claude → GitHub issue + Backstage notification | `services/incident-analyzer/`       |
| **incident-generator**| Chaos fixture that misbehaves on purpose so there's something to detect | `services/incident-generator/`      |
| **kind cluster**     | Local single-node K8s with port mappings             | `local/kind-config.yaml`            |

The architecture, GitOps flow, and observability wiring are described in [docs/architecture.md](docs/architecture.md). Working IN the repo (make targets, customizations, fork setup) is covered in [docs/development.md](docs/development.md).

Once the cluster is up, Backstage also serves the in-product **TechDocs** version of the operations guide at <http://localhost:7007/docs/default/component/forgepath-platform>.

---

## 🚫 What it isn't

- A production-ready platform (no HA, no PVCs, no real auth, no rate limiting)
- A complete AIOps tool, the AI Incident Analyzer detects and diagnoses, but it isn't a full alerting/correlation pipeline
- An autonomous remediation agent, the analyzer files an issue and notifies; humans drive every change
- A clone of Backstage, ArgoCD, Datadog, or any existing platform
- A universal framework for every Kubernetes use case

It is a practical starter kit meant to be read, tested, adapted, and extended.

---

## 🚧 Production gaps

**ForgePath is a learning project, not a production platform.** The shortcuts below are deliberate, they keep the repo small, local-first, and readable. Each is a conscious trade-off, not an oversight. If you wanted to take this to production, this is the list you'd have to work through:

| Area | What ForgePath does | What production would need |
|---|---|---|
| **Storage** | `emptyDir` everywhere, Loki, Grafana, Prometheus lose all data on pod restart | PersistentVolumeClaims (or managed log/metric backends), backups, retention policy |
| **Availability** | Single-node kind, one replica per component, no PodDisruptionBudgets | Multi-node, HA control plane, replicas + anti-affinity, autoscaling |
| **AuthN / AuthZ** | Backstage guest auth, `allow-all` permission policy, ArgoCD `server.insecure=true` | Real SSO/OIDC, RBAC and Backstage permission policies, TLS everywhere |
| **Secrets** | Materialized into K8s Secrets from `.env` + `~/.aws` at boot | A secrets manager (Vault / External Secrets / cloud KMS), rotation, no plaintext on disk |
| **Images** | Built locally and side-loaded into kind, tagged `:dev` | A registry, immutable digests, image signing + provenance, vulnerability scanning |
| **Analyzer state** | In-memory dedup cache, lost on restart, so an incident can re-file after a redeploy | Durable dedup (DB/CRD), correlation across incidents, alert suppression windows |
| **Networking** | No NetworkPolicies, no ingress controller, NodePort + host port-maps | Ingress/Gateway, NetworkPolicies, mTLS / service mesh as needed |
| **Tenancy** | Single trust domain, no resource quotas on preview namespaces | Namespace quotas/limits, multi-tenancy isolation, cost controls |
| **Supply chain** | CI lints + tests the services, validates manifests, and Trivy-scans both service images for CRITICAL/HIGH CVEs; no SBOM or signing | SBOMs, signed releases, provenance, policy gates |

See [docs/architecture.md](docs/architecture.md) for the intentional boundaries behind these choices, [SECURITY.md](SECURITY.md) for the security policy (secrets, LLM data, reporting, pre-exposure checklist), and [docs/threat-model.md](docs/threat-model.md) for the threat-by-threat breakdown.

---

## 📄 License

ForgePath is released under the [Apache License 2.0](LICENSE).
