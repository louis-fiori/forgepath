# Development

How to work in the repo: make targets, common customizations, fork setup.

## Make targets

`make help` lists everything. The interesting ones:

### Cluster lifecycle

| Target              | What it does                                            |
|---------------------|---------------------------------------------------------|
| `make local-up`     | Create the `forgepath` kind cluster + install platform  |
| `make local-down`   | Delete the kind cluster                                 |
| `make docker-up`    | Install platform on Docker Desktop's Kubernetes         |
| `make docker-down`  | Remove platform namespaces from docker-desktop          |

### Backstage build & sync

| Target                      | What it does                                                                  |
|-----------------------------|-------------------------------------------------------------------------------|
| `make backstage-init`       | One-time: scaffold `local/backstage/` with `npx create-app` (version pinned by `FORGEPATH_CREATE_APP_VERSION` in `.env`) + apply overlay |
| `make backstage-sync`       | Re-apply `platform/backstage/` sources on top of the scaffolded app; warns if the scaffold drifted from the overlay's upstream baseline |
| `make backstage-baseline`   | Accept the current scaffold as the new overlay baseline (after a `create-app` version bump, see `platform/backstage/overlay/README.md`) |
| `make backstage-install`    | `yarn install` inside `local/backstage/`                                      |
| `make backstage-build`      | Build `forgepath/backstage:dev` Docker image                                  |
| `make backstage-reload`     | Rebuild + `kind load` + `rollout restart deploy/backstage`                    |
| `make backstage-reload-docker` | Same, but on the docker-desktop context                                     |

### Credentials

| Target            | What it does                                                            |
|-------------------|-------------------------------------------------------------------------|
| `make argocd-pw`  | Print the bootstrap ArgoCD admin password                               |
| `make grafana-pw` | Print the Grafana admin password (randomized on first install)          |

### Workloads (incident services)

The `incident-generator` and `incident-analyzer` images are built on the dev host and **side-loaded into kind** (never pushed to a registry); `make local-up` builds them on first run if missing. Rebuild explicitly with:

| Target                        | What it does                                                                 |
|-------------------------------|------------------------------------------------------------------------------|
| `make incident TYPE=panic`      | Trigger an incident on the running generator (`TYPE`=panic/boom/crash/leak/slow/`<error-type>`, `ARGS="--count N"`) |
| `make incident-generator-build` | Build `incident-generator:dev`                                             |
| `make incident-generator-load`  | Build + `kind load` + `rollout restart deploy/incident-generator`         |
| `make incident-analyzer-build`  | Build `incident-analyzer:dev`                                              |
| `make incident-analyzer-load`   | Build + `kind load` + `rollout restart deploy/incident-analyzer`          |
| `make incident-analyzer-secrets`| Re-sync the analyzer's secrets from `.env` + `~/.aws` and restart the pod |

ArgoCD reconciles both Deployments from `gitops/platform/incident-{generator,analyzer}/` into namespaces of the same name, the `-load` targets just refresh the image in the node cache.

## Common customizations

### Add a Grafana dashboard

1. Drop a new `*.json` file under `gitops/platform/grafana/dashboards/`
2. Reference it in `gitops/platform/grafana/kustomization.yaml` under `configMapGenerator.files`
3. Commit + push

Kustomize regenerates the ConfigMap with a new hash suffix → ArgoCD rolls Grafana → the dashboard appears under the **Forgepath** folder. The Loki and Prometheus datasources are already wired (UIDs `loki` and `prometheus`).

### Add a catalog entry

1. Drop a YAML file under `platform/backstage/catalog/`
2. Reference it in `platform/backstage/overlay/files/app-config.local.yaml` under `catalog.locations`
3. `make backstage-reload`

The overlay app-config replaces the scaffold's default `catalog.locations` array entirely, entries don't merge across config files, so list every location you want surfaced.

### Add a scaffolder template

1. Create `platform/backstage/templates/<name>/template.yaml` (+ `content/` if it produces files)
2. Add a `file` location for `./examples/templates/<name>/template.yaml` in `app-config.local.yaml` with `rules: [allow: [Template]]`
3. `make backstage-reload`

The sync script renders `${FORGEPATH_*}` placeholders in `template.yaml` (single-brace form), Nunjucks `${{ ... }}` (double-brace) is left untouched. Use single-brace for fork-specific values, double-brace for scaffolder-time variables.

### Tweak Loki retention or limits

Edit `gitops/platform/loki/loki-configmap.yaml` (`limits_config.retention_period`, `ingestion_rate_mb`, etc.), commit, push. ArgoCD reapplies and the Loki Deployment uses `Recreate` strategy so the new config takes effect cleanly.

The default storage is `emptyDir`, restarting the Loki pod drops everything. Swap for a PVC in `loki-deployment.yaml` if you want logs to survive pod restarts.

### Configure the AI Incident Analyzer

The analyzer reads its LLM creds (and `GITHUB_TOKEN` / `BACKSTAGE_S2S_TOKEN`) from a Secret materialized by `make local-up` out of `.env` + `~/.aws`:

- **Bedrock (default)**: set `LLM_PROVIDER=bedrock` and either a static key (`AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`) or `AWS_PROFILE`, `local-up` mounts your `~/.aws` into the pod.
- **Direct Anthropic API**: set `LLM_PROVIDER=anthropic` and `ANTHROPIC_API_KEY`.

After editing `.env`, run `make incident-analyzer-secrets` to re-sync the Secret and restart the pod, no full `local-up`. Without resolvable LLM creds the analyzer still detects, it just won't diagnose. The operational knobs (`WATCH_NAMESPACES`, `ERROR_THRESHOLD`, `POLL_INTERVAL_SECONDS`, `MASKING_ENABLED`, the GitHub issue target, …) are also plain `.env` settings, the full variable reference lives in [`services/incident-analyzer/README.md`](../services/incident-analyzer/README.md).

### Use a fork

1. Fork the repo on GitHub
2. Edit `.env`:
   - `FORGEPATH_GITHUB_OWNER` → your GitHub login
   - `FORGEPATH_GITHUB_REPO`  → your fork's name
   - `FORGEPATH_TARGET_BRANCH` → the branch ArgoCD tracks (default `dev`)
3. `make local-up`

Everything that needs your fork's owner/repo/branch (`platform/argocd/bootstrap/*.yaml` ApplicationSets, scaffolder `template.yaml`) keeps `${FORGEPATH_*}` placeholders in Git and resolves them at runtime from `.env`. There's no rewrite-and-commit step.

## Troubleshooting

### A preview never appears

Most stuck previews fall into one of three buckets:

- **PR not labeled `preview`** → ApplicationSet skips it. Add the label on GitHub, wait 60s.
- **PAT expired** → ApplicationSet logs `401 Unauthorized`. Rotate the secret (see TechDocs § "Rotate the GitHub PAT").
- **Manifests reference a missing namespace** → ArgoCD reports `SyncFailed`. The `previews` ApplicationSet sets `CreateNamespace=true`, so this only happens if a manifest in the PR hardcodes a namespace.

Force a refresh:

```bash
kubectl --context kind-forgepath -n argocd annotate app preview-scaffold-<name> \
  argocd.argoproj.io/refresh=hard --overwrite
```

### Grafana dashboards don't update after a commit

The ConfigMap is hash-suffixed by kustomize, so changes always trigger a pod restart. If they don't, check that the new file is listed in `gitops/platform/grafana/kustomization.yaml` under `configMapGenerator.files`.

### Backstage doesn't see my template change

`make backstage-reload` rebuilds the Backstage image with the latest `platform/backstage/` sources baked in. The running pod loads templates from the image, not from Git.

For more detail post-install, the Backstage TechDocs site at <http://localhost:7007/docs/default/component/forgepath-platform> has runbooks for rotating credentials, debugging stuck previews, and upgrading ArgoCD.
