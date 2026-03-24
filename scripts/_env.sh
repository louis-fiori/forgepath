# Shared env loader, source-only (no shebang). Used by every script that
# needs the FORGEPATH_* settings. Loads .env at the repo root if present,
# then applies repo-author defaults so a fresh clone still works.
#
# Usage (from another script):
#   REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
#   . "${REPO_ROOT}/scripts/_env.sh"

_FORGEPATH_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Auto-bootstrap .env from .env.example on first run so a fresh clone
# doesn't require the `cp` step. .env is gitignored, editing it is the
# only thing the user has to do (typically just GITHUB_TOKEN).
if [ ! -f "${_FORGEPATH_REPO_ROOT}/.env" ] && [ -f "${_FORGEPATH_REPO_ROOT}/.env.example" ]; then
  cp "${_FORGEPATH_REPO_ROOT}/.env.example" "${_FORGEPATH_REPO_ROOT}/.env"
  echo "==> created .env from .env.example, edit it to set GITHUB_TOKEN" >&2
fi

if [ -f "${_FORGEPATH_REPO_ROOT}/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "${_FORGEPATH_REPO_ROOT}/.env"
  set +a
fi

# Defaults match the repo's canonical fork (louis-fiori/forgepath@dev) so
# the author can run everything without a .env. Forkers create their own
# .env and these defaults are overridden.
: "${FORGEPATH_GITHUB_OWNER:=louis-fiori}"
: "${FORGEPATH_GITHUB_REPO:=forgepath}"
: "${FORGEPATH_TARGET_BRANCH:=dev}"
# GITHUB_TOKEN has no default, it's a secret and the project should fail
# loudly if a step needs it and it's missing rather than silently fall back.
: "${GITHUB_TOKEN:=}"
export FORGEPATH_GITHUB_OWNER FORGEPATH_GITHUB_REPO FORGEPATH_TARGET_BRANCH GITHUB_TOKEN

# Renders FORGEPATH_* placeholders in a file to stdout. Leaves Nunjucks
# `${{ ... }}` (double-brace) untouched, sed only matches the single-brace
# `${VAR}` form used here.
forgepath_render() {
  sed \
    -e "s|\${FORGEPATH_GITHUB_OWNER}|${FORGEPATH_GITHUB_OWNER}|g" \
    -e "s|\${FORGEPATH_GITHUB_REPO}|${FORGEPATH_GITHUB_REPO}|g" \
    -e "s|\${FORGEPATH_TARGET_BRANCH}|${FORGEPATH_TARGET_BRANCH}|g" \
    "$1"
}

unset _FORGEPATH_REPO_ROOT
