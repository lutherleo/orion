"""Dev-benchmark scoring through eval's frozen matcher (merge plan Phase 6, Gate 3; decision D5).

NodeGoat and PyGoat are Orion's DEV benchmarks: their bugs are public and old (memorised by any frontier
model), so a score here is development evidence, never a pre-registered result, and never sits in an
`eval.report` table. Until 2026-10-29 they were scored only by `bench/scoring.py` (file basename + keyword,
many-to-many, no line or function check). This module scores the same outputs with the matcher of record,
`eval.match` (L1: same file AND (function OR a line within the fix site +/-10, range overlap);
L2: + the CWE family), one-to-one by maximum matching (`match_max`), over `eval.normalize`'s finding shape.

Answer keys: `eval/dataset/devbench/<benchmark>.json`, one key per `tests/ground_truth_<benchmark>.py` item,
each with fix sites read from the pinned fixture (file, line, end_line, function, and the first line's text as
`quote`, so `check_keys` can prove the key still points at the code it names). A key file says whether it is
`signed`; scores from `proposed` keys are printed as such.

Inputs (`findings_from`):
  * an Orion run dir (`<run_dir>/verdicts.json`, `orion scan`'s), or a verdicts JSON list;
  * `bench/prove.py`'s arm P result (`eval_arm` = a plain arm: structured findings with a CWE);
  * a `bench/research_eval` result JSON: the full verdicts of an Orion arm (`verdicts[].verdict`, since
    2026-10-29), else its `findings` ({text, evidence[, file]}: arms C and D, prose locations).

    python -m eval.devbench --benchmark nodegoat bench/research/nodegoat/C.json bench/research/nodegoat/D.json
    python -m eval.devbench --benchmark nodegoat --check-keys fixtures/NodeGoat
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from eval import match, normalize

KEYS_DIR = Path(__file__).resolve().parent / "dataset" / "devbench"
BENCHMARKS = ("nodegoat", "pygoat")


def load_keys(benchmark: str, keys_dir: Path = KEYS_DIR) -> dict:
    if benchmark not in BENCHMARKS:
        raise ValueError(f"unknown benchmark {benchmark!r} (known: {', '.join(BENCHMARKS)})")
    with open(keys_dir / f"{benchmark}.json", encoding="utf-8") as f:
        data = json.load(f)
    ids = [k["id"] for k in data["keys"]]
    if len(set(ids)) != len(ids):
        raise ValueError(f"{benchmark}: duplicate key ids")
    return data


def check_keys(data: dict, fixture_dir: str) -> list[str]:
    """Every problem with the keys against a fixture checkout ([] = all good): a missing file, a line past the
    end, an end_line before line, or a `quote` that is not the (stripped) text of `line`."""
    problems = []
    cache: dict[str, list[str]] = {}
    for k in data["keys"]:
        for n, s in enumerate(k.get("fix_sites") or []):
            where = f"{k['id']} site {n} ({s.get('file')}:{s.get('line')})"
            path = os.path.join(fixture_dir, s["file"])
            if s["file"] not in cache:
                if not os.path.isfile(path):
                    problems.append(f"{where}: file not found")
                    cache[s["file"]] = []
                    continue
                with open(path, encoding="utf-8", errors="replace", newline="") as f:
                    cache[s["file"]] = f.read().splitlines()
            lines = cache[s["file"]]
            if not lines:
                continue
            line, end = s.get("line"), s.get("end_line", s.get("line"))
            if not isinstance(line, int) or not 1 <= line <= len(lines):
                problems.append(f"{where}: line outside the file (1..{len(lines)})")
                continue
            if not isinstance(end, int) or end < line or end > len(lines):
                problems.append(f"{where}: bad end_line {end}")
            quote = (s.get("quote") or "").strip()
            if quote and quote != lines[line - 1].strip():
                problems.append(f"{where}: quote {quote[:60]!r} != source {lines[line - 1].strip()[:60]!r}")
    return problems


def _verdict_list(data) -> list | None:
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("verdicts"), list):
        full = [r["verdict"] for r in data["verdicts"] if isinstance(r, dict) and isinstance(r.get("verdict"), dict)]
        if full and len(full) == len(data["verdicts"]):
            return full
    return None


def findings_from(path: str, *, arm: str | None = None) -> tuple[str, list[dict]]:
    """(arm label, normalized findings) for a run dir, a verdicts list, or a research_eval result."""
    p = Path(path)
    if p.is_dir():
        engine = (normalize.load(str(p / "leads.json")) or {})
        engine = engine.get("engine") if isinstance(engine, dict) else None
        label = arm or ("orion-lm" if engine == "lm" else "orion")
        return label, normalize.load_arm(str(p / "verdicts.json"), arm=label)
    data = normalize.load(str(p))
    verdicts = _verdict_list(data)
    if verdicts is not None:
        label = arm or (f"orion:{data.get('label')}" if isinstance(data, dict) and data.get("label") else "orion")
        items = [v for v in verdicts if isinstance(v, dict) and normalize.counts_as_finding(v)]
        return label, [normalize.finding(v, arm=label) for v in items]
    if isinstance(data, dict) and str(data.get("eval_arm") or "").startswith("plain-") \
            and isinstance(data.get("findings"), list):
        # arm P (bench/prove.py): eval's plain arm, structured findings.json items
        label = arm or str(data.get("label") or data["eval_arm"])
        return label, [normalize.finding(f, arm=data["eval_arm"]) for f in data["findings"] if isinstance(f, dict)]
    if isinstance(data, dict) and isinstance(data.get("findings"), list):
        # arms C / D (and pre-2026-10-29 Orion arms): bare findings with prose locations. Not "plain-": that
        # prefix means the eval plain arms' structured findings.json.
        label = arm or f"bench:{data.get('label') or data.get('arm') or p.stem}"
        return label, [normalize.finding({"text": f.get("text") or "", "evidence": f.get("evidence") or "",
                                          **({"file": f["file"]} if f.get("file") else {})}, arm=label)
                       for f in data["findings"] if isinstance(f, dict)]
    raise ValueError(f"{path}: not an Orion run dir, a verdicts list or a research_eval result")


def _hungarian(cost: list[list[int]]) -> list[int | None]:
    """Minimum-cost assignment of every row to a distinct column (rows <= columns), O(rows^2 * cols)."""
    n, m = len(cost), len(cost[0]) if cost else 0
    inf = float("inf")
    u, v, p, way = [0.0] * (n + 1), [0.0] * (m + 1), [0] * (m + 1), [0] * (m + 1)
    for i in range(1, n + 1):
        p[0], j0 = i, 0
        minv, used = [inf] * (m + 1), [False] * (m + 1)
        while True:
            used[j0], i0, delta, j1 = True, p[j0], inf, 0
            for j in range(1, m + 1):
                if not used[j]:
                    cur = cost[i0 - 1][j - 1] - u[i0] - v[j]
                    if cur < minv[j]:
                        minv[j], way[j] = cur, j0
                    if minv[j] < delta:
                        delta, j1 = minv[j], j
            for j in range(m + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while j0:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
    out: list[int | None] = [None] * n
    for j in range(1, m + 1):
        if p[j]:
            out[p[j] - 1] = j - 1
    return out


def match_max(findings: list[dict], keys: list[dict]) -> list[dict]:
    """One-to-one credit by MAXIMUM matching: the most vulns credited (eval.match.l1), then the most at L2
    (eval.match.l2), then the lowest finding indices; rows shaped like `eval.match.match_run`'s.

    Why not match_run: its greedy pass (findings in order, first key that fits) is exact for eval, where a
    run is matched against ONE key, but with a benchmark's 15 keys it can hand a finding to a neighbouring
    key and leave the finding that names that key with nothing, so a vuln two findings cover between them
    goes uncredited. Measured 2026-10-29 on the committed NodeGoat arm C: its open-redirect finding at
    index.js:70 sits inside A7's window (55-60 +/-10) and took A7 first, so A10 (index.js:72) went
    uncredited. The L1/L2 tests themselves are eval.match's, unchanged."""
    n, m = len(keys), len(findings)
    if not n or not m:
        return []
    level = [[("L2" if match.l2(f, k) else "L1" if match.l1(f, k) else None) for f in findings] for k in keys]
    # weights ordered lexicographically: any extra credit beats every L2 bonus, any L2 beats every index cost
    l2_bonus = n * m + 1
    credit = n * l2_bonus + n * m + 1
    cost = [[-(credit + (l2_bonus if lv == "L2" else 0) - j) if lv else 0 for j, lv in enumerate(row)]
            + [0] * n for row in level]                                       # n dummy columns = "uncredited"
    rows = []
    for vi, fi in enumerate(_hungarian(cost)):
        if fi is None or fi >= m or level[vi][fi] is None:
            continue
        lv, k, f = level[vi][fi], keys[vi], findings[fi]
        rows.append({"finding_idx": fi, "vuln_id": str(k["id"]), "level": lv, "cwe_family": lv == "L2",
                     "path_level": json.dumps(match.path_levels(f, k)) if f.get("path") else None})
    rows.sort(key=lambda r: r["finding_idx"])
    return rows


def score(findings: list[dict], data: dict) -> dict:
    """`match_max` of `findings` against the benchmark's keys, plus what each unmatched finding is: a
    DUPLICATE when it L1-matches a key another finding already took (one-to-one credits only one), else
    UNMATCHED (it names no key's site: a false-alarm candidate, or a real bug the key set lacks)."""
    keys = data["keys"]
    rows = match_max(findings, keys)
    caught = {r["vuln_id"]: r for r in rows}
    used = {r["finding_idx"] for r in rows}
    duplicates, unmatched = [], []
    for i, f in enumerate(findings):
        if i in used:
            continue
        hit = next((k["id"] for k in keys if k["id"] in caught and match.l1(f, k)), None)
        (duplicates if hit else unmatched).append(i)
    checkable = [k for k in keys if not k.get("known_gap")]
    return {
        "n_keys": len(keys), "n_checkable": len(checkable), "n_findings": len(findings),
        "l1": sorted(caught, key=[k["id"] for k in keys].index),
        "l2": [k["id"] for k in keys if k["id"] in caught and caught[k["id"]]["level"] == "L2"],
        "missed": [k["id"] for k in keys if k["id"] not in caught],
        "duplicates": duplicates, "unmatched": unmatched,
        "unlocated": sum(1 for f in findings if not f.get("file")),
        "no_cwe": sum(1 for f in findings if not f.get("cwe")),
        "matches": rows,
    }


def table(benchmark: str, data: dict, results: list[tuple[str, dict]]) -> str:
    status = data.get("status", "proposed")
    head = (f"**{benchmark}** (eval matcher: L1 file + function or line +/-10, L2 + CWE family, one-to-one; "
            f"keys {status}" + (f" by {', '.join(data.get('signed_by') or [])}" if status == "signed" else
                                ", NOT signed: dev evidence on draft keys") + ")")
    lines = [head, "", "| Arm | L1 caught (location only) | L2 caught (+ CWE) | missed (L1) | duplicates | unmatched | "
                       "no CWE | unlocated |",
             "|---|---|---|---|---|---|---|---|"]
    for label, s in results:
        lines.append(f"| {label} | {len(s['l1'])} / {s['n_keys']} | {len(s['l2'])} / {s['n_keys']} | "
                     f"{', '.join(s['missed']) or '-'} | {len(s['duplicates'])} | {len(s['unmatched'])} | "
                     f"{s['no_cwe']} | {s['unlocated']} of {s['n_findings']} |")
    lines.append("")
    lines.append("L1 does not check the bug TYPE: with many keys in one repo, a finding of another class near a "
                 "fix site is credited. Only L2 checks the type, and a finding without a CWE can never reach it.")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Score dev-benchmark outputs with eval's frozen matcher")
    ap.add_argument("--benchmark", required=True, choices=BENCHMARKS)
    ap.add_argument("--check-keys", metavar="FIXTURE_DIR", help="check the keys against a fixture checkout")
    ap.add_argument("--json", action="store_true", help="print the scores as JSON")
    ap.add_argument("inputs", nargs="*", help="Orion run dirs, verdicts JSON lists or research_eval results")
    a = ap.parse_args(argv)
    data = load_keys(a.benchmark)
    if a.check_keys:
        problems = check_keys(data, a.check_keys)
        for p in problems:
            print(f"  BAD  {p}")
        n_sites = sum(len(k.get("fix_sites") or []) for k in data["keys"])
        print(f"{a.benchmark}: {len(data['keys'])} keys, {n_sites} fix sites, {len(problems)} problem(s)")
        if problems or not a.inputs:
            return 1 if problems else 0
    results = []
    for path in a.inputs:
        label, findings = findings_from(path)
        results.append((label, score(findings, data)))
    if a.json:
        print(json.dumps([{"arm": lbl, **s} for lbl, s in results], indent=1))
    else:
        print(table(a.benchmark, data, results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
