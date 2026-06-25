# Shared dependency spec + probes for the doctor / install-deps scripts.
# Source-only (no shebang). One place answers "what does ForgePath need" and
# "is this tool OK"; preflight.sh reports it, install-deps.sh installs the gaps.

# The full prerequisite set, in a safe install order (toolchain basics first,
# then docker, then node before yarn, then the k8s CLIs).
FORGEPATH_DEPS=(git curl make openssl docker node yarn kind kubectl)

# macos | wsl | linux | unknown. WSL2 is detected so install-deps can route
# Docker to Docker Desktop instead of an apt engine (no systemd in WSL).
forgepath_os() {
  case "$(uname -s)" in
    Darwin) echo macos ;;
    Linux)
      if grep -qiE 'microsoft|wsl' /proc/version 2>/dev/null; then echo wsl; else echo linux; fi ;;
    *) echo unknown ;;
  esac
}

# Package manager, in preference order: brew | apt | dnf | yum | pacman | zypper | none.
forgepath_pm() {
  command -v brew >/dev/null 2>&1 && { echo brew; return; }
  local pm
  for pm in apt-get dnf yum pacman zypper; do
    command -v "$pm" >/dev/null 2>&1 && { echo "${pm%-get}"; return; }
  done
  echo none
}

# GOARCH-style arch for the kind/kubectl binary downloads.
forgepath_arch() {
  case "$(uname -m)" in
    x86_64|amd64) echo amd64 ;;
    aarch64|arm64) echo arm64 ;;
    *) uname -m ;;
  esac
}

have() { command -v "$1" >/dev/null 2>&1; }

# Probe one tool. Echoes "STATUS<TAB>DETAIL" where STATUS is ok|old|missing.
# Only node (>=22) and docker (daemon up) gate on more than mere presence —
# the other tools' documented minimums are old enough that any current build passes.
forgepath_probe() {
  case "$1" in
    node)
      have node || { printf 'missing\t\n'; return; }
      local v maj; v="$(node -v 2>/dev/null)"; maj="${v#v}"; maj="${maj%%.*}"
      if [ "${maj:-0}" -lt 22 ] 2>/dev/null; then printf 'old\t%s (need 22+)\n' "$v"
      else printf 'ok\t%s\n' "$v"; fi ;;
    docker)
      have docker || { printf 'missing\t\n'; return; }
      local v; v="$(docker --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)"
      if docker info >/dev/null 2>&1; then printf 'ok\t%s\n' "$v"
      else printf 'old\t%s (daemon not reachable)\n' "$v"; fi ;;
    kind)    have kind    && printf 'ok\t%s\n' "$(kind --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)"             || printf 'missing\t\n' ;;
    kubectl) have kubectl && printf 'ok\t%s\n' "$(kubectl version --client 2>/dev/null | grep -oE 'v[0-9]+\.[0-9]+\.[0-9]+' | head -1)" || printf 'missing\t\n' ;;
    yarn)    have yarn    && printf 'ok\t%s\n' "$(yarn --version 2>/dev/null)"                                                            || printf 'missing\t\n' ;;
    openssl) have openssl && printf 'ok\t%s\n' "$(openssl version 2>/dev/null | awk '{print $2}')"                                        || printf 'missing\t\n' ;;
    make)    have make    && printf 'ok\t%s\n' "$(make --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+(\.[0-9]+)?' | head -1)"           || printf 'missing\t\n' ;;
    git)     have git     && printf 'ok\t%s\n' "$(git --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)"               || printf 'missing\t\n' ;;
    curl)    have curl    && printf 'ok\t%s\n' "$(curl --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)"              || printf 'missing\t\n' ;;
    *)       printf 'missing\t\n' ;;
  esac
}
