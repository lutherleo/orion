"""Write each `run:` of one workflow job to <out>/step_NN.sh, exactly as the YAML parses it."""
import os
import sys

import yaml

wf, job, out = sys.argv[1:4]
doc = yaml.safe_load(open(wf, encoding="utf-8"))
n = 0
for step in doc["jobs"][job]["steps"]:
    if "run" not in step:
        print("skip (uses):", step.get("uses"))
        continue
    n += 1
    with open(os.path.join(out, f"step_{n:02d}.sh"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(step["run"] + "\n")
    print(f"step {n:02d}: {step['run']}")
