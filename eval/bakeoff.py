"""Bake-off harness: run each candidate model through `train.devscore.judge` on one seeded dev sample.

`peak_rss_mb` is the child process's own `ru_maxrss` high-water mark, so each candidate runs in a FRESH
subprocess (`run_all` / `--one`). An in-process run (`isolated=False`, used by tests) records None, because a
process-wide high-water mark would include earlier candidates. It is never estimated. `train_cutoff` is
whatever string the caller supplies.

Sampling (2026-10-29): `--temperature/--top-p/--top-k` reach `orion.lm.model.load`; without them a model runs
with its own defaults (generation_config.json, else temperature 1.0). Each row records the sampling the model
ACTUALLY ran with (`sampling`, from the wrapper) and, with `--out`, every scored row's outcome (`rows`).

Gate 1 of the merge plan (Phase 6, decision D4): `--compare BASE.json C1.json` pairs two rows scored on the
same sample and applies `GATE1`, a rule fixed here BEFORE the run so the numbers cannot choose it.
"""
from __future__ import annotations

import argparse
import json
import random
import subprocess
import sys

from orion.lm import model as lm_model
from train import devscore

COLUMNS = ["model", "n", "vuln_recall", "patched_silence", "negative_silence", "pair_correct",
           "parse_fail_rate", "tokens", "tok_per_s", "peak_rss_mb", "train_cutoff", "temperature"]

# Gate 1 (D4): keep C1, and with it the frozen code-only sink rules, only if it is measurably better than its
# own base on the dev pairs and no worse on patched code. PROPOSED 2026-10-29; Krish / Love Kush confirm or
# change it before the run. Both bounds are the paired 95% cluster-bootstrap CI (cluster = pair).
GATE1 = {
    "pair_correct_gain_lo": 0.0,       # lower CI bound of C1 - base pair_correct must be ABOVE this
    "patched_silence_margin": 0.05,    # lower CI bound of C1 - base patched_silence must be >= -this
}


def sample_dev(dev_examples, *, sample_n: int, seed: int) -> list[dict]:
    """Seeded sample of whole pairs (so pair_correct stays defined), at most `sample_n` rows. The result
    depends only on the dev rows, `sample_n` and `seed`, never on the candidate. Rows (example rows or Stage 2
    traces) are read by `devscore.normalise_row`: only `split == "dev"` is sampled and a missing split raises.
    The original rows are returned."""
    groups: dict[str, list[tuple[dict, dict]]] = {}
    for r in dev_examples:
        n = devscore.normalise_row(r)
        if n["split"] == "dev":
            groups.setdefault(n["pair_id"], []).append((n, r))
    ids = sorted(groups)
    random.Random(seed).shuffle(ids)
    out: list[dict] = []
    for pid in ids:
        if len(out) + len(groups[pid]) <= sample_n:
            out.extend(r for _, r in sorted(groups[pid], key=lambda nr: nr[0]["label"]))
    return out


def _maxrss_mb() -> float:
    import resource  # POSIX-only; imported here so the module (and its tests) load on Windows
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return raw / (1024 * 1024) if sys.platform == "darwin" else raw / 1024  # bytes on macOS, KB on Linux


def run_candidate(model_path, dev_examples, *, schema_text=None, load=lm_model.load,
                  sample_n=300, seed=0, train_cutoff=None, isolated=False, sampling=None,
                  keep_rows=False) -> dict:
    """`sampling` = keyword arguments for `load` (temperature / top_p / top_k); None or {} = the model's own
    defaults. `keep_rows` adds `rows`, every scored row's outcome in sample order (for `compare`)."""
    sample = sample_dev(dev_examples, sample_n=sample_n, seed=seed)
    model = load(model_path, **sampling) if sampling else load(model_path)
    rows: list[dict] = []
    scores = devscore.judge(model.generate, sample, schema_text=schema_text, on_row=rows.append)
    ran = getattr(model, "sampling", None)
    out = {**scores, "model": str(model_path), "peak_rss_mb": _maxrss_mb() if isolated else None,
           "train_cutoff": train_cutoff, "sample_n": sample_n, "seed": seed,
           "sampling": dict(ran) if isinstance(ran, dict) else None,
           "temperature": ran.get("temperature") if isinstance(ran, dict) else None}
    if keep_rows:
        out["rows"] = rows
    return out


def _subprocess_run(cmd: list[str]) -> str:
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout


def _error_row(path, exc: Exception) -> dict:
    reason = f"{type(exc).__name__}: {exc}"
    stderr = getattr(exc, "stderr", None)
    if stderr:
        reason += " | stderr: " + str(stderr).strip()[:300]
    return {**{c: None for c in COLUMNS}, "model": str(path), "error": reason[:500]}


def _sampling_args(sampling) -> list[str]:
    flags = {"temperature": "--temperature", "top_p": "--top-p", "top_k": "--top-k"}
    return [a for k, v in (sampling or {}).items() if v is not None for a in (flags[k], str(v))]


def run_all(candidates, dev_path, *, sample_n=300, seed=0, train_cutoffs=None, run=_subprocess_run,
            sampling=None) -> list[dict]:
    """One fresh child process per candidate (`python -m eval.bakeoff --one ...`); `run(cmd) -> stdout`
    is injectable. The row is the last JSON line the child prints. `sampling` goes to every child."""
    rows = []
    for path in candidates:
        cmd = [sys.executable, "-m", "eval.bakeoff", "--one", str(path), "--dev", str(dev_path),
               "--sample-n", str(sample_n), "--seed", str(seed), *_sampling_args(sampling)]
        cutoff = (train_cutoffs or {}).get(path)
        if cutoff is not None:
            cmd += ["--train-cutoff", cutoff]
        try:
            lines = run(cmd).strip().splitlines()
            if not lines:
                raise ValueError("child printed nothing")
            rows.append(json.loads(lines[-1]))
        except Exception as exc:  # one bad candidate must not drop the others' rows
            rows.append(_error_row(path, exc))
    return rows


def _cell(v) -> str:
    if v is None:
        return "n/a"
    if isinstance(v, float):
        return f"{v:.3f}" if abs(v) < 10 else f"{v:.0f}"
    return str(v)


def table(rows) -> str:
    lines = ["| " + " | ".join(COLUMNS) + " |", "|" + "|".join("---" for _ in COLUMNS) + "|"]
    for r in rows:
        lines.append("| " + " | ".join(_cell(r.get(c)) for c in COLUMNS) + " |")
    return "\n".join(lines)


# ---- Gate 1: paired comparison of two candidates on one sample -----------------------------------

METRICS = ("vuln_recall", "patched_silence", "negative_silence", "nonvuln_silence", "pair_correct")
# per-pair counts: vulnerable rows / flagged, patched rows / flagged, negative rows / flagged, two-sided, correct
_COLS = 8


def _pair_counts(outcomes: list[dict]) -> tuple[list, list[list[int]]]:
    """(pair ids in first-seen order, one count row per pair). A pair is two-sided when it has vulnerable AND
    patched rows, and correct when every vulnerable row is flagged and every patched row silent
    (`devscore.judge`'s rule)."""
    order: list = []
    by: dict = {}
    for o in outcomes:
        if o["pair_id"] not in by:
            order.append(o["pair_id"])
            by[o["pair_id"]] = {"vulnerable": [], "patched": [], "negative": []}
        by[o["pair_id"]].setdefault(o["label"], []).append(bool(o["flagged"]))
    rows = []
    for pid in order:
        d = by[pid]
        v, pa, ng = d["vulnerable"], d["patched"], d["negative"]
        two = bool(v and pa)
        rows.append([len(v), sum(v), len(pa), sum(pa), len(ng), sum(ng), int(two),
                     int(two and all(v) and not any(pa))])
    return order, rows


def _from_sums(s):
    """METRICS from summed pair counts (last axis = _COLS); nan where a denominator is 0."""
    import numpy as np
    s = np.asarray(s, float)
    with np.errstate(divide="ignore", invalid="ignore"):
        nonvuln = s[..., 2] + s[..., 4]
        return {"vuln_recall": s[..., 1] / s[..., 0],
                "patched_silence": (s[..., 2] - s[..., 3]) / s[..., 2],
                "negative_silence": (s[..., 4] - s[..., 5]) / s[..., 4],
                "nonvuln_silence": (nonvuln - s[..., 3] - s[..., 5]) / nonvuln,
                "pair_correct": s[..., 7] / s[..., 6]}


def _point(v):
    import math
    v = float(v)
    return None if math.isnan(v) else v


def paired(ra: list[dict], rb: list[dict], *, B: int = 10_000, seed: int = 0, alpha: float = 0.05) -> dict:
    """Two outcome lists over the SAME rows (same i / pair_id / label, else ValueError): each METRIC for a and
    b, the difference b - a and its percentile CI from a cluster bootstrap that resamples PAIRS with
    replacement (a pair's rows stay together; a and b see the same resample). A metric whose denominator is
    empty in a draw leaves that draw out; no draw at all (fewer than two pairs) gives a [None, None] CI."""
    import numpy as np
    if [(o["i"], o["pair_id"], o["label"]) for o in ra] != [(o["i"], o["pair_id"], o["label"]) for o in rb]:
        raise ValueError("the two outcome lists are not over the same rows")
    ids, ca = _pair_counts(ra)
    _, cb = _pair_counts(rb)
    A, Bm = np.asarray(ca, float).reshape(-1, _COLS), np.asarray(cb, float).reshape(-1, _COLS)
    ma, mb = _from_sums(A.sum(axis=0)), _from_sums(Bm.sum(axis=0))
    out = {"n_pairs": len(ids), "n_rows": len(ra), "B": B, "alpha": alpha,
           "a": {m: _point(ma[m]) for m in METRICS}, "b": {m: _point(mb[m]) for m in METRICS}}
    out["diff"] = {m: (None if out["a"][m] is None or out["b"][m] is None else out["b"][m] - out["a"][m])
                   for m in METRICS}
    ci = {m: [None, None] for m in METRICS}
    if len(ids) >= 2:
        picks = np.random.default_rng(seed).integers(0, len(ids), size=(B, len(ids)))
        weights = np.zeros((B, len(ids)))
        np.add.at(weights, (np.repeat(np.arange(B), len(ids)), picks.ravel()), 1.0)
        da, db = _from_sums(weights @ A), _from_sums(weights @ Bm)
        for m in METRICS:
            d = db[m] - da[m]
            d = d[~np.isnan(d)]
            if d.size:
                ci[m] = [float(np.quantile(d, alpha / 2, method="lower")),
                         float(np.quantile(d, 1 - alpha / 2, method="higher"))]
    out["ci"] = ci
    return out


def _temperature(row: dict):
    return (row.get("sampling") or {}).get("temperature")


def compare(base: dict, cand: dict, *, B: int = 10_000, seed: int = 0, alpha: float = 0.05,
            rule: dict | None = None) -> dict:
    """Gate 1: `cand` against `base`, two `run_candidate(..., keep_rows=True)` rows scored on the SAME sample
    (same dev rows, sample_n, seed: checked row by row) at the same temperature, else ValueError. The paired
    bootstrap is `paired`. `decision` applies `rule` (default GATE1): KEEP_C1 when both bounds pass, RETRAIN
    when one fails, INCONCLUSIVE when a CI cannot be computed (fewer than two pairs, or none two-sided)."""
    rule = GATE1 if rule is None else rule
    ra, rb = base.get("rows"), cand.get("rows")
    if not ra or not rb:
        raise ValueError("both rows need per-row outcomes: score each candidate with --out")
    try:
        p = paired(ra, rb, B=B, seed=seed, alpha=alpha)
    except ValueError:
        raise ValueError("the two candidates were not scored on the same sample (dev file, --sample-n, --seed)")
    if _temperature(base) != _temperature(cand):
        raise ValueError(f"the two candidates ran at different temperatures: {_temperature(base)} vs "
                         f"{_temperature(cand)}")
    gain_lo, sil_lo = p["ci"]["pair_correct"][0], p["ci"]["patched_silence"][0]
    if gain_lo is None or sil_lo is None:
        decision = "INCONCLUSIVE"
    elif gain_lo > rule["pair_correct_gain_lo"] and sil_lo >= -rule["patched_silence_margin"]:
        decision = "KEEP_C1"
    else:
        decision = "RETRAIN"
    return {"base": base.get("model"), "candidate": cand.get("model"), "n_pairs": p["n_pairs"],
            "n_rows": p["n_rows"], "temperature": _temperature(base), "base_metrics": p["a"],
            "candidate_metrics": p["b"], "diff": p["diff"], "ci": p["ci"], "B": B, "alpha": alpha,
            "rule": dict(rule), "decision": decision}


def compare_text(c: dict) -> str:
    def signed(v):
        return "n/a" if v is None else f"{v:+.3f}"
    lines = [f"Gate 1: {c['candidate']} vs base {c['base']} at temperature {c['temperature']} "
             f"({c['n_pairs']} pairs, {c['n_rows']} rows; {int(round((1 - c['alpha']) * 100))}% CI, "
             f"B={c['B']}, cluster = pair)",
             "| metric | base | candidate | diff | CI |", "|---|---|---|---|---|"]
    for m in METRICS:
        lo, hi = c["ci"][m]
        lines.append(f"| {m} | {_cell(c['base_metrics'][m])} | {_cell(c['candidate_metrics'][m])} | "
                     f"{signed(c['diff'][m])} | [{signed(lo)}, {signed(hi)}] |")
    r = c["rule"]
    lines.append(f"rule: pair_correct gain CI low > {r['pair_correct_gain_lo']} and patched_silence CI low "
                 f">= -{r['patched_silence_margin']} -> {c['decision']}")
    return "\n".join(lines)


def _read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--one", help="model path: score this one candidate, print its row as JSON")
    mode.add_argument("--compare", nargs=2, metavar=("BASE_JSON", "CANDIDATE_JSON"),
                      help="Gate 1: compare two --out rows scored on the same sample")
    ap.add_argument("--dev", help="dev examples JSONL (with --one)")
    ap.add_argument("--sample-n", type=int, default=300)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--train-cutoff", default=None)
    ap.add_argument("--temperature", type=float, default=None, help="sampling temperature (0 = greedy)")
    ap.add_argument("--top-p", type=float, default=None)
    ap.add_argument("--top-k", type=int, default=None)
    ap.add_argument("--out", default=None, help="also write the row, with per-row outcomes, to this JSON file")
    a = ap.parse_args(argv)
    if a.compare:
        base, cand = (_read_json(p) for p in a.compare)
        c = compare(base, cand)
        print(table([base, cand]))
        print()
        print(compare_text(c))
        return
    if not a.dev:
        ap.error("--one needs --dev")
    with open(a.dev, encoding="utf-8") as f:
        dev = [json.loads(ln) for ln in f if ln.strip()]
    sampling = {k: v for k, v in (("temperature", a.temperature), ("top_p", a.top_p), ("top_k", a.top_k))
                if v is not None}
    row = run_candidate(a.one, dev, sample_n=a.sample_n, seed=a.seed, train_cutoff=a.train_cutoff, isolated=True,
                        sampling=sampling, keep_rows=bool(a.out))
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(row, f, indent=1)
    print(json.dumps({k: v for k, v in row.items() if k != "rows"}))


if __name__ == "__main__":
    main()
