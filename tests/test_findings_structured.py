"""Structured findings: leads and verdicts carry a validated file/line/function/CWE, the verifier's
reading overrides the analyst's, and the fingerprint collapses one bug reported by several shapes.
Pure -- no graph, no `claude`."""
from __future__ import annotations

from orion import discover, verify
from orion.contracts import Lead, Verdict, clean_location


def _lead(i=0, shape="A", conf="MEDIUM", **kw):
    return Lead(index=i, shape=shape, text=f"t{i}", evidence="e", confidence=conf, **kw)


def test_clean_location_validates_and_never_guesses():
    assert clean_location({"file": ".\\app\\x.js", "line_start": 5, "line_end": 9, "function": " f ",
                           "cwe": "cwe_89", "severity": "high"}) == {
        "file": "app/x.js", "line_start": 5, "line_end": 9, "function": "f", "cwe": "CWE-89",
        "severity": "HIGH"}
    assert clean_location({"file": "", "line_start": 0, "line_end": True, "cwe": "sqli",
                           "severity": "urgent", "function": "  "}) == {}
    assert clean_location({"line_start": 9, "line_end": 3}) == {"line_start": 9}   # inverted range
    assert clean_location({"cwe": 79}) == {"cwe": "CWE-79"}


def test_to_leads_carries_structured_fields():
    (lead,) = discover._to_leads({"leads": [{
        "text": "sqli", "evidence": "q", "confidence": "HIGH", "file": "app/db.js",
        "line_start": 12, "function": "find", "cwe": "89", "severity": "HIGH", "line_end": "x"}]}, "A")
    assert (lead.file, lead.line_start, lead.line_end, lead.function, lead.cwe) == (
        "app/db.js", 12, None, "find", "CWE-89")


def test_fingerprint_needs_file_cwe_and_where():
    assert _lead(file="a.js", cwe="CWE-79", function="render").fingerprint() == ("a.js", "CWE-79", "render")
    assert _lead(file="a.js", cwe="CWE-79", line_start=4).fingerprint() == ("a.js", "CWE-79", "4")
    assert _lead(file="a.js", cwe="CWE-79").fingerprint() is None
    assert _lead(file="a.js", function="f").fingerprint() is None


def test_dedup_collapses_one_bug_across_shapes_keeping_the_confident_report():
    leads = [
        _lead(0, "B", "LOW", file="server.js", cwe="CWE-79", function="setup"),
        _lead(1, "C", "HIGH", file="server.js", cwe="CWE-79", function="setup"),   # same bug
        _lead(2, "A", "HIGH", file="server.js", cwe="CWE-79", function="render"),  # other function
        _lead(3, "B", "LOW", file="server.js", cwe="CWE-352", function="setup"),   # other class
    ]
    kept = discover._dedup(leads)
    assert [(k.shape, k.function, k.cwe) for k in kept] == [
        ("C", "setup", "CWE-79"), ("A", "render", "CWE-79"), ("B", "setup", "CWE-352")]
    assert [k.index for k in kept] == [0, 1, 2]


def test_anchored_lead_is_never_displaced_by_a_fingerprint_duplicate():
    anchored = _lead(0, "A", "LOW", file="a.js", cwe="CWE-89", function="q", source_uid="s", sink_uid="k")
    dup = _lead(1, "B", "HIGH", file="a.js", cwe="CWE-89", function="q")
    (kept,) = discover._dedup([anchored, dup])
    assert kept.source_uid == "s"


def test_verdict_carries_verifier_location_and_it_wins():
    lead = _lead(file="app/a.js", line_start=10, line_end=12, function="f", cwe="CWE-89")
    v = verify._verdict_from_result(lead, {"decision": "CONFIRM", "reason": "r", "file": "app/b.js",
                                           "line_start": 40, "cwe": "CWE-943", "severity": "critical"})
    assert (v.file, v.line_start, v.cwe, v.severity) == ("app/b.js", 40, "CWE-943", "CRITICAL")
    loc = v.location()
    assert loc == {"file": "app/b.js", "line_start": 40, "line_end": None, "function": None,
                   "cwe": "CWE-943"}             # a moved file does not keep the old function/lines


def test_verdict_without_its_own_location_falls_back_to_the_lead():
    lead = _lead(file="app/a.js", line_start=10, function="f", cwe="CWE-89")
    v = Verdict(lead=lead, decision="CONFIRM", reason="r", line_end=14)
    assert v.location() == {"file": "app/a.js", "line_start": 10, "line_end": 14, "function": "f",
                            "cwe": "CWE-89"}


def test_verifier_message_shows_the_claimed_location():
    msg = verify._lead_message("sid", _lead(file="a.js", line_start=3, line_end=5, function="f", cwe="CWE-89"))
    assert "claimed location (unverified): a.js:3-5 (f) CWE-89" in msg
    assert "claimed location" not in verify._lead_message("sid", _lead())
