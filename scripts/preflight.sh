#!/usr/bin/env bash
# `make doctor`: check every prerequisite and print a status table. Exits non-zero
# if anything is missing or outdated, so it doubles as a CI/pre-up gate.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=./_deps.sh
. "${REPO_ROOT}/scripts/_deps.sh"

# Disable colour when stdout isn't a terminal (CI logs, pipes).
if [ -t 1 ]; then G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; B=$'\033[1m'; N=$'\033[0m'
else G=''; Y=''; R=''; B=''; N=''; fi

echo "${B}ForgePath preflight${N} — os=$(forgepath_os) arch=$(forgepath_arch) pkg-manager=$(forgepath_pm)"
echo

problems=0
for tool in "${FORGEPATH_DEPS[@]}"; do
  IFS=$'\t' read -r status detail <<<"$(forgepath_probe "$tool")"
  case "$status" in
    ok)      printf '  %s✓%s %-9s %s\n'  "$G" "$N" "$tool" "$detail" ;;
    old)     printf '  %s!%s %-9s %s\n'  "$Y" "$N" "$tool" "$detail"; problems=1 ;;
    missing) printf '  %s✗%s %-9s %s\n'  "$R" "$N" "$tool" "not installed"; problems=1 ;;
  esac
done

echo
if [ "$problems" -eq 0 ]; then
  echo "${G}All prerequisites satisfied.${N} Next: make backstage-init"
else
  echo "${Y}Some prerequisites are missing or outdated.${N} Install them with: make deps"
fi
exit "$problems"
