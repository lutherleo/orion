#!/usr/bin/env python
"""Prove the value: run Orion (arm O) through the SAME matcher + token ledger as the committed
Opus-alone (C) and Semgrep (D) rows, then print the comparison table.

    python bench/prove.py --dry-run                 # token-free: check every prerequisite, run nothing
    python bench/prove.py                           # arm O x {sonnet, opus} x {nodegoat, pygoat}
    python bench/prove.py --models sonnet --benchmarks nodegoat

Writes bench/research/<benchmark>/O-<model>.json per run (research_eval), refreshes the plots
(plot_results), and prints a markdown table ready for bench/research/REPORT.md. Every preflight check
is token-free; the runs themselves spend Claude tokens -- that is the point of the measurement.
Stops at the first failed prerequisite and says how to fix it.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

FIXTURES = {"nodegoat": "fixtures/NodeGoat", "pygoat": "fixtures/pygoat"}
RESULTS = _ROOT / "bench" / "research"


def _check_claude() -> str | None:
    exe = shutil.which("claude")
    if exe is None:
        return "`claude` CLI not on PATH -- install Claude Code and log in (`claude` once, interactively)"
    try:
        r = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"`claude --version` failed to run: {exc}"
    return None if r.returncode == 0 else f"`claude --version` exited {r.returncode}: {r.stderr.strip()[:200]}"


def _check_neo4j() -> str | None:
    try:
        from orion.graphdb import GraphDB
        db = GraphDB()
        try:
            db.ping()
        finally:
            db.close()
        return None
    except Exception as exc:  # noqa: BLE001
        return f"Neo4j not reachable ({type(exc).__name__}) -- start it: `docker compose up -d`"


def _check_joern() -> str | None:
    from orion import config
    home = Path(config.JOERN_HOME)
    if any((home / n).exists() for n in ("joern-parse", "joern-parse.bat")):
        return None
    return f"Joern not found at {home} -- install joern-cli there or set JOERN_HOME"


def _check_fixture(benchmark: str) -> str | None:
    path = _ROOT / FIXTURES[benchmark]
    if path.is_dir():
        return None
    return f"{FIXTURES[benchmark]} missing -- clone the {benchmark} fixture into orion/fixtures/"


def preflight(benchmarks: list[str]) -> list[str]:
    """Every missing prerequisite, as an actionable message ([] = ready). Token-free."""
    checks = [_check_claude, _check_neo4j, _check_joern] + [
        (lambda b=b: _check_fixture(b)) for b in benchmarks]
    return [msg for msg in (c() for c in checks) if msg]


def _row(label: str, r: dict) -> str:
    tot = (r.get("tokens") or {}).get("total") or {}
    toks = tot.get("total_tokens", 0)
    cost = tot.get("priced_cost_usd", 0.0)
    dec = r.get("decisions") or {}
    verdicts = f" ({dec.get('CONFIRM', 0)}C/{dec.get('INCONCLUSIVE', 0)}I/{dec.get('REJECT', 0)}R/" \
               f"{dec.get('ERROR', 0)}E)" if dec else ""
    return (f"| {label} | {r.get('recall', 0)} / {r.get('total', 0)} | "
            f"{r.get('false_positive_candidates', 0)}{verdicts} | "
            f"{toks / 1000:.0f}k / ${cost:.2f} | {r.get('wallclock_s', 0):.0f}s |")


def table(benchmarks: list[str], labels: list[str]) -> str:
    """Markdown comparison: the new O rows beside the committed C (Opus alone) and D (Semgrep) rows."""
    out = []
    for b in benchmarks:
        out += [f"\n**{b}**\n", "| Arm | Recall | FP candidates (verdicts) | Tokens / cost | Wall-clock |",
                "|---|---|---|---|---|"]
        for label, name in [("Opus 5, no Orion (C)", "C"), ("Semgrep (D)", "D")] + \
                           [(f"Orion + {lbl.partition('-')[2] or 'Claude'}", lbl) for lbl in labels]:
            f = RESULTS / b / f"{name}.json"
            if f.exists():
                out.append(_row(label, json.loads(f.read_text(encoding="utf-8"))))
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Measure Orion against the committed baselines")
    ap.add_argument("--models", default="sonnet,opus", help="comma-separated Claude models for arm O")
    ap.add_argument("--benchmarks", default="nodegoat,pygoat", help="comma-separated: nodegoat,pygoat")
    ap.add_argument("--dry-run", action="store_true", help="check prerequisites only (no tokens)")
    args = ap.parse_args(argv)
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    benchmarks = [b.strip() for b in args.benchmarks.split(",") if b.strip()]
    unknown = [b for b in benchmarks if b not in FIXTURES]
    if unknown:
        print(f"unknown benchmark(s): {', '.join(unknown)} (known: {', '.join(FIXTURES)})", file=sys.stderr)
        return 2

    problems = preflight(benchmarks)
    for p in problems:
        print(f"  MISSING  {p}")
    if problems:
        print(f"\npreflight: {len(problems)} prerequisite(s) missing -- nothing was run.")
        return 1
    print("preflight: all prerequisites present.")
    if args.dry_run:
        return 0

    from bench import plot_results, research_eval
    labels = [f"O-{m}" for m in models]
    for m, label in zip(models, labels):
        for b in benchmarks:
            print(f"\n=== arm O ({m}) on {b} ===")
            rc = research_eval.main(["--arm", "O", "--benchmark", b, "--repo", str(_ROOT / FIXTURES[b]),
                                     "--model", m, "--label", label, "--quiet"])
            if rc != 0:
                print(f"research_eval failed (rc={rc}); stopping", file=sys.stderr)
                return rc
    try:
        plot_results.main(["--dir", str(RESULTS)])
    except Exception as exc:  # noqa: BLE001 -- plots are a convenience, the JSONs are the evidence
        print(f"(plots skipped: {exc})", file=sys.stderr)
    print("\nPaste into bench/research/REPORT.md, then commit bench/research/*/O-*.json + the plots:")
    print(table(benchmarks, labels))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
