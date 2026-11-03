"""Does this box's cpg.bin reproduce the committed goldens at HEAD? Reports FLOWS_TO and flow counts; writes nothing."""
import json
import sys
import tempfile
from pathlib import Path

from tests import graph_snapshot
from tests.test_graph_snapshot import GOLDEN, snapshot

import orion
print("orion from", orion.__file__, "| tests.graph_snapshot from", graph_snapshot.__file__, flush=True)
for name, repo in (("nodegoat", "fixtures/NodeGoat"), ("pygoat", "fixtures/pygoat")):
    for variant in ("default", "lm"):
        gold = json.loads((GOLDEN / f"{name}_{variant}.json").read_text())
        with tempfile.TemporaryDirectory() as tmp:
            got = snapshot(repo, f"{repo}/cpg.bin", tmp, variant == "lm")
        d = graph_snapshot.diff(gold, got)
        keys = list(gold)
        print(name, variant, "golden keys", keys,
              "| counts gold", {k: len(gold[k]) for k in keys}, "| here", {k: len(got[k]) for k in keys},
              "| matches" if not d else "| DIFFERS: " + json.dumps({k: {kk: len(vv) for kk, vv in v.items()} if isinstance(v, dict) else v
                                                                     for k, v in d.items()})[:300], flush=True)
