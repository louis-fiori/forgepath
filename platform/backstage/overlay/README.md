# Backstage overlay (`platform/backstage/overlay/`)

The "platform delta" applied on top of a fresh `@backstage/create-app` scaffold.

`local/backstage/` is gitignored, anyone cloning this repo runs
`make backstage-init`, which:

1. Scaffolds a fresh Backstage app into `local/backstage/` if missing
   (`npx @backstage/create-app`, version pinned by `FORGEPATH_CREATE_APP_VERSION`
   in `.env`, which `scripts/backstage-init.sh` reads)
2. Layers everything under `files/` over the scaffolded tree
3. Injects the platform catalog from `platform/backstage/catalog/`
   into `local/backstage/examples/catalog/`
4. Injects the platform templates from `platform/backstage/templates/`
   into `local/backstage/examples/templates/`
5. Merges `package-additions.json` into `local/backstage/packages/backend/package.json`
6. Runs `yarn install`

Re-running `make backstage-init` is idempotent. Catalog and templates are
re-synced from the platform tree on every run, so editing
`platform/backstage/{catalog,templates}/` is the right way to add new
products, never edit inside `local/backstage/examples/{catalog,templates}/`,
your changes will be wiped on the next init.

## Directory map

| Path | Purpose |
|---|---|
| `gitops/platform/backstage/` | k8s manifests for Backstage itself (Namespace, Deployment, Service, RBAC). Reconciled by ArgoCD from the `gitops/apps/backstage.yaml` Application. |
| `platform/backstage/overlay/files/` | Files copied verbatim over the scaffolded tree. See "What's in `files/`" below. |
| `platform/backstage/overlay/upstream/` | Committed baseline: the pristine scaffold version of every file `files/` replaces. `backstage-sync.sh` diffs it against the fresh scaffold to detect upstream drift. See "Keeping up with upstream" below. |
| `platform/backstage/overlay/package-additions.json` | Dependencies merged into the scaffolded backend `package.json`. |
| `platform/backstage/catalog/` | Catalog entities (Component, Resource, ...) loaded by Backstage via locations in `app-config.local.yaml`. Source of truth for the platform's catalog. |
| `platform/backstage/templates/` | Scaffolder templates (one directory per template). Source of truth for the platform's deployable products. |

## What's in `files/`

| Path | Why |
|---|---|
| `.dockerignore` | Overrides the scaffold's `.dockerignore` to allow `app-config.local.yaml` into the docker image (the scaffold ignores `*.local.yaml` by default). |
| `app-config.local.yaml` | Additional Backstage config, loaded *after* `app-config.yaml` (via `--config` flags in `gitops/platform/backstage/deployment.yaml`). Overrides locations to point at the injected catalog/templates, configures the Kubernetes plugin against the in-cluster API, disables the broken standalone `/kubernetes` page. |
| `packages/app/src/App.tsx` | Replaces the scaffolded frontend app, adds the TechDocs plugin to the feature list so the entity docs tab renders. |
| `packages/backend/src/index.ts` | Full replacement of the scaffolded backend index, registers our four custom scaffolder actions and the `GITHUB_TOKEN` env-var wiring. |
| `packages/backend/src/modules/scaffolderActionGithubClosePullRequest.ts` | Custom action `github:closePullRequest`. Closes the open PR matching a given head branch. Used by the `destroy-service-from-kind` template to tear down preview environments. Reads `GITHUB_TOKEN` from env. |
| `packages/backend/src/modules/scaffolderActionGithubAddLabels.ts` | Custom action `github:addLabels`. Post-step companion to `publish:github:pull-request` (which has no labels input). Used to stamp the `preview` label on PRs opened by the deploy template. |
| `packages/backend/src/modules/scaffolderActionIncidentAnalyzerAnalyze.ts` | Custom action `incident-analyzer:analyze`. Calls the incident-analyzer's `/analyze` endpoint and returns the structured diagnosis. Used by the `analyze-incident` template to run an on-demand AI analysis from a Backstage form. Targets the in-cluster service URL by default; override with an `INCIDENT_ANALYZER_URL` env var on the Backstage Deployment. |
| `packages/backend/src/modules/scaffolderActionIncidentAnalyzerAnalyzeLog.ts` | Custom action `incident-analyzer:analyzeLog`. Calls the incident-analyzer's `/analyze-log` endpoint to analyze a single log line (pasted raw or fetched from Loki by a line-contains filter) and returns the diagnosis. Used by the `analyze-log` template. Same `INCIDENT_ANALYZER_URL` override as above. |
| `packages/backend/Dockerfile` | Replaces the scaffold's Dockerfile to install `mkdocs` + `mkdocs-techdocs-core` (Python) so the TechDocs backend can build docs in-process with `techdocs.generator.runIn: 'local'`. |

## What's in `package-additions.json`

One runtime dep required by the custom scaffolder actions:

- `@octokit/rest`, talks to the GitHub API (github:closePullRequest, github:addLabels)

Merged (not replaced) into the scaffolded `package.json`, so vendor upgrades
to other deps survive a re-init.

## Adding a new platform product

1. Drop a catalog entity in `platform/backstage/catalog/<name>.yaml`. Reference
   it from a location in `app-config.local.yaml` (path becomes
   `./examples/catalog/<name>.yaml` at runtime).
2. (Optional) Drop a scaffolder template in
   `platform/backstage/templates/<name>/template.yaml` (+ `content/`).
   Reference it from a `Template`-allowed location in
   `app-config.local.yaml`.
3. Run `make backstage-reload` to sync the platform sources and bake them
   into a refreshed docker image.

## Trade-offs of this approach

**Some overlay files are full replacements** (`backend/src/index.ts`,
`app/src/App.tsx`, `backend/Dockerfile`, `.dockerignore`). If Backstage
upstream changes one of them in a future scaffold, the overlay copy keeps
shipping the old content. The files are small and the deltas purely
additive, so merging is easy, the machinery below makes the situation
*detectable* instead of silent.

**`app-config.local.yaml` overrides `catalog.locations` entirely.** YAML
arrays don't merge across `--config` files in Backstage, the later one
wins. If the scaffold's location list changes upstream, you'd need to
copy that change into `app-config.local.yaml` by hand. (This file is an
addition, not a replacement, the scaffold doesn't generate it.)

## Keeping up with upstream

Three pieces keep scaffold drift visible:

1. **The `create-app` version is pinned** by `FORGEPATH_CREATE_APP_VERSION` in
   `.env` (read by `scripts/backstage-init.sh`, which falls back to a baked-in
   default if it's unset), so every clone scaffolds the exact generation the
   overlay was authored against. Current baseline:
   `@backstage/create-app@0.8.3` → Backstage **1.51.0**.
2. **`overlay/upstream/`** holds the committed, pristine scaffold version
   of every file that `overlay/files/` replaces.
3. **A fresh `make backstage-init`** snapshots the scaffold's originals
   into `local/backstage/.scaffold-pristine/` before the overlay lands;
   every `backstage-sync.sh` run (so every `make backstage-build`) then
   compares that snapshot against `overlay/upstream/` and prints a loud
   warning listing any file upstream has changed.

### Upgrading the scaffold

1. Bump `FORGEPATH_CREATE_APP_VERSION` in `.env` (and in `.env.example` for the
   committed baseline).
2. `rm -rf local/backstage && make backstage-init`, the sync step will
   flag every overlay-managed file the new scaffold changed.
3. For each flagged file, see what upstream changed
   (`diff -ru platform/backstage/overlay/upstream local/backstage/.scaffold-pristine`)
   and port it into `overlay/files/`.
4. `make backstage-baseline` to accept the new scaffold as the baseline,
   update the version above, review `git diff`, and commit.
