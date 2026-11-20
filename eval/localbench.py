"""NodeGoat and PyGoat on local small models: the lm-<alias> arms (eval/localmodels.py) on Orion's two dev
fixtures, scored by eval's matcher (eval/devbench.py). VLB and BountyBench go through eval.extbench instead
(`--arms lm-<alias>,...`).

    python -m eval.localbench run   --arms lm-orion-lm-base,lm-orion-lm [--benchmarks nodegoat,pygoat] [--runs 1] [--dry-run]
    python -m eval.localbench score [--run-no 1] [--json]

Each run scans the fixture in place (`orion scan fixtures/<X> --engine lm --model <path> --lm-verify`) and writes
eval/runs-dev/<benchmark>/<arm>/<run>/run.json, with the scan's own run dir under it (.orion/runs/). Re-running skips `ok` runs. The
engine reports no tokens, so only wall time and the progress-log `behaviour` counts are recorded. Dev evidence
only: both apps are old and public, and the keys are proposed, not signed.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from eval import devbench, localmodels
from eval.arms.launch import stream_subprocess
from eval.run import git_head

ROOT = Path(__file__).resolve().parent.parent
RUNS_ROOT = ROOT / "eval" / "runs-dev"
RESULTS_ROOT = ROOT / "eval" / "results"
FIXTURES = {"nodegoat": ROOT / "fixtures" / "NodeGoat", "pygoat": ROOT / "fixtures" / "pygoat"}
LEADS = ":leads"


def run_dir_for(benchmark: str, arm: str, run_no: int, root: Path = RUNS_ROOT) -> Path:
    return root / benchmark / arm / str(run_no)


def _record(d: Path) -> dict | None:
    try:
        return json.loads((d / "run.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _scan_dir(d: Path) -> Path | None:
    """The scan's own run dir: `orion scan` writes .orion/runs/<scan_id>/<ts>/ under its cwd, the run dir."""
    found = sorted((d / ".orion" / "runs").glob("*/*/leads.json"))
    return found[-1].parent if found else None


def run(arms: list[str], benchmarks: list[str], *, runs: int = 1, root: Path = RUNS_ROOT, dry_run: bool = False,
        on_line=lambda l: print(f"    {l}"[:300], flush=True), _stream=stream_subprocess,
        fixtures: dict[str, Path] | None = None) -> dict:
    fixtures = fixtures or FIXTURES
    bad = [a for a in arms if not localmodels.is_lm(a)]
    if bad:
        raise ValueError(f"not lm arms: {bad} (use lm-<alias> from {localmodels.REGISTRY.relative_to(ROOT)})")
    models = {a: localmodels.model_path(a, check=not dry_run) for a in arms}
    missing = [b for b in benchmarks if not (fixtures[b]).is_dir()]
    if missing and not dry_run:
        raise ValueError(f"fixture(s) not checked out: {', '.join(str(fixtures[b]) for b in missing)}")
    commit = git_head(str(ROOT))
    counts = {"ok": 0, "failed": 0, "skipped": 0}
    items = [(a, b, n) for a in arms for b in benchmarks for n in range(1, runs + 1)]   # one model loaded at a time
    for i, (arm, bench, n) in enumerate(items, 1):
        d = run_dir_for(bench, arm, n, root)
        tag = f"[{i}/{len(items)}] {arm} {bench} run {n}"
        if (_record(d) or {}).get("status") == "ok":
            print(f"{tag}: ok already, skipped", flush=True)
            counts["skipped"] += 1
            continue
        cmd = localmodels.scan_cmd(str(fixtures[bench]), models[arm], d / "verdicts.json")
        if dry_run:
            print(f"{tag}\n    argv {' '.join(cmd)}", flush=True)
            continue
        d.mkdir(parents=True, exist_ok=True)
        procs, t0 = [], time.monotonic()
        print(f"{tag}: {models[arm]} -> {d}", flush=True)
        status = _stream(cmd, env=localmodels.scan_env(dict(os.environ)), cwd=str(d), on_line=on_line,
                         on_start=procs.append)
        rc = procs[0].returncode if procs else None
        if status == "crashed" and rc == 3 and (d / "verdicts.json").is_file():
            status = "ok"            # exit 3: some batches or verdicts ended ERROR; the verdicts are still a result
        odir = _scan_dir(d)
        rec = {"benchmark": bench, "arm": arm, "model": models[arm], "run_no": n, "status": status, "rc": rc,
               "incomplete": rc == 3, "wall_s": round(time.monotonic() - t0, 1), "orion_commit": commit,
               "orion_run_dir": str(odir.relative_to(d)) if odir else None, "usage_total": localmodels.NO_USAGE,
               "behaviour": localmodels.behaviour(odir / "progress.jsonl") if odir else {}}
        (d / "run.json").write_text(json.dumps(rec, indent=1) + "\n", encoding="utf-8")
        print(f"{tag}: {status} in {rec['wall_s']}s, {rec['behaviour'].get('graph_queries')} graph queries",
              flush=True)
        counts["ok" if status == "ok" else "failed"] += 1
    return counts


def _findings(arm: str, rec: dict, d: Path) -> list[dict] | None:
    odir = d / rec["orion_run_dir"] if rec.get("orion_run_dir") else None
    if odir is None or not (odir / "leads.json").is_file():
        return None
    if arm.endswith(LEADS):
        leads = json.loads((odir / "leads.json").read_text(encoding="utf-8")).get("leads") or []
        p = d / "leads_only.json"
        p.write_text(json.dumps(leads), encoding="utf-8")
        return devbench.findings_from(str(p), arm=arm)[1]
    return devbench.findings_from(str(odir), arm=arm)[1]


def score(run_no: int = 1, root: Path = RUNS_ROOT) -> dict:
    out = {"evidence": "dev (old public apps, proposed keys, small N)", "run_no": run_no, "benchmarks": {}}
    for bench in devbench.BENCHMARKS:
        base = root / bench
        arms = sorted(p.name for p in base.iterdir() if p.is_dir()) if base.is_dir() else []
        data = devbench.load_keys(bench)
        rows, extra = [], {}
        for arm in arms:
            d = run_dir_for(bench, arm, run_no, root)
            rec = _record(d)
            if not rec or rec.get("status") != "ok":
                extra[arm] = {"missing": True}
                continue
            for label in (arm, arm + LEADS):
                f = _findings(label, rec, d)
                if f is not None:
                    rows.append((label, devbench.score(f, data)))
            extra[arm] = {"wall_s": rec.get("wall_s"), "incomplete": rec.get("incomplete"),
                          "behaviour": rec.get("behaviour")}
        out["benchmarks"][bench] = {"table": devbench.table(bench, data, rows) if rows else "(no ok runs)",
                                    "scores": {lbl: s for lbl, s in rows}, "runs": extra}
    return out


def behaviour_table(result: dict) -> str:
    lines = ["| Benchmark | Arm | wall s | graph queries | warnings | verdicts | incomplete |",
             "|---|---|---|---|---|---|---|"]
    for bench, b in result["benchmarks"].items():
        for arm, r in b["runs"].items():
            if r.get("missing"):
                lines.append(f"| {bench} | {arm} | missing | - | - | - | - |")
                continue
            bh = r.get("behaviour") or {}
            warns = ", ".join(f"{k} {v}" for k, v in (bh.get("warn_kinds") or {}).items()) or "0"
            dec = ", ".join(f"{k} {v}" for k, v in (bh.get("decisions") or {}).items()) or "-"
            lines.append(f"| {bench} | {arm} | {r.get('wall_s')} | {bh.get('graph_queries', '-')} | {warns} | {dec} "
                         f"| {'yes' if r.get('incomplete') else 'no'} |")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="NodeGoat / PyGoat on local small models (Orion-LM engine)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run lm arms on the fixtures (local compute only, no tokens)")
    r.add_argument("--arms", required=True, help="comma list of lm-<alias> (eval/dataset/local_models.json)")
    r.add_argument("--benchmarks", default=",".join(devbench.BENCHMARKS))
    r.add_argument("--runs", type=int, default=1)
    r.add_argument("--dry-run", action="store_true")
    c = sub.add_parser("score", help="score what is on disk")
    c.add_argument("--run-no", type=int, default=1)
    c.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "run":
        counts = run([x.strip() for x in a.arms.split(",") if x.strip()],
                     [x.strip() for x in a.benchmarks.split(",") if x.strip()], runs=a.runs, dry_run=a.dry_run)
        print(f"done: {counts}")
        return 0 if not counts["failed"] else 1
    res = score(a.run_no)
    RESULTS_ROOT.mkdir(parents=True, exist_ok=True)
    stem = RESULTS_ROOT / "localbench"
    stem.with_suffix(".json").write_text(json.dumps(res, indent=1, default=str) + "\n", encoding="utf-8")
    text = "\n\n".join([b["table"] for b in res["benchmarks"].values()] + [behaviour_table(res)])
    stem.with_suffix(".md").write_text(text + "\n", encoding="utf-8")
    print(json.dumps(res, indent=1, default=str) if a.json else text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
