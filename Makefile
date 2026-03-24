.PHONY: help local-up local-down docker-up docker-down argocd-pw grafana-pw backstage-init backstage-sync backstage-install backstage-baseline backstage-build backstage-reload backstage-reload-docker configure incident incident-generator-build incident-generator-load incident-analyzer-build incident-analyzer-load incident-analyzer-secrets _ensure-workload-images

.DEFAULT_GOAL := help

# NOTE: we deliberately do NOT `include .env` here. make and bash have
# different grammars, and `include` parses .env as a makefile, so a value with
# spaces/quotes or an inline `#` would silently corrupt *every* target. The real
# loader is scripts/_env.sh, sourced by every script these recipes call (it
# reads .env, applies the same defaults below, and exports them). The defaults
# here only cover make-level use (help text, command-line overrides like
# `make configure FORGEPATH_GITHUB_OWNER=alice`); .env never reaches make.
FORGEPATH_GITHUB_OWNER ?= louis-fiori
FORGEPATH_GITHUB_REPO ?= forgepath
FORGEPATH_TARGET_BRANCH ?= dev
export FORGEPATH_GITHUB_OWNER FORGEPATH_GITHUB_REPO FORGEPATH_TARGET_BRANCH

# All recipes pipe through bash (some use `. <file>` to source nvm and rely
# on $HOME/$NVM_DIR). Default /bin/sh on Debian/Ubuntu is dash, which trips
# on the brace-grouped one-liners below.
SHELL := bash

# Portable nvm loader: tries $NVM_DIR, then the common install locations on
# Linux/WSL (~/.nvm), Apple Silicon (/opt/homebrew/opt/nvm) and Intel macOS
# (/usr/local/opt/nvm). No-op if nvm isn't installed, recipes still work
# as long as a Node 22+ `node`/`yarn` is already on PATH.
define LOAD_NVM
{ \
  if [ -n "$$NVM_DIR" ] && [ -s "$$NVM_DIR/nvm.sh" ]; then . "$$NVM_DIR/nvm.sh"; \
  elif [ -s "$$HOME/.nvm/nvm.sh" ]; then . "$$HOME/.nvm/nvm.sh"; \
  elif [ -s /opt/homebrew/opt/nvm/nvm.sh ]; then . /opt/homebrew/opt/nvm/nvm.sh; \
  elif [ -s /usr/local/opt/nvm/nvm.sh ]; then . /usr/local/opt/nvm/nvm.sh; \
  fi; \
  command -v nvm >/dev/null 2>&1 && nvm use 22 >/dev/null 2>&1 || true; \
}
endef

help: ## Show this help message
	@awk 'BEGIN {FS = ":.*##"; printf "\nUsage: make \033[36m<target>\033[0m\n\nTargets:\n"} \
	  /^[a-zA-Z_-]+:.*?##/ { printf "  \033[36m%-24s\033[0m %s\n", $$1, $$2 } \
	  /^##@/ { printf "\n\033[1m%s\033[0m\n", substr($$0, 5) }' $(MAKEFILE_LIST)

##@ Configuration

configure: ## Rebrand gitops/apps/*.yaml from .env (idempotent)
	./scripts/configure.sh

##@ Cluster lifecycle

local-up: _ensure-image _ensure-workload-images ## Bring up the kind cluster and deploy the platform
	./scripts/local-up.sh kind

local-down: ## Tear down the kind cluster
	./scripts/local-down.sh kind

docker-up: _ensure-image _ensure-workload-images ## Deploy the platform on docker-desktop
	./scripts/local-up.sh docker-desktop

docker-down: ## Tear down the docker-desktop deployment
	./scripts/local-down.sh docker-desktop

##@ Credentials

argocd-pw: ## Print the ArgoCD bootstrap admin password
	@kubectl --context kind-forgepath -n argocd get secret argocd-initial-admin-secret \
	  -o jsonpath='{.data.password}' | base64 -d; echo

grafana-pw: ## Print the Grafana admin password
	@kubectl --context kind-forgepath -n grafana get secret grafana-admin \
	  -o jsonpath='{.data.admin-password}' | base64 -d; echo

##@ Backstage

backstage-init: ## Scaffold local/backstage/ and apply the forgepath overlay
	@$(LOAD_NVM); \
	./scripts/backstage-init.sh

backstage-sync: _ensure-scaffold ## Sync platform/ sources into the scaffolded app
	./scripts/backstage-sync.sh

backstage-install: ## Run yarn install in local/backstage/
	@$(LOAD_NVM); \
	cd local/backstage && yarn install

backstage-baseline: ## Accept the scaffold's pristine files as the new overlay baseline (after a create-app bump)
	@test -d local/backstage/.scaffold-pristine || \
	  { echo "no local/backstage/.scaffold-pristine/, re-scaffold first (rm -rf local/backstage && make backstage-init)"; exit 1; }
	rm -rf platform/backstage/overlay/upstream
	cp -R local/backstage/.scaffold-pristine platform/backstage/overlay/upstream
	@echo "Baseline refreshed, review with \`git diff platform/backstage/overlay/upstream\` and commit."

backstage-build: backstage-sync ## Build the Backstage Docker image (forgepath/backstage:dev)
	@$(LOAD_NVM); \
	cd local/backstage && yarn tsc && yarn build:all && yarn build-image --tag forgepath/backstage:dev

backstage-reload: backstage-build ## Rebuild + load into kind + restart deployment
	kind load docker-image forgepath/backstage:dev --name forgepath
	kubectl --context kind-forgepath -n backstage rollout restart deploy/backstage

backstage-reload-docker: backstage-build ## Rebuild + restart deployment on docker-desktop
	kubectl --context docker-desktop -n backstage rollout restart deploy/backstage

##@ Workloads

incident: ## Trigger an incident on the generator (TYPE=panic|boom|crash|leak|slow|<error-type>, ARGS="--count 30")
	./scripts/trigger-incident.sh $(or $(TYPE),boom) $(ARGS)

incident-generator-build: ## Build the incident-generator Docker image (incident-generator:dev)
	docker build -t incident-generator:dev services/incident-generator

incident-generator-load: incident-generator-build ## Build + side-load incident-generator:dev into kind
	kind load docker-image incident-generator:dev --name forgepath
	kubectl --context kind-forgepath -n incident-generator rollout restart deploy/incident-generator 2>/dev/null || true
	@echo "==> incident-generator:dev loaded. ArgoCD deploys it from gitops/platform/incident-generator/ (namespace incident-generator)."

incident-analyzer-build: ## Build the incident-analyzer Docker image (incident-analyzer:dev)
	docker build -t incident-analyzer:dev services/incident-analyzer

incident-analyzer-load: incident-analyzer-build ## Build + side-load incident-analyzer:dev into kind
	kind load docker-image incident-analyzer:dev --name forgepath
	kubectl --context kind-forgepath -n incident-analyzer rollout restart deploy/incident-analyzer 2>/dev/null || true
	@echo "==> incident-analyzer:dev loaded. ArgoCD deploys it from gitops/platform/incident-analyzer/ (namespace incident-analyzer)."

incident-analyzer-secrets: ## Re-sync incident-analyzer secrets from .env + ~/.aws and restart the pod
	./scripts/incident-analyzer-secrets.sh

# Ensures local/backstage/ has been scaffolded and the overlay applied.
_ensure-scaffold:
	@if [ ! -d local/backstage/packages ]; then \
	  echo "==> local/backstage/ not scaffolded yet, running backstage-init"; \
	  $(MAKE) backstage-init; \
	fi

_ensure-image: _ensure-scaffold
	@if ! docker image inspect forgepath/backstage:dev >/dev/null 2>&1; then \
	  echo "==> Backstage image not found, building (~5-10 min)"; \
	  $(MAKE) backstage-build; \
	fi

# Builds the incident-generator / incident-analyzer fixture images only if they
# aren't already in the local Docker cache (mirrors _ensure-image). local-up.sh
# then side-loads them into kind *before* ArgoCD deploys, so the pods start
# green instead of ErrImagePull. Rebuild explicitly with the *-load targets.
_ensure-workload-images:
	@if ! docker image inspect incident-generator:dev >/dev/null 2>&1; then \
	  echo "==> incident-generator:dev not found, building"; \
	  $(MAKE) incident-generator-build; \
	fi
	@if ! docker image inspect incident-analyzer:dev >/dev/null 2>&1; then \
	  echo "==> incident-analyzer:dev not found, building"; \
	  $(MAKE) incident-analyzer-build; \
	fi
