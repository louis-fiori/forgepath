#!/usr/bin/env bash
# Scaffolds a fresh Backstage app into local/backstage/ if missing, then runs
# backstage-sync.sh to layer the forgepath platform sources on top.
#
# Idempotent: safe to re-run. Will not delete local/backstage/.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=./_env.sh
. "${REPO_ROOT}/scripts/_env.sh"

BACKSTAGE_DIR="${REPO_ROOT}/local/backstage"
OVERLAY_DIR="${REPO_ROOT}/platform/backstage/overlay"

# Pinned so every clone scaffolds the exact generation the overlay (and the
# baseline in overlay/upstream/) was authored against. Bumping it (in .env) is
# deliberate: see "Upgrading the scaffold" in overlay/README.md.
CREATE_APP_VERSION="${FORGEPATH_CREATE_APP_VERSION:-0.8.3}"

require() {
  command -v "$1" >/dev/null || { echo "missing dependency: $1 (run: make deps)" >&2; exit 1; }
}
require node
require yarn
require npx

NODE_MAJOR="$(node -p 'process.versions.node.split(".")[0]')"
if [ "${NODE_MAJOR}" -lt 22 ]; then
  echo "Node 22+ required (got $(node -v))." >&2
  echo "Try: nvm install 22 && nvm use 22   (or: make deps)" >&2
  exit 1
fi

# 1. Scaffold a fresh Backstage app if missing.
if [ ! -d "${BACKSTAGE_DIR}/packages" ]; then
  echo "==> scaffolding Backstage into local/backstage (this takes ~5 min)"
  mkdir -p "$(dirname "${BACKSTAGE_DIR}")"
  APP_NAME="$(basename "${BACKSTAGE_DIR}")"
  pushd "$(dirname "${BACKSTAGE_DIR}")" >/dev/null
  # create-app has no --name flag; it always prompts for the app name. Feed the
  # answer (matching --path basename) on stdin so it runs non-interactively —
  # else it hangs under `make` and fails in CI with no TTY (SIGINT -> exit 130).
  printf '%s\n' "${APP_NAME}" | \
    npx --yes "@backstage/create-app@${CREATE_APP_VERSION}" --path "${APP_NAME}" --skip-install
  popd >/dev/null

  # Snapshot the pristine scaffold version of every overlay-overwritten file.
  # backstage-sync.sh diffs these against the committed baseline in
  # overlay/upstream/ to detect upstream drift after a create-app bump.
  echo "==> snapshotting pristine scaffold files into .scaffold-pristine/"
  PRISTINE_DIR="${BACKSTAGE_DIR}/.scaffold-pristine"
  rm -rf "${PRISTINE_DIR}"
  ( cd "${OVERLAY_DIR}/files" && find . -type f -print0 ) | \
    while IFS= read -r -d '' f; do
      src="${BACKSTAGE_DIR}/${f#./}"
      if [ -f "${src}" ]; then
        mkdir -p "${PRISTINE_DIR}/$(dirname "${f#./}")"
        cp "${src}" "${PRISTINE_DIR}/${f#./}"
      fi
    done
else
  echo "==> local/backstage/ already exists, skipping scaffold"
fi

# 2. Sync platform sources (overlay + catalog + templates + dashboards).
"${REPO_ROOT}/scripts/backstage-sync.sh"

# 3. Merge package-additions.json into packages/backend/package.json.
echo "==> merging backend dependencies from package-additions.json"
node -e "
  const fs = require('fs');
  const target = '${BACKSTAGE_DIR}/packages/backend/package.json';
  const adds = JSON.parse(fs.readFileSync('${OVERLAY_DIR}/package-additions.json', 'utf8'));
  const pkg = JSON.parse(fs.readFileSync(target, 'utf8'));
  for (const section of ['dependencies', 'devDependencies']) {
    if (adds[section]) {
      pkg[section] = Object.fromEntries(
        Object.entries({ ...pkg[section], ...adds[section] }).sort(
          ([a], [b]) => a.localeCompare(b),
        ),
      );
    }
  }
  fs.writeFileSync(target, JSON.stringify(pkg, null, 2) + '\n');
"

# 4. Install dependencies.
echo "==> running yarn install in local/backstage/"
( cd "${BACKSTAGE_DIR}" && yarn install )

echo
echo "Backstage scaffolded and platform sources applied."
echo "Next: make backstage-build  (then make local-up)"
