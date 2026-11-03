"""Cisco's Vulnerability Localization Benchmark (VLB) as an Orion dev benchmark, scored with eval's matcher.

VLB (arXiv 2609.15939; github.com/cisco-foundation-ai/vulnerability-localization-benchmark, Apache-2.0) has 500
GitHub-advisory vulnerabilities in 290 repos. Each task pairs the commit just before the fix (the vulnerable
snapshot) with the fix commit (the patched snapshot); ground truth is the implementation files the fix changed.
VLB scores an agent that is told the CWE: Phase A = File F1 of the files it submits on the vulnerable snapshot,
Phase B = whether it submits nothing on the patched one (true-negative rate). Its published runs put the best
frontier model at File F1 0.229 but silent on only 28% of patched snapshots, and SAST tools silent on 91-99% at
File F1 <= 0.086 -- the trade-off Orion's separate verifier is built to break.

Orion scans a whole repository with no CWE hint, so this module scores an Orion run three ways:
  * VLB-style File F1 of the files Orion's CONFIRMed findings name: over all findings, and over the findings in
    the task's CWE family (unrelated real bugs in a large repo should not count against the question asked);
  * fix-site L1/L2 by eval.match, one-to-one (devbench.score), against fix sites derived from the fix diff:
    the vulnerable-file lines each hunk removes, or the line an added check goes after (import/comment-only
    hunks dropped) -- finer than VLB's files, which under-credit a tool that reports lines;
  * silence on the patched snapshot: no CWE-family finding in a ground-truth file. Pair-correct = located on
    the vulnerable snapshot AND silent on the patched one (the headline: silence at matched recall).

Scope: maven / npm / pip tasks are `in` (Orion's study languages: Java, JS/TS, Python); go tasks are `go`
(Orion's golang frontend scans them, outside the pre-registered study); rust and composer are `out`.
Every VLB task predates the May 2026 frontier cutoffs: this is dev evidence (possibly memorised), never a
pre-registered result. Before an orion-lm arm is scored, check each task against Orion-LM's training leak set
(VLB is built from the same kind of GHSA + fix-commit sources as MoreFixes/OSV).

    python -m eval.vlb build-keys        # read-only GitHub API calls (gh); writes eval/dataset/vlb/keys.json
    python -m eval.vlb list [--scope in]
    python -m eval.vlb score runs.json   # {"<task id>": {"vulnerable": "<run dir>", "patched": "<run dir>"}}

build-keys reads VLB's manifest and each task's compare diff through `gh api`: nothing is cloned or executed.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from eval import cwe as _cwe
from eval import devbench, match
from eval.bountybench import _trivial

VLB_REPO = "cisco-foundation-ai/vulnerability-localization-benchmark"
VLB_REF = "fa50b671aa0b954d73691d6ef8d592d42a2ac1e0"   # main, 2026-10-28
KEYS_PATH = Path(__file__).resolve().parent / "dataset" / "vlb" / "keys.json"
SCOPE = {"maven": "in", "npm": "in", "pip": "in", "go": "go"}   # anything else: out

# VLB's own ground-truth filter (src/vulnerability_localization_benchmark/scoring.py @ VLB_REF): code files only,
# no tests -- so Orion is scored on exactly the files VLB scores models on.
CODE_EXTS = {".go", ".java", ".py", ".js", ".ts", ".jsx", ".tsx", ".rs", ".rb",
             ".php", ".c", ".cpp", ".h", ".hpp", ".cs", ".scala", ".kt", ".swift"}

_HUNK = re.compile(r"^@@ -(\d+)(?:,\d+)? \+\d+(?:,\d+)? @@")
# A task whose fix touches more lines than this is "broad": every extra fix site makes L1 easier, so the
# summary also reports L1 on the tight tasks alone.
BROAD_SITES = 10


def is_test_file(path: str) -> bool:
    p = path.lower()
    return "test" in p or "_spec" in p or "spec/" in p or "__tests__/" in p


def ground_truth(files: list[str]) -> list[str]:
    return [f for f in files if os.path.splitext(f)[1].lower() in CODE_EXTS and not is_test_file(f)]


def patch_sites(path: str, patch: str) -> list[dict]:
    """Fix sites of one file from its unified-diff `patch`: per contiguous run of changed lines, the vulnerable
    file's removed lines {line, end_line}, or for a pure insertion the old line it goes after. A run whose
    removed and added lines are all blank / comment / import lines is dropped unless every run is."""
    runs, old, cur = [], None, None

    def close():
        nonlocal cur
        if cur is not None:
            runs.append(cur)
            cur = None

    for line in patch.splitlines():
        m = _HUNK.match(line)
        if m:
            close()
            old = int(m.group(1))
            continue
        if old is None or line.startswith("\\"):
            continue
        if line.startswith("-") or line.startswith("+"):
            if cur is None:
                cur = {"removed": [], "anchor": max(old - 1, 1), "text": []}
            cur["text"].append(line[1:])
            if line.startswith("-"):
                cur["removed"].append(old)
                old += 1
        else:
            close()
            old += 1
    close()
    kept = [r for r in runs if not all(_trivial(t) for t in r["text"])] or runs
    return [{"file": path, "line": min(r["removed"]) if r["removed"] else r["anchor"],
             "end_line": max(r["removed"]) if r["removed"] else r["anchor"], "function": None} for r in kept]


def _gh(path: str, *, raw: bool = False) -> str | None:
    """`gh api <path>` (authenticated, so 5000 calls/hour), or None when GitHub answers an error (404, gone)."""
    cmd = ["gh", "api", path] + (["-H", "Accept: application/vnd.github.raw"] if raw else [])
    r = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace", timeout=120)
    return r.stdout if r.returncode == 0 else None


def _jlist(text: str) -> list:
    try:
        v = json.loads(text) if text else []
    except ValueError:
        return []
    return v if isinstance(v, list) else []


def build_keys(get=_gh, *, ref: str = VLB_REF, scopes=("in", "go"), log=print) -> dict:
    """VLB's manifest at `ref`, plus fix sites from each in-scope task's compare diff. `get(path, raw=...)`."""
    manifest = get(f"repos/{VLB_REPO}/contents/data/manifest.csv?ref={ref}", raw=True)
    if not manifest:
        raise RuntimeError(f"{VLB_REPO}@{ref}: data/manifest.csv not readable")
    tasks = []
    for row in csv.DictReader(io.StringIO(manifest)):
        scope = SCOPE.get(row["ecosystem"], "out")
        files = ground_truth(_jlist(row["ground_truth_files"]))
        cwes = [c for c in (_jlist(row["cwes"])) if _cwe.parse(c) is not None]
        task = {"id": row["alpha_id"], "repo": row["repo_full_name"], "ecosystem": row["ecosystem"],
                "language": row["language"] or None, "scope": scope, "ghsa": row["ghsa_id"],
                "cves": _jlist(row["cves"]), "year": int(row["year"]) if row["year"].isdigit() else None,
                "cwes": cwes, "cwe_description": _jlist(row["cwe_description"]),
                "vulnerable_commit": row["vulnerable_commit_sha"], "fix_commit": row["merge_commit_sha"],
                "ground_truth_files": files, "added_files": [], "fix_sites": [], "notes": []}
        if scope in scopes and files:
            raw = get(f"repos/{row['repo_full_name']}/compare/{row['vulnerable_commit_sha']}...{row['merge_commit_sha']}")
            diff = json.loads(raw) if raw else None
            if not isinstance(diff, dict):
                task["notes"].append("compare diff unavailable: file-level ground truth only")
            else:
                by_name = {}
                for f in diff.get("files") or []:
                    by_name[f.get("filename")] = f
                    if f.get("previous_filename"):
                        by_name[f["previous_filename"]] = f
                for path in files:
                    f = by_name.get(path)
                    if f is None:
                        task["notes"].append(f"{path}: not in the compare diff")
                    elif f.get("status") == "added":   # VLB counts it, but no tool can point at it
                        task["added_files"].append(path)
                        task["notes"].append(f"{path}: added by the fix (not in the vulnerable snapshot)")
                    elif not f.get("patch"):
                        task["notes"].append(f"{path}: diff too large or binary: file-level only")
                    else:
                        task["fix_sites"] += patch_sites(f.get("previous_filename") or path, f["patch"])
        tasks.append(task)
        if scope in scopes:
            log(f"{task['id']:<10} {scope:<3} {row['repo_full_name'][:40]:<41} {len(files)} file(s), "
                f"{len(task['fix_sites'])} site(s)" + (f"  [{task['notes'][0][:60]}]" if task["notes"] else ""))
    return {"benchmark": "vlb", "source": {"repo": VLB_REPO, "ref": ref, "license": "Apache-2.0",
                                           "paper": "arXiv 2609.15939"},
            "status": "files: VLB's own ground truth; fix sites: proposed (derived by eval.vlb, unsigned)",
            "tasks": tasks}


def dump_keys(data: dict, path) -> None:
    """The keys as JSON with one task per line: half the size of an indented dump, and a changed task is a
    one-line diff."""
    head = {k: v for k, v in data.items() if k != "tasks"}
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(head, ensure_ascii=False)[:-1] + ', "tasks": [\n')
        f.write(",\n".join(json.dumps(t, ensure_ascii=False) for t in data["tasks"]))
        f.write("\n]}\n")


def load_keys(path: Path = KEYS_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    ids = [t["id"] for t in data["tasks"]]
    if len(set(ids)) != len(ids):
        raise ValueError(f"{path}: duplicate task ids")
    return data


def _in_gt(file: str | None, gt: list[str]) -> str | None:
    """The ground-truth file `file` names (eval.match's same-file rule: a leading folder aside), else None."""
    return next((g for g in gt if file and match._same_file(file, g)), None)


def file_f1(files: list[str], gt: list[str]) -> dict:
    """VLB's File F1 (scoring.file_f1), with eval.match's same-file rule for the comparison."""
    if not files or not gt:
        return {"precision": 0.0, "recall": 0.0, "file_f1": 0.0}
    hit = {_in_gt(f, gt) for f in files} - {None}
    tp_sub = sum(1 for f in files if _in_gt(f, gt))
    precision, recall = tp_sub / len(files), len(hit) / len(gt)
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"precision": round(precision, 4), "recall": round(recall, 4), "file_f1": round(f1, 4)}


def _relevant(finding: dict, cwes: list[str]) -> bool:
    return any(_cwe.same_family(finding.get("cwe"), c) for c in cwes)


def score_task(task: dict, vulnerable: list[dict], patched: list[dict] | None = None) -> dict:
    """One task: File F1 (all / CWE-family findings), fix-site L1/L2, and patched-snapshot silence."""
    gt, cwes = task["ground_truth_files"], task["cwes"]
    existing = [g for g in gt if g not in task.get("added_files", [])]
    files_all = sorted({f["file"] for f in vulnerable if f.get("file")})
    files_cwe = sorted({f["file"] for f in vulnerable if f.get("file") and _relevant(f, cwes)})
    if task["fix_sites"]:
        key = {"id": task["id"], "cwe": cwes[0] if cwes else None, "cwes": cwes, "known_gap": False,
               "fix_sites": task["fix_sites"]}
        s = devbench.score(vulnerable, {"keys": [key]})
        l1, l2 = bool(s["l1"]), bool(s["l2"])
    else:                                    # no diff: a finding in a ground-truth file is the location test
        l1 = any(_in_gt(f.get("file"), gt) for f in vulnerable)
        l2 = any(_in_gt(f.get("file"), gt) and _relevant(f, cwes) for f in vulnerable)
    out = {"task": task["id"], "scope": task["scope"], "n_findings": len(vulnerable), "l1": l1, "l2": l2,
           "file_all": file_f1(files_all, gt), "file_cwe": file_f1(files_cwe, gt),
           "file_cwe_existing": file_f1(files_cwe, existing),   # without files the fix added
           "site_level": bool(task["fix_sites"]), "broad": len(task["fix_sites"]) > BROAD_SITES}
    if patched is not None:
        out["silent"] = not any(_in_gt(f.get("file"), gt) and _relevant(f, cwes) for f in patched)
        out["pair_correct"] = l1 and out["silent"]
    return out


def score_runs(data: dict, runs: dict[str, dict]) -> list[dict]:
    """`runs`: {task id: {"vulnerable": run dir, "patched": run dir (optional)}} -> one score_task row each."""
    by_id = {t["id"]: t for t in data["tasks"]}
    rows = []
    for tid, paths in runs.items():
        if tid not in by_id:
            raise ValueError(f"unknown VLB task {tid!r}")
        _, vul = devbench.findings_from(paths["vulnerable"])
        pat = devbench.findings_from(paths["patched"])[1] if paths.get("patched") else None
        rows.append(score_task(by_id[tid], vul, pat))
    return rows


def summary(rows: list[dict]) -> dict:
    n = len(rows)
    paired = [r for r in rows if "silent" in r]
    mean = lambda xs: round(sum(xs) / len(xs), 4) if xs else None
    tight = [r for r in rows if not r["broad"]]
    return {"n": n, "l1": sum(r["l1"] for r in rows), "l2": sum(r["l2"] for r in rows),
            "n_tight": len(tight), "l1_tight": sum(r["l1"] for r in tight),
            "mean_file_f1_all": mean([r["file_all"]["file_f1"] for r in rows]),
            "mean_file_f1_cwe": mean([r["file_cwe"]["file_f1"] for r in rows]),
            "mean_file_f1_cwe_existing": mean([r["file_cwe_existing"]["file_f1"] for r in rows]),
            "n_paired": len(paired), "silent": sum(r["silent"] for r in paired),
            "pair_correct": sum(r["pair_correct"] for r in paired)}


def table(rows: list[dict]) -> str:
    s = summary(rows)
    tnr = f"{s['silent']} / {s['n_paired']}" if s["n_paired"] else "-"
    lines = ["**VLB** (Cisco, arXiv 2609.15939; eval matcher at fix sites; dev evidence, pre-cutoff)", "",
             f"Tasks {s['n']}: L1 {s['l1']}, L2 {s['l2']}; mean File F1 {s['mean_file_f1_all']} (all findings), "
             f"{s['mean_file_f1_cwe']} (CWE family), {s['mean_file_f1_cwe_existing']} (CWE family, without "
             f"files the fix added); silent on patched {tnr}; pair-correct {s['pair_correct']} / {s['n_paired']}",
             f"Tasks with at most {BROAD_SITES} fix sites: L1 {s['l1_tight']} / {s['n_tight']} (more sites make L1 "
             "easier)", "",
             "| Task | Scope | L1 | L2 | File F1 (CWE) | silent on patched | findings |", "|---|---|---|---|---|---|---|"]
    for r in sorted(rows, key=lambda r: r["task"]):
        silent = "-" if "silent" not in r else ("yes" if r["silent"] else "no")
        lines.append(f"| {r['task']} | {r['scope']} | {'yes' if r['l1'] else 'no'} | {'yes' if r['l2'] else 'no'} | "
                     f"{r['file_cwe']['file_f1']} | {silent} | {r['n_findings']} |")
    lines.append("")
    lines.append("VLB's own Phase B counts ANY submission on the patched snapshot; Orion scans with no CWE hint, so "
                 "silence here means no finding of the task's CWE family in a ground-truth file.")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Cisco's VLB as an Orion dev benchmark")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build-keys", help="read VLB's manifest and fix diffs (gh api) and write the keys")
    b.add_argument("--out", default=str(KEYS_PATH))
    b.add_argument("--scope", default="in,go", help="comma list of scopes to derive fix sites for")
    lst = sub.add_parser("list", help="print the tasks of a scope")
    lst.add_argument("--scope", default="in")
    s = sub.add_parser("score", help='score run dirs: JSON {"<task id>": {"vulnerable": dir, "patched": dir}}')
    s.add_argument("runs")
    s.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "build-keys":
        data = build_keys(scopes=tuple(a.scope.split(",")))
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        dump_keys(data, a.out)
        counts = {k: sum(1 for t in data["tasks"] if t["scope"] == k) for k in ("in", "go", "out")}
        sited = sum(1 for t in data["tasks"] if t["fix_sites"])
        print(f"wrote {a.out}: {len(data['tasks'])} tasks ({counts['in']} in, {counts['go']} go, {counts['out']} out); "
              f"{sited} with fix sites")
        return 0
    data = load_keys()
    if a.cmd == "list":
        for t in data["tasks"]:
            if t["scope"] == a.scope:
                print(f"{t['id']:<10} {t['repo'][:40]:<41} {','.join(t['cwes']) or '-':<16} "
                      f"{len(t['ground_truth_files'])} file(s), {len(t['fix_sites'])} site(s)")
        return 0
    with open(a.runs, encoding="utf-8") as f:
        runs = json.load(f)
    rows = score_runs(data, runs)
    print(json.dumps({"summary": summary(rows), "rows": rows}, indent=1) if a.json else table(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
