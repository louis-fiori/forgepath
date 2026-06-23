#!/usr/bin/env bash
# Trigger an incident on the incident-generator from the host.
#
# The generator runs in-cluster (namespace incident-generator) and exposes
# on-demand failure endpoints (/boom, /panic, /crash, /leak, /slow, /error/<type>),
# reachable at http://localhost:8889 via the NodePort. If that host port isn't
# mapped (older cluster or a runtime without it), the script falls back to an
# ephemeral `kubectl port-forward`, so it works either way.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=./_env.sh
. "${REPO_ROOT}/scripts/_env.sh"

NS="incident-generator"
DEFAULT_URL="http://localhost:8889"
BASE_URL="${INCIDENT_GEN_URL:-${DEFAULT_URL}}"
KCTX="${FORGEPATH_KUBE_CONTEXT:-kind-forgepath}"

MB=16
MS=3000
COUNT=1

usage() {
  cat >&2 <<'EOF'
Usage: scripts/trigger-incident.sh <type> [options]

Types:
  boom              random catalogue failure (HTTP error + log line)
  panic             real panic, recovered to a logged 500
  crash             exit(1) -> CrashLoopBackOff (the pod restarts)
  leak [--mb N]     grow the heap by N MB (repeat toward an OOMKill; default 16)
  slow [--ms N]     sleep N ms then respond (latency incident; default 3000)
  <error-type>      a specific catalogue scenario via /error/<type>
                    (e.g. db-connection-timeout, payment-gateway-declined)
  list              print the known catalogue error types and exit

Options:
  --count N         fire the request N times (default 1)
  --mb N            heap growth for `leak`, in MB
  --ms N            sleep for `slow`, in ms
  --url URL         base URL (default: $INCIDENT_GEN_URL or http://localhost:8889)
  --context CTX     kube context for the port-forward fallback
                    (default: $FORGEPATH_KUBE_CONTEXT or kind-forgepath)

Examples:
  scripts/trigger-incident.sh panic
  scripts/trigger-incident.sh leak --mb 32 --count 5            # push toward an OOMKill
  scripts/trigger-incident.sh db-connection-timeout --count 30  # cross the analyzer's error threshold
EOF
}

if [ $# -eq 0 ]; then
  usage
  exit 2
fi

TYPE="$1"
shift

while [ $# -gt 0 ]; do
  case "$1" in
    --count) COUNT="$2"; shift 2 ;;
    --mb)    MB="$2"; shift 2 ;;
    --ms)    MS="$2"; shift 2 ;;
    --url)   BASE_URL="$2"; shift 2 ;;
    --context) KCTX="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage; exit 2 ;;
  esac
done

# --- Resolve a reachable base URL ------------------------------------------
# Probe the configured URL; if it doesn't answer, port-forward to the in-cluster
# Service and use that instead.
PF_PID=""
cleanup() { [ -n "${PF_PID}" ] && kill "${PF_PID}" 2>/dev/null || true; }
trap cleanup EXIT

reachable() { curl -fsS --max-time 2 "$1/healthz" >/dev/null 2>&1; }

if ! reachable "${BASE_URL}"; then
  echo "==> ${BASE_URL} not reachable, falling back to a kubectl port-forward (context ${KCTX})" >&2
  LOCAL_PORT=$(( (RANDOM % 1000) + 18000 ))
  kubectl --context "${KCTX}" -n "${NS}" port-forward "svc/${NS}" "${LOCAL_PORT}:80" >/dev/null 2>&1 &
  PF_PID=$!
  BASE_URL="http://localhost:${LOCAL_PORT}"
  for _ in $(seq 1 20); do
    reachable "${BASE_URL}" && break
    sleep 0.5
  done
  if ! reachable "${BASE_URL}"; then
    echo "could not reach the incident-generator (is the cluster up? try: make incident-generator-load)" >&2
    exit 1
  fi
fi

# `list` just dumps the index page (which prints the known error types).
if [ "${TYPE}" = "list" ]; then
  curl -fsS "${BASE_URL}/"
  exit 0
fi

# --- Map the friendly type to an endpoint path -----------------------------
case "${TYPE}" in
  boom)  PATH_Q="/boom" ;;
  panic) PATH_Q="/panic" ;;
  crash) PATH_Q="/crash" ;;
  leak)  PATH_Q="/leak?mb=${MB}" ;;
  slow)  PATH_Q="/slow?ms=${MS}" ;;
  *)     PATH_Q="/error/${TYPE}" ;;  # specific catalogue scenario
esac

echo "==> triggering '${TYPE}' x${COUNT} -> ${BASE_URL}${PATH_Q}" >&2
for i in $(seq 1 "${COUNT}"); do
  # Don't fail the script on HTTP 4xx/5xx (the failure codes are the point) or
  # on a reset connection (e.g. /crash exits mid-response).
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "${BASE_URL}${PATH_Q}" || echo "---")
  printf '  [%d/%d] HTTP %s\n' "${i}" "${COUNT}" "${code}" >&2
done
echo "==> done. Watch it land: Grafana 'Logs · Error explorer', or ask the analyzer to diagnose it." >&2
