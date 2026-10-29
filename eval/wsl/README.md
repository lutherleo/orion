# Running the eval on a Windows PC (WSL2 Ubuntu)

The harness is POSIX by design (bash usage shim, `:` PATH, `~/joern/joern-cli`), so on Windows it
runs **inside a WSL2 Ubuntu, unmodified** (24.04 or newer; set up and verified on 26.04 LTS). These
steps were run on an i7-11850H / 48 GB / NVIDIA T1200 (4 GB VRAM) laptop. Below, `<distro>` is the
WSL distribution name (`wsl -l -v`), e.g. `Ubuntu`.

## One-time setup

1. **WSL limits.** `C:\Users\<you>\.wslconfig`:
   ```ini
   [wsl2]
   memory=40GB
   processors=16
   swap=8GB
   ```
   The default (50% of RAM) is tight for gpt-oss:20b + Neo4j. Apply with `wsl --shutdown`.
2. **Distro.** Use an existing Ubuntu, or `wsl --install -d Ubuntu-24.04` and open it once to create
   your Linux user. systemd must be on (`/etc/wsl.conf` `[boot] systemd=true`, the default), because
   Docker and Ollama run as services.
3. **System packages** (no password needed via `-u root`). The Windows checkout has CRLF line endings,
   so strip them first:
   ```bash
   wsl -d <distro> -u root -- bash -c "sed 's/\r$//' /mnt/c/<path>/orion/eval/wsl/provision_root.sh > /root/p.sh && bash /root/p.sh <linux-user>"
   wsl --terminate <distro>   # so the new docker-group membership takes effect
   ```
   Docker Desktop on Windows is not used: WSL puts its `docker.exe` on the Linux PATH, and the scripts
   deliberately install and use a Linux Docker Engine instead.
   It installs base packages, cloc, Docker Engine, Node.js 22, the Codex CLI and Ollama (systemd service).
4. **Repo on the Linux filesystem** (`/mnt/c` is far too slow for Joern):
   ```bash
   git clone /mnt/c/<path>/orion ~/orion && cd ~/orion && git checkout Oracle
   bash eval/wsl/provision_user.sh
   ```
   This runs `bench/setup.sh` (JDK, Joern, venv with CUDA torch, Claude CLI, Neo4j), registers the
   JDK as the system `java` (so Joern runs from any shell, not just ones that sourced
   `bench/env.sh`), installs `.[eval]`, clones the NodeGoat and PyGoat fixtures, makes NodeGoat
   runnable (MongoDB 4.4 container `orion-nodegoat-mongo` on 127.0.0.1:27017, runtime npm deps, demo
   data), and pulls `gemma3:12b`, `gpt-oss:20b` and `qwen3-coder:30b`.
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
  `git pull \\wsl.localhost\<distro>\home\<user>\orion Oracle`.

## What to expect from the hardware

The study models don't fit in 4 GB of VRAM (gemma3:12b is about 8 GB, gpt-oss:20b about 13 GB).
Ollama offloads what fits to the GPU and runs the rest on the CPU from RAM. "Shared GPU memory" in
Task Manager is system RAM reached over PCIe, and doesn't help. Expect a few tokens/s. Check the
split with `ollama ps` and the speed in Phase 0 (`eval/README.md`) before committing to full runs.
