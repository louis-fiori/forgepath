# Deploy & destroy flow

The flow is "PR is the lifecycle handle":

- **Opening a PR** with the `preview` label deploys the service into a
  fresh `preview-scaffold-<name>` namespace.
- **Closing the PR** removes the Application from the ApplicationSet,
  which finalizes the namespace and everything inside.

## Deploy

1. Open Backstage → **Create** → **Deploy a service (via GitHub PR)**.
2. Fill in the form (name, image, replicas, port…) and submit.
3. The `deploy-service-to-kind` template runs four steps:
    1. `fetch:template` renders five files into a workspace directory
       `pr/gitops/workloads/<name>/`:
        - `k8s/deployment.yaml` + `k8s/service.yaml`, the K8s workload
          (kept under `k8s/` so ArgoCD can include them exclusively)
        - `catalog-info.yaml`, the Backstage Component definition
        - `mkdocs.yml` + `docs/index.md`, the TechDocs runbook source
    2. `publish:github:pull-request` opens a PR on `louis-fiori/forgepath`
       targeting `dev`, on branch `scaffold-<name>`, with the rendered
       files committed under `gitops/workloads/<name>/`.
    3. `github:addLabels` (custom action) stamps the `preview` label.
    4. `catalog:register` registers the Component in Backstage using
       the catalog-info.yaml URL on the PR branch. The Component is
       visible in the catalog immediately, before ArgoCD has even
       reconciled.
4. The Backstage scaffolder output gives you the PR URL **and a direct
   link to the new Component page** in Backstage.
5. Within ~60s the **ArgoCD ApplicationSet `previews`** notices the PR,
   instantiates an Application `preview-scaffold-<name>`, and starts syncing the
   manifests from the PR branch. The AppSet's `directory.include: '*/k8s/*.yaml'`
   whitelists the K8s subdirectory exclusively, so `catalog-info.yaml`,
   `mkdocs.yml`, and `docs/**` are ignored by ArgoCD (consumed by Backstage).
6. The pod appears in the `preview-scaffold-<name>` namespace. Because the
   Component's `backstage.io/kubernetes-label-selector` matches
   `app=<name>`, the Kubernetes tab on the catalog entity surfaces the
   pod (with logs) across all namespaces.
7. The Docs tab renders the runbook skeleton from
   `gitops/workloads/<name>/docs/index.md`, edit it directly in the PR
   to fill in the service-specific operational detail.

## Destroy

1. Open Backstage → **Create** → **Destroy a deployed service**.
2. Type the service name. Submit.
3. The `destroy-service-from-kind` template invokes the custom action
   `github:closePullRequest`, which closes the PR with head branch
   `scaffold-<name>` via the GitHub API.
4. The ApplicationSet sees the PR is no longer open → removes the
   Application → the resources finalizer cascade-deletes everything in
   the `preview-scaffold-<name>` namespace.
5. The Backstage Component lingers in the catalog until the
   `scaffold-<name>` branch is deleted on GitHub. Once the branch is
   gone, the registered location 404s on the next catalog refresh and
   the entity is marked orphan (and dropped). When the prod flow is
   wired up, a `main`-targeted GitHub discovery provider will take over
   and persist the Component beyond the PR lifecycle.

## Why this is "GitOps-correct"

- The cluster state on `dev` only ever changes through a git commit
  (the PR's branch).
- Closing a PR doesn't touch the cluster directly, it changes a state
  ArgoCD watches, and ArgoCD does the work.
- Manual `kubectl delete` on a preview namespace would just be re-synced
  by ArgoCD (selfHeal is on for previews, off for platform components).

## What's intentionally *not* in this flow

- **No merge to main.** Today the previews only ever exist as PRs. The
  step "merge to main = promote to prod" is the [next thing on the
  roadmap](ops.md#promoting-to-prod-todo).
- **No CI gating.** PRs are auto-deployed when the label is on. A real
  setup would gate on tests / image scanning before allowing the label.
