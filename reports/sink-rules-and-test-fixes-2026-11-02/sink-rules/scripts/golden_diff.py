"""The re-recorded NodeGoat goldens against HEAD's: is FLOWS_TO identical, and are the flows a subset?
A flow row is [sink, category, rank, path]."""
import json
import subprocess

for variant in ("default", "lm"):
    path = f"tests/golden/nodegoat_{variant}.json"
    old = json.loads(subprocess.run(["git", "show", f"e755f75:{path}"], capture_output=True, check=True).stdout)
    new = json.load(open(path, encoding="utf-8"))
    print(variant, "FLOWS_TO identical:", old["flows_to"] == new["flows_to"], len(new["flows_to"]))

    def key(r):
        return json.dumps([r[0], r[1], r[3]])

    a, b = {key(r) for r in old["candidate_flows"]}, {key(r) for r in new["candidate_flows"]}
    print(f"  flows {len(a)} -> {len(b)}; new rows not in old (rank aside): {len(b - a)}")
