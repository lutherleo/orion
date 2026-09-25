"""Frozen data contracts shared across every Orion component.

This module is the integration spine. Discovery, verification, reporting, and the monitor all
speak in these types, so the parallel build streams agree on these shapes without seeing each
other's code. Do not rename a field here without updating every consumer — that is the whole
point of freezing it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Literal

Shape = Literal["A", "B", "C", "D"]
Confidence = Literal["LOW", "MEDIUM", "HIGH"]
Decision = Literal["CONFIRM", "REJECT", "INCONCLUSIVE", "ERROR"]
Severity = Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]
_SEVERITIES = ("LOW", "MEDIUM", "HIGH", "CRITICAL")
_CWE = re.compile(r"(?:CWE)?[\s:_-]*(\d{1,4})$", re.IGNORECASE)


def clean_location(raw: dict) -> dict:
    """The structured location/classification fields of an agent reply, validated -- never guessed.

    Field names match the plain-agent findings schema (eval/arms/findings.schema.json) so Orion and a
    plain agent are scored the same way. Anything malformed is DROPPED (the field stays None), not
    repaired into something the agent did not say: `file` is forward-slashed and stripped of `./`;
    lines must be positive ints (line_end >= line_start); `cwe` normalizes "89" / "cwe_89" /
    "CWE-89" to "CWE-89"; `severity` must be one of LOW/MEDIUM/HIGH/CRITICAL. Pure."""
    out: dict = {}
    f = raw.get("file")
    if isinstance(f, str) and f.strip():
        f = f.strip().replace("\\", "/")
        out["file"] = f[2:] if f.startswith("./") else f
    for k in ("line_start", "line_end"):
        v = raw.get(k)
        if isinstance(v, int) and not isinstance(v, bool) and v >= 1:
            out[k] = v
    if "line_end" in out and out.get("line_start", 0) > out["line_end"]:
        del out["line_end"]
    fn = raw.get("function")
    if isinstance(fn, str) and fn.strip():
        out["function"] = fn.strip()
    m = _CWE.match(str(raw.get("cwe") or "").strip())
    if m:
        out["cwe"] = f"CWE-{int(m.group(1))}"
    sev = raw.get("severity")
    if isinstance(sev, str) and sev.strip().upper() in _SEVERITIES:
        out["severity"] = sev.strip().upper()
    return out


@dataclass
class Lead:
    """One candidate lead from the discovery fleet. Never a confirmed finding on its own."""
    index: int
    shape: Shape
    text: str          # human-readable candidate-lead statement
    evidence: str      # the Cypher query result / snippet the discoverer cited
    confidence: Confidence
    # Structural endpoints, set when the lead is anchored on a precomputed :CandidateFlow (shape A).
    # Two leads with the same (source_uid, sink_uid) are the SAME flow however differently worded, so
    # these drive structural dedup (discover._dedup). None for leads with no graph anchor (B/C/D),
    # which fall back to lexical dedup.
    source_uid: str | None = None
    sink_uid: str | None = None
    # Structured location + class, taken from the graph nodes the analyst queried (all optional; see
    # clean_location). They drive fingerprint dedup, SARIF output and structured scoring.
    file: str | None = None        # repo-relative path
    line_start: int | None = None
    line_end: int | None = None
    function: str | None = None
    cwe: str | None = None         # "CWE-<n>"

    def fingerprint(self) -> tuple[str, str, str] | None:
        """(file, cwe, function-or-line): the same bug however it is worded or whichever shape found
        it. None without both a file and a CWE (then dedup falls back to other keys)."""
        where = self.function or (str(self.line_start) if self.line_start else "")
        return (self.file, self.cwe, where) if (self.file and self.cwe and where) else None


@dataclass
class Verdict:
    """The independent verifier's decision on a single Lead."""
    lead: Lead
    decision: Decision
    reason: str
    evidence: str = ""  # what the verifier itself queried or read
    # Betweenness centrality (0-1) of the lead's sink call, when the lead is anchored on a
    # :CandidateFlow. A blast-radius signal: report ranking uses it to float a bug on a high-traffic
    # chokepoint above an equally-decided one in a backwater. 0.0 when unknown.
    sink_centrality: float = 0.0
    # The verifier's own reading of location/class/severity (optional). When given it overrides the
    # analyst's, because the verifier re-derived it from real source.
    file: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    cwe: str | None = None
    severity: Severity | None = None

    def location(self) -> dict:
        """Effective {file, line_start, line_end, function, cwe}: the verifier's value where it gave
        one, else the lead's. A corrected file resets the line range to the verifier's."""
        lead = self.lead
        moved = bool(self.file and self.file != lead.file)
        return {
            "file": self.file or lead.file,
            "line_start": self.line_start or (None if moved else lead.line_start),
            "line_end": self.line_end or (None if moved else lead.line_end),
            "function": None if moved else lead.function,
            "cwe": self.cwe or lead.cwe,
        }


# A progress event is a plain dict appended one-per-line to the run's JSONL log, and also the
# argument to OnEvent. Keys:
#   ts    : str  ISO timestamp
#   phase : "build" | "discover" | "verify" | "report"
#   shape : str | None   (discovery shape A/B/C/D, when relevant)
#   lead  : int | None   (lead index, when relevant)
#   turn  : int | None   (agent turn, when relevant)
#   event : "start" | "query" | "tool" | "lead" | "verdict" | "warn" | "done" | "error"
#   detail: str
ProgressEvent = dict
OnEvent = Callable[[ProgressEvent], None]
