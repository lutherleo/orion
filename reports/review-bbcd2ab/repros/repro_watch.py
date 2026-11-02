"""Ctrl-C during `orion scan --resume <dir> --watch` (claude engine). Token-free: `claude -p` is faked.

    python reports/review-bbcd2ab/repros/repro_watch.py [--no-watch] [ORION_CHECKOUT]

A fake `claude -p` blocks until "Ctrl-C" lands, then ends as a real interrupted one does (exit 0, an
error_during_execution result: measured, see claude_interrupt_exit.py); every later launch ends the same way at once
(a real retry would start a fresh session that the Ctrl-C never reached, and run on).
Ctrl-C itself is _thread.interrupt_main(): a KeyboardInterrupt in the main thread, where SIGINT's handler raises
it. The retry backoff is left at its real value, so the main thread has seconds to flag the stop.

A run that stops cleanly: <= 4 launches (the verify concurrency; no retry), interrupted() True, the CLI raises
KeyboardInterrupt, no verdicts.json, no verdict logged.
"""
import _thread
import json
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

args = [a for a in sys.argv[1:] if a != "--no-watch"]
watch = "--no-watch" not in sys.argv[1:]
checkout = Path(args[0]) if args else Path(__file__).resolve().parents[3]
sys.path.insert(0, str(checkout))

from orion import claude_cli, cli, embed   # noqa: E402
from orion.graph import scans              # noqa: E402

work = Path(tempfile.mkdtemp(prefix="repro-watch-"))
repo = work / "repo"
repo.mkdir()
run_dir = work / "run"
run_dir.mkdir()
leads = [{"index": i, "shape": "A", "text": f"claim {i}", "evidence": "e", "confidence": "HIGH"} for i in range(6)]
(run_dir / "leads.json").write_text(json.dumps({"scan_id": "s1", "repo": str(repo), "engine": "claude",
                                                "model": "sonnet", "leads": leads}), encoding="utf-8")

scans.check_usable = lambda *a, **k: None            # no Neo4j: the run's scan record is taken as usable
embed.ensure_exploit_index = lambda: False           # no embedding model load

ABORTED = '{"type":"result","subtype":"error_during_execution","is_error":true}\n'
killed = threading.Event()
launches: list[float] = []
lock = threading.Lock()
real_run = subprocess.run


def fake_run(cmd, **kw):
    if "claude" not in Path(str(cmd[0])).name.lower():      # anything else the CLI runs is left alone
        return real_run(cmd, **kw)
    with lock:
        launches.append(time.monotonic())
        first = len(launches) == 1
    if first:
        threading.Timer(0.5, lambda: (_thread.interrupt_main(), killed.set())).start()   # Ctrl-C mid-session
    killed.wait(30)
    return subprocess.CompletedProcess(cmd, 0, stdout=ABORTED, stderr="")


claude_cli.subprocess.run = fake_run

t0 = time.monotonic()
argv = ["scan", "--resume", str(run_dir), "--quiet"] + (["--watch"] if watch else [])
try:
    code = cli.main(argv)
    outcome = f"returned exit code {code}"
except KeyboardInterrupt:
    outcome = "raised KeyboardInterrupt"
log = run_dir / "verdicts.jsonl"
print("\n==== RESULT", "(--watch)" if watch else "(no --watch)", "====")
print("orion code:", Path(sys.modules["orion"].__file__).parent)
print("cli.main:", outcome, f"after {time.monotonic() - t0:.1f}s")
print("claude -p launches:", len(launches), "(6 leads; no retry means <= 4, the verify concurrency)")
print("claude_cli.interrupted():", claude_cli.interrupted())
print("verdicts.json written:", (run_dir / "verdicts.json").exists())
print("verdicts.jsonl lines:", len(log.read_text().splitlines()) if log.exists() else 0)
