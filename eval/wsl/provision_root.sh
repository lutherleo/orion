#!/usr/bin/env bash
# System-level setup for the Orion eval inside WSL2 Ubuntu. Run as root, no password needed:
#
#   wsl -d Ubuntu-24.04 -u root -- bash /mnt/c/<path-to>/orion/eval/wsl/provision_root.sh [linux-user]
#
# Installs: base packages + cloc, Docker Engine (for Neo4j + the harness sandbox), Node.js 22 + the
# Codex CLI (plain-gpt arm), and Ollama as a systemd service (CUDA through the WSL GPU driver).
# Idempotent: each step is skipped when already done. With [linux-user], also puts that user in the
# `docker` group. Everything user-local (JDK, Joern, venv, Claude CLI, models, fixtures) is
# provision_user.sh's job.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run as root: wsl -d Ubuntu-24.04 -u root -- bash $0" >&2; exit 1; }
say() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
export DEBIAN_FRONTEND=noninteractive
# WSL appends the WINDOWS PATH (/mnt/c/...): a Docker Desktop or Git-for-Windows binary would satisfy a
# plain `command -v` and skip the Linux install. Only a Linux-side binary counts.
have() { local p; p="$(command -v "$1" 2>/dev/null)" && [[ "$p" != /mnt/* ]]; }

say "apt packages"
apt-get update -y
apt-get install -y curl unzip tar xz-utils git ca-certificates jq python3 python3-venv \
                   build-essential cloc zstd

if ! have docker; then
  say "Docker Engine"
  curl -fsSL https://get.docker.com | sh
fi
systemctl enable --now docker
docker version --format 'docker {{.Server.Version}}'

if ! have node || [ "$(node -p 'process.versions.node.split(".")[0]')" -lt 20 ]; then
  say "Node.js 22"
  curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
  apt-get install -y nodejs
fi
node --version
if ! have codex; then
  say "Codex CLI"
  npm install -g @openai/codex
fi
codex --version || true

if ! have ollama; then
  say "Ollama"
  curl -fsSL https://ollama.com/install.sh | sh
fi
systemctl enable --now ollama
for _ in $(seq 1 30); do curl -sf http://127.0.0.1:11434/api/tags >/dev/null && break; sleep 1; done
ollama --version

if [ "${1:-}" ]; then
  say "docker group for $1"
  usermod -aG docker "$1"
fi

say "root provisioning done"
