"""Shared, benchmark-agnostic finding→ground-truth matcher (PLAN2).

Lifted verbatim (semantics-preserving) from `scripts/run_nodegoat_eval.py`, generalized to take the
ground-truth set + class-keyword map as arguments so BOTH NodeGoat and PyGoat — and every study arm
(Orion, ungrounded LLM review, Semgrep) — score through the exact same logic. A finding is just a
`(text, evidence)` pair: `text` is the focused CLAIM, `evidence` is the supporting query/snippet.

Match rule (asymmetric on purpose, the property that held 14/15 @ 0 FP on NodeGoat):
  - FILE match comes from text+evidence (a lead often names the file in either place);
  - CLASS token comes from the focused CLAIM only (text), so a broad evidence dump can't cross-credit
    several ground truths at once.
"""
from __future__ import annotations

from typing import Iterable, Sequence


def _text_blob(text: str) -> str:
    return (text or "").lower()


def _file_blob(text: str, evidence: str) -> str:
    return " ".join(p for p in (text or "", evidence or "") if p).lower()


def _matches(gt, text_blob: str, file_blob: str, class_keywords: dict[str, tuple[str, ...]]) -> bool:
    """A finding matches a ground truth iff (a gt file appears in text+evidence) AND (a distinctive
    class token appears in the claim). Identical rule to the committed NodeGoat matcher."""
    if not any(f.lower() in file_blob for f in gt.files):
        return False
    kws = class_keywords.get(gt.id, ())
    if kws and not any(k in text_blob for k in kws):
        return False
    return True


def match(findings: Sequence[tuple[str, str]], ground_truth: Iterable,
          class_keywords: dict[str, tuple[str, ...]]) -> tuple[dict[str, list[int]], list[int]]:
    """Given `findings` as (text, evidence) pairs, return (found: {gt_id -> [indices]}, unmatched:
    [indices matching no gt]). Pure."""
    ground_truth = list(ground_truth)
    text_blobs = [_text_blob(t) for t, _ in findings]
    file_blobs = [_file_blob(t, e) for t, e in findings]
    found: dict[str, list[int]] = {}
    matched_any: set[int] = set()
    for gt in ground_truth:
        hits = [i for i in range(len(findings))
                if _matches(gt, text_blobs[i], file_blobs[i], class_keywords)]
        if hits:
            found[gt.id] = hits
            matched_any.update(hits)
    unmatched = [i for i in range(len(findings)) if i not in matched_any]
    return found, unmatched


def score(findings: Sequence[tuple[str, str]], ground_truth: Iterable,
          class_keywords: dict[str, tuple[str, ...]]) -> dict:
    """Full scored result for one arm×benchmark: recall, missed ids, false-positive candidates."""
    ground_truth = list(ground_truth)
    found, unmatched = match(findings, ground_truth, class_keywords)
    total = len(ground_truth)
    checkable = sum(1 for g in ground_truth if not getattr(g, "known_gap", False))
    return {
        "recall": len(found),
        "total": total,
        "checkable": checkable,
        "found": {gid: idxs for gid, idxs in found.items()},
        "missed": [g.id for g in ground_truth if g.id not in found],
        "false_positive_candidates": len(unmatched),
        "false_positive_indices": unmatched,
    }
