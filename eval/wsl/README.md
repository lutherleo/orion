# Running the eval on a Windows PC (WSL2 Ubuntu)

The harness is POSIX by design (bash usage shim, `:` PATH, `~/joern/joern-cli`), so on Windows it
runs **inside WSL2 Ubuntu 24.04, unmodified**. These steps were set up on an i7-11850H / 48 GB /
NVIDIA T1200 (4 GB VRAM) laptop.

## One-time setup

1. **WSL limits.** `C:\Users\<you>\.wslconfig`:
   ```ini
   [wsl2]
   memory=40GB
   processors=16
   swap=8GB
   ```
   The default (50% of RAM) is tight for gpt-oss:20b + Neo4j. Apply with `wsl --shutdown`.
2. **Distro.** `wsl --install -d Ubuntu-24.04`, then open it once and create your Linux user.
   systemd is on by default (`/etc/wsl.conf`), which Docker and Ollama need.
3. **System packages** (no password needed via `-u root`):
   ```bash
   wsl -d Ubuntu-24.04 -u root -- bash /mnt/c/<path>/orion/eval/wsl/provision_root.sh <linux-user>
   wsl --shutdown        # so the new docker-group membership takes effect
   ```
   It installs base packages, cloc, Docker Engine, Node.js 22, the Codex CLI and Ollama (systemd service).
4. **Repo on the Linux filesystem** (`/mnt/c` is far too slow for Joern):
   ```bash
   git clone /mnt/c/<path>/orion ~/orion && cd ~/orion && git checkout Oracle
   bash eval/wsl/provision_user.sh
   ```
   This runs `bench/setup.sh` (JDK, Joern, venv with CUDA torch, Claude CLI, Neo4j), installs
   `.[eval]`, clones the NodeGoat and PyGoat fixtures, and pulls `gemma3:12b` and `gpt-oss:20b`
   (about 21 GB).
5. **Log in** (interactive, once): `claude`, then `codex`.
6. **Check:**
   ```bash
   source bench/env.sh
   ./.venv/bin/python -m eval.preflight          # ALL PRESENT
   ./.venv/bin/python bench/prove.py --dry-run
   ./.venv/bin/python -m pytest -m "not slow"    # Neo4j-backed tests now run too
   ```

## Syncing with the Windows checkout

The WSL clone's `origin` is the Windows checkout, never GitHub.
- Windows → WSL: `git pull --ff-only` in `~/orion`.
- WSL → Windows (eval results committed on `Oracle` in WSL): in the Windows checkout,
  `git pull \\wsl.localhost\Ubuntu-24.04\home\<user>\orion Oracle`.

## What to expect from the hardware

The study models don't fit in 4 GB of VRAM (gemma3:12b is about 8 GB, gpt-oss:20b about 13 GB).
Ollama offloads what fits to the GPU and runs the rest on the CPU from RAM. "Shared GPU memory" in
Task Manager is system RAM reached over PCIe, and doesn't help. Expect a few tokens/s. Check the
split with `ollama ps` and the speed in Phase 0 (`eval/README.md`) before committing to full runs.
