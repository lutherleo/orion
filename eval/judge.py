"""LLM judge for final scoring only: classify, calibration (kappa / AC1), PPI precision.

SCOPE GUARD: this module is for the report / final-scoring path ONLY. Nothing under `train/` and not
`eval.bakeoff` may import it, so no judge output can reach training selection or dev scoring (spec
5.4 "LLM judge scope"). `eval/tests/test_judge.py` checks this in a fresh interpreter.

Undefined statistics are returned as None, never 0 or NaN: kappa/AC1 when chance agreement is 1 (or
fewer than two categories, or no labels), a CI when too few human labels exist, precision when there
are no findings. A missing value stays missing.
"""
import hashlib
import math
import re
from collections import Counter

LABELS = ("real_other_bug", "false_alarm", "unsure")

# Krish freezes the judge model id and the prompt text at the pre-registration tag (Task F4); both are
# recorded for that gate. None = not yet chosen; nothing is guessed.
JUDGE_MODEL = None

# Draft. Blinded to arm identity: the prompt is built only from the finding's own content.
JUDGE_PROMPT = """You are reviewing one security finding reported against a code repository.
The finding did not match any known vulnerability in the answer key. Decide what it is.

Finding:
{finding}

Answer with exactly one label and nothing else:
- real_other_bug: a genuine security bug, just not one in the answer key
- false_alarm: not a real vulnerability (safe code, guarded, or unreachable)
- unsure: the finding gives too little to decide
"""


def prompt_sha256() -> str:
    return hashlib.sha256(JUDGE_PROMPT.encode("utf-8")).hexdigest()


def _render(finding: dict) -> str:
    # Only content fields: never `arm` (blinding), never the path/run metadata.
    parts = []
    for key in ("file", "function", "line", "cwe"):
        v = finding.get(key)
        if v not in (None, ""):
            parts.append(f"{key}: {v}")
    parts.append(f"description: {finding.get('raw_text') or ''}")
    return "\n".join(parts)


def _parse_label(reply):
    if not isinstance(reply, str):
        return None
    for cand in (reply, reply.strip().splitlines()[0] if reply.strip() else ""):
        c = re.sub(r"[\s\-]+", "_", cand.strip().strip("`'\".*").lower())
        if c in LABELS:
            return c
    return None


def classify(finding: dict, *, ask) -> dict:
    """Label one finding via the injected `ask(prompt) -> str`. Any reply that is not exactly one of
    the three labels becomes `unsure`; the raw reply is always kept. Exceptions from `ask` propagate."""
    prompt = JUDGE_PROMPT.format(finding=_render(finding))
    reply = ask(prompt)
    label = _parse_label(reply)
    return {"label": label or "unsure", "raw_reply": reply, "parsed": label is not None}


def _agreement(a, b, categories) -> dict:
    n = len(a)
    if n == 0:
        return {"n": 0, "agreement": None, "kappa": None, "ac1": None}
    po = sum(x == y for x, y in zip(a, b)) / n
    ca, cb = Counter(a), Counter(b)
    cats = set(ca) | set(cb)
    pe_k = sum((ca[k] / n) * (cb[k] / n) for k in cats)
    kappa = None if pe_k >= 1 else (po - pe_k) / (1 - pe_k)
    ac1 = None
    q = len(categories)  # fixed label-scale size, not the number observed (comparable across arms)
    if q >= 2:
        pi = {k: (ca[k] + cb[k]) / (2 * n) for k in cats}
        pe_g = sum(p * (1 - p) for p in pi.values()) / (q - 1)
        ac1 = None if pe_g >= 1 else (po - pe_g) / (1 - pe_g)
    return {"n": n, "agreement": po, "kappa": kappa, "ac1": ac1}


def calibrate(judge_labels, human_labels, arms=None, categories=LABELS) -> dict:
    """Cohen's kappa and Gwet's AC1 between judge and human labels (>=100 blinded findings in the real
    run; `min_n_met` says whether that holds). `arms`, if given, is aligned with the labels and adds a
    `per_arm` breakdown (self-preference check). AC1 uses q = len(categories) (default: the three judge
    labels), fixed across arms; a label outside `categories` raises ValueError. kappa is None when
    expected agreement is 1 (genuinely undefined). Undefined values are None."""
    categories = tuple(dict.fromkeys(categories))
    j, h = list(judge_labels), list(human_labels)
    bad = (set(j) | set(h)) - set(categories)
    if bad:
        raise ValueError(f"labels outside categories: {sorted(map(str, bad))}")
    if len(j) != len(h):
        raise ValueError(f"label length mismatch: {len(j)} judge vs {len(h)} human")
    out = _agreement(j, h, categories)
    out["min_n_met"] = out["n"] >= 100
    out["categories"] = list(categories)        # which task this calibrates (credit_locationless checks)
    if arms is not None:
        arms = list(arms)
        if len(arms) != len(j):
            raise ValueError(f"arms length {len(arms)} != labels length {len(j)}")
        per = {}
        for arm in sorted(set(arms), key=str):
            idx = [i for i, x in enumerate(arms) if x == arm]
            per[arm] = _agreement([j[i] for i in idx], [h[i] for i in idx], categories)
        out["per_arm"] = per
    return out


def _is_real(label) -> float:
    # `unsure` and anything else count as NOT a real bug (conservative for precision).
    return 1.0 if label == "real_other_bug" else 0.0


def _var(xs):
    n = len(xs)
    if n < 2:
        return None
    m = sum(xs) / n
    return sum((x - m) ** 2 for x in xs) / (n - 1)


def ppi_precision(matched, judged, human, z: float = 1.96) -> dict:
    """Prediction-powered precision (Angelopoulos et al. 2023, mean estimation).

    matched: count (or list) of findings matched by the deterministic matcher; treated as true.
    judged:  judge label for EVERY unmatched finding.
    human:   sequence aligned with `judged`; a human label, or None where no human label exists.
    theta_unmatched = mean(f over all unmatched) + mean(y - f over human-labelled), f/y = 1 iff
    `real_other_bug`. Precision = (M + N*theta) / (M + N). CI is normal-approx, 95% by default; None
    when fewer than 2 labelled findings. `classical` is the human-labels-only estimate (same CI).
    """
    m = len(matched) if not isinstance(matched, int) else matched
    f_all = [_is_real(x) for x in judged]
    human = list(human)
    if len(human) != len(f_all):
        raise ValueError("human must align with judged (use None for unlabelled)")
    big_n = len(f_all)
    total = m + big_n
    lab = [(f_all[i], _is_real(y)) for i, y in enumerate(human) if y is not None]
    n = len(lab)
    res = {"matched": m, "unmatched": big_n, "n_labeled": n, "precision": None, "ci": None,
           "classical": None, "classical_ci": None}
    if total == 0:
        return res
    if big_n == 0:
        res["precision"] = 1.0 if m else None
        return res
    if n == 0:
        return res
    rect = [y - f for f, y in lab]
    theta = sum(f_all) / big_n + sum(rect) / n
    w = big_n / total
    res["precision"] = (m + big_n * theta) / total
    vf, vr = _var(f_all), _var(rect)
    if vr is not None and vf is not None:
        se = w * math.sqrt(vf / big_n + vr / n)
        res["ci"] = [res["precision"] - z * se, res["precision"] + z * se]
    ys = [y for _, y in lab]
    cl = sum(ys) / n
    res["classical"] = (m + big_n * cl) / total
    vy = _var(ys)
    if vy is not None:
        se = w * math.sqrt(vy / n)
        res["classical_ci"] = [res["classical"] - z * se, res["classical"] + z * se]
    return res


def judge_record() -> dict:
    """What the report must print about the judge: model id (None if unfrozen) and prompt hashes."""
    return {"model": JUDGE_MODEL, "prompt_sha256": prompt_sha256(),
            "match_prompt_sha256": match_prompt_sha256()}


# ---- location-less findings (the frozen matching rule, 2026-09-17; built 2026-10-29) --------------
# "Deterministic matcher first, LLM judge only for location-less findings (validated vs Krish's labels
# on >=100)". A finding that names no file cannot meet the L1 location rule, so -- and only then -- the
# judge is asked whether it describes the answer key's vulnerability. A "same_vulnerability" answer is
# credited as a match at level "J1" (counted like L1, never as L2), and ONLY after the judge was
# calibrated on this task (`credit_locationless` refuses otherwise). Nothing here runs by itself.

MATCH_LABELS = ("same_vulnerability", "different", "unsure")
JUDGE_LEVEL = "J1"

MATCH_PROMPT = """You are checking one security finding against one known vulnerability.
The finding names no file, so it could not be matched by location. Decide whether it describes
the SAME vulnerability as the known one.

Known vulnerability:
{vuln}

Finding:
{finding}

Answer with exactly one label and nothing else:
- same_vulnerability: the finding describes this vulnerability (the same flaw in the same code)
- different: it describes something else
- unsure: the finding gives too little to decide
"""


def match_prompt_sha256() -> str:
    return hashlib.sha256(MATCH_PROMPT.encode("utf-8")).hexdigest()


def _render_vuln(answer_key: dict) -> str:
    """The answer key as the judge sees it: its CWE(s) and fix sites (never the arm or the run)."""
    cwes = [c for c in [answer_key.get("cwe"), *(answer_key.get("cwes") or [])] if c]
    lines = [f"cwe: {', '.join(dict.fromkeys(cwes))}"] if cwes else []
    for s in answer_key.get("fix_sites") or []:
        where = f"{s.get('file')}" + (f" in {s.get('function')}" if s.get("function") else "")
        if s.get("line") is not None:
            where += f" (lines {s.get('line')}-{s.get('end_line', s.get('line'))})"
        lines.append(f"fixed at: {where}")
    return "\n".join(lines) or "(no description)"


def _parse_match(reply):
    if not isinstance(reply, str):
        return None
    for cand in (reply, reply.strip().splitlines()[0] if reply.strip() else ""):
        c = re.sub(r"[\s\-]+", "_", cand.strip().strip("`'\".*").lower())
        if c in MATCH_LABELS:
            return c
    return None


def classify_match(finding: dict, answer_key: dict, *, ask) -> dict:
    """Is location-less `finding` the answer key's vulnerability? Via the injected `ask(prompt) -> str`;
    an unparseable reply is `unsure`, the raw reply is kept. Exceptions from `ask` propagate."""
    reply = ask(MATCH_PROMPT.format(vuln=_render_vuln(answer_key), finding=_render(finding)))
    label = _parse_match(reply)
    return {"label": label or "unsure", "raw_reply": reply, "parsed": label is not None}


def credit_locationless(conn, run_id, findings: list, answer_keys: list, *, ask, calibration: dict) -> list:
    """Judge every location-less finding (file is None) of one run against each answer key the
    deterministic matcher left uncaught in that run, and write a "J1" matches row per
    "same_vulnerability" -- one-to-one: a finding credits one vuln, a vuln is credited once. Returns
    every decision {finding_idx, vuln_id, label, raw_reply, parsed} for the audit trail.

    Refuses (ValueError) unless the judge model is frozen (JUDGE_MODEL) and `calibration` is a
    `calibrate(..., categories=MATCH_LABELS)` result with `min_n_met` -- the frozen rule's >=100 labels.
    Whether its agreement is good enough to use is Krish's call, recorded with the tag."""
    from eval import match as _match
    if JUDGE_MODEL is None:
        raise ValueError("the judge model is not frozen (judge.JUDGE_MODEL is None)")
    if not (isinstance(calibration, dict) and calibration.get("min_n_met")
            and calibration.get("categories") == list(MATCH_LABELS)):
        raise ValueError("credit_locationless needs a calibration on the match task with >=100 labels "
                         "(judge.calibrate(..., categories=judge.MATCH_LABELS))")
    caught = {r["vuln_id"] for r in _match.read_matches(conn, run_id)}
    decisions, rows, used = [], [], set()
    for fi, f in enumerate(findings):
        if f.get("file") is not None:
            continue                                       # the location rule decides it, never the judge
        for key in answer_keys:
            vid = key.get("id")
            if vid in caught or vid in used:
                continue
            d = classify_match(f, key, ask=ask)
            decisions.append({"finding_idx": fi, "vuln_id": vid, **d})
            if d["label"] == "same_vulnerability":
                used.add(vid)
                rows.append({"finding_idx": fi, "vuln_id": vid, "level": JUDGE_LEVEL, "cwe_family": False,
                             "path_level": None})
                break
    if rows:
        _match.write_matches(conn, run_id, rows)
    return decisions
