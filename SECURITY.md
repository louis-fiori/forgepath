# Security Policy

ForgePath is a **learning / portfolio project**, an internal-developer-platform
demo that runs entirely in a local `kind` cluster. It is **not** built or
operated as a production system. This document covers what's in scope, the
security posture of the demo, what data leaves the cluster, the threat model in
brief, how to report an issue, and what you'd lock down before exposing any of
it on a network.

Full threat-by-threat breakdown: [docs/threat-model.md](docs/threat-model.md).
Broader (non-security) production shortcuts: the
[Production gaps](README.md#-production-gaps) table in the README.

## Supported scope

- The codebase on the default branch (`main`) and the active development branch (`dev`).
- The two first-party services: `incident-generator` (Go) and `incident-analyzer` (Python/FastAPI).
- The platform glue: the ArgoCD bootstrap ApplicationSets, the Backstage overlay + custom
  scaffolder actions, the GitOps manifests under `gitops/`, and the bootstrap
  scripts under `scripts/`.

Third-party components (ArgoCD, Backstage, Grafana, Loki, Prometheus, the Claude
SDK, kind) are version-pinned, but their own CVEs are tracked upstream; the CI
Trivy scan covers the two first-party images only.

## Non-production disclaimer

ForgePath is deliberately local-first and permissive so the whole platform fits
in one readable repo and boots on a laptop. **By design**:

- Backstage runs with **guest auth**, an **allow-all permission policy**, and
  `dangerouslyDisableDefaultAuthPolicy: true`.
- ArgoCD runs with `server.insecure=true` (no TLS on the API/UI).
- All storage is `emptyDir` (no persistence).
- There are **no NetworkPolicies**, no ingress controller, and no admission policies.
- Secrets are materialized into Kubernetes `Secret` objects (base64, not
  encrypted at rest beyond what the cluster provides).

None of these are suitable for a shared or internet-exposed environment. **Do
not deploy ForgePath as-is anywhere reachable by untrusted users or networks.**
See the pre-exposure checklist at the bottom.

## Secrets management

- The only secret you provide is a **fine-grained GitHub PAT** (and optionally
  AWS credentials for Bedrock), kept in `.env`, which is **gitignored**.
- `scripts/local-up.sh` reads `.env` and generates the in-cluster Secrets at
  boot, there are no committed secret files. Values are piped to
  `kubectl apply` via **stdin heredocs**, so they never appear in the process
  argument list (`ps`).
- Required PAT scopes (see `.env.example`): Contents (RW), Pull requests (RW),
  Issues (RW), Metadata (R). Grant nothing more.
- The PAT is mounted into **three** components, Backstage
  (`backstage-github-token`), ArgoCD (`forgepath-repo-creds`), and the
  incident-analyzer (`incident-analyzer-secrets`). One broad token across three
  blast radii is a known trade-off (threat-model **T6**).
- AWS credentials, when used, are mounted from your `~/.aws` into a Secret
  (`incident-analyzer-aws`), read-only. Prefer short-lived / STS credentials and
  a least-privilege role (`bedrock:InvokeModel`).
- **Caveat:** `kubectl apply` records the applied manifest in the
  `last-applied-configuration` annotation, so Secret values exist in plaintext
  in that annotation on the object. A real deployment would use a secrets
  manager (Vault / External Secrets / sealed-secrets) and encryption at rest.

## Data sent to the LLM

The incident-analyzer sends incident context to Claude (via AWS Bedrock or the
direct Anthropic API). Before anything leaves the cluster:

- **Masking** (`MASKING_ENABLED=true`, on by default) redacts well-known
  sensitive shapes from log samples and pod events: emails, phone numbers,
  payment card numbers (Luhn-checked), IBANs, JWTs / bearer tokens / AWS keys,
  IPv4/IPv6 addresses, and `key=value` secret pairs (including JSON-quoted). It
  applies to both the LLM context **and** the candidate returned by the API. It
  is **best-effort and pattern-based**, it reduces exposure, it does not
  guarantee zero leakage; anything outside the known patterns can still reach
  the model (threat-model **T4**).
- The prompt also includes the affected service's **TechDocs runbook** as
  grounding. A runbook is attacker-influenceable via a PR, so it is a
  prompt-injection vector (threat-model **T5**). The analyzer consumes only the
  structured diagnosis it gets back and **does not execute** model output.
- The destination depends on `LLM_PROVIDER`: with **Bedrock**, data stays within
  your AWS account's Bedrock region; with the **direct API**, it goes to
  Anthropic. Choose the provider whose data-handling terms you accept.

## Threat model (summary)

Full breakdown in [docs/threat-model.md](docs/threat-model.md). Headline risks:

- **Untrusted preview PRs** are the biggest structural risk: ArgoCD applies any
  manifests under `gitops/workloads/**` in a PR labelled `preview`. Today's
  guardrail is *trust in who can apply that label*, not policy enforcement.
- **Costly endpoints** (`/analyze`, `/analyze-log`, `/settings`) trigger LLM
  calls and open issues; gated by a shared S2S token, but **no rate limiting**.
- **Masking is best-effort**, so secrets in unusual shapes can reach the LLM.
- **One PAT in three components**; **no NetworkPolicies** (free lateral movement).

## Reporting a vulnerability

This is a personal learning project with **no security SLA**. If you find something:

- Preferred: open a **private vulnerability report** via the repository's
  **Security → Report a vulnerability** (GitHub private advisories).
- For non-sensitive hardening ideas, a regular issue or PR is welcome.

Please don't open a public issue with exploit details for anything that could
affect someone running the demo.

## Checklist before exposing ForgePath on any network

ForgePath is meant to run on `localhost`. If you ever put it somewhere reachable
by others, treat the following as **mandatory**, not optional:

- [ ] Replace Backstage guest auth with **SSO/OIDC**; remove
      `dangerouslyDisableDefaultAuthPolicy`; replace the allow-all policy with a
      real **permission policy**.
- [ ] Put **TLS** in front of Backstage and ArgoCD (drop `server.insecure`).
- [ ] Restrict who can apply the **`preview`** label to maintainers; add an
      admission policy (Kyverno / OPA Gatekeeper) forbidding `privileged`,
      `hostPath`, `hostNetwork` and requiring `runAsNonRoot` + dropped
      capabilities; **allowlist images**; set **ResourceQuotas** per preview namespace.
- [ ] Add **NetworkPolicies** (default-deny + explicit allows) so a compromised
      pod can't reach Loki / Prometheus / the analyzer / the API server laterally.
- [ ] Move secrets to a **secrets manager**; scope the GitHub PAT down per
      component (or use a GitHub App); use short-lived AWS credentials.
- [ ] Add **rate limiting** / quotas on the analyzer's `/analyze*` endpoints.
- [ ] Keep `MASKING_ENABLED=true`; review masking coverage for your data; add a
      stricter DLP step if logs may carry regulated data.
- [ ] Turn on persistence + backups; work through the full
      [Production gaps](README.md#-production-gaps) table.
