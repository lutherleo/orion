"""PLAN2 arm harnesses: ungrounded-review + semgrep finding mappers, and the claude_cli use_mcp toggle.
Token-free — no subprocess, no API, no semgrep binary needed (mappers take pre-parsed data)."""
from __future__ import annotations

from bench import semgrep_adapter, ungrounded_review
from orion import claude_cli


# --- ungrounded review result -> findings -----------------------------------------------------

def test_ungrounded_findings_from_leads():
    result = {"leads": [
        {"shape": "A", "text": "SQL injection in views.py", "evidence": "objects.raw(q)", "confidence": "HIGH"},
        {"shape": "A", "text": "", "evidence": ""},                 # empty -> dropped
        "not a dict",                                                # junk -> dropped
    ]}
    fs = ungrounded_review.findings_from_result(result)
    assert fs == [("SQL injection in views.py", "objects.raw(q)")]


def test_ungrounded_error_result_yields_no_findings():
    assert ungrounded_review.findings_from_result({"_error": "boom"}) == []
    assert ungrounded_review.findings_from_result({"leads": "nope"}) == []


# --- semgrep json -> findings -----------------------------------------------------------------

def test_semgrep_findings_mapping():
    sg = {"results": [
        {"check_id": "python.django.sql-injection", "path": "app/views.py",
         "start": {"line": 42}, "extra": {"message": "SQL injection via raw()", "lines": "objects.raw(q)"}},
        {"check_id": "x", "path": "a.py", "start": {}, "extra": {}},
    ], "_meta": {"version": "1.0"}}
    fs = semgrep_adapter.findings_from_semgrep(sg)
    assert len(fs) == 2
    text, evidence = fs[0]
    assert "sql-injection" in text and "SQL injection via raw()" in text
    assert "app/views.py:42" in evidence


def test_semgrep_rule_cwes_reach_the_evidence_for_eval_l2():
    from eval import normalize
    sg = {"results": [{"check_id": "python.django.security.injection.sql", "path": "app/views.py",
                       "start": {"line": 42}, "extra": {"message": "SQL injection via raw()", "lines": "q",
                                                         "metadata": {"cwe": ["CWE-89: Improper Neutralization "
                                                                              "of Special Elements in SQL"]}}}]}
    [(text, evidence)] = semgrep_adapter.findings_from_semgrep(sg)
    assert "CWE" not in text and evidence.endswith("[CWE-89]")
    f = normalize.finding({"text": text, "evidence": evidence}, arm="bench:D")
    assert (f["file"], f["line"], f["cwe"]) == ("app/views.py", 42, "CWE-89")


def test_semgrep_error_yields_no_findings():
    assert semgrep_adapter.findings_from_semgrep({"_error": "not installed"}) == []


# --- claude_cli use_mcp toggle ----------------------------------------------------------------

def _cmd(use_mcp):
    return claude_cli._build_cmd(
        session_id="s", system="sys", json_schema=None, add_dir="/repo",
        extra_allowed=("Read", "Grep", "Glob"), max_turns=10, use_mcp=use_mcp)


def test_use_mcp_true_includes_graph_tools():
    cmd = _cmd(True)
    assert "--mcp-config" in cmd
    allowed = cmd[cmd.index("--allowedTools") + 1]
    assert "mcp__orion__run_cypher" in allowed


def test_use_mcp_false_drops_graph_tools_and_mcp_config():
    cmd = _cmd(False)
    assert "--mcp-config" not in cmd
    assert "--strict-mcp-config" not in cmd
    allowed = cmd[cmd.index("--allowedTools") + 1]
    assert "mcp__orion__run_cypher" not in allowed
    assert "Read" in allowed and "Grep" in allowed          # source-reading tools remain


# --- arm O (Orion + Claude) wiring and the prove.py harness -------------------------------------

def test_arm_o_writes_labelled_result_with_every_decision(tmp_path, monkeypatch):
    import json
    from bench import research_eval
    rows = [{"decision": "CONFIRM", "shape": "A", "text": "NoSQL injection via $where"},
            {"decision": "REJECT", "shape": "B", "text": "maybe"}]
    monkeypatch.setattr(research_eval, "_arm_orion", lambda repo, sid, ev, **kw: (
        [("NoSQL injection via $where", "q", "app/data/allocations-dao.js"),
         ("stray claim", "", "x.js")], rows))
    out = tmp_path / "O-test.json"
    assert research_eval.main(["--arm", "O", "--benchmark", "nodegoat", "--repo", ".", "--model",
                               "sonnet", "--label", "O-test", "--out", str(out), "--quiet"]) == 0
    r = json.loads(out.read_text())
    assert (r["arm"], r["label"], r["model"]) == ("O", "O-test", "sonnet")
    assert "A1-2" in r["found"]                                   # credited via the structured file
    assert r["decisions"] == {"CONFIRM": 1, "INCONCLUSIVE": 0, "REJECT": 1, "ERROR": 0}
    assert r["false_positive_candidates_detail"] == [{"text": "stray claim", "file": "x.js"}]
    assert r["findings"][0]["file"] == "app/data/allocations-dao.js"


def test_prove_preflight_reports_each_missing_prerequisite(monkeypatch):
    from bench import prove
    monkeypatch.setattr(prove, "_check_claude", lambda: "no claude")
    monkeypatch.setattr(prove, "_check_claude_login", lambda: None)
    monkeypatch.setattr(prove, "_check_neo4j", lambda: None)
    monkeypatch.setattr(prove, "_check_joern", lambda: "no joern")
    monkeypatch.setattr(prove, "_check_fixture", lambda b: None if b == "nodegoat" else f"no {b}")
    assert prove.preflight(["nodegoat", "pygoat"]) == ["no claude", "no joern", "no pygoat"]
    assert prove.main(["--dry-run", "--benchmarks", "nodegoat,pygoat"]) == 1
    assert prove.main(["--benchmarks", "juice-shop"]) == 2


def test_prove_table_includes_committed_baselines():
    from bench import prove
    t = prove.table(["nodegoat"], ["O-sonnet"])
    assert "Opus 5, no Orion (C)" in t and "Semgrep (D)" in t
    assert "| 15 / 15 |" in t                                    # the committed Opus-alone row


def test_plot_results_recognises_orion_runs():
    from bench import plot_results as pr
    assert pr._is_arm("O-opus") and pr._is_arm("O") and not pr._is_arm("notes")
    assert pr._label("O-opus", {"model": "opus"}) == "Orion + opus"
    assert pr._arms({"nodegoat": {"O-sonnet": {}, "C": {}, "D": {}}}) == ["C", "D", "O-sonnet"]


# --- Gate 3 (merge plan Phase 6): arm P = eval's plain Opus arm on the fixture --------------------

def test_arm_p_runs_eval_plain_arm_on_a_git_free_export_and_scores_at_l2(tmp_path, monkeypatch):
    import json
    import os
    from bench import prove
    from eval import devbench
    from eval.arms import ArmResult
    seen = {}

    def export(src, sha, dest):
        seen["export"] = (src, sha)
        os.makedirs(dest)
        open(os.path.join(dest, "server.js"), "w").close()

    def plain(**kw):
        seen["kw"] = kw
        assert os.path.isfile(os.path.join(kw["repo_dir"], "server.js"))
        assert not os.path.exists(os.path.join(kw["repo_dir"], ".git"))
        with open(kw["json_out"], "w", encoding="utf-8") as f:
            json.dump({"findings": [{"file": "app/routes/index.js", "function": "index", "line_start": 72,
                                     "line_end": 72, "cwe": "CWE-601", "title": "open redirect",
                                     "explanation": "e"}]}, f)
        return ArmResult(status="ok", findings_path=kw["json_out"], usage=[{"uncached_in": 1}])

    monkeypatch.setattr(prove, "_ROOT", tmp_path)
    (tmp_path / "fixtures" / "NodeGoat").mkdir(parents=True)
    monkeypatch.setattr(prove.subprocess, "run", lambda *a, **k: type("R", (), {"stdout": "abc123\n"})())
    out = prove.run_plain("nodegoat", results=tmp_path / "res", on_line=lambda l: None, _plain=plain, _export=export)
    r = json.loads(out.read_text(encoding="utf-8"))
    assert (r["arm"], r["eval_arm"], r["status"], r["fixture_commit"]) == ("P", "plain-opus5", "ok", "abc123")
    assert seen["kw"]["arm"] == "plain-opus5" and seen["kw"]["model_tag"] == "claude-opus-5"
    assert seen["kw"]["effort"] == "xhigh" and seen["export"][1] == "abc123"
    assert not os.path.exists(seen["kw"]["repo_dir"])                       # the tree is gone after the run
    label, fs = devbench.findings_from(str(out))
    s = devbench.score(fs, devbench.load_keys("nodegoat"))
    assert label == "P-plain-opus5" and s["l2"] == ["A10"]


def test_prove_preflight_checks_the_plain_login_only_when_p_runs(monkeypatch):
    from bench import prove
    for name in ("_check_claude", "_check_claude_login", "_check_neo4j", "_check_joern"):
        monkeypatch.setattr(prove, name, lambda: None)
    monkeypatch.setattr(prove, "_check_fixture", lambda b: None)
    monkeypatch.setattr(prove, "_check_plain_login", lambda: "no eval login")
    assert prove.preflight(["nodegoat"]) == []
    assert prove.preflight(["nodegoat"], plain=True) == ["no eval login"]
    assert prove.main(["--dry-run", "--benchmarks", "nodegoat"]) == 1
    assert prove.main(["--dry-run", "--benchmarks", "nodegoat", "--no-plain"]) == 0
