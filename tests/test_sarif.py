"""SARIF 2.1.0 export (orion/sarif.py). Pure -- verdicts in, dict out."""
from __future__ import annotations

import json

from orion import report
from orion.contracts import Lead, Verdict
from orion.sarif import fingerprint, to_sarif


def _v(decision, i=0, **lead_kw):
    lead = Lead(index=i, shape="A", text=f"claim {i}", evidence="e", confidence="HIGH", **lead_kw)
    return Verdict(lead=lead, decision=decision, reason="because", sink_centrality=0.25)


def _run(verdicts):
    doc = to_sarif(verdicts)
    json.dumps(doc)                                   # serializable as-is
    return doc, doc["runs"][0]


def test_document_shape_levels_and_rules():
    vs = [_v("CONFIRM", 0, file="app/a.js", line_start=3, line_end=4, function="f", cwe="CWE-89"),
          _v("INCONCLUSIVE", 1, file="app/b.js", cwe="CWE-79"),
          _v("REJECT", 2, file="app/c.js", cwe="CWE-79"),
          _v("ERROR", 3, file="app/d.js", cwe="CWE-22"),
          _v("CONFIRM", 4, file="app/e.js")]                       # no CWE -> generic rule
    doc, run = _run(vs)
    assert doc["version"] == "2.1.0" and doc["$schema"].endswith("sarif-2.1.0.json")
    assert run["tool"]["driver"]["name"] == "Orion"
    assert [r["id"] for r in run["tool"]["driver"]["rules"]] == ["CWE-79", "CWE-89", "orion/unclassified"]
    assert run["tool"]["driver"]["rules"][1]["helpUri"].endswith("/definitions/89.html")
    results = run["results"]
    assert [(r["ruleId"], r["level"]) for r in results] == [
        ("CWE-89", "error"), ("CWE-79", "warning"), ("orion/unclassified", "error")]
    loc = results[0]["locations"][0]
    assert loc["physicalLocation"]["artifactLocation"]["uri"] == "app/a.js"
    assert loc["physicalLocation"]["region"] == {"startLine": 3, "endLine": 4}
    assert loc["logicalLocations"] == [{"name": "f", "kind": "function"}]
    assert "region" not in results[1]["locations"][0]["physicalLocation"]
    assert results[0]["properties"]["decision"] == "CONFIRM"


def test_unlocated_findings_are_counted_not_emitted():
    _, run = _run([_v("CONFIRM", 0, cwe="CWE-89"), _v("CONFIRM", 1, file="x.py", cwe="CWE-89")])
    assert len(run["results"]) == 1 and run["properties"]["unlocated"] == 1


def test_fingerprint_is_stable_across_wording_and_uses_verifier_location():
    a = _v("CONFIRM", 0, file="x.py", cwe="CWE-89", function="q")
    b = _v("CONFIRM", 7, file="x.py", cwe="CWE-89", function="q")
    b.lead.text = "worded completely differently"
    assert fingerprint(a) == fingerprint(b)
    a.file, a.severity = "y.py", "HIGH"                       # verifier moved it
    _, run = _run([a])
    assert run["results"][0]["locations"][0]["physicalLocation"]["artifactLocation"]["uri"] == "y.py"
    assert run["results"][0]["properties"]["security-severity"] == "8.0"
    assert fingerprint(a) != fingerprint(b)


def test_report_shows_the_location_line():
    v = _v("CONFIRM", 0, file="app/a.js", line_start=3, function="f", cwe="CWE-89")
    v.severity = "HIGH"
    assert "location:          app/a.js:3 (f) CWE-89 HIGH" in report.render([v])
    assert "location:" not in report.render([_v("CONFIRM")])
