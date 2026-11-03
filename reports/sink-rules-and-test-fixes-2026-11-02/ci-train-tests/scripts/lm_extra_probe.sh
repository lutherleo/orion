#!/bin/bash
# Does the `lm` extra resolve on Linux under Python 3.12? Dry run only: nothing is installed.
set -u
S=$(mktemp -d /tmp/lmcheck.XXXXXX)
cd "$S" || exit 1
uv venv -q --python 3.12 --seed "$S/v" || exit 1
"$S/v/bin/python" --version
"$S/v/bin/pip" --version
"$S/v/bin/python" -c "import platform; print(platform.machine(), platform.libc_ver())"

cat > "$S/show.py" <<'EOF'
import json, os, sys
path, wanted = sys.argv[1], {w.lower() for w in sys.argv[2:]}
if not os.path.exists(path):
    print("  no report written")
    raise SystemExit
rows = json.load(open(path))["install"]
print(f"  {len(rows)} packages would install")
for i in rows:
    m = i["metadata"]
    if not wanted or m["name"].lower().replace("_", "-") in wanted:
        print("  ", m["name"], m["version"], i["download_info"]["url"].rsplit("/", 1)[-1])
EOF

echo "--- dry-run: mlx-lm + outlines pins"
"$S/v/bin/pip" install --dry-run --quiet --report "$S/rep.json" "mlx-lm==0.32.2" "outlines==1.3.3" > "$S/out.txt" 2>&1
echo "pip exit=$?"
tail -15 "$S/out.txt"
"$S/v/bin/python" "$S/show.py" "$S/rep.json" mlx mlx-lm mlx-metal outlines outlines-core transformers torch numpy

echo "--- dry-run: the five tree-sitter pins"
"$S/v/bin/pip" install --dry-run --quiet --report "$S/rep2.json" tree-sitter==0.26.0 tree-sitter-javascript==0.25.0 tree-sitter-typescript==0.23.2 tree-sitter-python==0.25.0 tree-sitter-java==0.23.5 > "$S/out2.txt" 2>&1
echo "pip exit=$?"
tail -5 "$S/out2.txt"
"$S/v/bin/python" "$S/show.py" "$S/rep2.json"

rm -rf "$S"
