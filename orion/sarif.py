"""Render verdicts as SARIF 2.1.0 -- the format GitHub code scanning (and most security dashboards)
ingest. Pure: verdicts in, a JSON-ready dict out.

Only findings worth a human's attention are emitted: CONFIRM as `error`, INCONCLUSIVE as `warning`.
REJECT (checked out clean) and ERROR (the verifier itself failed) are not findings. Each result uses
the verifier-corrected location (`Verdict.location()`), one rule per CWE, and a stable
`partialFingerprints` entry so a code-scanning backend tracks the same bug across runs instead of
reopening it. A verdict with no file cannot be placed in the code, so it is left out and counted in
`runs[0].properties.unlocated` (the text report still lists it).
"""
from __future__ import annotations

import hashlib

from .contracts import Verdict

SCHEMA_URI = "https://json.schemastore.org/sarif-2.1.0.json"
_LEVEL = {"CONFIRM": "error", "INCONCLUSIVE": "warning"}
_SECURITY_SEVERITY = {"LOW": "3.0", "MEDIUM": "5.5", "HIGH": "8.0", "CRITICAL": "9.5"}
_GENERIC_RULE = "orion/unclassified"


def _rule_id(cwe: str | None) -> str:
    return cwe or _GENERIC_RULE


def _rule(rule_id: str) -> dict:
    rule: dict = {"id": rule_id, "name": rule_id.replace("-", "").replace("/", "_"),
                  "shortDescription": {"text": rule_id},
                  "properties": {"tags": ["security"]}}
    if rule_id.startswith("CWE-"):
        n = rule_id.split("-", 1)[1]
        rule["helpUri"] = f"https://cwe.mitre.org/data/definitions/{n}.html"
        rule["properties"]["tags"].append(f"external/cwe/cwe-{n}")
    return rule


def fingerprint(v: Verdict) -> str:
    """Stable identity of a finding across runs: its file, class and function/line -- not its
    wording, which changes every run."""
    loc = v.location()
    key = "|".join(str(x or "") for x in (loc["file"], loc["cwe"], loc["function"] or loc["line_start"]))
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


def to_sarif(verdicts: list[Verdict]) -> dict:
    rules: dict[str, dict] = {}
    results: list[dict] = []
    unlocated = 0
    for v in verdicts:
        level = _LEVEL.get(v.decision)
        if level is None:
            continue
        loc = v.location()
        if not loc["file"]:
            unlocated += 1
            continue
        rule_id = _rule_id(loc["cwe"])
        rules.setdefault(rule_id, _rule(rule_id))
        region: dict = {}
        if loc["line_start"]:
            region["startLine"] = loc["line_start"]
            if loc["line_end"]:
                region["endLine"] = loc["line_end"]
        physical: dict = {"artifactLocation": {"uri": loc["file"], "uriBaseId": "%SRCROOT%"}}
        if region:
            physical["region"] = region
        props = {"shape": v.lead.shape, "confidence": v.lead.confidence, "decision": v.decision,
                 "sinkCentrality": round(v.sink_centrality, 4)}
        if v.severity:
            props["severity"] = v.severity
            props["security-severity"] = _SECURITY_SEVERITY[v.severity]
        result = {
            "ruleId": rule_id,
            "level": level,
            "message": {"text": f"{v.lead.text}\n\nVerifier ({v.decision}): {v.reason}".strip()},
            "locations": [{"physicalLocation": physical}],
            "partialFingerprints": {"orion/v1": fingerprint(v)},
            "properties": props,
        }
        if loc["function"]:
            result["locations"][0]["logicalLocations"] = [{"name": loc["function"], "kind": "function"}]
        results.append(result)
    return {
        "$schema": SCHEMA_URI,
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {
                "name": "Orion",
                "informationUri": "https://github.com/lutherleo/orion",
                "rules": [rules[k] for k in sorted(rules)],
            }},
            "originalUriBaseIds": {"%SRCROOT%": {"description": {"text": "the scanned repository"}}},
            "results": results,
            "properties": {"unlocated": unlocated},
        }],
    }
