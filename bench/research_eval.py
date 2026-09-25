#!/usr/bin/env python
"""PLAN2 research-eval runner: run ONE arm on ONE benchmark, score it, write a result JSON.

    python bench/research_eval.py --arm {A,B,C,D,O} --benchmark {nodegoat,pygoat} --repo <path> \
        [--model <id>] [--label <name>] [--scan-id <id>] [--semgrep-config auto] [--out <path>] [--quiet]

Arms (PLAN2):
  A  Local + Orion       — full graph pipeline (build/reuse → discover → verify); findings = CONFIRMs.
  B  Local, no Orion     — ungrounded LLM source review (bench.ungrounded_review).
  C  Frontier, no Orion  — same ungrounded review at --model claude-opus-5.
  D  Semgrep             — deterministic scanner (bench.semgrep_adapter); no tokens.
  O  Orion + Claude      — arm A's pipeline on a Claude model (--model, default config.MODEL): the
                           row that compares Orion itself against C on the same matcher and ledger.
                           Use --label to keep several models apart (O-sonnet, O-opus).

Everything is scored through the SAME matcher (bench.scoring) against the benchmark's ground truth, and
LLM arms accumulate token/cost via bench.token_ledger (fed by the usage events run_agent now emits).
Writes {arm, benchmark, model, recall, total, checkable, found, missed, false_positives, tokens,
wallclock_s, findings} to bench/research/<benchmark>/<arm>.json (or --out).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from bench import scoring
from bench.token_ledger import TokenLedger


def _ground_truth(benchmark: str):
    if benchmark == "nodegoat":
        from tests import ground_truth_nodegoat as m
    elif benchmark == "pygoat":
        from tests import ground_truth_pygoat as m
    else:
        raise SystemExit(f"unknown benchmark {benchmark!r} (nodegoat|pygoat)")
    return m.GROUND_TRUTH, m.CLASS_KEYWORDS, getattr(m, "RECALL_BAR", None)


def _mk_on_event(ledger: TokenLedger, quiet: bool):
    def _log(ev: dict) -> None:
        if quiet:
            return
        detail = ev.get("detail", "")
        if isinstance(detail, str) and len(detail) > 120:
            detail = detail[:120] + "…"
        print(f"  [{ev.get('phase','?')}/{ev.get('event','')}] {detail}".rstrip(), file=sys.stderr)
    return ledger.wrap(_log)


# ── Arm A: Local + Orion (full grounded pipeline) ──────────────────────────────────────────────
def _arm_orion(repo: str, scan_id: str | None, on_event) -> tuple[list[tuple[str, str, str]], list[dict]]:
    """Run Orion's pipeline exactly as `orion scan` does (same profile, same graph-sized discovery
    timeout). Returns (findings, verdict_rows): findings are the CONFIRMs as (text, evidence, file)
    triples -- the file is the verifier-corrected structured location -- and verdict_rows records
    EVERY decision so false positives and verifier spend can be audited afterwards."""
    from orion import config, discover, embed, graph_build, graphdb, verify
    from orion.graph import profiles
    profile = profiles.select_profile(repo)
    if scan_id is None:
        print(f"[eval] building graph for {repo} …", file=sys.stderr)
        scan_id = graph_build.build(repo, on_event=on_event, stream=True)
        try:
            embed.index(repo, scan_id)
        except Exception as e:  # noqa: BLE001 — embedding best-effort
            print(f"[eval] WARN embed.index failed ({e!r}); graph-only", file=sys.stderr)
    db = graphdb.GraphDB()
    try:
        timeout = config.discover_timeout(db.node_count(scan_id))
    finally:
        db.close()
    leads = discover.discover(scan_id, on_event, profile, timeout=timeout)
    verdicts = verify.verify_all(scan_id, leads, repo, on_event)
    rows = [{"decision": v.decision, "shape": v.lead.shape, "text": v.lead.text[:300],
             **{k: val for k, val in v.location().items() if val}} for v in verdicts]
    findings = [(v.lead.text or "", v.lead.evidence or "", v.location()["file"] or "")
                for v in verdicts if v.decision == "CONFIRM"]
    return findings, rows


# ── Arms B/C: ungrounded LLM review ────────────────────────────────────────────────────────────
def _arm_ungrounded(repo: str, on_event) -> list[tuple[str, str]]:
    from bench import ungrounded_review
    result = ungrounded_review.review(repo, on_event)
    if isinstance(result, dict) and "_error" in result:
        print(f"[eval] ungrounded review error: {result['_error'][:200]}", file=sys.stderr)
    return ungrounded_review.findings_from_result(result)


# ── Arm D: Semgrep ─────────────────────────────────────────────────────────────────────────────
def _arm_semgrep(repo: str, ruleset: str) -> tuple[list[tuple[str, str]], dict]:
    from bench import semgrep_adapter
    sg = semgrep_adapter.run_semgrep(repo, ruleset=ruleset)
    if "_error" in sg:
        print(f"[eval] semgrep error: {sg['_error']}", file=sys.stderr)
        return [], {"error": sg["_error"]}
    return semgrep_adapter.findings_from_semgrep(sg), sg.get("_meta", {})


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="PLAN2 research-eval runner")
    ap.add_argument("--arm", required=True, choices=["A", "B", "C", "D", "O"])
    ap.add_argument("--label", default=None,
                    help="result name (default: the arm) -> bench/research/<benchmark>/<label>.json")
    ap.add_argument("--benchmark", required=True, choices=["nodegoat", "pygoat"])
    ap.add_argument("--repo", required=True, help="path to the target checkout")
    ap.add_argument("--model", default=None, help="override ORION_MODEL/config.MODEL for this arm")
    ap.add_argument("--scan-id", default=None, help="reuse an existing scan (arm A only)")
    ap.add_argument("--semgrep-config", default="auto", help="semgrep --config ruleset (arm D)")
    ap.add_argument("--out", default=None, help="output JSON path (default bench/research/<bm>/<arm>.json)")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    if args.model:                      # per-arm model override (claude_cli reads config.MODEL at call time)
        from orion import config
        config.MODEL = args.model

    ground_truth, class_keywords, bar = _ground_truth(args.benchmark)
    # Backfill model for pricing: the CLI reports an empty model on a Pro/OAuth subscription, so the
    # ledger can't price real token counts unless we tell it which model this arm ran (arms A/B/C only).
    from orion import config as _cfg0
    default_model = "" if args.arm == "D" else (args.model or _cfg0.MODEL)
    ledger = TokenLedger(default_model=default_model)
    on_event = _mk_on_event(ledger, args.quiet)
    extra_meta: dict = {}
    verdict_rows: list[dict] | None = None

    t0 = time.monotonic()
    if args.arm in ("A", "O"):
        findings, verdict_rows = _arm_orion(args.repo, args.scan_id, on_event)
    elif args.arm in ("B", "C"):
        findings = _arm_ungrounded(args.repo, on_event)
    else:  # D
        findings, extra_meta = _arm_semgrep(args.repo, args.semgrep_config)
    wallclock = round(time.monotonic() - t0, 1)

    scored = scoring.score(findings, ground_truth, class_keywords)
    from orion import config as _cfg
    result = {
        "arm": args.arm,
        "label": args.label or args.arm,
        "benchmark": args.benchmark,
        "repo": args.repo,
        "model": args.model or (_cfg.MODEL if args.arm != "D" else "semgrep"),
        "recall": scored["recall"],
        "total": scored["total"],
        "checkable": scored["checkable"],
        "recall_bar": bar,
        "found": scored["found"],
        "missed": scored["missed"],
        "false_positive_candidates": scored["false_positive_candidates"],
        # The unmatched findings themselves, so "true" false positives can be hand-labelled.
        "false_positive_candidates_detail": [
            {"text": findings[i][0], "file": (findings[i][2] if len(findings[i]) > 2 else "")}
            for i in scored["false_positive_indices"]],
        "n_findings": len(findings),
        "findings": [{"text": f[0], "evidence": f[1][:400],
                      **({"file": f[2]} if len(f) > 2 and f[2] else {})} for f in findings],
        "tokens": ledger.summary(),
        "wallclock_s": wallclock,
        "extra": extra_meta,
    }
    if verdict_rows is not None:          # Orion arms: every decision, not just the CONFIRMs
        result["verdicts"] = verdict_rows
        result["decisions"] = {d: sum(1 for r in verdict_rows if r["decision"] == d)
                               for d in ("CONFIRM", "INCONCLUSIVE", "REJECT", "ERROR")}

    name = args.label or args.arm
    out = Path(args.out) if args.out else _ROOT / "bench" / "research" / args.benchmark / f"{name}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, default=str))

    tot = result["tokens"]["total"]
    print(f"\narm {name} / {args.benchmark}: recall {scored['recall']}/{scored['total']} "
          f"(checkable {scored['checkable']}), FP-candidates {scored['false_positive_candidates']}, "
          f"{tot['total_tokens']} tokens, ${tot['priced_cost_usd']:.4f} priced, {wallclock}s")
    print(f"[eval] wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
