#!/bin/bash
# Two comparisons that are NOT in the workflow, same clean setup as ci_replay.sh:
#   notree: train/tests with only the first install step (dev + numpy), so what skips without tree-sitter
#   tree:   tests/ with the workflow's tree-sitter step on top, so what moving that step up would do
set -u
OUT=$1
HERE=$(cd "$(dirname "$0")" && pwd)
W=$(mktemp -d /tmp/ci-diag.XXXXXX)
mkdir -p "$OUT" "$W/home" "$W/tmp" "$W/steps"
LOG="$OUT/diag.log"
: > "$LOG"
say() { echo "$*" | tee -a "$LOG"; }

GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0='*' \
  git clone -q --depth 1 --branch ci/run-train-tests "file:///mnt/c/Users/h/Downloads/orion/.git" "$W/repo" 2>&1 | tee -a "$LOG"
[ -d "$W/repo/.git" ] || { say "clone failed"; exit 2; }
say "ref @ $(git -C "$W/repo" rev-parse --short HEAD)"
uv venv -q --python 3.12 --seed "$W/v" || { say "venv failed"; exit 2; }
uv run -q --no-project --python 3.12 --with pyyaml python -I "$HERE/ci_steps.py" \
  "$W/repo/.github/workflows/tests.yml" unit "$W/steps" 2>&1 | tee -a "$LOG"

run_script() {  # <name> <script>
  local name=$1 script=$2
  ( cd "$W/repo" && env -i HOME="$W/home" PATH="$W/v/bin:/usr/local/bin:/usr/bin:/bin" LANG=C.UTF-8 \
      CI=true GITHUB_ACTIONS=true TMPDIR="$W/tmp" NEO4J_URI=bolt://127.0.0.1:1 \
      PYTEST_ADDOPTS="--junitxml=$OUT/diag-$name.xml -rs" \
      bash --noprofile --norc -eo pipefail "$script" ) >> "$LOG" 2>&1
  say "=== $name: exit $?"
}

cat > "$W/notree.sh" <<'EOF'
pytest train/tests -m "not slow" -q
EOF
cat > "$W/tree.sh" <<'EOF'
pytest -m "not slow" -q
EOF

run_script install "$W/steps/step_01.sh"
run_script notree-train "$W/notree.sh"
run_script install-ts "$W/steps/step_04.sh"
run_script tree-tests "$W/tree.sh"

say "--- summary"
rm -f "$OUT/diag-install.xml" "$OUT/diag-install-ts.xml"
python3 -I "$HERE/ci_summary.py" "$OUT" diag 2>&1 | tee -a "$LOG"
rm -rf "$W"
