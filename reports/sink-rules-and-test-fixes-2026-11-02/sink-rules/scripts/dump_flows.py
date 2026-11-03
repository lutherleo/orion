"""Token-free, no Neo4j: rebuild each fixture's batch from cpg.bin, keep it pickled, dump the flows.
Usage (WSL, cwd ~/orion for the fixtures, PYTHONPATH = the worktree):
  python dump_flows.py <out_dir>
"""
import collections
import json
import pickle
import sys
import tempfile
from pathlib import Path

from orion.graph import joern_adapter, pathfind, profiles, reachability
from orion.graph import stream_build as S

out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
FIX = {"nodegoat": "fixtures/NodeGoat", "pygoat": "fixtures/pygoat"}

for name, repo in FIX.items():
    base = profiles.select_profile(repo)
    with tempfile.TemporaryDirectory() as tmp:
        env = S.build_envelope(f"{repo}/cpg.bin", tmp, base)
    b = joern_adapter.normalize(env, "golden", language="golden")
    reachability.tag_reachability(b)
    reachability.tag_centrality(b)
    flows_to = sum(1 for e in b.edges if e[0] == "FLOWS_TO")
    (out / f"{name}.batch.pkl").write_bytes(pickle.dumps(
        {"nodes": [n for n in b.nodes if n[0] == "CpgCall"],
         "edges": [e for e in b.edges if e[0] in ("FLOWS_TO", "ENTERS_AT", "CONTAINS_CALL")],
         "flows_to": flows_to}))
    for variant in ("default", "lm"):
        prof = profiles.with_lm_sinks(base) if variant == "lm" else base
        import copy
        bb = copy.copy(b)
        bb.nodes = list(b.nodes)
        summary = pathfind.pathfind(bb, prof)
        calls = {p["uid"]: p for l, p in bb.nodes if l == "CpgCall"}
        rows = []
        for l, p in bb.nodes:
            if l != "CandidateFlow":
                continue
            c = calls[p["sink_uid"]]
            rows.append({"rank": p["rank"], "cat": p["sink_category"], "file": c.get("file_path"),
                         "line": c.get("line"), "name": c.get("name"), "code": c.get("code"),
                         "path_len": len(json.loads(p["path_uids"])), "uid": p["sink_uid"]})
        rows.sort(key=lambda r: r["rank"])
        (out / f"{name}.{variant}.flows.json").write_text(json.dumps(rows, indent=0))
        counts = collections.Counter(r["cat"] for r in rows)
        print(name, variant, "profile", prof.name, "FLOWS_TO", flows_to, summary, dict(sorted(counts.items())))
