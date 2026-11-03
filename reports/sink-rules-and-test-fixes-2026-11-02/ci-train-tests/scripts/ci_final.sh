#!/bin/bash
# Test-by-test comparisons of the replays, and where PyYAML comes from in a fresh dev + numpy resolve.
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
R="$HERE/replay"
P="python3 -I $HERE/ci_compare.py"
echo "== tests/ on base vs on the new commit (both without tree-sitter)"
$P "$R/base-step_02.xml" "$R/new-step_02.xml"
echo "== eval/tests on base vs on the new commit (both without tree-sitter)"
$P "$R/base-step_03.xml" "$R/new-step_03.xml"
echo "== train/tests without tree-sitter vs the workflow's step"
$P "$R/diag-notree-train.xml" "$R/new-step_05.xml"
echo "== tests/ in the workflow vs with tree-sitter installed first"
$P "$R/new-step_02.xml" "$R/diag-tree-tests.xml"
echo "== eval/tests in the workflow vs with tree-sitter installed first"
$P "$R/new-step_03.xml" "$R/new-diag_3.xml"
echo "== PyYAML: who asks for it (fresh resolve, nothing installed)"
W=$(mktemp -d /tmp/ci-yaml.XXXXXX)
git -c safe.directory='*' -C /mnt/c/Users/h/Downloads/orion/.git show ci/run-train-tests:pyproject.toml > "$W/pyproject.toml"
cd "$W" || exit 2
uv pip compile -q --python 3.12 --no-header --extra dev pyproject.toml -o reqs.txt
grep -n -i -A4 '^pyyaml' reqs.txt
grep -n -i -E '^(numpy|tree-sitter|transformers|mlx)' reqs.txt || echo "(no numpy / tree-sitter / transformers / mlx from dev alone)"
rm -rf "$W"
