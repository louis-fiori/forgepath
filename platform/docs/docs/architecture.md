# Architecture

## Layout in this repo

```
forgepath/
├── platform/                # Sources (human-edited)
│   ├── argocd/install/      # Kustomize base for the ArgoCD install
│   ├── argocd/bootstrap/    # platform + previews ApplicationSets, rendered &
│   │                        # applied at boot by scripts/local-up.sh; secrets
│   │                        # are generated there too, never stored here
│   ├── backstage/           # Catalog, scaffolder templates, overlay
│   └── docs/                # This site
│
├── gitops/                  # The source of truth ArgoCD watches
│   ├── platform/            # Platform manifests (prom/loki/grafana/backstage/incident-*)
│   └── workloads/           # Preview workloads (populated by PRs)
│
├── local/                   # Generated, gitignored
│   └── backstage/           # Scaffolded Backstage app
│
└── scripts/                 # Bootstrap and sync helpers
```

## Flow at a glance

```
                 ┌──────────┐         ┌──────────────┐
                 │ Developer│         │  GitHub      │
                 └────┬─────┘         │  louis-fiori │
                      │ submits       │  /forgepath  │
                      │ template      └──────┬───────┘
                      ▼                      ▲
                 ┌──────────┐ opens PR       │
                 │ Backstage├────────────────┤
                 │  (UI)    │ closes PR      │
                 └──────────┘                │
                                             │ polls every 60s
                                             ▼
                                       ┌─────────────┐
                                       │  ArgoCD     │
                                       │  Application│
                                       │  Set        │
                                       └──────┬──────┘
                                              │ instantiates one
                                              │ Application per PR
                                              ▼
                                       ┌────────────────────────┐
                                       │ kind cluster           │
                                       │ preview-scaffold-<svc> │
                                       └────────────────────────┘
```

## Who owns what

Everything in the cluster belongs to `group:default/platform-team`. The
`System: forgepath` entity is the umbrella that groups Components, so the
catalog filter "system=forgepath" gives you the platform map.

## Boundaries

- Backstage **never** applies manifests directly. It produces PRs.
- ArgoCD **never** runs custom logic. It reconciles git → cluster.
- The kind cluster has no inbound from GitHub, ArgoCD does the pulling.
- Secrets (the PAT) stay in the cluster namespace where they're consumed
 , never committed to git.

## Incident detection (AI)

Alongside the deploy flow, two services give the platform a self-diagnosing
loop:

- **incident-generator** continuously emits structured error / OOMKill / panic
  logs (and exposes endpoints to trigger them on demand), so there's always
  realistic signal.
- **incident-analyzer** gathers that signal, error counts and samples from
  Loki, pod state from the Kubernetes API, metrics from Prometheus, and on an
  error spike / OOMKill / CrashLoop asks **Claude** (via Bedrock or the direct
  Anthropic API) for a root cause + remediation grounded in the service's
  TechDocs runbook. It then **files a GitHub issue** and a **Backstage
  notification**. It runs on a background poll loop or on demand from the
  "Analyze an incident" Backstage template.

```
incident-generator ──logs──▶ Loki ─┐
              pod state ──▶ K8s API ─┼─▶ incident-analyzer ──▶ Claude
                metrics ──▶ Prometheus┘            │
                                                   ├──▶ GitHub issue
                                                   └──▶ Backstage notification
```

**Sensitive data is masked** (emails, card numbers, IBANs, auth tokens,
IPv4/IPv6 addresses, phone numbers, secret key/value pairs) before any log line
or event leaves the cluster for the LLM. The costly endpoints (`/analyze`,
`/analyze-log`, `/settings`) require a shared service-to-service bearer token.
