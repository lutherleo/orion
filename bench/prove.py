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

Gate 3 of the merge plan (Phase 6, D5): after the legacy table it prints the SAME results scored by eval's
frozen matcher (`eval/devbench.py`: file + function or line +/-10, one-to-one, against the fixture-read keys
in eval/dataset/devbench/). `--rescore` prints only that, for the results already on disk (token-free).

That comparison is made at L2 (location + CWE family): with 15-16 bugs per repo, L1 alone credits a finding
of another class near a fix site, and the committed C rows are prose with no CWE. So the run also has arm
**P**: eval's own plain frontier arm (`eval/arms/plain.py`, the frozen prompt and findings.json schema, one
CWE per finding, model and effort from `eval/run.py`), run on a .git-free export of the pinned fixture in an
opaque temp dir outside any repo. `--no-plain` skips it. It needs eval's clean Claude home with a copied
login (eval/README.md, one-time step); the preflight checks for it.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

FIXTURES = {"nodegoat": "fixtures/NodeGoat", "pygoat": "fixtures/pygoat"}
RESULTS = _ROOT / "bench" / "research"
PLAIN_ARM = "plain-opus5"            # arm P: eval's Opus-alone arm


def _check_claude() -> str | None:
    exe = shutil.which("claude")
    if exe is None:
        return "`claude` CLI not on PATH -- install Claude Code and log in (`claude` once, interactively)"
    try:
        r = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"`claude --version` failed to run: {exc}"
    return None if r.returncode == 0 else f"`claude --version` exited {r.returncode}: {r.stderr.strip()[:200]}"


def _check_claude_login() -> str | None:
    """`claude auth status` (local, token-free) must not say loggedIn=false, unless ANTHROPIC_API_KEY is set.
    An older CLI without the subcommand, or output that is not JSON, is not counted as a failure."""
    import os
    exe = shutil.which("claude")
    if exe is None or os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        r = subprocess.run([exe, "auth", "status"], capture_output=True, text=True, timeout=30)
        status = json.loads(r.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if isinstance(status, dict) and status.get("loggedIn") is False:
        return "`claude` is not logged in -- run `claude` once and log in (or set ANTHROPIC_API_KEY)"
    return None


def _check_plain_login() -> str | None:
    from eval.arms.plain import CONFIG_ROOT, config_home
    cred = Path(config_home(PLAIN_ARM, CONFIG_ROOT)) / ".credentials.json"
    if cred.is_file() and cred.stat().st_size > 0:
        return None
    return (f"eval's clean Claude home has no login ({cred}) -- do the one-time auth copy in eval/README.md, "
            "or pass --no-plain")


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


def preflight(benchmarks: list[str], *, plain: bool = False) -> list[str]:
    """Every missing prerequisite, as an actionable message ([] = ready). Token-free."""
    checks = [_check_claude, _check_claude_login, _check_neo4j, _check_joern] + [
        (lambda b=b: _check_fixture(b)) for b in benchmarks] + ([_check_plain_login] if plain else [])
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


def _quiet(line: str) -> None:
    if not line.lstrip().startswith("{"):          # the CLI's stream-json is in usage.jsonl; show the rest
        print(f"  [P] {line}")


def run_plain(benchmark: str, *, arm: str = PLAIN_ARM, results: Path = RESULTS, on_line=_quiet,
              _plain=None, _export=None) -> Path:
    """Arm P on one benchmark: export the fixture's HEAD with no .git into a fresh opaque temp dir (checked
    by eval's leak guard: no CLAUDE.md or .git above it), run eval's plain arm there, delete the tree, and
    write bench/research/<benchmark>/P-<arm>.json (status, usage blocks as measured, the structured
    findings; the raw findings.json and usage.jsonl stay in bench/research/<benchmark>/P-<arm>/)."""
    from eval import repo
    from eval.arms.plain import plain_arm
    from eval.run import EFFORT, MODEL_TAGS, _leak_guard
    from eval.shim_setup import shim_dir
    fixture = _ROOT / FIXTURES[benchmark]
    sha = subprocess.run(["git", "-C", str(fixture), "rev-parse", "HEAD"], capture_output=True, text=True,
                         check=True).stdout.strip()
    run_dir = results / benchmark / f"P-{arm}"
    run_dir.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="w"))
    tree = work / "tree"
    try:
        _leak_guard(str(tree))
        (_export or repo.export_tree)(str(fixture), sha, str(tree))
        t0 = time.monotonic()
        res = (_plain or plain_arm)(arm=arm, repo_dir=str(tree), model_tag=MODEL_TAGS[arm], effort=EFFORT[arm],
                                    json_out=str(run_dir / "findings.json"), usage_log=str(run_dir / "usage.jsonl"),
                                    shim_dir=shim_dir(), base_env=dict(os.environ),
                                    on_line=on_line)
        wall = round(time.monotonic() - t0, 1)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    findings = []
    if res.findings_path:
        findings = json.loads(Path(res.findings_path).read_text(encoding="utf-8")).get("findings") or []
    out = results / benchmark / f"P-{arm}.json"
    out.write_text(json.dumps({"arm": "P", "label": f"P-{arm}", "eval_arm": arm, "benchmark": benchmark,
                               "fixture_commit": sha, "model": MODEL_TAGS[arm], "effort": EFFORT[arm],
                               "status": res.status, "usage": res.usage, "wallclock_s": wall,
                               "findings": findings}, indent=2, default=str), encoding="utf-8")
    return out


def eval_table(benchmarks: list[str], labels: list[str]) -> str:
    """The committed C / D rows, arm P and the given O rows, scored by eval's matcher (eval/devbench.py)."""
    from eval import devbench
    out = []
    for b in benchmarks:
        data = devbench.load_keys(b)
        results = []
        for name in ["C", "D", f"P-{PLAIN_ARM}", *labels]:
            f = RESULTS / b / f"{name}.json"
            if f.exists():
                label, findings = devbench.findings_from(str(f))
                results.append((name, devbench.score(findings, data)))
        out += ["", devbench.table(b, data, results)]
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Measure Orion against the committed baselines")
    ap.add_argument("--models", default="sonnet,opus", help="comma-separated Claude models for arm O")
    ap.add_argument("--benchmarks", default="nodegoat,pygoat", help="comma-separated: nodegoat,pygoat")
    ap.add_argument("--dry-run", action="store_true", help="check prerequisites only (no tokens)")
    ap.add_argument("--rescore", action="store_true",
                    help="score the results already on disk with eval's matcher, run nothing (no tokens)")
    ap.add_argument("--no-plain", action="store_true",
                    help=f"skip arm P (eval's {PLAIN_ARM} arm, the Opus-alone row that reports CWEs)")
    args = ap.parse_args(argv)
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    benchmarks = [b.strip() for b in args.benchmarks.split(",") if b.strip()]
    unknown = [b for b in benchmarks if b not in FIXTURES]
    if unknown:
        print(f"unknown benchmark(s): {', '.join(unknown)} (known: {', '.join(FIXTURES)})", file=sys.stderr)
        return 2
    labels = [f"O-{m}" for m in models]
    if args.rescore:
        print(eval_table(benchmarks, labels))
        return 0

    problems = preflight(benchmarks, plain=not args.no_plain)
    for p in problems:
        print(f"  MISSING  {p}")
    if problems:
        print(f"\npreflight: {len(problems)} prerequisite(s) missing -- nothing was run.")
        return 1
    print("preflight: all prerequisites present.")
    if args.dry_run:
        return 0

    from bench import plot_results, research_eval
    for m, label in zip(models, labels):
        for b in benchmarks:
            print(f"\n=== arm O ({m}) on {b} ===")
            rc = research_eval.main(["--arm", "O", "--benchmark", b, "--repo", str(_ROOT / FIXTURES[b]),
                                     "--model", m, "--label", label, "--quiet"])
            if rc != 0:
                print(f"research_eval failed (rc={rc}); stopping", file=sys.stderr)
                return rc
    if not args.no_plain:
        for b in benchmarks:
            print(f"\n=== arm P ({PLAIN_ARM}, eval's plain arm) on {b} ===")
            out = run_plain(b)
            status = json.loads(out.read_text(encoding="utf-8"))["status"]
            print(f"[P] {b}: status {status}, wrote {out}")
            if status != "ok":
                print(f"arm P did not finish ok ({status}): its row is not a clean result", file=sys.stderr)
    try:
        plot_results.main(["--dir", str(RESULTS)])
    except Exception as exc:  # noqa: BLE001 -- plots are a convenience, the JSONs are the evidence
        print(f"(plots skipped: {exc})", file=sys.stderr)
    print("\nPaste into bench/research/REPORT.md, then commit bench/research/*/O-*.json, P-*.json + the plots:")
    print(table(benchmarks, labels))
    print("\nThe same results through eval's frozen matcher (Gate 3):")
    print(eval_table(benchmarks, labels))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
