#!/usr/bin/env bash
# In-place rebrand for files that ArgoCD reads straight from git (and
# therefore can't honor runtime ${...} substitution).
#
# Reads .env (via scripts/_env.sh), computes the current owner/repo/branch
# from one of the gitops files, and sed-replaces only when something
# actually changes. Safe to re-run.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=./_env.sh
. "${REPO_ROOT}/scripts/_env.sh"

GITOPS_APPS="${REPO_ROOT}/gitops/apps"

# Files this script rewrites: every Application manifest under gitops/apps/.
# Each carries the canonical `repoURL: https://github.com/<owner>/<repo>.git`
# (or a close variant) and is reconciled-from-git by ArgoCD, no runtime
# substitution possible. We glob rather than hard-code the list so a newly
# added app is rebranded automatically instead of silently deploying the
# canonical repo on a fork. The sed patterns below match only the *current*
# literal values, so sweeping every file is safe.
shopt -s nullglob
TARGETS=("${GITOPS_APPS}"/*.yaml)
shopt -u nullglob

if [ ${#TARGETS[@]} -eq 0 ]; then
  echo "ERROR: no Application manifests found under ${GITOPS_APPS}" >&2
  exit 1
fi

# Discover the *currently committed* values so we know what to replace.
# Use backstage.yaml as the source of truth, it has a static targetRevision
# (the AppSet uses '{{branch}}' which we must not touch).
SAMPLE="${GITOPS_APPS}/backstage.yaml"
if [ ! -f "${SAMPLE}" ]; then
  echo "ERROR: ${SAMPLE} not found" >&2
  exit 1
fi

current_repo_url="$(awk '/^[[:space:]]*repoURL:/ {print $2; exit}' "${SAMPLE}")"
current_owner="$(printf '%s\n' "${current_repo_url}" | sed -E 's|^https://github.com/([^/]+)/.*$|\1|')"
current_repo="$(printf '%s\n' "${current_repo_url}" | sed -E 's|^https://github.com/[^/]+/([^.]+)\.git$|\1|')"
current_branch="$(awk '/^[[:space:]]*targetRevision:/ {print $2; exit}' "${SAMPLE}")"

want_owner="${FORGEPATH_GITHUB_OWNER}"
want_repo="${FORGEPATH_GITHUB_REPO}"
want_branch="${FORGEPATH_TARGET_BRANCH}"

if [ "${current_owner}" = "${want_owner}" ] \
   && [ "${current_repo}" = "${want_repo}" ] \
   && [ "${current_branch}" = "${want_branch}" ]; then
  echo "==> gitops already configured for ${want_owner}/${want_repo}@${want_branch}, nothing to do."
  exit 0
fi

echo "==> rebranding gitops files:"
echo "    from: ${current_owner}/${current_repo}@${current_branch}"
echo "    to:   ${want_owner}/${want_repo}@${want_branch}"

# Detect BSD (macOS) vs GNU sed, the in-place flag differs.
if sed --version >/dev/null 2>&1; then SED_INPLACE=(-i); else SED_INPLACE=(-i ''); fi

# Each pattern matches the *current* literal value (not a wildcard), so we
# can never accidentally clobber the ApplicationSet's '{{branch}}' template
# variable or any other unrelated string.
for f in "${TARGETS[@]}"; do
  if [ ! -f "${f}" ]; then
    echo "    skip (missing): $(basename "${f}")"
    continue
  fi
  sed "${SED_INPLACE[@]}" \
    -e "s|https://github.com/${current_owner}/${current_repo}\\.git|https://github.com/${want_owner}/${want_repo}.git|g" \
    -e "s|https://github.com/${current_owner}/${current_repo}\\b|https://github.com/${want_owner}/${want_repo}|g" \
    -e "s|^\\([[:space:]]*\\)owner: ${current_owner}\$|\\1owner: ${want_owner}|" \
    -e "s|^\\([[:space:]]*\\)repo: ${current_repo}\$|\\1repo: ${want_repo}|" \
    -e "s|^\\([[:space:]]*\\)username: ${current_owner}\$|\\1username: ${want_owner}|" \
    -e "s|^\\([[:space:]]*\\)targetRevision: ${current_branch}\$|\\1targetRevision: ${want_branch}|" \
    "${f}"
  echo "    updated: ${f#${REPO_ROOT}/}"
done

echo
echo "==> done. Review with:  git diff -- gitops/apps/"
echo "==> commit and push so ArgoCD reconciles from your fork."
