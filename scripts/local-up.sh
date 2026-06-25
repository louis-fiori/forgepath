#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=./_env.sh
. "${REPO_ROOT}/scripts/_env.sh"

RUNTIME="${1:-kind}"
CLUSTER_NAME="forgepath"

require() {
  command -v "$1" >/dev/null || { echo "missing dependency: $1 (run: make deps)" >&2; exit 1; }
}
require kubectl
require docker

# Preflight the PAT before any cluster mutation — fail now, not after the ~5 min
# ArgoCD rollout (it's only consumed later, at Secret creation).
if [ -z "${GITHUB_TOKEN}" ]; then
  echo "ERROR: GITHUB_TOKEN is empty." >&2
  echo "  Copy .env.example to .env and fill in a fine-grained GitHub PAT scoped" >&2
  echo "  to ${FORGEPATH_GITHUB_OWNER}/${FORGEPATH_GITHUB_REPO}:" >&2
  echo "    - Contents: Read and write     (scaffolder writes the PR branch)" >&2
  echo "    - Pull requests: Read and write" >&2
  echo "    - Issues: Read and write       (incident-analyzer files incident issues)" >&2
  echo "    - Metadata: Read               (mandatory, auto-added)" >&2
  echo "  Then re-run \`make local-up\`." >&2
  exit 1
fi

case "${RUNTIME}" in
  kind)
    require kind
    KCTX="kind-${CLUSTER_NAME}"
    if kind get clusters | grep -qx "${CLUSTER_NAME}"; then
      echo "==> kind cluster '${CLUSTER_NAME}' already exists, skipping create"
    else
      echo "==> creating kind cluster '${CLUSTER_NAME}'"
      kind create cluster --config "${REPO_ROOT}/local/kind-config.yaml"
    fi
    ;;
  docker-desktop)
    KCTX="docker-desktop"
    if ! kubectl config get-contexts -o name | grep -qx "${KCTX}"; then
      echo "Docker Desktop Kubernetes is not enabled." >&2
      echo "Open Docker Desktop -> Settings -> Kubernetes -> Enable Kubernetes." >&2
      exit 1
    fi
    ;;
  *)
    echo "Unknown runtime: '${RUNTIME}' (expected: kind | docker-desktop)" >&2
    exit 1
    ;;
esac

echo "==> using context '${KCTX}'"

if [ "${RUNTIME}" = "kind" ] && docker image inspect forgepath/backstage:dev >/dev/null 2>&1; then
  echo "==> loading forgepath/backstage:dev into kind"
  kind load docker-image forgepath/backstage:dev --name "${CLUSTER_NAME}"
fi

# Side-load the incident-generator fixture image if built locally
# (`make incident-generator-build`). Manifests use imagePullPolicy: IfNotPresent,
# so it must be in the node cache before ArgoCD deploys. Optional; absence is OK.
if [ "${RUNTIME}" = "kind" ] && docker image inspect incident-generator:dev >/dev/null 2>&1; then
  echo "==> loading incident-generator:dev into kind"
  kind load docker-image incident-generator:dev --name "${CLUSTER_NAME}"
fi

# Side-load the incident-analyzer image if built locally
# (`make incident-analyzer-build`). Same IfNotPresent contract as above.
if [ "${RUNTIME}" = "kind" ] && docker image inspect incident-analyzer:dev >/dev/null 2>&1; then
  echo "==> loading incident-analyzer:dev into kind"
  kind load docker-image incident-analyzer:dev --name "${CLUSTER_NAME}"
fi

echo "==> installing ArgoCD (v3.4.2)"
# Server-Side Apply is mandatory: the applicationsets.argoproj.io CRD exceeds the
# 256KB `last-applied-configuration` annotation limit, so client-side apply fails.
kubectl --context "${KCTX}" apply -k "${REPO_ROOT}/platform/argocd/install" \
  --server-side --force-conflicts

echo "==> waiting for ArgoCD server rollout (up to 5 min)"
kubectl --context "${KCTX}" -n argocd rollout status deploy/argocd-server --timeout=300s

# Backstage and ArgoCD each need the PAT as a Secret in their namespace, both
# generated here from $GITHUB_TOKEN (single source of truth, no .local.yaml).
echo "==> ensuring namespaces argocd, backstage, grafana, incident-analyzer exist"
for ns in argocd backstage grafana incident-analyzer; do
  kubectl --context "${KCTX}" create namespace "${ns}" \
    --dry-run=client -o yaml | \
    kubectl --context "${KCTX}" apply -f - >/dev/null
done

# Generate a random Grafana admin password on first install (mirrors the ArgoCD
# bootstrap secret pattern). If the Secret exists we keep it, so re-running
# doesn't rotate the password. Read it back with `make grafana-pw`.
if ! kubectl --context "${KCTX}" -n grafana get secret grafana-admin >/dev/null 2>&1; then
  # 12 random bytes -> 24 hex chars. openssl avoids the SIGPIPE trap of
  # `tr -dc … </dev/urandom | head -c 24`: head closes the pipe, tr exits 141,
  # and pipefail aborts the script.
  GRAFANA_PW="$(openssl rand -hex 12)"
  # stdin heredoc, not `--from-literal`: literal values land in the process argv,
  # readable by any local user via `ps`. stringData keeps the password on stdin only.
  cat <<EOF | kubectl --context "${KCTX}" apply -f - >/dev/null
apiVersion: v1
kind: Secret
metadata:
  name: grafana-admin
  namespace: grafana
stringData:
  admin-user: admin
  admin-password: "${GRAFANA_PW}"
EOF
  echo "==> generated random grafana admin password, read it with \`make grafana-pw\`"
  unset GRAFANA_PW
else
  echo "==> grafana-admin Secret already present, keeping existing password"
fi

echo "==> creating Secret backstage/backstage-github-token from \$GITHUB_TOKEN"
# Mounted into the Backstage backend as GITHUB_TOKEN. Drives the built-in
# publish:github:pull-request and the custom github:closePullRequest actions.
# stdin heredoc (not --from-literal) so the PAT never appears in the process argv.
cat <<EOF | kubectl --context "${KCTX}" apply -f - >/dev/null
apiVersion: v1
kind: Secret
metadata:
  name: backstage-github-token
  namespace: backstage
stringData:
  token: "${GITHUB_TOKEN}"
EOF

# Shared service-to-service token for Backstage notifications. Reuse the existing
# one if present (don't rotate on re-run), else $BACKSTAGE_S2S_TOKEN (.env), else
# random. The SAME value goes into Backstage (backstage-s2s-token) and the analyzer
# (incident-analyzer-secrets) so the bearer token matches on both ends.
if S2S="$(kubectl --context "${KCTX}" -n backstage get secret backstage-s2s-token \
      -o jsonpath='{.data.token}' 2>/dev/null | base64 -d)" && [ -n "${S2S}" ]; then
  echo "==> backstage-s2s-token Secret already present, reusing it"
else
  S2S="${BACKSTAGE_S2S_TOKEN:-$(openssl rand -hex 32)}"
  echo "==> creating Secret backstage/backstage-s2s-token (service-to-service auth)"
fi
# stdin heredoc (not --from-literal) so the token never appears in the process argv.
cat <<EOF | kubectl --context "${KCTX}" apply -f - >/dev/null
apiVersion: v1
kind: Secret
metadata:
  name: backstage-s2s-token
  namespace: backstage
stringData:
  token: "${S2S}"
EOF

echo "==> creating Secret incident-analyzer/incident-analyzer-secrets"
# ANTHROPIC_API_KEY enables the direct-API LLM path; GITHUB_TOKEN files issues
# (needs Issues: write); the s2s token matches Backstage for notifications.
# Bedrock auth is AWS_PROFILE (from mounted ~/.aws) or explicit
# AWS_ACCESS_KEY_ID/SECRET (+ session token); the access key wins when set.
# MASKING_ENABLED redacts PII/cardholder data from log samples before the LLM.
# Non-secret keys (provider, model, thresholds, github owner/repo/branch) ride
# along so every .env setting reaches the pod without a manifest edit. Empty
# values just disable that path (env refs are optional).
# stdin heredoc (not --from-literal) so the API key / PAT / token never hit the
# process argv. Numbers/booleans quoted to stay strings (k8s rejects non-string
# stringData values).
cat <<EOF | kubectl --context "${KCTX}" apply -f - >/dev/null
apiVersion: v1
kind: Secret
metadata:
  name: incident-analyzer-secrets
  namespace: incident-analyzer
stringData:
  # Keys are env var names: the Deployment injects the whole Secret via
  # `envFrom: secretRef`, so each must match its env var exactly (UPPER_SNAKE).
  # Keep in sync with scripts/incident-analyzer-secrets.sh.
  ANTHROPIC_API_KEY: "${ANTHROPIC_API_KEY:-}"
  AWS_PROFILE: "${AWS_PROFILE:-default}"
  AWS_ACCESS_KEY_ID: "${AWS_ACCESS_KEY_ID:-}"
  AWS_SECRET_ACCESS_KEY: "${AWS_SECRET_ACCESS_KEY:-}"
  AWS_SESSION_TOKEN: "${AWS_SESSION_TOKEN:-}"
  AWS_REGION: "${AWS_REGION:-us-east-1}"
  BEDROCK_MODEL_ID: "${BEDROCK_MODEL_ID:-us.anthropic.claude-sonnet-4-6}"
  LLM_PROVIDER: "${LLM_PROVIDER:-bedrock}"
  ANALYZER_MODEL: "${ANALYZER_MODEL:-claude-sonnet-4-6}"
  WATCH_NAMESPACES: "${WATCH_NAMESPACES:-incident-generator}"
  POLL_INTERVAL_SECONDS: "${POLL_INTERVAL_SECONDS:-60}"
  ERROR_THRESHOLD: "${ERROR_THRESHOLD:-20}"
  GITHUB_OWNER: "${FORGEPATH_GITHUB_OWNER}"
  GITHUB_REPO: "${FORGEPATH_GITHUB_REPO}"
  GITHUB_BRANCH: "${FORGEPATH_TARGET_BRANCH}"
  ISSUES_ENABLED: "${ISSUES_ENABLED:-true}"
  POLL_ENABLED: "${POLL_ENABLED:-true}"
  MASKING_ENABLED: "${MASKING_ENABLED:-true}"
  GITHUB_TOKEN: "${GITHUB_TOKEN}"
  BACKSTAGE_S2S_TOKEN: "${S2S}"
EOF
unset S2S

# Materialize local ~/.aws into a Secret mounted at /aws so the Bedrock path can
# resolve an AWS profile (static-key or assume-role) without keys in .env. Not
# needed if you set AWS_ACCESS_KEY_ID/SECRET above. Only existing files are
# included; with no ~/.aws the Secret is empty and the optional volume no-ops.
AWS_DIR="${AWS_CONFIG_DIR:-$HOME/.aws}"
AWS_FROM_FILE=()
[ -f "${AWS_DIR}/config" ] && AWS_FROM_FILE+=(--from-file=config="${AWS_DIR}/config")
[ -f "${AWS_DIR}/credentials" ] && AWS_FROM_FILE+=(--from-file=credentials="${AWS_DIR}/credentials")
if [ "${#AWS_FROM_FILE[@]}" -gt 0 ]; then
  echo "==> creating Secret incident-analyzer/incident-analyzer-aws from ${AWS_DIR} (profile=${AWS_PROFILE:-default})"
  kubectl --context "${KCTX}" -n incident-analyzer create secret generic incident-analyzer-aws \
    "${AWS_FROM_FILE[@]}" \
    --dry-run=client -o yaml | \
    kubectl --context "${KCTX}" apply -f - >/dev/null
else
  echo "==> no ${AWS_DIR}/{config,credentials} found, skipping incident-analyzer-aws (detection-only unless ANTHROPIC_API_KEY set)"
fi

echo "==> creating Secret argocd/forgepath-repo-creds from \$GITHUB_TOKEN"
# ArgoCD reads this via the `argocd.argoproj.io/secret-type: repo-creds` label,
# to clone the fork over HTTPS and authenticate the pullRequest ApplicationSet
# generator against the GitHub API.
cat <<EOF | kubectl --context "${KCTX}" apply -f - >/dev/null
apiVersion: v1
kind: Secret
metadata:
  name: forgepath-repo-creds
  namespace: argocd
  labels:
    argocd.argoproj.io/secret-type: repo-creds
stringData:
  type: git
  url: https://github.com/${FORGEPATH_GITHUB_OWNER}/${FORGEPATH_GITHUB_REPO}
  username: ${FORGEPATH_GITHUB_OWNER}
  password: ${GITHUB_TOKEN}
EOF

echo "==> applying ArgoCD bootstrap ApplicationSets"
echo "    (resolving FORGEPATH_* placeholders: ${FORGEPATH_GITHUB_OWNER}/${FORGEPATH_GITHUB_REPO}@${FORGEPATH_TARGET_BRANCH})"
# Bootstrap manifests carry ${FORGEPATH_*} placeholders rendered and applied here
# — ArgoCD never reads them from git, so a fork needs nothing beyond .env (no
# `make configure`, no commit). The generated Applications live in-cluster with
# rendered values; only workload manifests under gitops/ are pulled from git.
for _manifest in "${REPO_ROOT}"/platform/argocd/bootstrap/*.yaml; do
  forgepath_render "${_manifest}" | kubectl --context "${KCTX}" apply -f -
done

echo "==> waiting for Backstage rollout (up to 5 min, deployed by ArgoCD)"
kubectl --context "${KCTX}" -n backstage rollout status deploy/backstage --timeout=300s || \
  echo "    (skipping: deploy/backstage not yet created, check ArgoCD UI for sync errors)"

echo
echo "Backstage is up: http://localhost:7007"
echo "ArgoCD UI is up: http://localhost:8080 (user: admin, password: \`make argocd-pw\`)"
echo "Catalog products are deployable on demand via http://localhost:7007/create"
