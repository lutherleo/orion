"""The 2026-10-29 scoring fixes (merge plan Phase 5, eval/CHANGELOG.md), each pinned on synthetic
inputs. Report-side rules (failed runs missing + bounds) are in test_report.py. Token-free."""
import json

import pytest

from eval import db, judge, match, normalize
from eval.arms import plain


# --- which Orion verdicts are findings; whose location (absorbs the old eval/convert.py) ----------

VERDICTS = [
    {"lead": {"index": 0, "shape": "A", "text": "cmd injection at createFile.ts:116", "evidence": "q",
              "confidence": "HIGH"}, "decision": "CONFIRM", "reason": "reaches exec"},
    {"lead": {"index": 1, "shape": "B", "text": "maybe", "evidence": "", "confidence": "LOW"},
     "decision": "REJECT", "reason": "no flow"},
    {"lead": {"index": 2, "shape": "C", "text": "x", "evidence": "", "confidence": "LOW"},
     "decision": "ERROR", "reason": "claude -p timed out"},
    {"lead": {"index": 3, "shape": "A", "text": "y", "evidence": "", "confidence": "MEDIUM"},
     "decision": "INCONCLUSIVE", "reason": "could not tell"},                      # verified, unsure
    {"lead": {"index": 4, "shape": "LM", "text": "z", "evidence": "", "confidence": "HIGH", "file": "a.py",
              "line_start": 3}, "decision": "INCONCLUSIVE", "reason": "unverified (engine=lm)"},
]


def test_only_confirms_and_unverified_leads_are_findings(tmp_path):
    assert [normalize.counts_as_finding(v) for v in VERDICTS] == [True, False, False, False, True]
    p = tmp_path / "findings.json"
    p.write_text(json.dumps(VERDICTS))
    assert [f["raw_text"] for f in normalize.load_arm(str(p), arm="orion-lm")] == [
        "cmd injection at createFile.ts:116", "z"]
    assert normalize.load(str(tmp_path / "missing.json")) == []


@pytest.mark.parametrize("verdict_fields", [
    {},                                                             # the analyst's location stands
    {"line_start": 9},                                              # the verifier re-read the line
    {"file": "b.py", "line_start": 2, "cwe": "CWE-79"},             # it moved the file
    {"file": "a.py", "line_end": 12},                               # same file, new end
])
def test_verdict_location_matches_contracts(verdict_fields):
    from orion.contracts import Lead, Verdict
    lead = {"index": 0, "shape": "A", "text": "t", "evidence": "e", "confidence": "HIGH", "file": "a.py",
            "line_start": 3, "line_end": 5, "function": "f", "cwe": "CWE-89"}
    want = Verdict(Lead(**lead), "CONFIRM", "r", **verdict_fields).location()
    assert normalize._verdict_location(verdict_fields, lead) == want


# --- the range-overlap line rule ------------------------------------------------------------------

SITE = {"file": "app/a.py", "line": 40, "end_line": 42, "function": None}


@pytest.mark.parametrize("start,end,hit", [
    (45, None, True),       # a point inside the +-10 window
    (20, 35, True),         # a range starting 20 lines early but reaching the window: newly a hit
    (10, 25, False),        # a range ending before the window
    (1, 200, False),        # a whole-file range is longer than _MAX_RANGE: only its start (1) counts
    (35, 30, True),         # inverted end ignored: the start alone is in the window
])
def test_reported_ranges_overlap_the_fix_window(start, end, hit):
    f = {"file": "app/a.py", "function": "g", "line": start, "line_end": end}
    assert match.l1_fix_site(f, SITE) is hit


# --- plain arms: each finding validated on its own --------------------------------------------------

def test_one_bad_finding_no_longer_voids_the_run(tmp_path):
    good = {"file": "a.py", "function": "f", "line_start": 3, "line_end": 4, "cwe": "CWE-89",
            "title": "t", "explanation": "e"}
    bad = dict(good, function=None)
    repo = tmp_path / "tree"
    repo.mkdir()
    (repo / "findings.json").write_text(json.dumps({"findings": [good, bad, good]}))
    out = tmp_path / "run" / "findings.json"
    out.parent.mkdir()
    lines = []
    assert plain._capture_findings(str(repo), str(out), lines.append) == str(out)
    kept = json.loads(out.read_text())["findings"]
    assert kept == [good, good]
    assert json.loads((out.parent / "findings.raw.json").read_text())["findings"][1] == bad
    assert any("dropped 1 of 3" in ln for ln in lines)


def test_a_file_without_a_findings_list_is_still_a_miss(tmp_path):
    repo = tmp_path / "tree"
    repo.mkdir()
    (repo / "findings.json").write_text(json.dumps({"nope": []}))
    assert plain._capture_findings(str(repo), str(tmp_path / "f.json"), lambda _l: None) is None


# --- frontier tokens without a result event --------------------------------------------------------

def _assistant(mid, **usage):
    return json.dumps({"type": "assistant", "message": {"id": mid, "usage": usage, "content": []}})


def test_a_hung_claude_run_keeps_its_per_message_tokens(tmp_path):
    log = tmp_path / "usage.jsonl"
    log.write_text("\n".join([
        '{"type":"system"}',
        _assistant("m1", input_tokens=10, cache_creation_input_tokens=100, cache_read_input_tokens=0,
                   output_tokens=5),
        _assistant("m1", input_tokens=10, cache_creation_input_tokens=100, cache_read_input_tokens=0,
                   output_tokens=40),                                       # same message, later block
        _assistant("m2", input_tokens=2, cache_creation_input_tokens=0, cache_read_input_tokens=100,
                   output_tokens=7),
    ]) + "\n")                                                              # killed: no result event
    warned = []
    [block] = plain.read_usage(str(log), source="claude", on_line=warned.append)
    assert (block["uncached_in"], block["cache_write"], block["cache_read"], block["out"]) == (12, 100, 100, 47)
    assert block["exit_reason"] == plain.PER_MESSAGE and any("lower bound" in w for w in warned)


def test_a_result_event_still_wins_over_per_message_usage(tmp_path):
    log = tmp_path / "usage.jsonl"
    log.write_text(_assistant("m1", input_tokens=1, output_tokens=1) + "\n"
                   + '{"type":"result","usage":{"input_tokens":9,"output_tokens":9}}\n')
    [block] = plain.read_usage(str(log), source="claude")
    assert block["uncached_in"] == 9 and "exit_reason" not in block


# --- the judge credits location-less findings, only once calibrated --------------------------------

KEY = {"id": "V1", "fix_sites": [{"file": "app/a.py", "line": 40, "function": "view"}], "cwe": "CWE-89"}


def _calibration(n=120):
    labels = ["same_vulnerability", "different"] * (n // 2)
    return judge.calibrate(labels, labels, categories=judge.MATCH_LABELS)


def test_crediting_refuses_an_unfrozen_or_uncalibrated_judge(tmp_path, monkeypatch):
    conn = db.connect(str(tmp_path / "t.db"))
    with pytest.raises(ValueError, match="not frozen"):
        judge.credit_locationless(conn, 1, [], [KEY], ask=lambda p: "", calibration=_calibration())
    monkeypatch.setattr(judge, "JUDGE_MODEL", "judge-model-x")
    with pytest.raises(ValueError, match=">=100"):
        judge.credit_locationless(conn, 1, [], [KEY], ask=lambda p: "", calibration=_calibration(40))
    with pytest.raises(ValueError):                                # a false-alarm-task calibration
        judge.credit_locationless(conn, 1, [], [KEY], ask=lambda p: "",
                                  calibration={**judge.calibrate(["unsure"] * 100, ["unsure"] * 100),
                                               "min_n_met": True})


def test_only_location_less_findings_are_judged_and_credited_once(tmp_path, monkeypatch):
    monkeypatch.setattr(judge, "JUDGE_MODEL", "judge-model-x")
    conn = db.connect(str(tmp_path / "t.db"))
    rid = db.start_run(conn, arm="plain-opus5", repo="V1", tier="headline", run_no=1, model_tag="m",
                       cli_versions="", orion_commit="c", commit_kind="vulnerable", commit_sha="v")
    asked = []

    def ask(prompt):
        asked.append(prompt)
        return "same_vulnerability"

    findings = [{"file": "app/zzz.py", "line": 1, "cwe": "CWE-89", "raw_text": "has a file"},
                {"file": None, "cwe": "CWE-89", "raw_text": "SQL injection in the search view"},
                {"file": None, "cwe": "CWE-89", "raw_text": "the same bug again"}]
    decisions = judge.credit_locationless(conn, rid, findings, [KEY], ask=ask, calibration=_calibration())
    assert [d["finding_idx"] for d in decisions] == [1] and len(asked) == 1      # file-bearing one skipped
    assert "fixed at: app/a.py in view (lines 40-40)" in asked[0] and "SQL injection" in asked[0]
    rows = match.read_matches(conn, rid)
    assert [(r["vuln_id"], r["level"]) for r in rows] == [("V1", judge.JUDGE_LEVEL)]
    from eval import report
    assert report._caught(conn, rid)["l1"] == {"V1"} and report._caught(conn, rid)["l2"] == set()
