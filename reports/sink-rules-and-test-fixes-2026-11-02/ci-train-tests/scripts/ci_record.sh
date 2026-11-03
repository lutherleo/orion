#!/bin/bash
# Put the checks that so far only printed to the terminal on file, next to the replay logs.
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
R="$HERE/replay"
bash "$HERE/ci_final.sh" > "$R/comparisons.txt" 2>&1
python3 -I "$HERE/ci_skips.py" "$R/new-step_02.xml" "$R/new-step_03.xml" "$R/new-step_05.xml" "$R/diag-notree-train.xml" "$R/diag-tree-tests.xml" > "$R/skip-reasons.txt" 2>&1
bash "$HERE/lm_extra_probe.sh" > "$R/lm-extra-dry-run.txt" 2>&1
bash "$HERE/ci_lint.sh" > "$R/actionlint.txt" 2>&1
cat "$R/skip-reasons.txt"
echo ---; cat "$R/actionlint.txt"
echo ---; grep -E "packages would install|pip exit" "$R/lm-extra-dry-run.txt"
echo ---; grep -E "passed|failed" "$R/new.log" | grep -v "^SKIPPED" | grep -v "^new-"
