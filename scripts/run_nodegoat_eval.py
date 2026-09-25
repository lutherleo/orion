#!/usr/bin/env python
"""NodeGoat recall harness — scores Orion's full pipeline against the 15 ground-truth vulns.

Runs (or reuses) a scan, discovers + verifies, then matches every CONFIRMED verdict to
`tests.ground_truth_nodegoat.GROUND_TRUTH` by **file overlap + vulnerability class**, and prints:

    - N/15 recall (and N/14 over the checkable subset; A9/deps is a known data-gap),
    - which ground-truth vulns were MISSED,
    - which CONFIRMED verdicts matched no ground truth (candidate false positives).

Usage:
    ./.venv/bin/python scripts/run_nodegoat_eval.py [REPO] [--scan-id ID] [--json OUT] [--quiet]

REPO defaults to fixtures/NodeGoat. Pass --scan-id to reuse an already built+indexed scan
(skips build/index — the integration path builds once, then evals without rebuilding).

The matching logic (`match_verdicts`) is a pure function so it can be unit-tested without tokens.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tests.ground_truth_nodegoat import (
    CLASS_KEYWORDS, GROUND_TRUTH, RECALL_BAR, TOTAL, CHECKABLE, GroundTruth)
from bench import scoring


def _finding_of(v) -> tuple[str, str]:
    """One CONFIRM verdict as the (text, evidence) pair the shared matcher scores: the lead's focused
    CLAIM (`lead.text`) + its cited `lead.evidence`. Class token comes from text, file from both."""
    lead = v.lead
    return (getattr(lead, "text", "") or "", getattr(lead, "evidence", "") or "")


def match_verdicts(confirmed) -> tuple[dict[str, list[int]], list[int]]:
    """Pure matcher (thin wrapper over bench.scoring.match). Given the list of CONFIRM verdicts, return
    (found: {gt_id -> [verdict indices that matched it]}, unmatched: [indices matching no gt])."""
    return scoring.match([_finding_of(v) for v in confirmed], GROUND_TRUTH, CLASS_KEYWORDS)


def _run_pipeline(repo: str, scan_id: str | None, quiet: bool):
    """Build/index (unless scan_id given) then discover + verify. Returns (scan_id, verdicts)."""
    from orion import graph_build, embed, discover, verify
    from orion.graph import profiles

    # Measure the SAME discovery path `orion scan` ships: profile-injected prompt (cli.py passes it).
    profile = profiles.select_profile(repo)

    def on_event(ev: dict) -> None:
        if quiet:
            return
        phase = ev.get("phase", ev.get("event", "?"))
        detail = ev.get("detail", "")
        if isinstance(detail, str) and len(detail) > 100:
            detail = detail[:100] + "…"
        print(f"  [{phase}] {ev.get('event','')} {detail}".rstrip(), file=sys.stderr)

    if scan_id is None:
        print(f"[eval] building graph for {repo} …", file=sys.stderr)
        scan_id = graph_build.build(repo)
        print(f"[eval] scan_id = {scan_id}", file=sys.stderr)
        try:
            print("[eval] indexing (semantic) …", file=sys.stderr)
            embed.index(repo, scan_id)
        except Exception as e:  # embedding is best-effort; graph-only path still works
            print(f"[eval] WARN: embed.index failed ({e!r}); continuing graph-only", file=sys.stderr)
    else:
        print(f"[eval] reusing scan_id = {scan_id} (skipping build/index)", file=sys.stderr)

    print(f"[eval] discovery … (profile: {profile.name})", file=sys.stderr)
    leads = discover.discover(scan_id, on_event, profile)
    print(f"[eval] {len(leads)} leads", file=sys.stderr)

    print("[eval] verification …", file=sys.stderr)
    verdicts = verify.verify_all(scan_id, leads, repo, on_event)
    return scan_id, verdicts


def render(confirmed, found, unmatched) -> str:
    n_found = len(found)
    lines: list[str] = []
    lines.append("=" * 72)
    lines.append(f"NodeGoat recall: {n_found}/{TOTAL}  (checkable {n_found}/{CHECKABLE}; bar = {RECALL_BAR})")
    verdict = "PASS ✅" if n_found >= RECALL_BAR else "BELOW BAR ❌"
    lines.append(f"  {verdict}   false-positive candidates: {len(unmatched)}")
    lines.append("=" * 72)
    lines.append("")
    lines.append("Ground truth:")
    for gt in GROUND_TRUTH:
        if gt.id in found:
            tag = "FOUND  ✅"
            by = " (verdict " + ",".join(f"#{i}" for i in found[gt.id]) + ")"
        else:
            tag = "MISS   ✗ " if not gt.known_gap else "MISS   – "
            by = "  [known data-gap]" if gt.known_gap else ""
        lines.append(f"  {tag} {gt.id:<6} {gt.name}{by}")
    lines.append("")
    if unmatched:
        lines.append("CONFIRMED verdicts matching NO ground truth (candidate false positives):")
        for i in unmatched:
            v = confirmed[i]
            lines.append(f"  #{i} [{v.lead.shape}/{v.lead.confidence}] {v.lead.text[:90]}")
        lines.append("")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="NodeGoat recall harness for Orion")
    ap.add_argument("repo", nargs="?", default="fixtures/NodeGoat", help="target repo (default fixtures/NodeGoat)")
    ap.add_argument("--scan-id", default=None, help="reuse an already built+indexed scan (skip build/index)")
    ap.add_argument("--json", dest="json_out", default=None, help="also write a JSON result to this path")
    ap.add_argument("--quiet", action="store_true", help="suppress per-event progress on stderr")
    args = ap.parse_args(argv)

    scan_id, verdicts = _run_pipeline(args.repo, args.scan_id, args.quiet)
    confirmed = [v for v in verdicts if v.decision == "CONFIRM"]
    found, unmatched = match_verdicts(confirmed)

    text = render(confirmed, found, unmatched)
    print("\n" + text)

    if args.json_out:
        payload = {
            "scan_id": scan_id,
            "recall": len(found),
            "total": TOTAL,
            "checkable": CHECKABLE,
            "bar": RECALL_BAR,
            "pass": len(found) >= RECALL_BAR,
            "found": {gt_id: idxs for gt_id, idxs in found.items()},
            "missed": [gt.id for gt in GROUND_TRUTH if gt.id not in found],
            "false_positive_candidates": len(unmatched),
            "verdicts": [dataclasses.asdict(v) for v in verdicts],
        }
        Path(args.json_out).write_text(json.dumps(payload, indent=2))
        print(f"[eval] wrote {args.json_out}", file=sys.stderr)

    return 0 if len(found) >= RECALL_BAR else 1


if __name__ == "__main__":
    raise SystemExit(main())
