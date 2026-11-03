"""Compare outcomes test by test between two junit files: `ci_compare.py <a.xml> <b.xml>`."""
import collections
import sys
import xml.etree.ElementTree as ET


def outcomes(path):
    res = {}
    for c in ET.parse(path).getroot().iter("testcase"):
        kind = "passed"
        for child in c:
            if child.tag in ("failure", "error"):
                kind = "failed"
            elif child.tag == "skipped":
                kind = "skipped"
        res[f"{c.get('classname')}::{c.get('name')}"] = kind
    return res


a, b = outcomes(sys.argv[1]), outcomes(sys.argv[2])
print(f"{sys.argv[1].rsplit('/', 1)[-1]} ({len(a)}) vs {sys.argv[2].rsplit('/', 1)[-1]} ({len(b)})")
print("  only in first:", sorted(set(a) - set(b))[:10], "| only in second:", sorted(set(b) - set(a))[:10])
moved = collections.Counter()
by_file = collections.Counter()
for k in sorted(set(a) & set(b)):
    if a[k] != b[k]:
        moved[f"{a[k]} -> {b[k]}"] += 1
        by_file[(f"{a[k]} -> {b[k]}", k.split("::")[0].rsplit(".", 1)[-1])] += 1
print("  outcome changes:", dict(moved) or "none")
for (m, f), n in sorted(by_file.items()):
    print(f"    {m}: {f} x{n}")
