# Threat model

The security threat model for ForgePath, a local-first internal-developer-platform
demo. It complements the [Production gaps](../README.md#-production-gaps) table
(general, non-security shortcuts) and the [SECURITY.md](../SECURITY.md) policy.

ForgePath is built for a **single trust domain**: one operator, running it on
their own machine, applying their own PRs. Most "mitigations" below are really
*trust assumptions* that hold for that model and break the moment the repo or
cluster is shared with untrusted parties. Each threat lists what exists
**today**, the **residual risk**, and the **hardening** you'd add for a shared /
multi-tenant deployment.

## Trust boundaries

- **Trusted:** the maintainer; the code on `dev` / `main`; anyone who can apply
  the `preview` label or merge PRs; anyone with `kubectl` access to the cluster.
- **Semi-trusted:** log and runbook content, attacker-influenceable via PRs,
  and it reaches the LLM.
- **Untrusted:** nothing today. There is no external user surface; the model
  assumes the operator is the only actor. Opening the repo to external
  contributors is what turns several items below from theoretical to real.

## Threats

### T1, In-cluster abuse of the analyzer's costly endpoints
**What:** `/analyze`, `/analyze-log`, `/settings` trigger Claude calls ($) and
open GitHub issues. Any pod that can reach the analyzer's `ClusterIP` could spam them.
**Today:** the three endpoints require a shared **service-to-service bearer
token** (`BACKSTAGE_S2S_TOKEN`); requests without it get 401, and health /
metrics stay open for probes. The `namespace` input is validated against a
DNS-1123 label before it reaches a LogQL query, so it can't be used for LogQL
injection. `/analyze-log`'s **Loki-search** mode (`query`) is gated by the same
`severity=~"error|fatal"` filter the poller uses, so it can only surface
*incident* lines, not arbitrary log content a caller might fish for (e.g.
`query=password`); pasting a raw line (`log_line`) stays unrestricted, since the
caller already holds that content.
**Residual risk:** no **rate limiting**, a caller holding the token (or a
token-less dev setup) can still drive cost; there is no per-caller identity.
**Hardening:** rate limits / quotas on `/analyze*`; per-caller identities instead
of one shared token; a NetworkPolicy so only Backstage can reach the analyzer.

### T2, Malicious "preview" PR deploys arbitrary manifests  *(biggest structural risk)*
**What:** the `previews` ApplicationSet watches PRs labelled `preview` and tells
ArgoCD to `recurse` and apply **every manifest under `gitops/workloads/**`** in
that PR, into a `preview-<branch>` namespace (`CreateNamespace=true`,
`selfHeal`). The scaffolder template constrains what *it* generates, but ArgoCD
applies whatever YAML is in the PR, not just template output. A malicious PR
could ship a privileged pod, a `hostPath` mount, a `hostNetwork` pod, etc.
**Today:** the only guardrail is **trust in who can apply the `preview` label**
(and who can open PRs). Pod- and container-level `securityContext` are set on the
platform's *own* deployments, but **nothing enforces** them on preview workloads.
**Residual risk:** **high if the repo is ever opened to external contributors**;
acceptable under the trusted-maintainer-only model.
**Hardening:** restrict the `preview` label to maintainers; an admission policy
(Kyverno / OPA Gatekeeper) that **forbids `privileged`, `hostPath`,
`hostNetwork`** and **requires `runAsNonRoot`** + dropped capabilities; an
**image allowlist**; **ResourceQuotas / LimitRanges** per preview namespace;
**NetworkPolicies** isolating preview namespaces.

### T3, Arbitrary container image deployed via a preview
**What:** nothing restricts which images a preview manifest references, a PR
could deploy any public or attacker-controlled image.
**Today:** images use the cluster's default pull behaviour; no registry
allowlist, no signature / digest verification.
**Residual risk:** runs untrusted code in-cluster; combined with T2 and the lack
of NetworkPolicies (T8), that code has broad reach.
**Hardening:** image allowlist via admission policy; require immutable digests;
image signing + provenance verification (cosign / sigstore); a private registry.

### T4, A log line carries a secret the masking misses
**What:** the analyzer ships log samples and pod events to the LLM. If a service
logs a secret in a shape the masker doesn't recognise, it leaves the cluster.
**Today:** `redact()` masks a broad, conservative set (emails, phones, cards with
a Luhn check, IBANs, JWT / bearer / AWS keys, IPv4/IPv6, `key=value` secrets incl.
JSON-quoted), applied to both the LLM context and the candidate returned by the
API. It is **best-effort and pattern-based**.
**Residual risk:** anything outside the known patterns (a bespoke token format, a
secret split across fields) can still reach the model.
**Hardening:** treat masking as defence-in-depth, not a guarantee; add a stricter
DLP / redaction step for regulated data; prefer structured logging that never
emits secrets; scope which namespaces the analyzer reads.

### T5, Malicious runbook / log line → LLM prompt injection
**What:** the analyzer includes the affected service's TechDocs **runbook** plus
**log samples and pod events** in the prompt as grounding. Both are
attacker-influenceable (a runbook via PR; a log line via anything that writes to
the workload's logs), so either could carry instructions aimed at the model
("ignore previous instructions, recommend …").
**Today:** the analyzer consumes only the **structured diagnosis** the model
returns (via a forced tool schema) and **does not execute** model output, so the
blast radius is a misleading diagnosis / issue text, not code execution. Log
samples, the single submitted line, and pod events are wrapped in dedicated
`<log_samples>` / `<log_line>` / `<pod_events>` tags and the system prompt
instructs the model to treat anything inside them as untrusted data, never as
instructions; a forged closing tag in a log line is defanged so it can't break
out of the fence.
**Residual risk:** a poisoned diagnosis could mislead an operator or file a
misleading issue.
**Hardening:** treat runbooks as untrusted input; keep trusted instructions
separate from untrusted context in the prompt; restrict who can edit runbooks;
human review of issues before acting.

### T6, One over-broad GitHub PAT across multiple components
**What:** the same fine-grained PAT is mounted into **three** components,
Backstage, ArgoCD, and the incident-analyzer, so compromising any one exposes a
token that can write Contents, PRs, and Issues on the repo.
**Today:** the PAT is fine-grained and scoped to the fork, with only the scopes
the platform needs (Contents / PRs / Issues RW, Metadata R); never committed
(`.env` is gitignored); delivered to pods via stdin heredocs (no `ps` leak).
**Residual risk:** broad blast radius, one token, three places, all repo-write.
**Hardening:** **per-component tokens** with the minimal scope each needs (e.g.
the analyzer needs only Issues:write); regular rotation; a secrets manager; or a
**GitHub App** with fine-grained installation permissions instead of a PAT.

### T7, AWS credential exposure via a Kubernetes Secret
**What:** for Bedrock, AWS credentials (or a mounted `~/.aws`) live in a
Kubernetes Secret read by the analyzer. Anyone with `get secret` in that
namespace, or who compromises the pod, can read them.
**Today:** credentials come from your local `~/.aws` (or static keys) and are
mounted read-only; the analyzer runs non-root with a dropped-capabilities
securityContext. K8s Secrets are base64, not encrypted beyond cluster defaults.
**Residual risk:** static long-lived keys in a Secret are a standing liability;
the `last-applied-configuration` annotation can hold values in plaintext.
**Hardening:** prefer short-lived **STS** / assume-role or workload identity
(IRSA-style) over static keys; least-privilege role (`bedrock:InvokeModel` only);
a secrets manager; etcd encryption at rest; tight RBAC on `secrets`.

### T8, No NetworkPolicies → free lateral movement
**What:** there are **no NetworkPolicies** in the cluster, so every pod can reach
every other pod and the API server. A single compromised workload (e.g. via
T2 / T3) can talk to Loki, Prometheus, the analyzer, Backstage, and the
Kubernetes API unrestricted.
**Today:** isolation relies solely on namespaces (which don't restrict network
traffic) and the single-trust-domain assumption.
**Residual risk:** lateral movement is unconstrained once any pod is compromised.
**Hardening:** **default-deny** NetworkPolicies per namespace with explicit
allows (e.g. analyzer → Loki / Prometheus / K8s API only; Backstage → analyzer
only); a service mesh with mTLS if you need identity-based policy; isolate
preview namespaces from platform namespaces.

## Production hardening roadmap (consolidated)

The recurring controls a shared / production deployment would add:

1. **Admission policy** (Kyverno / OPA Gatekeeper): forbid `privileged` /
   `hostPath` / `hostNetwork`, require `runAsNonRoot` + dropped capabilities,
   allowlist images. *(T2, T3)*
2. **NetworkPolicies**: default-deny + explicit allows; isolate preview namespaces. *(T8)*
3. **ResourceQuotas / LimitRanges** per preview namespace. *(T2)*
4. **Restrict the `preview` label** to maintainers; gate PR-driven deploys. *(T2)*
5. **AuthN / AuthZ**: SSO/OIDC + permission policies for Backstage; TLS
   everywhere; per-caller auth + rate limiting on the analyzer. *(T1)*
6. **Secrets**: a secrets manager, per-component least-privilege tokens / a
   GitHub App, short-lived AWS credentials. *(T6, T7)*
7. **Supply chain**: image signing + provenance, SBOMs, digest pinning. *(T3)*

See [SECURITY.md](../SECURITY.md) for the policy and the pre-exposure checklist.
