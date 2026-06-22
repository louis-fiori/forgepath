# Quickstart

End-to-end install of the ForgePath platform on a fresh machine, plus a first scaffolded service.

## Prerequisites

| Tool       | Min version | Notes                                                          |
|------------|-------------|----------------------------------------------------------------|
| Docker     | recent      | Engine running; Docker Desktop is fine                         |
| kind       | 0.20+       | https://kind.sigs.k8s.io/                                      |
| kubectl    | 1.28+       | https://kubernetes.io/docs/tasks/tools/                        |
| Node       | 22+         | Recommended via [nvm](https://github.com/nvm-sh/nvm)           |
| Yarn       | 1.x classic | Bundled with Backstage scaffolder                              |
| GNU make   | 3.81+       | Default on macOS and Linux                                     |
| openssl    | recent      | Used to generate the Grafana admin password                    |

### Platform support

Tested on macOS (Apple Silicon + Intel) and Linux. **On Windows, use WSL2 with Docker Desktop's WSL backend**, all scripts run unchanged inside the WSL shell. Native Windows shells (PowerShell, cmd) are not supported.

## 1. Clone and configure

```bash
git clone https://github.com/louis-fiori/forgepath.git
cd forgepath
cp .env.example .env
```

Open `.env` and fill `GITHUB_TOKEN` with a **fine-grained PAT scoped to your fork**:

- **Contents**: Read and write, scaffolder writes the PR branch
- **Pull requests**: Read and write, open and close PRs
- **Issues**: Read and write, the incident-analyzer files incident issues
- **Metadata**: Read, mandatory, auto-added

If you forked the repo, also update `FORGEPATH_GITHUB_OWNER` / `FORGEPATH_GITHUB_REPO` / `FORGEPATH_TARGET_BRANCH`, then run `make configure` once. This rewrites the `repoURL` in the `gitops/apps/` Application manifests to point at your fork, those files are reconciled straight from Git by ArgoCD and can't honor runtime substitution.

## 2. Scaffold and build Backstage

```bash
make backstage-init    # ~5 min, one-time, runs npx @backstage/create-app + applies the forgepath overlay
make backstage-build   # ~5 min, one-time, yarn build + builds forgepath/backstage:dev Docker image
```

Both are idempotent. Re-run them after pulling new commits that touch `platform/backstage/` to pick up template/catalog changes.

## 3. Bring up the cluster

```bash
make local-up
```

This:

1. Creates the `forgepath` kind cluster (with port mappings to `localhost:7007/8080/3000/8888/8889`)
2. Side-loads the locally built images into the node cache, `forgepath/backstage:dev`, and the `incident-generator:dev` / `incident-analyzer:dev` fixtures (built on first run if missing), so the pods start green instead of `ErrImagePull`
3. Installs ArgoCD via server-side apply
4. Generates a random Grafana admin password (read it back with `make grafana-pw`)
5. Creates the GitHub PAT secrets in the `backstage` and `argocd` namespaces from `$GITHUB_TOKEN`, plus the `incident-analyzer` secrets (LLM creds, S2S token), and mounts your `~/.aws` for the Bedrock path if present
6. Applies the root app-of-apps, from there ArgoCD discovers and deploys Backstage, Prometheus, Loki, Grafana, and the incident-generator / incident-analyzer services

It takes ~3 min for everything to settle. Watch the live state at <http://localhost:8080/applications>.

> The AI Incident Analyzer is optional, without LLM creds in `.env` (`ANTHROPIC_API_KEY`, or AWS creds for Bedrock) it still detects incidents, it just won't ask Claude to diagnose them. See [development.md § Configure the AI Incident Analyzer](development.md#configure-the-ai-incident-analyzer).

## 4. URLs cheat sheet

| Service           | URL                       | Credentials                          |
|-------------------|---------------------------|--------------------------------------|
| Backstage         | http://localhost:7007     | guest auth                           |
| ArgoCD UI         | http://localhost:8080     | admin / `make argocd-pw`             |
| Grafana           | http://localhost:3000     | admin / `make grafana-pw`            |
| Preview demo slot | http://localhost:8888     | n/a (any preview with `exposeOnLocalhost: true`) |
| incident-generator | http://localhost:8889    | n/a (`make incident TYPE=panic` to trigger an incident) |

The Grafana home includes the auto-provisioned dashboards: `Cluster pods`, `Service logs`, and `Logs · Error explorer`.

## 5. Deploy your first service

1. Open <http://localhost:7007/create> in Backstage
2. Click **Deploy a service (via GitHub PR)**
3. Fill the form (any DNS-1123 name, an image like `nginx:1.27-alpine`, replicas, port)
4. Submit

The scaffolder will:

1. Render the K8s manifests + a Backstage Component + a TechDocs runbook
2. Open a PR on your fork with `gitops/workloads/<name>/`
3. Apply the `preview` label
4. Register the Component in the Backstage catalog immediately (it shows up in `/catalog` before ArgoCD has even synced)

Within ~60s the `previews` ApplicationSet picks up the labeled PR and deploys the workload into the `preview-scaffold-<name>` namespace. Open the new Component in the catalog: the **Kubernetes** tab shows the pod, the **Docs** tab renders the runbook, and the links in the sidebar deep-link straight into Grafana with the namespace pre-filtered.

To tear it down: **Create → Destroy a deployed service**, type the same name, submit. The destroy template closes the PR, ArgoCD removes the Application within ~60s, and the namespace is finalized.

## 6. Analyze an incident (optional)

The `incident-generator` runs out of the box and continuously emits errors, so there's always something to detect. To force a specific one on demand, hit its endpoints (exposed at `http://localhost:8889`) via the wrapper:

```bash
make incident TYPE=panic                          # a recovered panic
make incident TYPE=leak ARGS="--mb 32 --count 5"  # push the pod toward an OOMKill
make incident TYPE=list                           # list the known error types
```

Then see the AI Incident Analyzer pick it up:

1. Open <http://localhost:7007/create> → **Analyze an incident (AI Incident Analyzer)**
2. Leave the namespace as `incident-generator`, pick a lookback window, and submit
3. The diagnosis (root cause + remediation, severity, confidence) renders back in the task page; if issue creation is enabled it links to the GitHub issue it filed

This requires LLM creds (see the note in step 3). You can also drive it directly: `kubectl -n incident-analyzer port-forward svc/incident-analyzer 8081:80` (8080 is already taken by the ArgoCD UI), then (the `/analyze*` endpoints require the shared S2S token) `TOKEN=$(kubectl -n backstage get secret backstage-s2s-token -o jsonpath='{.data.token}' | base64 -d)` and `curl -H "Authorization: Bearer $TOKEN" "localhost:8081/analyze?namespace=incident-generator&force=true"`.

## 7. Tear everything down

```bash
make local-down   # deletes the kind cluster
```

For docker-desktop (alternative runtime): `make docker-up` / `make docker-down`.

## Next steps

- [Architecture](architecture.md), components, GitOps flow, observability wiring, namespace strategy
- [Development](development.md), make targets, common customizations, fork setup
- Once Backstage is up, the in-product TechDocs serves the deeper operator guide at <http://localhost:7007/docs/default/component/forgepath-platform>.
