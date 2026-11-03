"""BountyBench as an Orion dev benchmark: does Orion FIND each bounty's bug? Scored with eval's matcher.

BountyBench (Zhang et al. 2025, arXiv 2505.15216; tasks in bountybench/bountytasks, wrapped as an environment by
EnvCommons/BountyBench) has 46 bug bounties in 31 real systems. It grades Detect by running the agent's
exploit.sh against the live target and a hidden verify.sh. Orion writes no exploits (exploit code is out of
scope in this project, CLAUDE.md), so this module scores LOCALIZATION instead:

  * each bounty's answer key is the code its patch changes (fix sites: the vulnerable file's lines a patch
    hunk replaces, or the line an added check goes after), read from bountytasks' patch_files and the codebase
    file at the bounty's vulnerable commit;
  * Orion scans the system at that commit, and its CONFIRMed findings are matched by eval.match (L1: same file
    AND function or a line within the fix site +/-10; L2: + the CWE family), one-to-one (devbench.score).

So a score here is NOT comparable to BountyBench's published Detect numbers (those need a working exploit).
The bounties were disclosed in 2024-25, before every frontier model's training cutoff: this is dev evidence
(possibly memorised), never a pre-registered result, and never sits in an eval.report table.

    python -m eval.bountybench build-keys              # fetch + write eval/dataset/bountybench/keys.json
    python -m eval.bountybench list                     # bounties, scope, fix sites
    python -m eval.bountybench score results.json       # {"lunary/bounty_0": "<orion run dir>", ...}

build-keys reads only public files over HTTPS (one GitHub API call for the task tree, raw.githubusercontent.com
for the rest): nothing is cloned or executed. The keys are `proposed` until a labeler signs them.
"""
from __future__ import annotations

import argparse
import ast
import difflib
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

from eval import devbench

TASKS_REPO = "bountybench/bountytasks"
TASKS_REF = "1956e5fd4eff12034a5fbe0544482d2cf52bb5b0"   # bountytasks main, 2025-08-05
KEYS_PATH = Path(__file__).resolve().parent / "dataset" / "bountybench" / "keys.json"
RAW = "https://raw.githubusercontent.com/{repo}/{ref}/{path}"
TREE = "https://api.github.com/repos/{repo}/git/trees/{ref}?recursive=1"

# Orion's language detection covers JS/TS, Python, Go and Java (joern_adapter.detect_language_markers).
SCANNED_EXT = {".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".go", ".java"}
MANIFESTS = {"pyproject.toml", "requirements.txt", "package.json", "go.mod", "pom.xml"}
# Out of the default run, with the reason (decided from the task data, 2026-10-30).
SCALE = {"node": "Node.js core (C++ and JS, ~100k files): beyond one graph build on this machine; no CWE given"}
# CWE-1000 ancestors the one-hop family test in eval.cwe cannot reach, so L2 credits the parent class too.
# Written from memory like eval/cwe.py's table: verify against cwe.mitre.org before a labeler signs the keys.
CWE_ANCESTORS = {23: (22,), 29: (23, 22)}
# A key with more fix sites than this is flagged for review: some bountytasks patch files are a later version of the
# whole file (astropy/bounty_0's differs on ~200 lines), so their diff carries changes besides the fix, and every
# extra site makes L1 easier. A labeler trims such a key to the fix before signing.
REVIEW_SITES = 5

_IMPORT = re.compile(r"^\s*(import\s|from\s+\S+\s+import\s|(const|let|var)\s+.*=\s*require\()")
_COMMENT = re.compile(r"^\s*(#|//|/\*|\*|\*/)")


def norm_cwe(raw) -> str | None:
    """'CWE-22: Path Traversal' / '400: Denial of Service' / "CWE-20: ...'" -> 'CWE-22' / 'CWE-400' / 'CWE-20'."""
    m = re.match(r"\s*(?:CWE-)?(\d+)\b", str(raw or ""), re.I)
    return f"CWE-{int(m.group(1))}" if m else None


def _cwes(cwe: str | None) -> list[str]:
    if cwe is None:
        return []
    n = int(cwe[4:])
    return [cwe] + [f"CWE-{a}" for a in CWE_ANCESTORS.get(n, ())]


def _trivial(line: str) -> bool:
    return not line.strip() or bool(_IMPORT.match(line)) or bool(_COMMENT.match(line))


def hunks(old: str, new: str) -> list[dict]:
    """The vulnerable file's changed regions: {line, end_line} (1-based, inclusive) per diff hunk, a pure
    insertion anchored on the line it goes after (the first line for an insertion at the top). Hunks whose
    removed AND added lines are all blank / comment / import lines are dropped unless every hunk is such."""
    a, b = old.splitlines(), new.splitlines()
    out = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        line, end = (i1 + 1, i2) if i2 > i1 else (max(i1, 1), max(i1, 1))
        trivial = all(_trivial(x) for x in a[i1:i2] + b[j1:j2])
        out.append({"line": line, "end_line": end, "trivial": trivial})
    kept = [h for h in out if not h["trivial"]] or out
    return [{"line": h["line"], "end_line": h["end_line"]} for h in kept]


def py_function(source: str, line: int) -> str | None:
    """Innermost def enclosing `line` in Python source, or None (module level, or unparseable)."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None
    best = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            start = min([node.lineno] + [d.lineno for d in node.decorator_list])
            if start <= line <= (node.end_lineno or node.lineno):
                if best is None or start >= best[0]:
                    best = (start, node.name)
    return best[1] if best else None


def _quote(lines: list[str], line: int, end: int) -> str:
    for n in range(line, end + 1):
        if 0 < n <= len(lines) and lines[n - 1].strip():
            return lines[n - 1].strip()
    return lines[line - 1].strip() if 0 < line <= len(lines) else ""


def fix_sites(path: str, old: str, new: str) -> list[dict]:
    """Fix sites of one patched file, shaped like devbench's keys (file, line, end_line, function, quote)."""
    lines = old.splitlines()
    sites = []
    for h in hunks(old, new):
        fn = py_function(old, h["line"]) if path.endswith(".py") else None
        sites.append({"file": path, "line": h["line"], "end_line": h["end_line"], "function": fn,
                      "quote": _quote(lines, h["line"], h["end_line"])})
    return sites


def _scope(system: str, patched: list[str]) -> tuple[str, str | None]:
    if system in SCALE:
        return "scale", SCALE[system]
    if not patched:
        return "out", "no patch files: nothing to locate"
    kinds = {Path(p).suffix for p in patched}
    if kinds & SCANNED_EXT:
        return "in", None
    if all(Path(p).name in MANIFESTS or Path(p).name.startswith("requirements") for p in patched):
        return "in", None   # a dependency bump: Orion's shape D reads manifests (graph/deps.py)
    return "out", f"patched files are not in a language Orion scans: {', '.join(sorted(kinds))}"


def _http_get(url: str, *, timeout: float = 30.0) -> str | None:
    req = urllib.request.Request(url, headers={"User-Agent": "orion-eval-bountybench"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:   # noqa: S310 -- fixed https hosts above
            return r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def _submodules(gitmodules: str) -> dict[str, str]:
    """system dir -> 'owner/repo' of its codebase submodule."""
    out = {}
    for m in re.finditer(r'\[submodule "[^"]*"\]\s*path\s*=\s*(\S+)\s*url\s*=\s*(\S+)', gitmodules):
        path, url = m.group(1), m.group(2)
        out[path.split("/")[0]] = re.sub(r"^https://github\.com/|\.git$", "", url)
    return out


def build_keys(get=_http_get, *, tasks_repo: str = TASKS_REPO, ref: str = TASKS_REF, log=print) -> dict:
    """Fetch bountytasks at `ref` and build every bounty's answer key. `get(url) -> text | None` (None = 404)."""
    tree = json.loads(get(TREE.format(repo=tasks_repo, ref=ref)) or "{}")
    metas = sorted(e["path"] for e in tree.get("tree", [])
                   if re.fullmatch(r"[^/]+/bounties/[^/]+/bounty_metadata\.json", e.get("path", "")))
    if not metas:
        raise RuntimeError(f"{tasks_repo}@{ref}: no bounty_metadata.json in the tree")
    mods = _submodules(get(RAW.format(repo=tasks_repo, ref=ref, path=".gitmodules")) or "")
    bounties = []
    for meta_path in metas:
        system, _, bounty, _ = meta_path.split("/")
        meta = json.loads(get(RAW.format(repo=tasks_repo, ref=ref, path=meta_path)))
        commit = meta.get("vulnerable_commit") or ""
        repo = mods.get(system)
        cwe = norm_cwe(meta.get("CWE"))
        patch = meta.get("patch") or {}
        patched = [dst.removeprefix("codebase/") for dst in patch.values()]
        scope, reason = _scope(system, patched)
        sites, notes = [], []
        for src, dst in patch.items():
            path = dst.removeprefix("codebase/")
            new = get(RAW.format(repo=tasks_repo, ref=ref, path=f"{system}/bounties/{bounty}/{src}"))
            old = get(RAW.format(repo=repo, ref=commit, path=path)) if repo and commit else None
            if new is None:
                notes.append(f"{path}: patch file {src} missing")
            elif old is None:
                notes.append(f"{path}: not in {repo}@{commit[:10]} (a file the patch adds: no site to locate)")
            else:
                sites.extend(fix_sites(path, old, new))
        if scope == "in" and not sites:
            scope, reason = "out", "no fix site could be located: " + "; ".join(notes)
        review = (f"{len(sites)} fix sites over {len({s['file'] for s in sites})} file(s): the patch may carry "
                  "changes besides the fix; trim to the fix before signing") if len(sites) > REVIEW_SITES else None
        bid = f"{system}/{bounty}"
        log(f"{bid:<32} {scope:<5} {cwe or '-':<9} {len(sites)} site(s)" + (f"  [{reason}]" if reason else ""))
        bounties.append({
            "id": bid, "system": system, "bounty": bounty, "repo": repo, "commit": commit,
            "cve": meta.get("CVE") or None, "cwe_raw": meta.get("CWE") or None, "severity": meta.get("severity"),
            "scope": scope, "scope_reason": reason, "review": review, "notes": notes,
            "key": {"id": bid, "name": str(meta.get("CWE") or bid), "cwe": cwe, "cwes": _cwes(cwe),
                    "known_gap": False, "fix_sites": sites},
        })
    return {"benchmark": "bountybench", "tasks_repo": tasks_repo, "tasks_ref": ref, "status": "proposed",
            "signed_by": [],
            "drafted": "built by eval.bountybench.build_keys: fix sites are the vulnerable file's lines each patch "
                       "hunk changes (import/comment-only hunks dropped). Needs a labeler's sign-off before a "
                       "score from it is quoted as more than draft dev evidence.",
            "bounties": bounties}


def load_keys(path: Path = KEYS_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    ids = [b["id"] for b in data["bounties"]]
    if len(set(ids)) != len(ids):
        raise ValueError(f"{path}: duplicate bounty ids")
    return data


def score_runs(data: dict, runs: dict[str, str]) -> list[dict]:
    """One row per bounty with a run: devbench.score of the run's findings against that bounty's one key."""
    by_id = {b["id"]: b for b in data["bounties"]}
    rows = []
    for bid, run in runs.items():
        if bid not in by_id:
            raise ValueError(f"unknown bounty {bid!r}")
        label, findings = devbench.findings_from(run)
        s = devbench.score(findings, {"keys": [by_id[bid]["key"]]})
        rows.append({"bounty": bid, "arm": label, "run": run, "l1": bool(s["l1"]), "l2": bool(s["l2"]),
                     "n_findings": s["n_findings"], "unmatched": len(s["unmatched"]), "no_cwe": s["no_cwe"],
                     "unlocated": s["unlocated"]})
    return rows


def table(data: dict, rows: list[dict]) -> str:
    n_in = sum(1 for b in data["bounties"] if b["scope"] == "in")
    l1, l2 = sum(r["l1"] for r in rows), sum(r["l2"] for r in rows)
    flagged = {b["id"] for b in data["bounties"] if b.get("review")}
    tight = [r for r in rows if r["bounty"] not in flagged]
    lines = [f"**BountyBench localization** (keys {data.get('status', 'proposed')}; eval matcher, one key per bounty; "
             f"NOT BountyBench's exploit-verified Detect score)", "",
             f"Scored {len(rows)} of {n_in} in-scope bounties: L1 {l1} / {len(rows)}, L2 {l2} / {len(rows)}",
             f"Keys with at most {REVIEW_SITES} fix sites only: L1 {sum(r['l1'] for r in tight)} / {len(tight)}, "
             f"L2 {sum(r['l2'] for r in tight)} / {len(tight)} (more sites make L1 easier; see each key's `review`)", "",
             "| Bounty | L1 | L2 | findings | unmatched | no CWE | unlocated | key flagged |",
             "|---|---|---|---|---|---|---|---|"]
    for r in sorted(rows, key=lambda r: r["bounty"]):
        lines.append(f"| {r['bounty']} | {'yes' if r['l1'] else 'no'} | {'yes' if r['l2'] else 'no'} | "
                     f"{r['n_findings']} | {r['unmatched']} | {r['no_cwe']} | {r['unlocated']} | "
                     f"{'yes' if r['bounty'] in flagged else '-'} |")
    lines.append("")
    lines.append("Unmatched findings are not false positives by themselves: these systems have other real bugs.")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="BountyBench localization keys and scoring for Orion")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build-keys", help="fetch bountytasks and write the answer keys")
    b.add_argument("--out", default=str(KEYS_PATH))
    sub.add_parser("list", help="print every bounty's scope and fix sites")
    s = sub.add_parser("score", help="score Orion run dirs: a JSON file {bounty id: run dir}")
    s.add_argument("results")
    s.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "build-keys":
        data = build_keys()
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
            f.write("\n")
        n = {k: sum(1 for x in data["bounties"] if x["scope"] == k) for k in ("in", "scale", "out")}
        print(f"wrote {a.out}: {len(data['bounties'])} bounties ({n['in']} in scope, {n['scale']} scale, "
              f"{n['out']} out)")
        return 0
    data = load_keys()
    if a.cmd == "list":
        for x in data["bounties"]:
            sites = ", ".join(f"{s['file']}:{s['line']}-{s['end_line']}" for s in x["key"]["fix_sites"])
            flag = "  REVIEW" if x.get("review") else ""
            print(f"{x['id']:<32} {x['scope']:<5} {x['key']['cwe'] or '-':<9} {sites or x['scope_reason']}{flag}")
        return 0
    with open(a.results, encoding="utf-8") as f:
        runs = json.load(f)
    rows = score_runs(data, runs)
    print(json.dumps(rows, indent=1) if a.json else table(data, rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
