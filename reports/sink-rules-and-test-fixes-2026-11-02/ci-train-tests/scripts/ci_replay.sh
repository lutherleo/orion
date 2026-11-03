#!/bin/bash
# Replay the `unit` job of a ref's own .github/workflows/tests.yml: shallow clone, fresh Python 3.12 venv,
# each run: step as the YAML parses it, in GitHub's shell flags, with a scrubbed env. Keeps going after a
# failed step (GitHub would stop) so every count is on record.
#   ci_replay.sh <ref> <label> <out dir> [diagnostic command...]
# Each diagnostic runs AFTER the workflow's steps, in the same venv, and is NOT part of the workflow.
set -u
REF=$1; LABEL=$2; OUT=$3; shift 3
HERE=$(cd "$(dirname "$0")" && pwd)
W=$(mktemp -d /tmp/ci-replay.XXXXXX)
mkdir -p "$OUT" "$W/home" "$W/tmp" "$W/steps"
LOG="$OUT/$LABEL.log"
: > "$LOG"
say() { echo "$*" | tee -a "$LOG"; }

GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0='*' \
  git clone -q --depth 1 --branch "$REF" "file:///mnt/c/Users/h/Downloads/orion/.git" "$W/repo" 2>&1 | tee -a "$LOG"
[ -d "$W/repo/.git" ] || { say "clone failed"; exit 2; }
say "ref $REF @ $(git -C "$W/repo" rev-parse --short HEAD), $(git -C "$W/repo" rev-list --count HEAD) commit(s) in the clone"
say "fixtures/ present: $([ -d "$W/repo/fixtures" ] && echo yes || echo no)"

uv venv -q --python 3.12 --seed "$W/v" || { say "venv failed"; exit 2; }
say "venv: $("$W/v/bin/python" --version), packages before step 1: $("$W/v/bin/pip" list --format=freeze 2>/dev/null | wc -l)"

uv run -q --no-project --python 3.12 --with pyyaml python -I "$HERE/ci_steps.py" \
  "$W/repo/.github/workflows/tests.yml" unit "$W/steps" 2>&1 | tee -a "$LOG" || { say "yaml parse failed"; exit 2; }

run_step() {  # <name> <script>: the runner's shell flags in a scrubbed env. NEO4J_URI is a dead port: the
              # runner's unit job has no Neo4j, and this box may have one on the default port.
  local name=$1 script=$2
  ( cd "$W/repo" && env -i HOME="$W/home" PATH="$W/v/bin:/usr/local/bin:/usr/bin:/bin" LANG=C.UTF-8 \
      CI=true GITHUB_ACTIONS=true TMPDIR="$W/tmp" NEO4J_URI=bolt://127.0.0.1:1 \
      PYTEST_ADDOPTS="--junitxml=$OUT/$LABEL-$name.xml -rs" \
      bash --noprofile --norc -eo pipefail "$script" ) >> "$LOG" 2>&1
  local rc=$?
  say "=== $name: exit $rc"
}

for f in "$W"/steps/step_*.sh; do
  n=$(basename "$f" .sh)
  say ">>> $n: $(cat "$f")"
  run_step "$n" "$f"
done

i=0
for cmd in "$@"; do
  i=$((i + 1))
  echo "$cmd" > "$W/steps/diag_$i.sh"
  say ">>> diag_$i (NOT in the workflow): $cmd"
  run_step "diag_$i" "$W/steps/diag_$i.sh"
done

say "--- pip freeze at the end"
"$W/v/bin/pip" list --format=freeze 2>/dev/null >> "$LOG"
say "--- summary"
python3 -I "$HERE/ci_summary.py" "$OUT" "$LABEL" 2>&1 | tee -a "$LOG"
rm -rf "$W"
