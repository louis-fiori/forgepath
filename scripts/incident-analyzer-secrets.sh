#!/usr/bin/env bash
# Re-sync the incident-analyzer secrets from .env + ~/.aws, then restart the
# Deployment to pick them up (a Secret change does NOT restart consuming pods).
# Idempotent; run after editing .env or rotating your AWS profile/region/model.
#
#   incident-analyzer-secrets : profile / region / model / issues toggle / tokens
#   incident-analyzer-aws     : ~/.aws (config + credentials) for Bedrock profile resolution
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=./_env.sh
. "${REPO_ROOT}/scripts/_env.sh"

KCTX="${FORGEPATH_KUBE_CONTEXT:-kind-forgepath}"
NS="incident-analyzer"

command -v kubectl >/dev/null || { echo "missing dependency: kubectl" >&2; exit 1; }

kubectl --context "${KCTX}" create namespace "${NS}" \
  --dry-run=client -o yaml | kubectl --context "${KCTX}" apply -f - >/dev/null

# Preserve the service-to-service token shared with Backstage (source of truth:
# the backstage namespace). Fall back to $BACKSTAGE_S2S_TOKEN, else empty.
S2S="$(kubectl --context "${KCTX}" -n backstage get secret backstage-s2s-token \
  -o jsonpath='{.data.token}' 2>/dev/null | base64 -d || true)"
S2S="${S2S:-${BACKSTAGE_S2S_TOKEN:-}}"

AWS_AUTH_MODE="profile=${AWS_PROFILE:-default}"
[ -n "${AWS_ACCESS_KEY_ID:-}" ] && AWS_AUTH_MODE="access-key (${AWS_ACCESS_KEY_ID:0:4}…)"
echo "==> ${NS}/incident-analyzer-secrets (provider=${LLM_PROVIDER:-bedrock} ${AWS_AUTH_MODE} region=${AWS_REGION:-us-east-1} model=${BEDROCK_MODEL_ID:-us.anthropic.claude-sonnet-4-6} issues=${ISSUES_ENABLED:-true} poll=${POLL_ENABLED:-true} masking=${MASKING_ENABLED:-true})"
# stdin heredoc (not --from-literal) so the API key / PAT / token never hit the
# process argv (readable via `ps`). Numbers/booleans quoted to stay strings
# (k8s rejects non-string stringData values).
cat <<EOF | kubectl --context "${KCTX}" apply -f - >/dev/null
apiVersion: v1
kind: Secret
metadata:
  name: incident-analyzer-secrets
  namespace: ${NS}
stringData:
  # Keys are env var names: the Deployment injects the whole Secret via
  # `envFrom: secretRef`, so each must match its env var exactly (UPPER_SNAKE).
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
  GITHUB_TOKEN: "${GITHUB_TOKEN:-}"
  BACKSTAGE_S2S_TOKEN: "${S2S}"
EOF

# Mount local ~/.aws so the Bedrock path can resolve the profile. Only existing
# files are included; override the source dir with AWS_CONFIG_DIR.
AWS_DIR="${AWS_CONFIG_DIR:-$HOME/.aws}"
AWS_FROM_FILE=()
[ -f "${AWS_DIR}/config" ] && AWS_FROM_FILE+=(--from-file=config="${AWS_DIR}/config")
[ -f "${AWS_DIR}/credentials" ] && AWS_FROM_FILE+=(--from-file=credentials="${AWS_DIR}/credentials")
if [ "${#AWS_FROM_FILE[@]}" -gt 0 ]; then
  echo "==> ${NS}/incident-analyzer-aws from ${AWS_DIR}"
  kubectl --context "${KCTX}" -n "${NS}" create secret generic incident-analyzer-aws \
    "${AWS_FROM_FILE[@]}" \
    --dry-run=client -o yaml | kubectl --context "${KCTX}" apply -f - >/dev/null
else
  echo "==> no ${AWS_DIR}/{config,credentials}, skipping incident-analyzer-aws"
fi

echo "==> restarting deploy/incident-analyzer to pick up the new secrets"
if kubectl --context "${KCTX}" -n "${NS}" rollout restart deploy/incident-analyzer 2>/dev/null; then
  kubectl --context "${KCTX}" -n "${NS}" rollout status deploy/incident-analyzer --timeout=120s
else
  echo "    (deploy/incident-analyzer not present yet, ArgoCD will create it; secrets are ready)"
fi
