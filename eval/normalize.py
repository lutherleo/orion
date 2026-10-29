"""Normalize every arm's output to one finding shape:
{arm, file, function, line, line_end, cwe, confidence, path: [{file, function, line, role}], raw_text}.

Never fabricates: anything absent from the raw finding is None ([] for path). Never drops a
finding the arm reported: location-less ones are returned with file=None for the judge to handle
later. What is NOT a finding (2026-10-29, eval/CHANGELOG.md; absorbs the old eval/convert.py): an
Orion verdict the verifier REJECTED or that ERRORed, and an INCONCLUSIVE one that WAS verified
(`counts_as_finding`) -- the verifier's ruling is the point of an Orion arm. An unverified lead
(the orion-lm arm without --lm-verify: INCONCLUSIVE "unverified ...") is a finding as written. A
verdict's location is `Verdict.location()`'s: the verifier's reading where it gave one.

On-disk formats read by load_arm:
  plain-*   : {"findings": [{file, function, line_start, line_end, cwe, title, explanation, path?}]}
  orion-lm* : a JSON list of dataclasses.asdict(Verdict) dicts (same as `orion scan --json`).
"""
import json
import os
import re


def load(path: str):
    """The parsed JSON at `path`, or [] when it is missing or unreadable (nothing invented)."""
    if not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return []


def counts_as_finding(verdict: dict) -> bool:
    """Whether one Orion verdict is a finding: CONFIRM always; INCONCLUSIVE only for a lead that was
    never verified (its reason starts "unverified", as orion.cli.lm_verdicts writes it); REJECT and
    ERROR never. A bare lead (no `decision`) is a finding as written."""
    decision = verdict.get("decision")
    if decision is None or decision == "CONFIRM":
        return True
    if decision == "INCONCLUSIVE":
        return str(verdict.get("reason") or "").startswith("unverified")
    return False


def _verdict_location(v: dict, lead: dict) -> dict:
    """orion.contracts.Verdict.location() on the JSON form (tests/test_normalize.py checks parity):
    the verifier's file / lines / cwe where it gave them, else the lead's; a corrected file resets the
    lines and the function to the verifier's."""
    moved = bool(v.get("file") and v.get("file") != lead.get("file"))
    return {"file": v.get("file") or lead.get("file"),
            "line_start": v.get("line_start") or (None if moved else _lead_line(lead)),
            "line_end": v.get("line_end") or (None if moved else lead.get("line_end")),
            "function": None if moved else lead.get("function"),
            "cwe": v.get("cwe") or lead.get("cwe")}

_EXT = r"(?:py|js|jsx|ts|tsx|mjs|cjs|java|go|rb|php|c|cc|cpp|h|hpp|cs|rs|kt|scala|swift|sql|html|ejs|xml|json|ya?ml)"
_FILE_RE = re.compile(r"(?<![\w./-])((?:[\w.-]+/)*[\w.-]+\.%s)(?![\w/])" % _EXT, re.I)
_CWE_RE = re.compile(r"\bCWE[-\s]?0*(\d+)", re.I)
_LINE_RE = re.compile(r"\blines?\s+(\d+)|\bL(\d+)\b|:(\d+)", re.I)
# well-known framework/library names that look like files; dropped when bare tokens
_STOP = {"express.js", "node.js", "vue.js", "react.js", "angular.js", "next.js", "nuxt.js",
         "d3.js", "three.js", "jquery.js"}


def _files(blob: str) -> list[str]:
    return [m.group(1) for m in _FILE_RE.finditer(blob)
            if "/" in m.group(1) or m.group(1).lower() not in _STOP]


def _cwe(*blobs: str):
    for b in blobs:
        m = _CWE_RE.search(b or "")
        if m:
            return f"CWE-{int(m.group(1))}"
    return None


def _same_cite(a: str, b: str) -> bool:
    """One file cited two ways: equal, or one is a path ending in "/" + the other ("server.js" and
    "fixtures/NodeGoat/server.js")."""
    return a == b or a.endswith("/" + b) or b.endswith("/" + a)


def _extract_file_line(text: str, evidence: str):
    """File may be cited in text or evidence (cf. run_nodegoat_eval._file_blob). Rank candidates:
    present in both text and evidence, then containing a '/', then bare name.ext; first-seen
    within a tier. A file counts as present in both when it is cited there in either form
    (`_same_cite`, 2026-10-29, eval/CHANGELOG.md); the longest form cited is returned."""
    in_text, in_ev = _files(text), _files(evidence)
    cands = list(dict.fromkeys(in_text + in_ev))
    if not cands:
        return None, None

    def cited(f, where):
        return any(_same_cite(f, w) for w in where)

    def tier(f):
        return 0 if (cited(f, in_text) and cited(f, in_ev)) else 1 if "/" in f else 2

    pick = min(cands, key=tier)  # min is stable: first-seen wins ties
    forms = sorted({c for c in cands if _same_cite(c, pick)}, key=len, reverse=True)
    file = forms[0]
    for blob in ((text, evidence) if cited(pick, in_text) else (evidence, text)):
        for form in forms:
            i = blob.find(form)
            if i >= 0:
                m = _LINE_RE.search(blob[i + len(form):i + len(form) + 40])
                if m:
                    return file, int(next(g for g in m.groups() if g))
    return file, None


def _path(raw) -> list[dict]:
    if not isinstance(raw, list):
        return []
    return [{"file": s.get("file"), "function": s.get("function"),
             "line": s.get("line"), "role": s.get("role")} for s in raw if isinstance(s, dict)]


def _lead_line(lead: dict):
    """A lead's line: `line_start` (contracts.Lead since the 2026-10-27 merge), else the `line` that
    lm runs recorded before it."""
    return lead.get("line_start") if lead.get("line_start") is not None else lead.get("line")


def finding(raw: dict, *, arm: str) -> dict:
    out = {"arm": arm, "file": None, "function": None, "line": None, "line_end": None, "cwe": None,
           "confidence": None, "path": [], "raw_text": ""}
    if arm.startswith("plain-"):
        out.update(file=raw.get("file"), function=raw.get("function"), line=raw.get("line_start"),
                   line_end=raw.get("line_end"), cwe=raw.get("cwe"), path=_path(raw.get("path")),
                   raw_text=" ".join(p for p in (raw.get("title"), raw.get("explanation")) if p))
        return out
    lead = raw.get("lead") if isinstance(raw.get("lead"), dict) else raw
    loc = _verdict_location(raw, lead) if lead is not raw else _verdict_location({}, lead)
    out["confidence"] = lead.get("confidence")
    out["raw_text"] = lead.get("text") or ""
    if lead.get("shape") == "LM" or arm.startswith("orion-lm") and loc["file"] is not None:
        out.update(file=loc["file"], function=loc["function"], line=loc["line_start"],
                   line_end=loc["line_end"], cwe=loc["cwe"], path=_path(lead.get("path")))
        return out
    # claude engine (or an LM lead without structure): location is prose
    text, ev = lead.get("text") or "", lead.get("evidence") or ""
    file, line = _extract_file_line(text, ev)
    out.update(file=loc["file"] or file, function=loc["function"],
               line=loc["line_start"] if loc["file"] else line,
               line_end=loc["line_end"] if loc["file"] else None,
               cwe=loc["cwe"] or _cwe(text, ev),
               path=_path(lead.get("path")))
    return out


def load_arm(path: str, *, arm: str) -> list[dict]:
    try:
        data = load(path)
        if arm.startswith("plain-"):
            items = data.get("findings") if isinstance(data, dict) else None
        else:
            items = [it for it in data if isinstance(it, dict) and counts_as_finding(it)] \
                if isinstance(data, list) else data
        if not isinstance(items, list):
            return []
        return [finding(it, arm=arm) for it in items if isinstance(it, dict)]
    except Exception:
        return []
