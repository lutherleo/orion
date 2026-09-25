import json
import os


def load(path: str) -> list[dict]:
    if not os.path.isfile(path):
        return []
    try:
        return json.loads(open(path).read())
    except Exception:
        return []


def confirmed_verdicts(verdicts: list[dict]) -> list[dict]:
    out = []
    for v in verdicts:
        if v.get("decision") != "CONFIRM":
            continue
        lead = v.get("lead") or {}
        out.append({
            "decision": "CONFIRM",
            "reason": v.get("reason") or "",
            "evidence": v.get("evidence") or "",
            "sink_centrality": float(v.get("sink_centrality") or 0.0),
            "text": lead.get("text") or "",
            "text_evidence": lead.get("evidence") or "",
            # Structured location, in the plain-agent schema's field names: the verifier's reading
            # first (it re-derived it from source), else the analyst's. Only fields that actually
            # carry a value -- an older, prose-only verdict gets no invented keys.
            **{k: val for k in ("file", "line_start", "line_end", "cwe", "function")
               if (val := v.get(k) or lead.get(k))},
        })
    return out
