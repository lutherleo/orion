"""Before/after flows per fixture and category, then every sink that was added, dropped or re-filed.
Usage: compare_flows.py <before_dir> <after_dir> [detail]. Reads the *.flows.json dump_flows.py wrote."""
import collections
import json
import sys
from pathlib import Path

before, after = Path(sys.argv[1]), Path(sys.argv[2])
detail = len(sys.argv) > 3
ORDER = ["code_exec", "deserialize", "sql", "nosql", "ssrf", "template", "xxe", "prototype_pollution", "path",
         "redirect", "render", "log"]


def load(d, which):
    return json.loads((d / f"{which}.flows.json").read_text())


# A sink's identity across the two dumps: the uid hashes only the "golden" scan id and the location, so
# it is stable between builds of the same cpg.bin.
for which in ("nodegoat.default", "nodegoat.lm", "pygoat.default", "pygoat.lm"):
    a, b = load(before, which), load(after, which)
    ca, cb = collections.Counter(r["cat"] for r in a), collections.Counter(r["cat"] for r in b)
    print(f"\n== {which}: {len(a)} -> {len(b)}")
    for c in ORDER:
        if ca[c] or cb[c]:
            print(f"   {c:<20}{ca[c]:>4} -> {cb[c]:<4}")
    ua, ub = {r["uid"]: r for r in a}, {r["uid"]: r for r in b}
    added = [r for u, r in ub.items() if u not in ua]
    dropped = [r for u, r in ua.items() if u not in ub]
    moved = [(ua[u], ub[u]) for u in ua.keys() & ub.keys() if ua[u]["cat"] != ub[u]["cat"]]
    same = sum(1 for u in ua.keys() & ub.keys() if ua[u]["cat"] == ub[u]["cat"])
    print(f"   same sink and category {same}, added {len(added)}, dropped {len(dropped)}, re-filed {len(moved)}")
    print(f"   top 5 after: " + "; ".join(f'{r["cat"]} {str(r["file"]).rsplit("/", 1)[-1]}:{r["line"]}' for r in b[:5]))
    if detail:
        for r in sorted(added, key=lambda r: r["rank"]):
            print(f'   + {r["cat"]:<10} rank {r["rank"]:>3} {r["file"]}:{r["line"]} {r["name"]} | {str(r["code"]).splitlines()[0][:80]}')
        for r in sorted(dropped, key=lambda r: (r["cat"], str(r["file"]), r["line"] or 0)):
            print(f'   - {r["cat"]:<10} {r["file"]}:{r["line"]} {str(r["name"])[:26]} | {str(r["code"]).splitlines()[0][:80]}')
        for x, y in moved:
            print(f'   ~ {x["cat"]} -> {y["cat"]} {y["file"]}:{y["line"]} {y["name"]} | {str(y["code"]).splitlines()[0][:80]}')
