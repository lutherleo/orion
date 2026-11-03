"""Skip reasons per junit file, with run-specific details removed so equal reasons group together."""
import collections
import re
import sys
import xml.etree.ElementTree as ET

for path in sys.argv[1:]:
    reasons = collections.Counter()
    for c in ET.parse(path).getroot().iter("testcase"):
        for child in c:
            if child.tag == "skipped":
                m = child.get("message") or ""
                m = re.sub(r"^Skipped: ", "", m)
                m = re.sub(r"Neo4j not reachable.*", "Neo4j not reachable", m, flags=re.S)
                m = re.sub(r"under /tmp/\S+", "under $HOME/joern/joern-cli", m)
                reasons[m[:100]] += 1
    print(path.rsplit("/", 1)[-1], sum(reasons.values()), "skipped")
    for r, n in reasons.most_common():
        print(f"  {n:3}  {r}")
