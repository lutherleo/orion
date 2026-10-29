"""Gate 2 of the merge plan (Phase 6, decision D2): should Orion-LM's verifier (`orion scan --engine lm
--lm-verify`) be on by default? Measured on the bake-off's seeded dev sample, on ONE model, tool-less like
`train.devscore` (dev rows are code-only cases with no graph; a first action that is not `final` gets one
forced final).

Per dev row: discovery asks the row exactly as `devscore` does (the trained format). When it flags the row,
every lead is re-judged ALONE by the verifier's own TASK turn (`orion.lm.verify.task_message`, on the lead as
`orion.lm.engine` builds it) and its answer is mapped by `orion.lm.verify._verdict`, the runtime's rule:
CONFIRM / INCONCLUSIVE (LOW) / REJECT (no lead) / ERROR (overflow or no final). The model is re-seeded before
every session, as at runtime. A row stays flagged after verification iff one of its leads is CONFIRMed:
that is what `eval.normalize` counts as a finding once the verifier ran.

Reported: discovery's and the verified pipeline's metrics (`eval.bakeoff.METRICS`) with the paired
cluster-bootstrap CI of the change (`eval.bakeoff.paired`), the verifier's decisions on the real vulns
discovery found and on its false alarms, and both passes' tokens. `decision` applies `GATE2`, fixed here
BEFORE the run. All counts are measured; nothing is estimated.

    python -m eval.verifygate --model models/orion-lm --dev train/data/stage1.jsonl --temperature 0 \\
        --out eval/runs/gates/gate2-orion-lm.json
"""
from __future__ import annotations

import argparse
import json

from eval import bakeoff
from orion.lm import engine, sinks
from orion.lm import format as fmt
from orion.lm import model as lm_model
from orion.lm import verify
from train import devscore

# Gate 2 (D2): on by default only if the verifier keeps (almost) every real vuln discovery found AND
# measurably cuts its false alarms. PROPOSED 2026-10-29; Krish / Love Kush confirm or change it before the run.
GATE2 = {
    "max_vuln_loss": 0.02,       # share of discovery's flagged vulnerable rows the verifier un-flags (count)
    "false_alarm_cut_lo": 0.0,   # lower CI bound of the nonvuln_silence gain must be ABOVE this
}
DECISIONS = ("CONFIRM", "INCONCLUSIVE", "REJECT", "ERROR")


def _session(model, generate, messages: list[dict], acc: dict):
    seed = getattr(model, "seed_session", None)
    if callable(seed):
        seed()
    return devscore.final_leads(generate, messages, acc)


def _verify_lead(model, generate, case: dict, raw: dict, index: int, schema_text, acc: dict) -> str:
    lead = engine._to_lead(raw if isinstance(raw, dict) else {}, case, index, unresolved=False)
    messages = [fmt.system_message(schema_text), verify.task_message(case, lead)]
    leads, _pf, _overflow = _session(model, generate, messages, acc)
    if leads is None:
        return "ERROR"
    return verify._verdict(lead, str(case.get("flow_id")), [x for x in leads if isinstance(x, dict)], []).decision


def run(model, dev_examples, *, sample_n: int = 300, seed: int = 0, schema_text=None) -> dict:
    """Score `model` (anything with `generate(messages)`) on the seeded dev sample; see the module doc."""
    generate = model.generate if hasattr(model, "generate") else model
    sample = bakeoff.sample_dev(dev_examples, sample_n=sample_n, seed=seed)
    rows = [r for r in map(devscore.normalise_row, sample) if r["split"] == "dev"]
    acc_d = {"tokens": 0, "completion": 0, "seconds": 0.0}
    acc_v = {"tokens": 0, "completion": 0, "seconds": 0.0}
    before, after, per_row = [], [], []
    counts = {"vulnerable": dict.fromkeys(DECISIONS, 0), "false_alarm": dict.fromkeys(DECISIONS, 0)}
    for i, r in enumerate(rows):
        leads, pf, overflow = _session(model, generate, devscore.messages_for(r, schema_text), acc_d)
        leads = [x for x in (leads or []) if x]
        case = sinks.annotate(r["case"])
        decisions = [_verify_lead(model, generate, case, raw, k, schema_text, acc_v) for k, raw in enumerate(leads)]
        flagged, kept = bool(leads), "CONFIRM" in decisions
        if flagged:
            bucket = counts["vulnerable" if r["label"] == "vulnerable" else "false_alarm"]
            # a row is counted once, by its best outcome: CONFIRM > INCONCLUSIVE > REJECT > ERROR
            bucket[next(d for d in DECISIONS if d in decisions)] += 1
        base = {"i": i, "pair_id": r["pair_id"], "label": r["label"]}
        before.append({**base, "flagged": flagged})
        after.append({**base, "flagged": kept})
        per_row.append({**base, "flagged": flagged, "parse_failed": pf, "overflow": overflow,
                        "decisions": decisions})
    p = bakeoff.paired(before, after)
    found = sum(counts["vulnerable"].values())
    lost = found - counts["vulnerable"]["CONFIRM"]
    alarms = sum(counts["false_alarm"].values())
    out = {"model": None, "n": len(rows), "n_pairs": p["n_pairs"], "sample_n": sample_n, "seed": seed,
           "discovery": p["a"], "verified": p["b"], "diff": p["diff"], "ci": p["ci"], "B": p["B"],
           "verifier_on_real_vulns": {**counts["vulnerable"], "found": found, "lost": lost,
                                      "lost_rate": lost / found if found else None},
           "verifier_on_false_alarms": {**counts["false_alarm"], "flagged": alarms,
                                        "removed": alarms - counts["false_alarm"]["CONFIRM"],
                                        "removed_rate": ((alarms - counts["false_alarm"]["CONFIRM"]) / alarms
                                                         if alarms else None)},
           "tokens": {"discovery": acc_d["tokens"], "verify": acc_v["tokens"]},
           "rule": dict(GATE2), "rows": per_row}
    ran = getattr(model, "sampling", None)
    out["sampling"] = dict(ran) if isinstance(ran, dict) else None
    out["decision"] = decide(out)
    return out


def decide(result: dict, rule: dict | None = None) -> str:
    """DEFAULT_ON when the verifier loses at most `max_vuln_loss` of the real vulns discovery found AND the
    CI of the nonvuln_silence gain is above `false_alarm_cut_lo`; KEEP_OPT_IN when either fails;
    INCONCLUSIVE when discovery found no real vuln or the CI cannot be computed."""
    rule = GATE2 if rule is None else rule
    lost_rate = result["verifier_on_real_vulns"]["lost_rate"]
    cut_lo = result["ci"]["nonvuln_silence"][0]
    if lost_rate is None or cut_lo is None:
        return "INCONCLUSIVE"
    if lost_rate <= rule["max_vuln_loss"] and cut_lo > rule["false_alarm_cut_lo"]:
        return "DEFAULT_ON"
    return "KEEP_OPT_IN"


def text(result: dict) -> str:
    def cell(v):
        return "n/a" if v is None else f"{v:.3f}"

    def signed(v):
        return "n/a" if v is None else f"{v:+.3f}"
    temp = (result.get("sampling") or {}).get("temperature")
    lines = [f"Gate 2: --lm-verify on {result['model']} at temperature {temp} ({result['n']} dev rows, "
             f"{result['n_pairs']} pairs; 95% CI, B={result['B']}, cluster = pair)",
             "| metric | discovery | + verifier | diff | CI |", "|---|---|---|---|---|"]
    for m in bakeoff.METRICS:
        lo, hi = result["ci"][m]
        lines.append(f"| {m} | {cell(result['discovery'][m])} | {cell(result['verified'][m])} | "
                     f"{signed(result['diff'][m])} | [{signed(lo)}, {signed(hi)}] |")
    rv, fa = result["verifier_on_real_vulns"], result["verifier_on_false_alarms"]
    lines.append(f"real vulns discovery found: {rv['found']}; the verifier kept {rv['CONFIRM']}, lost {rv['lost']} "
                 f"({rv['INCONCLUSIVE']} INCONCLUSIVE, {rv['REJECT']} REJECT, {rv['ERROR']} ERROR)")
    lines.append(f"false alarms discovery raised: {fa['flagged']}; the verifier removed {fa['removed']} "
                 f"({fa['INCONCLUSIVE']} INCONCLUSIVE, {fa['REJECT']} REJECT, {fa['ERROR']} ERROR), kept {fa['CONFIRM']}")
    lines.append(f"tokens: discovery {result['tokens']['discovery']}, verifier {result['tokens']['verify']}")
    r = result["rule"]
    lines.append(f"rule: lost share <= {r['max_vuln_loss']} and nonvuln_silence gain CI low > "
                 f"{r['false_alarm_cut_lo']} -> {result['decision']}")
    return "\n".join(lines)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--model", required=True, help="model dir (e.g. models/orion-lm)")
    ap.add_argument("--dev", required=True, help="dev examples JSONL (the bake-off's input)")
    ap.add_argument("--sample-n", type=int, default=300)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--temperature", type=float, default=0.0, help="sampling temperature (default 0, greedy)")
    ap.add_argument("--out", default=None, help="write the full result (per-row decisions included) here")
    a = ap.parse_args(argv)
    with open(a.dev, encoding="utf-8") as f:
        dev = [json.loads(ln) for ln in f if ln.strip()]
    model = lm_model.load(a.model, temperature=a.temperature)
    result = run(model, dev, sample_n=a.sample_n, seed=a.seed)
    result["model"] = str(a.model)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=1)
    print(text(result))


if __name__ == "__main__":
    main()
