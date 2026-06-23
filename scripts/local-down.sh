#!/usr/bin/env bash
set -euo pipefail

RUNTIME="${1:-kind}"
CLUSTER_NAME="forgepath"

case "${RUNTIME}" in
  kind)
    if kind get clusters | grep -qx "${CLUSTER_NAME}"; then
      echo "==> deleting kind cluster '${CLUSTER_NAME}'"
      kind delete cluster --name "${CLUSTER_NAME}"
    else
      echo "kind cluster '${CLUSTER_NAME}' not found, nothing to do"
    fi
    ;;
  docker-desktop)
    echo "==> deleting platform namespaces from docker-desktop cluster"
    # Mirror every namespace local-up.sh / ArgoCD create (argocd and the two
    # incident namespaces were previously left behind).
    kubectl --context docker-desktop delete namespace \
      argocd backstage grafana loki prometheus incident-generator incident-analyzer \
      --ignore-not-found
    # Cluster-scoped RBAC survives namespace deletion. incident-analyzer-reader
    # (analyzer's cross-namespace pod/event reads) was being orphaned.
    kubectl --context docker-desktop delete clusterrolebinding \
      backstage-kubernetes-reader prometheus promtail incident-analyzer-reader --ignore-not-found
    kubectl --context docker-desktop delete clusterrole \
      backstage-kubernetes-reader prometheus promtail incident-analyzer-reader --ignore-not-found
    ;;
  *)
    echo "Unknown runtime: '${RUNTIME}' (expected: kind | docker-desktop)" >&2
    exit 1
    ;;
esac
