#!/usr/bin/env bash
# `make deps`: install every missing prerequisite via the host's package manager.
#
# Supported: macOS (Homebrew), Debian/Ubuntu (apt), Fedora/RHEL (dnf/yum),
# Arch (pacman), openSUSE (zypper), and WSL2 (apt + Docker Desktop handoff).
# Idempotent — tools already present (and at the right version) are skipped.
# Node is installed via nvm; kind/kubectl as pinned binaries where no package exists.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=./_deps.sh
. "${REPO_ROOT}/scripts/_deps.sh"

OS="$(forgepath_os)"
PM="$(forgepath_pm)"
ARCH="$(forgepath_arch)"

# Pinned versions for the manual binary installs; override via env if needed.
KIND_VERSION="${KIND_VERSION:-0.23.0}"
KUBECTL_VERSION="${KUBECTL_VERSION:-1.31.0}"
NVM_VERSION="${NVM_VERSION:-0.40.1}"

log()  { echo "==> $*"; }
warn() { echo "!!  $*" >&2; }

# Run privileged commands via sudo unless we're already root.
sudo_if() { if [ "$(id -u)" -eq 0 ]; then "$@"; else sudo "$@"; fi; }

# Install a package, named per manager. Pass "" to skip on a given manager.
#   pm_install <brew> <apt> <dnf/yum> <pacman> <zypper>
pm_install() {
  local brew_p="$1" apt_p="$2" dnf_p="$3" pac_p="$4" zyp_p="$5"
  case "$PM" in
    brew)   [ -n "$brew_p" ] && brew install $brew_p ;;
    apt)    [ -n "$apt_p" ]  && { sudo_if apt-get update -qq; sudo_if apt-get install -y $apt_p; } ;;
    dnf)    [ -n "$dnf_p" ]  && sudo_if dnf install -y $dnf_p ;;
    yum)    [ -n "$dnf_p" ]  && sudo_if yum install -y $dnf_p ;;
    pacman) [ -n "$pac_p" ]  && sudo_if pacman -S --needed --noconfirm $pac_p ;;
    zypper) [ -n "$zyp_p" ]  && sudo_if zypper install -y $zyp_p ;;
    *) return 1 ;;
  esac
}

# Download an executable to /usr/local/bin (used for kind + kubectl, which no
# package manager ships consistently across distros).
install_binary() {
  local name="$1" url="$2" tmp
  tmp="$(mktemp)"
  log "downloading ${name} from ${url}"
  curl -fsSL -o "$tmp" "$url"
  chmod +x "$tmp"
  sudo_if mv "$tmp" "/usr/local/bin/${name}"
}

install_kind() {
  if [ "$PM" = brew ]; then brew install kind; return; fi
  install_binary kind \
    "https://kind.sigs.k8s.io/dl/v${KIND_VERSION}/kind-$(uname -s | tr '[:upper:]' '[:lower:]')-${ARCH}"
}

install_kubectl() {
  if [ "$PM" = brew ]; then brew install kubectl; return; fi
  install_binary kubectl \
    "https://dl.k8s.io/release/v${KUBECTL_VERSION}/bin/$(uname -s | tr '[:upper:]' '[:lower:]')/${ARCH}/kubectl"
}

# Source nvm from $NVM_DIR or the common install locations (mirrors the Makefile).
load_nvm() {
  export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
  # shellcheck disable=SC1090,SC1091
  if   [ -s "$NVM_DIR/nvm.sh" ];                 then . "$NVM_DIR/nvm.sh"
  elif [ -s /opt/homebrew/opt/nvm/nvm.sh ];      then . /opt/homebrew/opt/nvm/nvm.sh
  elif [ -s /usr/local/opt/nvm/nvm.sh ];         then . /usr/local/opt/nvm/nvm.sh
  fi
}

install_node() {
  load_nvm
  if ! command -v nvm >/dev/null 2>&1; then
    log "installing nvm v${NVM_VERSION}"
    curl -fsSL "https://raw.githubusercontent.com/nvm-sh/nvm/v${NVM_VERSION}/install.sh" | bash
    load_nvm
  fi
  if command -v nvm >/dev/null 2>&1; then
    nvm install 22 && nvm alias default 22 >/dev/null
  else
    warn "nvm unavailable; install Node 22+ manually (https://nodejs.org)"
    return 1
  fi
}

install_yarn() {
  # corepack ships with Node 22 and provides a yarn shim — no global install.
  load_nvm
  if command -v corepack >/dev/null 2>&1; then
    corepack enable >/dev/null 2>&1 || true
    corepack prepare yarn@stable --activate >/dev/null 2>&1 || true
  fi
  have yarn || npm install -g yarn
}

install_docker() {
  case "$OS" in
    macos)
      brew install --cask docker || warn "could not install Docker Desktop via brew"
      warn "Launch Docker Desktop once so the engine starts, then re-run make deps." ;;
    wsl)
      warn "On WSL2, don't apt-install the engine. Install Docker Desktop on Windows"
      warn "and turn on Settings -> Resources -> WSL integration for this distro." ;;
    *)
      # Native Linux: distro engine + enable the daemon + group membership.
      pm_install "" docker.io docker docker docker
      command -v systemctl >/dev/null 2>&1 && sudo_if systemctl enable --now docker || true
      sudo_if usermod -aG docker "$USER" 2>/dev/null || true
      warn "Added you to the 'docker' group — log out/in (or: newgrp docker) to use docker without sudo." ;;
  esac
}

# --- bootstrap the package manager itself ----------------------------------
if [ "$PM" = none ]; then
  if [ "$OS" = macos ]; then
    warn "Homebrew not found. Install it first, then re-run make deps:"
    warn '  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
  else
    warn "No supported package manager found (apt/dnf/yum/pacman/zypper)."
    warn "Install the prerequisites manually — see docs/quickstart.md."
  fi
  exit 1
fi

log "os=${OS} pkg-manager=${PM} arch=${ARCH}"

# --- install each missing dependency ---------------------------------------
for tool in "${FORGEPATH_DEPS[@]}"; do
  IFS=$'\t' read -r status _ <<<"$(forgepath_probe "$tool")"
  if [ "$status" = ok ]; then
    log "${tool}: already installed, skipping"
    continue
  fi
  log "${tool}: installing"
  case "$tool" in
    git)     pm_install git git git git git ;;
    curl)    pm_install curl curl curl curl curl ;;
    make)    pm_install make build-essential make make make ;;
    openssl) pm_install openssl openssl openssl openssl openssl ;;
    docker)  install_docker ;;
    node)    install_node ;;
    yarn)    install_yarn ;;
    kind)    install_kind ;;
    kubectl) install_kubectl ;;
  esac
done

echo
log "done — re-checking with the doctor:"
echo
# Don't let a non-zero doctor (e.g. Docker daemon not started yet) fail the install.
"${REPO_ROOT}/scripts/preflight.sh" || true

echo
log "If Node/Yarn was just installed via nvm, open a new shell (or: . \"\$NVM_DIR/nvm.sh\") before make backstage-init."
