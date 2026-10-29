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

say "system java (so non-login shells, e.g. \`wsl -- bash -c ...\`, can run Joern)"
# bench/setup.sh installs the JDK user-local and only bench/env.sh puts it on PATH; a plain
# `wsl -- cmd` never reads that, so Joern failed there. Register it as the system java instead.
for t in java javac jar keytool; do
  [ -x "$HOME/jdk/current/bin/$t" ] && sudo update-alternatives --install "/usr/bin/$t" "$t" "$HOME/jdk/current/bin/$t" 2100 >/dev/null
done
java -version 2>&1 | head -1

say "eval extras (psutil)"
export PATH="$HOME/.local/bin:$PATH"
# Keep the transformers<5 pin from bench/setup.sh: re-resolving the extras without it upgrades
# transformers and breaks the jina embedding model (get_extended_attention_mask).
VIRTUAL_ENV="$REPO_ROOT/.venv" uv pip install -e ".[semantic,dev,eval]" "transformers<5"

say "fixtures"
mkdir -p fixtures
[ -d fixtures/NodeGoat/.git ] || git clone --depth 1 https://github.com/OWASP/NodeGoat.git fixtures/NodeGoat
[ -d fixtures/pygoat/.git ] || git clone --depth 1 https://github.com/adeyosemanputra/pygoat.git fixtures/pygoat

say "NodeGoat runtime (for --runtime / tests/test_runtime_live.py)"
# NodeGoat's mongodb@2 driver cannot talk to Mongo 6+, so 4.4; published on loopback only.
docker ps -a --format '{{.Names}}' | grep -qx orion-nodegoat-mongo || \
  docker run -d --name orion-nodegoat-mongo --restart unless-stopped -p 127.0.0.1:27017:27017 mongo:4.4 >/dev/null
docker start orion-nodegoat-mongo >/dev/null
# Runtime deps only; --ignore-scripts also skips Cypress's large browser download.
(cd fixtures/NodeGoat && npm install --omit=dev --ignore-scripts --no-audit --no-fund --loglevel=error)
for _ in $(seq 1 30); do
  docker exec orion-nodegoat-mongo mongo --quiet --eval 'db.runCommand({ping:1}).ok' 2>/dev/null | grep -q 1 && break
  sleep 2
done
(cd fixtures/NodeGoat && node artifacts/db-reset.js >/dev/null && echo "NodeGoat demo data seeded")

say "Ollama models (eval/run.py MODEL_TAGS; qwen3-coder is for the exploratory arms)"
for tag in gemma3:12b gpt-oss:20b qwen3-coder:30b; do
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
