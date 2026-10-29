"""Dev-benchmark scoring through eval's matcher (merge plan Phase 6, Gate 3). Token-free."""
import json
import os
from pathlib import Path

import pytest

from eval import devbench, match, normalize

ROOT = Path(__file__).resolve().parents[2]


def _key(kid, file, line, end=None, function=None, cwe="CWE-89"):
    return {"id": kid, "cwe": cwe, "cwes": [cwe], "known_gap": False,
            "fix_sites": [{"file": file, "line": line, "end_line": end or line, "function": function}]}


KEYS = {"keys": [_key("A7", "app/routes/index.js", 55, 60, cwe="CWE-285"),
                 _key("A10", "app/routes/index.js", 72, cwe="CWE-601")]}


def _f(file, line, cwe=None, text=""):
    return {"arm": "x", "file": file, "function": None, "line": line, "line_end": None, "cwe": cwe,
            "confidence": None, "path": [], "raw_text": text}


def test_maximum_matching_credits_both_neighbours_where_greedy_misses_one():
    """The measured NodeGoat C case: a redirect finding at index.js:70 sits in A7's window too."""
    findings = [_f("app/routes/index.js", 70), _f("app/routes/index.js", 55)]
    greedy = match.match_run(findings, KEYS["keys"])
    assert [r["vuln_id"] for r in greedy] == ["A7"]                           # A10 lost to order
    best = devbench.match_max(findings, KEYS["keys"])
    assert {(r["finding_idx"], r["vuln_id"]) for r in best} == {(0, "A10"), (1, "A7")}


def test_maximum_matching_prefers_l2_then_the_lowest_index():
    keys = [_key("V", "a.py", 10, cwe="CWE-89")]
    rows = devbench.match_max([_f("a.py", 10), _f("a.py", 11, cwe="CWE-89"), _f("a.py", 12, cwe="CWE-89")], keys)
    assert [(r["finding_idx"], r["level"]) for r in rows] == [(1, "L2")]
    rows = devbench.match_max([_f("b.py", 1), _f("a.py", 10), _f("a.py", 10)], keys)
    assert [(r["finding_idx"], r["level"]) for r in rows] == [(1, "L1")]
    assert devbench.match_max([], keys) == [] and devbench.match_max([_f("a.py", 1)], []) == []


def test_score_splits_the_uncredited_into_duplicates_and_unmatched():
    findings = [_f("app/routes/index.js", 72, "CWE-601"), _f("app/routes/index.js", 73), _f("z.js", 1)]
    s = devbench.score(findings, KEYS)
    assert s["l1"] == ["A10"] and s["l2"] == ["A10"] and s["missed"] == ["A7"]
    assert s["duplicates"] == [1] and s["unmatched"] == [2] and s["n_checkable"] == 2
    assert s["no_cwe"] == 2
    assert "| x | 1 / 2 | 1 / 2 | A7 | 1 | 1 | 2 | 0 of 3 |" in devbench.table("nodegoat", KEYS, [("x", s)])


def test_check_keys_catches_a_moved_line_and_a_missing_file(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\nrun(q)\n", encoding="utf-8")
    good = {"keys": [{"id": "V", "fix_sites": [{"file": "a.py", "line": 2, "end_line": 2, "quote": "run(q)"}]}]}
    assert devbench.check_keys(good, str(tmp_path)) == []
    bad = {"keys": [{"id": "V", "fix_sites": [{"file": "a.py", "line": 1, "end_line": 1, "quote": "run(q)"},
                                              {"file": "a.py", "line": 9, "end_line": 9},
                                              {"file": "a.py", "line": 2, "end_line": 1},
                                              {"file": "gone.py", "line": 1}]}]}
    problems = devbench.check_keys(bad, str(tmp_path))
    assert len(problems) == 4 and "quote" in problems[0] and "outside" in problems[1]
    assert "end_line" in problems[2] and "not found" in problems[3]


@pytest.mark.parametrize("benchmark", devbench.BENCHMARKS)
def test_committed_keys_cover_the_ground_truth(benchmark):
    import importlib
    gt = importlib.import_module(f"tests.ground_truth_{benchmark}").GROUND_TRUTH
    data = devbench.load_keys(benchmark)
    assert [k["id"] for k in data["keys"]] == [g.id for g in gt]
    assert [k["known_gap"] for k in data["keys"]] == [g.known_gap for g in gt]
    assert data["status"] in ("proposed", "signed") and (data["status"] != "signed" or data["signed_by"])
    for k in data["keys"]:
        assert k["cwe"] in k["cwes"]
        for s in k["fix_sites"]:
            assert s["line"] <= s["end_line"] and s["quote"]


@pytest.mark.parametrize("benchmark,fixture", [("nodegoat", "fixtures/NodeGoat"), ("pygoat", "fixtures/pygoat")])
def test_committed_keys_match_the_fixture(benchmark, fixture):
    path = ROOT / fixture
    if not path.is_dir():
        pytest.skip(f"{fixture} not checked out here")
    assert devbench.check_keys(devbench.load_keys(benchmark), str(path)) == []


def test_findings_from_reads_every_input_shape(tmp_path):
    lead = {"index": 0, "shape": "A", "text": "x", "evidence": "", "confidence": "HIGH", "file": "a.py",
            "line_start": 3}
    verdicts = [{"lead": lead, "decision": "CONFIRM", "reason": "r"},
                {"lead": dict(lead, index=1), "decision": "REJECT", "reason": "r"}]
    run = tmp_path / "run"
    run.mkdir()
    (run / "verdicts.json").write_text(json.dumps(verdicts), encoding="utf-8")
    (run / "leads.json").write_text(json.dumps({"engine": "claude", "leads": []}), encoding="utf-8")
    label, fs = devbench.findings_from(str(run))
    assert label == "orion" and [(f["file"], f["line"]) for f in fs] == [("a.py", 3)]
    res = tmp_path / "O-x.json"
    res.write_text(json.dumps({"label": "O-x", "verdicts": [{"decision": v["decision"], "verdict": v}
                                                            for v in verdicts]}), encoding="utf-8")
    label, fs = devbench.findings_from(str(res))
    assert label == "orion:O-x" and len(fs) == 1
    old = tmp_path / "C.json"
    old.write_text(json.dumps({"arm": "C", "findings": [{"text": "SQLi in app/x.py", "evidence": "app/x.py:12 q"}]}),
                   encoding="utf-8")
    label, fs = devbench.findings_from(str(old))
    assert label == "bench:C" and (fs[0]["file"], fs[0]["line"]) == ("app/x.py", 12)
    with pytest.raises(ValueError):
        bad = tmp_path / "bad.json"
        bad.write_text("{}", encoding="utf-8")
        devbench.findings_from(str(bad))


def test_research_eval_rows_carry_the_full_verdict(tmp_path, monkeypatch):
    from bench import research_eval
    from orion.contracts import Lead, Verdict
    v = Verdict(Lead(index=0, shape="A", text="NoSQL injection via $where", evidence="q", confidence="HIGH",
                     file="app/data/allocations-dao.js", line_start=78), "CONFIRM", "reaches $where")

    import orion.discover
    import orion.graphdb
    import orion.verify
    monkeypatch.setattr(orion.discover, "discover", lambda *a, **k: [v.lead])
    monkeypatch.setattr(orion.verify, "verify_all", lambda *a, **k: [v])

    class DB:
        def node_count(self, sid):
            return 10

        def close(self):
            pass

    monkeypatch.setattr(orion.graphdb, "GraphDB", DB)
    findings, rows = research_eval._arm_orion(str(ROOT / "fixtures" / "NodeGoat"), "sid", lambda e: None)
    assert rows[0]["verdict"]["lead"]["line_start"] == 78 and rows[0]["verdict"]["decision"] == "CONFIRM"
    out = tmp_path / "O.json"
    out.write_text(json.dumps({"label": "O", "verdicts": rows}), encoding="utf-8")
    label, fs = devbench.findings_from(str(out))
    assert (fs[0]["file"], fs[0]["line"]) == ("app/data/allocations-dao.js", 78)


# --- eval.normalize: one file cited two ways (2026-10-29) -------------------------------------------

def test_a_bare_name_and_its_path_are_one_citation():
    """The measured NodeGoat C finding: 'server.js' in the text, 'fixtures/NodeGoat/server.js:78' in the
    evidence. Before the fix the secondary config/env/all.js won the tier for containing a '/'."""
    text = ("Session security misconfiguration in server.js: the signing secret is a hardcoded constant checked "
            "into config/env/all.js.")
    evidence = ("fixtures/NodeGoat/server.js:78-102 -- app.use(session({...})). Secret source: "
                "fixtures/NodeGoat/config/env/all.js:8 cookieSecret")
    assert normalize._extract_file_line(text, evidence) == ("fixtures/NodeGoat/server.js", 78)
    # unchanged when each file is cited one way
    assert normalize._extract_file_line("bug in app/a.py line 4", "") == ("app/a.py", 4)
    assert normalize._extract_file_line("see a.py", "b/c.py:3") == ("b/c.py", 3)
