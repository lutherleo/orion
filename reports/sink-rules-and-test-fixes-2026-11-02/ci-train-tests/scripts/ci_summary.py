"""Totals per junit file, and per test file for any file given with --by-file."""
import collections
import glob
import os
import sys
import xml.etree.ElementTree as ET


def outcome(case):
    for child in case:
        if child.tag in ("failure", "error"):
            return "failed"
        if child.tag == "skipped":
            return "skipped"
    return "passed"


out, label = sys.argv[1:3]
for path in sorted(glob.glob(os.path.join(out, f"{label}-*.xml"))):
    cases = list(ET.parse(path).getroot().iter("testcase"))
    tot = collections.Counter(outcome(c) for c in cases)
    print(f"{os.path.basename(path)}: {tot['failed']} failed, {tot['passed']} passed, {tot['skipped']} skipped, {len(cases)} total")
    if "train" in path or any("train.tests" in (c.get("classname") or "") for c in cases):
        by = collections.defaultdict(collections.Counter)
        reasons = collections.Counter()
        for c in cases:
            by[(c.get("classname") or "").split("::")[0].rsplit(".", 1)[-1]][outcome(c)] += 1
            for child in c:
                if child.tag == "skipped":
                    reasons[(child.get("message") or "")[:90]] += 1
        for k in sorted(by):
            v = by[k]
            print(f"   {k:28} {v['passed']:4} passed {v['skipped']:3} skipped {v['failed']:3} failed")
        for r, n in reasons.most_common():
            print(f"   skip x{n}: {r}")
