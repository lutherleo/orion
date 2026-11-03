"""Re-record the two NodeGoat goldens from this box's cpg.bin, with the code on PYTHONPATH (cwd = ~/orion)."""
from tests.test_graph_snapshot import write_golden

for variant in ("default", "lm"):
    print("wrote", write_golden("nodegoat", variant), flush=True)
