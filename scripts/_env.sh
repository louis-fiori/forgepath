# Shared env loader, source-only (no shebang). Loads repo-root .env if present,
# then applies repo-author defaults so a fresh clone still works.
#
# Usage (from another script):
#   REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
#   . "${REPO_ROOT}/scripts/_env.sh"

_FORGEPATH_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Auto-bootstrap .env from .env.example on first run (saves the `cp` step).
# .env is gitignored; editing it (typically just GITHUB_TOKEN) is all the user does.
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

# Defaults match the canonical fork (louis-fiori/forgepath@dev) so the author
# runs without a .env; forkers' own .env overrides them.
: "${FORGEPATH_GITHUB_OWNER:=louis-fiori}"
: "${FORGEPATH_GITHUB_REPO:=forgepath}"
: "${FORGEPATH_TARGET_BRANCH:=dev}"
# GITHUB_TOKEN has no default: it's a secret, so a step needing it should fail
# loudly rather than silently fall back.
: "${GITHUB_TOKEN:=}"
export FORGEPATH_GITHUB_OWNER FORGEPATH_GITHUB_REPO FORGEPATH_TARGET_BRANCH GITHUB_TOKEN

# Renders FORGEPATH_* placeholders in a file to stdout. sed matches only the
# single-brace `${VAR}` form, leaving Nunjucks `${{ ... }}` untouched.
forgepath_render() {
  sed \
    -e "s|\${FORGEPATH_GITHUB_OWNER}|${FORGEPATH_GITHUB_OWNER}|g" \
    -e "s|\${FORGEPATH_GITHUB_REPO}|${FORGEPATH_GITHUB_REPO}|g" \
    -e "s|\${FORGEPATH_TARGET_BRANCH}|${FORGEPATH_TARGET_BRANCH}|g" \
    "$1"
}

unset _FORGEPATH_REPO_ROOT
