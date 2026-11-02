"""`python -m eval.repoqa run --resume --repo ethereum/web3.py --repo microsoft/gctoolkit`, with `orion` imported
from the fix's worktree. eval/repoqa.py exists only (uncommitted) in the main checkout, so it runs from there by
path -- its default --out is that checkout's eval/results/repoqa-pilot.json, the file being resumed."""
import runpy
import sys

WORKTREE = "/mnt/c/Users/h/Downloads/orion/.claude/worktrees/happy-meninsky-ac2c8a"
MAIN = "/mnt/c/Users/h/Downloads/orion"
sys.path.insert(0, WORKTREE)
import orion  # noqa: E402
from orion import embed  # noqa: E402

assert orion.__file__.startswith(WORKTREE), orion.__file__
assert hasattr(embed, "_encode_split"), "the worktree's embed.py has no fallback"
print("orion from", orion.__file__, flush=True)
sys.argv = ["eval.repoqa", "run", "--resume", "--repo", "ethereum/web3.py", "--repo", "microsoft/gctoolkit"]
runpy.run_path(f"{MAIN}/eval/repoqa.py", run_name="__main__")
