"""Lead dedup shared by both discovery engines: the Claude fleet (discover.py) and Orion-LM
(orion/lm/engine.py). Claude-free, so the lm package can use it; moved from discover._dedup on
2026-10-28 (the lm engine had kept a structural/lexical-only copy of an older version).
"""
from __future__ import annotations

from dataclasses import replace

from .contracts import Lead

_CONFIDENCE_RANK = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}


def dedup_keep(leads: list[Lead]) -> list[Lead]:
    """The leads that survive dedup, as the ORIGINAL objects in their kept order (`dedup` reindexes
    them; the Orion-LM engine keeps the originals to map each back to its batch).
    Every collapsed duplicate is one verifier session (the run's dominant cost) not spent.

    STRUCTURAL first: a lead anchored on a :CandidateFlow carries `source_uid`+`sink_uid`; two leads
    with the same (source_uid, sink_uid) are the SAME flow no matter how differently they are worded,
    so they collapse on that endpoint pair alone -- this is what the lexical key silently missed
    (agents describing one flow in different words double-counted). We key on the exact endpoint pair
    rather than clustering on partial (source-only / sink-only) overlap on purpose: two genuinely
    distinct bugs that merely share a source must NOT be merged in a precision-first tool.

    FINGERPRINT next: a lead that names its file + CWE + function (or line) is the same bug as any
    other lead with that fingerprint, whichever shape found it and however it is worded -- two
    shapes routinely report one XSS. The HIGHER-confidence lead is kept, in the earlier one's slot,
    unless the earlier one is structurally anchored (then it always stays).
    The key includes the function/line, so two bugs of one class in different functions stay apart.

    LEXICAL fallback: leads with neither anchor keep the original `(shape, text[:80])` key. The
    keyspaces are disjoint, so leads keyed differently never collide."""
    seen_struct: set[tuple[str, str]] = set()
    seen_lex: set[tuple[str, str]] = set()
    by_fingerprint: dict[tuple, int] = {}          # fingerprint -> position in `kept`
    kept: list[Lead] = []
    for lead in leads:
        fp = lead.fingerprint()
        if lead.source_uid and lead.sink_uid:
            key = (lead.source_uid, lead.sink_uid)
            if key in seen_struct:
                continue
            seen_struct.add(key)
            if fp is not None:
                by_fingerprint.setdefault(fp, len(kept))   # later unanchored duplicates fold into it
        elif fp is not None:
            pos = by_fingerprint.get(fp)
            if pos is not None:
                # An anchored lead is never displaced: its flow endpoints feed the verifier's
                # evidence subgraph. Otherwise the more confident report of the bug wins.
                incumbent = kept[pos]
                if (not incumbent.source_uid
                        and _CONFIDENCE_RANK[lead.confidence] > _CONFIDENCE_RANK[incumbent.confidence]):
                    kept[pos] = lead
                continue
            by_fingerprint[fp] = len(kept)
        else:
            key = (lead.shape, lead.text[:80])
            if key in seen_lex:
                continue
            seen_lex.add(key)
        kept.append(lead)
    return kept


def dedup(leads: list[Lead]) -> list[Lead]:
    """`dedup_keep`, reindexed sequentially (the shape discovery returns)."""
    return [replace(lead, index=i) for i, lead in enumerate(dedup_keep(leads))]
