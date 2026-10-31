"""External dev benchmarks run end to end: the SAME model with and without Orion, scored by eval's matcher.

    python -m eval.extbench select --benchmark vlb --pilot 5          # freeze a pilot task list (gh api, no clone)
    python -m eval.extbench run --benchmark vlb --arms plain-sonnet5,orion-sonnet5 [--runs 1] [--dry-run]
    python -m eval.extbench score --benchmark vlb [--json]

VLB (eval/vlb.py) and BountyBench (eval/bountybench.py) ship answer keys only; this module clones each task's
repo into a bare mirror (eval/mirrors/), exports a .git-free snapshot per run into an opaque temp dir that
eval's leak guard checks, runs the arm there, deletes the tree, and scores the results.

Arms:
  plain-sonnet5 / plain-opus5   eval's frozen plain arm (eval/arms/plain.py: prompt, findings.json schema,
                                clean Claude home, no web), exactly as eval.run launches it
  orion-sonnet5 / orion-opus5   `orion scan <tree>` with default options, on the plain twin's exact model id and
                                effort (eval/run.py MODEL_TAGS / EFFORT), so a gap between twins is Orion's
  orion-*:leads                 scored only: the same Orion run's leads.json, before verification

The Orion arm uses the operator's normal Claude home (the verifier's fp-check plugin lives there); the plain
arm uses eval's clean home. Runs are sequential, grouped by model (all sonnet runs, then all opus runs), and
each writes eval/runs-ext/<bench>/<arm>/<task>/<snapshot>/<run>[.aN]/run.json. A run whose output carries a
usage-limit signature is `aborted`: the driver waits --resume-wait seconds and retries it as a new attempt.
Re-running skips runs that are `ok`; Ctrl-C leaves no tree behind. Failed runs are MISSING when scored
(eval/CHANGELOG.md 2026-10-29). Dev evidence only: both benchmarks predate the models' cutoffs and their keys
are proposed, not signed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from eval import bountybench, devbench, repo, vlb
from eval.arms.launch import stream_subprocess
from eval.arms.plain import TASK_PROMPT, _build_cmd, plain_arm
from eval.run import _RAW_RE, EFFORT, MODEL_TAGS, TREE_ROOT_NAME, _leak_guard, git_head
from eval.shim_setup import shim_dir

ROOT = Path(__file__).resolve().parent.parent
RUNS_ROOT = ROOT / "eval" / "runs-ext"
MIRROR_ROOT = ROOT / "eval" / "mirrors"
RESULTS_ROOT = ROOT / "eval" / "results"
BENCHMARKS = ("vlb", "bountybench")
PLAIN = ("plain-sonnet5", "plain-opus5")
ORION = {"orion-sonnet5": "plain-sonnet5", "orion-opus5": "plain-opus5"}     # Orion arm -> its plain twin
ARMS = (*PLAIN, *ORION)
LEADS = ":leads"                    # score-time suffix: an Orion arm's leads before verification
PILOT_MIX = {"npm": 2, "pip": 2, "maven": 1}
# ORION_* behaviour knobs an operator's shell may carry; the Orion arm measures the defaults, so they are dropped
# (the model and effort are then pinned). Infrastructure knobs (Joern heap, Neo4j, the embedder) are kept.
ORION_OPTION_VARS = ("ORION_MODEL", "ORION_EFFORT", "ORION_MAX_TURNS", "ORION_VERIFY_MAX_TURNS", "ORION_CALL_TIMEOUT",
                     "ORION_VERIFY_CONCURRENCY", "ORION_DISCOVER_TIMEOUT", "ORION_DISCOVER_TIMEOUT_PER_NODE",
                     "ORION_DISCOVER_TIMEOUT_CAP", "ORION_VERIFY_TIMEOUT", "ORION_VERIFY_ROUTING",
                     "ORION_VERIFY_LIGHT_MAX_TURNS", "ORION_VERIFY_LIGHT_TIMEOUT", "ORION_VERIFY_LIGHT_EFFORT",
                     "ORION_VERIFY_CONTEXT", "ORION_SHORTLISTS", "ORION_SHORTLIST_INLINE", "ORION_SEMANTIC_MODE")


def model_of(arm: str) -> tuple[str, str]:
    """(exact model id, effort) of an arm: an Orion arm runs on its plain twin's."""
    twin = ORION.get(arm, arm)
    return MODEL_TAGS[twin], EFFORT[twin]


# ── tasks and the pilot pick ──────────────────────────────────────────────────────────────────────────────

def load_keys(benchmark: str) -> dict:
    return vlb.load_keys() if benchmark == "vlb" else bountybench.load_keys()


def tasks(benchmark: str, data: dict) -> dict[str, dict]:
    """{task id: {"id", "repo" (owner/name), "snapshots": [(name, git ref), ...]}} for every task in the keys."""
    if benchmark == "vlb":
        return {t["id"]: {"id": t["id"], "repo": t["repo"],
                          "snapshots": [("vulnerable", t["vulnerable_commit"]), ("patched", t["fix_commit"])]}
                for t in data["tasks"]}
    return {b["id"]: {"id": b["id"], "repo": b["repo"], "snapshots": [("vulnerable", b["commit"])]}
            for b in data["bounties"]}


def _default_run(cmd: list[str]) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout


def gh_repo_size(name: str, *, run=_default_run) -> int | None:
    """GitHub's size (KB) of owner/name, or None when the API cannot say (gone, private, gh missing)."""
    try:
        return int(run(["gh", "api", f"repos/{name}", "--jq", ".size"]).strip())
    except (subprocess.CalledProcessError, OSError, ValueError):
        return None


def _vlb_candidate(t: dict) -> bool:
    existing = [g for g in t["ground_truth_files"] if g not in t.get("added_files", [])]
    return (t["scope"] == "in" and bool(t["cwes"]) and 1 <= len(t["fix_sites"]) <= vlb.BROAD_SITES
            and bool(existing))


def _bb_candidate(b: dict) -> bool:
    return b["scope"] == "in" and not b.get("review") and bool(b["key"]["fix_sites"]) and bool(b["key"]["cwe"])


def _smallest(cands: list[dict], k: int, size, sizes: dict, skipped: list, *, distinct) -> list[dict]:
    """The k candidates with the smallest repos (then by id), one per `distinct(c)` value."""
    rows = []
    for c in cands:
        if c["repo"] not in sizes:
            sizes[c["repo"]] = size(c["repo"])
            if sizes[c["repo"]] is None:
                skipped.append(c["repo"])
        if sizes[c["repo"]] is not None:
            rows.append((sizes[c["repo"]], c["id"], c))
    picked, seen = [], set()
    for _s, _id, c in sorted(rows, key=lambda r: (r[0], r[1])):
        if distinct(c) in seen:
            continue
        seen.add(distinct(c))
        picked.append(c)
        if len(picked) == k:
            break
    return picked


def select(benchmark: str, data: dict, n: int, *, size=gh_repo_size, mix: dict | None = None) -> dict:
    """A frozen pilot list. VLB: in scope, has a CWE, 1..BROAD_SITES fix sites, not only files the fix added;
    `mix` tasks per ecosystem (default PILOT_MIX, which must sum to n). BountyBench: in scope, key not
    flagged `review`, has fix sites and a CWE. Smallest GitHub repo first, then by id; one task per repo
    (VLB) or per system (BountyBench). Deterministic for fixed repo sizes."""
    sizes: dict[str, int | None] = {}
    skipped: list[str] = []
    if benchmark == "vlb":
        mix = dict(mix or PILOT_MIX)
        if sum(mix.values()) != n:
            raise ValueError(f"--pilot {n} does not match the ecosystem mix {mix} (sum {sum(mix.values())})")
        picked = []
        for eco, k in mix.items():
            cands = [t for t in data["tasks"] if t["ecosystem"] == eco and _vlb_candidate(t)]
            picked += _smallest(cands, k, size, sizes, skipped, distinct=lambda t: t["repo"])
        rule = ("VLB in scope, has a CWE, 1-%d fix sites, not only added files; per ecosystem %s; smallest GitHub "
                "repo first, then id; one task per repo" % (vlb.BROAD_SITES, mix))
        rows = [{"id": t["id"], "repo": t["repo"], "ecosystem": t["ecosystem"], "cwes": t["cwes"],
                 "fix_sites": len(t["fix_sites"]), "size_kb": sizes[t["repo"]]} for t in picked]
    else:
        cands = [b for b in data["bounties"] if _bb_candidate(b)]
        picked = _smallest(cands, n, size, sizes, skipped, distinct=lambda b: b["system"])
        rule = ("BountyBench in scope, key not flagged review, has fix sites and a CWE; smallest GitHub repo first, "
                "then id; one bounty per system")
        rows = [{"id": b["id"], "repo": b["repo"], "cwe": b["key"]["cwe"], "fix_sites": len(b["key"]["fix_sites"]),
                 "size_kb": sizes[b["repo"]]} for b in picked]
    if len(rows) < n:
        raise ValueError(f"only {len(rows)} {benchmark} tasks match the pilot rule (wanted {n})")
    return {"benchmark": benchmark, "rule": rule, "keys_status": data.get("status"),
            "selected": [r["id"] for r in rows], "tasks": rows, "repos_without_size": sorted(set(skipped)),
            "note": "Smallest repos first keeps the pilot cheap; it biases the pilot toward small libraries."}


def pilot_path(benchmark: str) -> Path:
    return ROOT / "eval" / "dataset" / benchmark / "pilot.json"


# ── run dirs ──────────────────────────────────────────────────────────────────────────────────────────────

def _safe(task_id: str) -> str:
    return task_id.replace("/", "__")


def run_dir_for(benchmark, arm, task_id, snapshot, run_no, attempt=1, root: Path = RUNS_ROOT) -> Path:
    return root / benchmark / arm / _safe(task_id) / snapshot / (str(run_no) if attempt == 1 else f"{run_no}.a{attempt}")


def _record(d: Path) -> dict | None:
    try:
        return json.loads((d / "run.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def attempts(benchmark, arm, task_id, snapshot, run_no, root: Path = RUNS_ROOT) -> list[tuple[int, Path, dict | None]]:
    out, a = [], 1
    while (d := run_dir_for(benchmark, arm, task_id, snapshot, run_no, a, root)).exists():
        out.append((a, d, _record(d)))
        a += 1
    return out


def ok_run(benchmark, arm, task_id, snapshot, run_no, root: Path = RUNS_ROOT) -> tuple[Path, dict] | None:
    for _a, d, rec in reversed(attempts(benchmark, arm, task_id, snapshot, run_no, root)):
        if rec and rec.get("status") == "ok":
            return d, rec
    return None


def mirror_dir(name: str, root: Path = MIRROR_ROOT) -> Path:
    return root / (name.replace("/", "__") + ".git")


def resolve(mirror: Path, ref: str, *, run=_default_run) -> str:
    """The commit a ref names (BountyBench pins tags such as v5.0.1)."""
    return run(["git", "-C", str(mirror), "rev-parse", "--verify", f"{ref}^{{commit}}"]).strip()


# ── usage ─────────────────────────────────────────────────────────────────────────────────────────────────

def _block(input_tokens, cache_read, cache_write, output, cost) -> dict:
    return {"uncached_in": input_tokens, "cache_read": cache_read, "cache_write": cache_write, "out": output,
            "cli_cost_usd": cost}


def orion_usage(progress: Path) -> list[dict]:
    """One block per `claude -p` call from an Orion run's progress.jsonl `usage` events (claude_cli._emit_usage)."""
    blocks = []
    if not progress.is_file():
        return blocks
    for line in progress.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            ev = json.loads(line)
            if ev.get("event") != "usage":
                continue
            u = json.loads(ev["detail"]) if isinstance(ev.get("detail"), str) else ev.get("detail")
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
        if isinstance(u, dict):
            blocks.append(_block(u.get("input_tokens"), u.get("cache_read_input_tokens"),
                                 u.get("cache_creation_input_tokens"), u.get("output_tokens"), u.get("total_cost_usd")))
    return blocks


def plain_usage(usage_log: Path, blocks: list[dict]) -> list[dict]:
    """The plain arm's normalized blocks (read_usage) with the CLI's own cost from the same `result` events."""
    costs = []
    if usage_log.is_file():
        for line in usage_log.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if isinstance(ev, dict) and ev.get("type") == "result":
                costs.append(ev.get("total_cost_usd"))
    out = [dict(b) for b in blocks]
    for b, c in zip(out, costs):
        b["cli_cost_usd"] = c
    for b in out[len(costs):]:
        b.setdefault("cli_cost_usd", None)
    return out


def usage_total(blocks: list[dict]) -> dict:
    """Sums over calls. A field no call measured is None; cost is None if any call's cost is missing."""
    def s(k):
        vals = [b.get(k) for b in blocks if b.get(k) is not None]
        return sum(vals) if vals else None
    costs = [b.get("cli_cost_usd") for b in blocks]
    return {"calls": len(blocks), **{k: s(k) for k in ("uncached_in", "cache_read", "cache_write", "out")},
            "tokens": sum(s(k) or 0 for k in ("uncached_in", "cache_read", "cache_write", "out")),
            "cli_cost_usd": round(sum(costs), 4) if costs and all(c is not None for c in costs) else None}


# ── the arms ──────────────────────────────────────────────────────────────────────────────────────────────

def orion_cmd(tree: str, run_dir: Path) -> list[str]:
    return [sys.executable, "-m", "orion", "scan", tree, "--json", str(run_dir / "verdicts.json")]


def orion_env(arm: str, base: dict) -> dict:
    """The scan's env: option knobs dropped (defaults measured), model + effort pinned to the plain twin's,
    and this checkout first on PYTHONPATH (from the run dir, `-m orion` would otherwise import an installed one)."""
    model, effort = model_of(arm)
    env = {k: v for k, v in base.items() if k not in ORION_OPTION_VARS}
    env.update(ORION_MODEL=model, ORION_EFFORT=effort,
               PYTHONPATH=os.pathsep.join(p for p in (str(ROOT), base.get("PYTHONPATH", "")) if p))
    return env


def _orion_run_dir(run_dir: Path) -> Path | None:
    found = sorted((run_dir / ".orion" / "runs").glob("*/*/leads.json"))
    return found[-1].parent if found else None


def _limit_hit(stdout: list[str], orion_dir: Path | None) -> str | None:
    """A usage-limit signature in an error-shaped place: an ERROR verdict's reason, an error/warn progress event,
    or a non-progress stdout line (a traceback, the CLI's own stderr) that says error. Progress lines are read
    from progress.jsonl by event instead: a lead about a login that is not rate-limited must not abort the run."""
    texts = [l for l in stdout if not l.lstrip().startswith("[") and "error" in l.lower()]
    if orion_dir is not None:
        for line in (orion_dir / "progress.jsonl").read_text(encoding="utf-8", errors="replace").splitlines() \
                if (orion_dir / "progress.jsonl").is_file() else []:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if any(w in str(ev.get("event", "")).lower() for w in ("error", "warn", "fail")):
                texts.append(str(ev.get("detail", "")))
        try:
            verdicts = json.loads((orion_dir / "verdicts.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            verdicts = []
        texts += [str(v.get("reason", "")) for v in verdicts if isinstance(v, dict) and v.get("decision") == "ERROR"]
    return next((t for t in texts if _RAW_RE.search(t)), None)


def _clear_scan(scan_id: str) -> str:
    try:
        from orion.graphdb import GraphDB
        db = GraphDB()
        try:
            db.clear_scan(scan_id)
        finally:
            db.close()
        return "cleared"
    except Exception as exc:  # noqa: BLE001 -- a leftover graph costs disk, not correctness
        return f"not cleared ({type(exc).__name__}: {exc})"


def run_orion_arm(arm: str, tree: str, run_dir: Path, on_line, *, _stream=stream_subprocess, _clear=_clear_scan) -> dict:
    procs, lines = [], []

    def _line(l):
        lines.append(l)
        on_line(l)

    status = _stream(orion_cmd(tree, run_dir), env=orion_env(arm, dict(os.environ)), cwd=str(run_dir),
                     on_line=_line, on_start=procs.append)
    rc = procs[0].returncode if procs else None
    odir = _orion_run_dir(run_dir)
    incomplete = rc == 3
    if status == "crashed" and rc == 3 and (run_dir / "verdicts.json").is_file():
        status = "ok"                    # exit 3: some leads ended ERROR; the verdicts are still a result
    limit = _limit_hit(lines, odir)
    if limit:
        status = "aborted"
    out = {"status": status, "rc": rc, "incomplete": incomplete, "limit_line": limit,
           "usage": orion_usage(odir / "progress.jsonl") if odir else [],
           "orion_run_dir": str(odir.relative_to(run_dir)) if odir else None, "scan_id": None, "graph": None}
    if odir:
        try:
            out["scan_id"] = json.loads((odir / "leads.json").read_text(encoding="utf-8")).get("scan_id")
        except (OSError, ValueError):
            pass
    if out["scan_id"]:
        out["graph"] = _clear(out["scan_id"])
    return out


def run_plain_arm(arm: str, tree: str, run_dir: Path, on_line, *, _plain=plain_arm) -> dict:
    model, effort = model_of(arm)
    res = _plain(arm=arm, repo_dir=tree, model_tag=model, effort=effort, json_out=str(run_dir / "findings.json"),
                 usage_log=str(run_dir / "usage.jsonl"), shim_dir=shim_dir(), base_env=dict(os.environ),
                 on_line=on_line)
    return {"status": res.status, "findings": res.findings_path is not None,
            "usage": plain_usage(run_dir / "usage.jsonl", res.usage)}


# ── the run loop ──────────────────────────────────────────────────────────────────────────────────────────

def plan(task_list: list[dict], arms: list[str], runs: int) -> list[tuple]:
    """(task, snapshot, ref, arm, run_no) in run order: grouped by model (in --arms order), then task, snapshot."""
    models = list(dict.fromkeys(model_of(a)[0] for a in arms))
    return [(t, snap, ref, arm, n) for m in models for t in task_list for snap, ref in t["snapshots"]
            for arm in arms if model_of(arm)[0] == m for n in range(1, runs + 1)]


def _quiet(line: str) -> None:
    if not line.lstrip().startswith("{"):            # stream-json goes to usage.jsonl; show the rest
        print(f"    {line}"[:300], flush=True)


def run(benchmark: str, task_ids: list[str], arms: list[str], *, runs: int = 1, resume_wait: int = 900,
        root: Path = RUNS_ROOT, mirrors: Path = MIRROR_ROOT, tree_root: str | None = None, dry_run: bool = False,
        on_line=_quiet, sleep=time.sleep, max_attempts: int = 4, _mirror=repo.mirror, _export=repo.export_tree,
        _resolve=resolve, _arms=None) -> dict:
    """Run every planned (task, snapshot, arm, run) not yet `ok`. Returns {"ok", "aborted_retries", "failed", "skipped"}.
    An aborted run is retried until its `max_attempts`-th attempt, then left aborted (a failure) and the loop moves on."""
    bad = [a for a in arms if a not in ARMS]
    if bad:
        raise ValueError(f"unknown arm(s) {bad} (known: {', '.join(ARMS)})")
    data = load_keys(benchmark)
    all_tasks = tasks(benchmark, data)
    missing = [t for t in task_ids if t not in all_tasks]
    if missing:
        raise ValueError(f"unknown {benchmark} task(s): {missing}")
    runners = _arms or {**{a: run_plain_arm for a in PLAIN}, **{a: run_orion_arm for a in ORION}}
    tree_root = tree_root or os.path.join(tempfile.gettempdir(), TREE_ROOT_NAME)
    commit = git_head(str(ROOT))
    items = plan([all_tasks[t] for t in task_ids], arms, runs)
    counts = {"ok": 0, "aborted_retries": 0, "failed": 0, "skipped": 0}
    mirrored, shas = set(), {}
    for i, (task, snap, ref, arm, n) in enumerate(items, 1):
        tag = f"[{i}/{len(items)}] {arm} {task['id']} {snap} run {n}"
        if ok_run(benchmark, arm, task["id"], snap, n, root):
            print(f"{tag}: ok already, skipped", flush=True)
            counts["skipped"] += 1
            continue
        mirror = mirror_dir(task["repo"], mirrors)
        if dry_run:
            model, effort = model_of(arm)
            tree = os.path.join(tree_root, "<token>", "tree")
            _leak_guard(tree)
            argv = orion_cmd(tree, run_dir_for(benchmark, arm, task["id"], snap, n, 1, root)) if arm in ORION \
                else ["<TASK_PROMPT>" if x == TASK_PROMPT else x for x in _build_cmd(arm, tree, model, effort)]
            print(f"{tag}\n    mirror https://github.com/{task['repo']}.git -> {mirror}\n    export {ref} -> {tree} "
                  f"(leak guard ok)\n    {model} effort {effort}\n    argv {' '.join(argv)}", flush=True)
            continue
        if task["repo"] not in mirrored:
            print(f"{tag}: mirroring {task['repo']}", flush=True)
            mirror.parent.mkdir(parents=True, exist_ok=True)
            _mirror(f"https://github.com/{task['repo']}.git", str(mirror))
            mirrored.add(task["repo"])
        if (task["repo"], ref) not in shas:
            shas[task["repo"], ref] = _resolve(mirror, ref)
        sha = shas[task["repo"], ref]
        attempt = len(attempts(benchmark, arm, task["id"], snap, n, root)) + 1
        while True:
            d = run_dir_for(benchmark, arm, task["id"], snap, n, attempt, root)
            d.mkdir(parents=True, exist_ok=True)
            token = hashlib.sha256(f"{benchmark}|{arm}|{task['id']}|{snap}|{n}|{attempt}|{time.time_ns()}".encode()
                                   ).hexdigest()[:16]
            work = os.path.join(tree_root, token)
            tree = os.path.join(work, "tree")
            print(f"{tag}: attempt {attempt}, {sha[:12]} -> {d}", flush=True)
            t0 = time.monotonic()
            try:
                _leak_guard(tree)
                _export(str(mirror), sha, tree)
                res = runners[arm](arm, tree, d, on_line)
            finally:
                repo.remove_tree(work)
            model, effort = model_of(arm)
            rec = {"benchmark": benchmark, "task": task["id"], "repo": task["repo"], "snapshot": snap, "ref": ref,
                   "commit": sha, "arm": arm, "model": model, "effort": effort, "run_no": n, "attempt": attempt,
                   "wall_s": round(time.monotonic() - t0, 1), "orion_commit": commit,
                   "usage_total": usage_total(res.get("usage") or []), **res}
            (d / "run.json").write_text(json.dumps(rec, indent=1, default=str), encoding="utf-8")
            tot = rec["usage_total"]
            print(f"{tag}: {rec['status']} in {rec['wall_s']}s, {tot['tokens']} tokens, CLI ${tot['cli_cost_usd']}",
                  flush=True)
            if rec["status"] == "aborted" and attempt >= max_attempts:
                print(f"{tag}: still aborted after {attempt} attempts; left as a failure", flush=True)
            elif rec["status"] == "aborted":
                counts["aborted_retries"] += 1
                print(f"{tag}: usage limit ({str(rec.get('limit_line') or '')[:120]}); waiting {resume_wait}s",
                      flush=True)
                sleep(resume_wait)
                attempt += 1
                continue
            counts["ok" if rec["status"] == "ok" else "failed"] += 1
            break
    return counts


# ── scoring ───────────────────────────────────────────────────────────────────────────────────────────────

def _leads_file(orion_dir: Path) -> Path:
    """The run's leads as a bare list (each lead a finding as written: normalize.counts_as_finding)."""
    out = orion_dir / "leads_only.json"
    leads = json.loads((orion_dir / "leads.json").read_text(encoding="utf-8")).get("leads") or []
    out.write_text(json.dumps(leads), encoding="utf-8")
    return out


def _scoring_path(arm: str, d: Path, rec: dict) -> str | None:
    if arm in PLAIN:
        return str(d)
    if not rec.get("orion_run_dir"):
        return None
    odir = d / rec["orion_run_dir"]
    return str(_leads_file(odir)) if arm.endswith(LEADS) else str(odir)


def collect(benchmark: str, arm: str, task_ids: list[str], snapshots: dict[str, list[str]], run_no: int = 1,
            root: Path = RUNS_ROOT) -> tuple[dict, list[str], list[dict]]:
    """(runs map for the benchmark's scorer, task ids with no ok vulnerable run, the ok run records)."""
    base = arm[:-len(LEADS)] if arm.endswith(LEADS) else arm
    runs, missing, recs = {}, [], []
    for tid in task_ids:
        paths = {}
        for snap in snapshots[tid]:
            hit = ok_run(benchmark, base, tid, snap, run_no, root)
            if hit:
                p = _scoring_path(arm, *hit)
                if p:
                    paths[snap] = p
                    recs.append(hit[1])
        if "vulnerable" not in paths:
            missing.append(tid)
            continue
        runs[tid] = paths if benchmark == "vlb" else paths["vulnerable"]
    return runs, missing, recs


def _cost_row(recs: list[dict]) -> dict:
    blocks = [b for r in recs for b in (r.get("usage") or [])]
    return {**usage_total(blocks), "wall_s": round(sum(r.get("wall_s") or 0 for r in recs), 1)}


def score(benchmark: str, task_ids: list[str], arms: list[str], run_no: int = 1, root: Path = RUNS_ROOT) -> dict:
    data = load_keys(benchmark)
    snaps = {tid: [s for s, _ in t["snapshots"]] for tid, t in tasks(benchmark, data).items() if tid in task_ids}
    out = {"benchmark": benchmark, "keys_status": data.get("status"), "run_no": run_no, "tasks": task_ids,
           "evidence": "dev (pre-cutoff benchmark, proposed keys, small N)", "arms": {}}
    for arm in arms:
        runs, missing, recs = collect(benchmark, arm, task_ids, snaps, run_no, root)
        if benchmark == "vlb":
            rows = vlb.score_runs(data, runs)
            summ = vlb.summary(rows)
        else:
            rows = bountybench.score_runs(data, runs)
            flagged = {b["id"] for b in data["bounties"] if b.get("review")}
            summ = {"n": len(rows), "l1": sum(r["l1"] for r in rows), "l2": sum(r["l2"] for r in rows),
                    "n_findings": sum(r["n_findings"] for r in rows),
                    "l2_tight": sum(r["l2"] for r in rows if r["bounty"] not in flagged)}
        cost = _cost_row([] if arm.endswith(LEADS) else recs)
        commits = sorted({r.get("orion_commit") for r in recs if r.get("orion_commit")})
        out["arms"][arm] = {"summary": summ, "missing": missing, "cost": cost, "orion_commits": commits,
                            "incomplete_runs": sum(1 for r in recs if r.get("incomplete")), "rows": rows}
    return out


def _fmt(v, nd=3):
    return "-" if v is None else (f"{v:.{nd}f}" if isinstance(v, float) else str(v))


def table(result: dict) -> str:
    b = result["benchmark"]
    lines = [f"**{b}** ({result['evidence']}; keys {result['keys_status']}; run {result['run_no']})", ""]
    if b == "vlb":
        lines += ["| Arm | scored | missing | L1 | L2 | mean File F1 (CWE) | silent on patched | pair-correct | "
                  "tokens | CLI $ | $ per L2 | wall s |", "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    else:
        lines += ["| Arm | scored | missing | L1 | L2 | L2 (keys not flagged) | findings | tokens | CLI $ | $ per L2 | "
                  "wall s |", "|---|---|---|---|---|---|---|---|---|---|---|"]
    for arm, a in result["arms"].items():
        s, c = a["summary"], a["cost"]
        per = round(c["cli_cost_usd"] / s["l2"], 2) if c["cli_cost_usd"] is not None and s["l2"] else None
        cost = ("(same run)", "(same run)", "-") if arm.endswith(LEADS) else (c["tokens"], _fmt(c["cli_cost_usd"], 2),
                                                                             _fmt(per, 2))
        if b == "vlb":
            silent = f"{s['silent']} / {s['n_paired']}" if s["n_paired"] else "-"
            pair = f"{s['pair_correct']} / {s['n_paired']}" if s["n_paired"] else "-"
            lines.append(f"| {arm} | {s['n']} | {len(a['missing'])} | {s['l1']} | {s['l2']} | "
                         f"{_fmt(s['mean_file_f1_cwe'])} | {silent} | {pair} | {cost[0]} | {cost[1]} | {cost[2]} | "
                         f"{'-' if arm.endswith(LEADS) else c['wall_s']} |")
        else:
            lines.append(f"| {arm} | {s['n']} | {len(a['missing'])} | {s['l1']} | {s['l2']} | {s['l2_tight']} | "
                         f"{s['n_findings']} | {cost[0]} | {cost[1]} | {cost[2]} | "
                         f"{'-' if arm.endswith(LEADS) else c['wall_s']} |")
    lines += ["", "CLI $ is the cost `claude -p` reports (notional on a subscription login). `:leads` rows score the "
              "same Orion run before its verifier. Missing = no ok run on the vulnerable snapshot (excluded, not "
              "counted as a miss)."]
    return "\n".join(lines)


def _arms_on_disk(benchmark: str, root: Path) -> list[str]:
    have = [a for a in ARMS if (root / benchmark / a).is_dir()]
    return [x for a in have for x in ((a, a + LEADS) if a in ORION else (a,))]


def _task_ids(benchmark: str, tasks_file: str | None) -> list[str]:
    path = Path(tasks_file) if tasks_file else pilot_path(benchmark)
    data = json.loads(path.read_text(encoding="utf-8"))
    return list(data["selected"] if isinstance(data, dict) else data)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="VLB / BountyBench end to end: the same model with and without Orion")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("select", help="freeze a pilot task list (reads repo sizes with gh api; clones nothing)")
    s.add_argument("--benchmark", choices=BENCHMARKS, required=True)
    s.add_argument("--pilot", type=int, default=5)
    s.add_argument("--out", default=None)
    r = sub.add_parser("run", help="run the arms on the task list (spends model usage unless --dry-run)")
    r.add_argument("--benchmark", choices=BENCHMARKS, required=True)
    r.add_argument("--tasks", default=None, help="a pilot.json or a JSON list of task ids (default: the pilot)")
    r.add_argument("--arms", default=",".join(ARMS))
    r.add_argument("--runs", type=int, default=1)
    r.add_argument("--resume-wait", type=int, default=900, help="seconds to wait after a usage limit")
    r.add_argument("--dry-run", action="store_true", help="print the plan; no clone, no model call")
    c = sub.add_parser("score", help="score what is on disk (token-free)")
    c.add_argument("--benchmark", choices=BENCHMARKS, required=True)
    c.add_argument("--tasks", default=None)
    c.add_argument("--run-no", type=int, default=1)
    c.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "select":
        res = select(a.benchmark, load_keys(a.benchmark), a.pilot)
        out = Path(a.out) if a.out else pilot_path(a.benchmark)
        out.write_text(json.dumps(res, indent=1) + "\n", encoding="utf-8")
        for t in res["tasks"]:
            print(f"{t['id']:<28} {t['repo'][:44]:<45} {t['size_kb']:>8} KB")
        print(f"wrote {out}" + (f"; no size for {len(res['repos_without_size'])} repo(s)" if res["repos_without_size"]
                                else ""))
        return 0
    ids = _task_ids(a.benchmark, a.tasks)
    if a.cmd == "run":
        counts = run(a.benchmark, ids, [x.strip() for x in a.arms.split(",") if x.strip()], runs=a.runs,
                     resume_wait=a.resume_wait, dry_run=a.dry_run)
        print(f"done: {counts}")
        return 0 if not counts["failed"] else 1
    res = score(a.benchmark, ids, _arms_on_disk(a.benchmark, RUNS_ROOT), a.run_no)
    RESULTS_ROOT.mkdir(parents=True, exist_ok=True)
    stem = RESULTS_ROOT / f"{a.benchmark}-pilot"
    stem.with_suffix(".json").write_text(json.dumps(res, indent=1, default=str) + "\n", encoding="utf-8")
    text = table(res)
    stem.with_suffix(".md").write_text(text + "\n", encoding="utf-8")
    print(json.dumps(res, indent=1, default=str) if a.json else text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
