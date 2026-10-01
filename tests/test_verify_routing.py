"""Adaptive verification (opt-in): deterministic routing, a smaller-budget light pass on the SAME model,
and escalation of anything the light pass can't settle. Token-free -- run_agent is stubbed."""
from __future__ import annotations

import json

from orion import claude_cli, cli, config, report, sarif, verify
from orion.contracts import Lead, Verdict


def _lead(shape="A", cwe=None, anchored=False, **kw):
    if anchored:
        kw.update(source_uid="a" * 40, sink_uid="b" * 40)
    return Lead(index=0, shape=shape, text="claim", evidence="e", confidence="HIGH", cwe=cwe, **kw)


def _sub(*files):
    return {"path": [{"uid": str(i), "file_path": f} for i, f in enumerate(files)], "sink_centrality": 0.1}


# ── the routing table ──────────────────────────────────────────────────
def test_route_table():
    r = verify.route
    assert r(_lead("D", "CWE-1333")) == "light"                     # ReDoS literal
    assert r(_lead("B", "CWE-693")) == "light"                      # missing protection header
    assert r(_lead("C")) == "light"                                 # unanchored disabled-fix lead
    assert r(_lead("D")) == "light"
    assert r(_lead("A")) == "full"                                  # data flow by default
    assert r(_lead("B")) == "full"                                  # absent control without a CWE
    for cwe in ("CWE-284", "CWE-285", "CWE-287", "CWE-639", "CWE-862", "CWE-863"):
        assert r(_lead("C", cwe)) == "full"                         # authz/auth: always full
    assert r(_lead("A", "CWE-1333", anchored=True), _sub("a.js", "b.js")) == "full"   # crosses files
    assert r(_lead("A", "CWE-1333", anchored=True), None) == "full"                    # path unknown
    assert r(_lead("A", "CWE-1333", anchored=True), _sub("a.js", "a.js")) == "light"   # one file


# ── the light pass, escalation, and routing off ────────────────────────
def _run(lead, decisions, routing=True, subgraph=None):
    calls = []

    def run_agent(sid, system, message, **kw):
        calls.append({"sid": sid, **kw})
        return {"decision": decisions[len(calls) - 1], "reason": "r"}

    v = verify.verify_lead("sid", lead, ".", lambda ev: None, run_agent,
                           fetch_subgraph=(lambda *a: subgraph) if subgraph else None, routing=routing)
    return v, calls


def test_light_pass_settles_a_clear_lead_on_a_small_budget():
    v, calls = _run(_lead("D", "CWE-1333"), ["CONFIRM"])
    assert (v.decision, v.route, len(calls)) == ("CONFIRM", "light", 1)
    assert calls[0]["max_turns"] == config.VERIFY_LIGHT_MAX_TURNS
    assert calls[0]["timeout"] == config.VERIFY_LIGHT_TIMEOUT
    assert calls[0]["effort"] == config.VERIFY_LIGHT_EFFORT


def test_unsure_light_pass_escalates_to_full_in_a_fresh_session():
    for unsure in ("INCONCLUSIVE", "ERROR"):
        v, calls = _run(_lead("D", "CWE-1333"), [unsure, "REJECT"])
        assert (v.decision, v.route, len(calls)) == ("REJECT", "light→full", 2)
        assert calls[1]["max_turns"] == config.VERIFY_MAX_TURNS and "effort" not in calls[1]
        assert calls[0]["sid"] != calls[1]["sid"]


def test_full_route_and_routing_off_call_exactly_as_before():
    for routing in (True, False):
        v, calls = _run(_lead("A"), ["CONFIRM"], routing=routing)
        assert (v.route, len(calls)) == ("full", 1)
        assert calls[0]["max_turns"] == config.VERIFY_MAX_TURNS
        assert calls[0]["timeout"] == config.VERIFY_TIMEOUT and calls[0]["retries"] == 2
        assert "effort" not in calls[0]
    _, calls = _run(_lead("D", "CWE-1333"), ["INCONCLUSIVE"], routing=False)
    assert len(calls) == 1                                          # off: no light pass, no escalation


def test_one_model_on_every_route():
    """Routing only changes the BUDGET: no call names a model, so --model is config.MODEL throughout."""
    _, calls = _run(_lead("D", "CWE-1333"), ["INCONCLUSIVE", "CONFIRM"])
    assert all("model" not in c for c in calls)
    for effort in (None, config.VERIFY_LIGHT_EFFORT):
        cmd = claude_cli._build_cmd(session_id="s", system="x", json_schema=None, add_dir=None,
                                    extra_allowed=(), max_turns=8, effort=effort)
        assert cmd[cmd.index("--model") + 1] == config.MODEL
        assert cmd[cmd.index("--effort") + 1] == (effort or config.EFFORT)


def test_verify_all_defaults_routing_off(monkeypatch):
    monkeypatch.setattr(config, "VERIFY_ROUTING", False)
    seen = []
    monkeypatch.setattr(verify, "verify_lead",
                        lambda *a: seen.append(a[-1]) or Verdict(lead=a[1], decision="CONFIRM", reason=""))
    verify.verify_all("s", [_lead("D", "CWE-1333")], ".", lambda ev: None, run_agent=lambda *a, **k: {})
    verify.verify_all("s", [_lead("D", "CWE-1333")], ".", lambda ev: None, run_agent=lambda *a, **k: {},
                      routing=True)
    assert seen == [False, True]


# ── the route is recorded everywhere measurement reads it ──────────────
def test_route_shows_in_report_and_sarif():
    v = Verdict(lead=_lead("D", "CWE-1333", file="a.js"), decision="CONFIRM", reason="r", route="light")
    assert "verified light" in report.render([v])
    assert "verified" not in report.render([Verdict(lead=_lead(), decision="CONFIRM", reason="r")])
    assert sarif.to_sarif([v])["runs"][0]["results"][0]["properties"]["verifyRoute"] == "light"


def test_resume_restores_route_and_location(tmp_path):
    lead = _lead("D", "CWE-1333")
    (tmp_path / "leads.json").write_text(json.dumps(
        {"scan_id": "s", "repo": None, "leads": [lead.__dict__]}))
    (tmp_path / "verdicts.jsonl").write_text(json.dumps(
        {"lead": lead.__dict__, "decision": "CONFIRM", "reason": "r", "file": "x.js", "line_start": 7,
         "severity": "HIGH", "route": "light→full", "some_future_field": 1}) + "\n")
    _, _, _, finished = cli._load_run(str(tmp_path))
    v = finished[0]
    assert (v.route, v.file, v.line_start, v.severity) == ("light→full", "x.js", 7, "HIGH")
