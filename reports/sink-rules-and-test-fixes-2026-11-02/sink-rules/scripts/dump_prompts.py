"""Every discovery system prompt this code renders, as JSON, for a byte compare between two checkouts.
Usage: PYTHONPATH=<checkout> python dump_prompts.py <out.json>"""
import itertools
import json
import sys

import orion
from orion import strategies
from orion.graph import profiles

print("orion from", orion.__file__)
out = {}
PROFILES = {"express": profiles.EXPRESS, "generic": profiles.GENERIC,
            "express+lm": profiles.with_lm_sinks(profiles.EXPRESS),
            "generic+lm": profiles.with_lm_sinks(profiles.GENERIC), "none": None}
for shape, (pname, prof), files in itertools.product(
        ("A", "B", "C", "D"), PROFILES.items(), ((), ("app/server.js", "views.py"))):
    out[f"{shape}|{pname}|{len(files)}"] = strategies.system_for(shape, "scan123", files, prof)
    for flag in ("dynamic_hint", "shortlist_hint"):
        out[f"{shape}|{pname}|{len(files)}|{flag}"] = strategies.system_for(shape, "scan123", files, prof, **{flag: True})
out["_mcp_schema"] = strategies.SCHEMA
json.dump(out, open(sys.argv[1], "w", encoding="utf-8", newline="\n"), ensure_ascii=False, indent=0, sort_keys=True)
print("prompts", len(out))
