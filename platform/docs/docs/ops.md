# Operations

## Bootstrap from scratch

Pre-requirements: Docker, kind, kubectl, Node 22 (via nvm), Yarn, GNU make.

Tested on macOS (Apple Silicon + Intel) and Linux. On Windows, use WSL2 (Ubuntu)
with Docker Desktop's WSL backend, every script runs unchanged inside the
WSL shell. Native Windows shells (PowerShell, cmd) are not supported.

```bash
# 1. Copy .env.example and fill in your fine-grained PAT
cp .env.example .env
# Edit .env, set GITHUB_TOKEN (and FORGEPATH_GITHUB_OWNER/REPO/BRANCH if forked)

# 2. Scaffold + build the Backstage image (~5-10 min the first time)
make backstage-init
make backstage-build

# 3. Bring up the cluster + apply everything
make local-up
```

`scripts/local-up.sh` reads `$GITHUB_TOKEN` (and the rest of `.env`) and
generates the in-cluster Secrets from it, no `.local.yaml` files to maintain:

| Secret | Namespace | Carries |
|---|---|---|
| `grafana-admin` | `grafana` | random admin password (first run only) |
| `backstage-github-token` | `backstage` | the PAT, for the Backstage backend |
| `backstage-s2s-token` | `backstage` | shared service-to-service token |
| `incident-analyzer-secrets` | `incident-analyzer` | analyzer config + the PAT (to file issues) + the s2s token |
| `incident-analyzer-aws` | `incident-analyzer` | your `~/.aws`, when present (Bedrock profile) |
| `forgepath-repo-creds` | `argocd` | the PAT, for ArgoCD + the pullRequest generator |

After `make local-up`:

| Service        | URL                                | Credentials              |
|----------------|------------------------------------|--------------------------|
| Backstage      | http://localhost:7007              | guest auth               |
| ArgoCD UI      | http://localhost:8080              | admin / `make argocd-pw` |
| Grafana        | http://localhost:3000              | admin / `make grafana-pw` |
| Preview demo slot | http://localhost:8888           | n/a (any preview service deployed with `exposeOnLocalhost: true`) |
| incident-generator | http://localhost:8889           | n/a (`make incident TYPE=panic` to trigger an incident) |

The `incident-analyzer` is `ClusterIP`-only (no localhost port); reach its
on-demand API via `kubectl -n incident-analyzer port-forward svc/incident-analyzer 8081:80`
(any free local port) or the "Analyze an incident" Backstage template.

## Update the Backstage image

```bash
make backstage-reload  # rebuilds + reloads + restarts the deployment
```

The sync script always picks up the latest sources from `platform/`. Catalog
edits, scaffolder template tweaks, and overlay file changes flow in.

## Rotate the GitHub PAT

The PAT lives in `.env` (gitignored) and is materialized into the in-cluster
Secrets at boot. It's consumed in **three** places: Backstage
(`backstage-github-token`), ArgoCD (`forgepath-repo-creds`) and the
incident-analyzer (`incident-analyzer-secrets`, used to file issues), miss the
last one and the analyzer keeps trying to open issues with the dead token.

Easiest rotation: update `GITHUB_TOKEN` in `.env`, then re-run `make local-up`
(idempotent, it regenerates every Secret). For a hot rotation without
re-running the full bootstrap:

```bash
# Update GITHUB_TOKEN in .env, then re-apply the Backstage + ArgoCD Secrets.
# stdin heredocs (not --from-literal) keep the token out of the process argv.
source .env
kubectl --context kind-forgepath apply -f - <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: backstage-github-token
  namespace: backstage
stringData:
  token: "${GITHUB_TOKEN}"
EOF
kubectl --context kind-forgepath apply -f - <<EOF
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
  password: "${GITHUB_TOKEN}"
EOF

# Re-sync the analyzer's copy of the PAT (it files issues) and roll its pod:
make incident-analyzer-secrets

# Roll the Backstage pod so it picks up the new GITHUB_TOKEN:
kubectl --context kind-forgepath -n backstage rollout restart deploy/backstage
```

ArgoCD picks up the `repo-creds` Secret automatically, no restart needed.

## Upgrade ArgoCD

Edit the pinned tag in `platform/argocd/install/kustomization.yaml`,
commit, push. The next `make local-up` (or a manual
`kubectl apply -k platform/argocd/install --server-side --force-conflicts`)
brings the new version in.

Note: cross-major-version upgrades (e.g. v3 → v4) may require RBAC or CRD
migration steps, read the ArgoCD release notes first.

## Debug a stuck preview

```bash
# What's the ApplicationSet seeing?
kubectl --context kind-forgepath -n argocd logs \
  deploy/argocd-applicationset-controller --tail=50 | grep previews

# What's the Application status?
kubectl --context kind-forgepath -n argocd get applications

# Force a refresh
kubectl --context kind-forgepath -n argocd annotate app preview-scaffold-hello-service \
  argocd.argoproj.io/refresh=hard --overwrite
```

Most stuck previews fall into one of three buckets:

- **PR not labeled `preview`** → ApplicationSet skips it. Add the label
  on GitHub, wait 60s.
- **PAT expired** → ApplicationSet logs `401 Unauthorized`. Rotate per
  the section above.
- **Manifests reference a missing namespace** → ArgoCD reports
  `SyncFailed`. The `previews` ApplicationSet sets
  `CreateNamespace=true`, so this only happens if a manifest in the PR
  hardcodes a different namespace than `preview-scaffold-<name>`.

## Promoting to prod (TODO)

The current setup only supports preview environments. Two future paths
to a "merge to main = prod" experience:

- **Same cluster, namespace `prod-*`**, add a second ApplicationSet
  (`directory` generator on `main`) that deploys to `prod-<service>`
  namespaces. Simple, no real isolation.
- **Second kind cluster `forgepath-prod`**, register it in ArgoCD as a
  remote cluster, target it from the prod ApplicationSet. Closer to the
  real shape; doubles local resource usage.

See `platform/argocd/bootstrap/previews-appset.yaml` for the pattern to copy.
