#!/usr/bin/env bash
# User-level setup for the Orion eval inside WSL2 Ubuntu (run AFTER provision_root.sh), from the WSL
# clone of the repo:
#
#   cd ~/orion && bash eval/wsl/provision_user.sh
#
# Reuses bench/setup.sh (JDK 21, Joern, uv venv with the semantic extras + CUDA torch, Claude CLI,
# Neo4j via docker compose, bench/env.sh), then adds what the eval needs on top: psutil (peak-RSS
# capture), the NodeGoat + PyGoat fixtures, and the pre-registered Ollama models. Idempotent.
# Interactive logins (claude, codex) are left to you -- this script never handles credentials.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"
say() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }

[ "$(id -u)" -ne 0 ] || { echo "run as your normal user, not root" >&2; exit 1; }
docker ps >/dev/null 2>&1 || {
  echo "docker is not usable as $USER -- run provision_root.sh with your username, then 'wsl --shutdown' and reopen" >&2
  exit 1; }

say "bench/setup.sh (JDK, Joern, venv, Claude CLI, Neo4j)"
bash bench/setup.sh

say "eval extras (psutil)"
export PATH="$HOME/.local/bin:$PATH"
VIRTUAL_ENV="$REPO_ROOT/.venv" uv pip install -e ".[semantic,dev,eval]"

say "fixtures"
mkdir -p fixtures
[ -d fixtures/NodeGoat/.git ] || git clone --depth 1 https://github.com/OWASP/NodeGoat.git fixtures/NodeGoat
[ -d fixtures/pygoat/.git ] || git clone --depth 1 https://github.com/adeyosemanputra/pygoat.git fixtures/pygoat

say "Ollama models (pre-registered tags, eval/run.py MODEL_TAGS)"
for tag in gemma3:12b gpt-oss:20b; do
  ollama list | awk '{print $1}' | grep -qx "$tag" || ollama pull "$tag"
done
ollama list

say "user provisioning done"
cat <<EOF
Remaining one-time steps (interactive, yours):
  1. claude                       # log in to Claude Code (subscription OAuth)
  2. codex                        # sign in to Codex with your ChatGPT account
Then check everything:
  source bench/env.sh
  ./.venv/bin/python -m eval.preflight          # expect: ALL PRESENT
  ./.venv/bin/python bench/prove.py --dry-run   # expect: all prerequisites present
EOF
