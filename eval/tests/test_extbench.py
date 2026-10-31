"""eval.extbench: VLB / BountyBench end to end with the same model in and out of Orion. Token-free and offline:
gh, git, the arms and the clock are stubs."""
import json
import os

import pytest

from eval import devbench
from eval import extbench as eb


def _task(tid, repo, eco="npm", sites=2, cwes=("CWE-79",), added=()):
    return {"id": tid, "repo": repo, "ecosystem": eco, "scope": "in", "cwes": list(cwes),
            "ground_truth_files": ["lib/a.js", *added], "added_files": list(added),
            "fix_sites": [{"file": "lib/a.js", "line": 4 + i, "end_line": 4 + i, "function": None} for i in range(sites)],
            "vulnerable_commit": "v" * 40, "fix_commit": "f" * 40}


def test_select_is_deterministic_smallest_repo_first_one_task_per_repo():
    data = {"status": "proposed", "tasks": [
        _task("a", "o/r1"), _task("b", "o/r2", sites=12), _task("c", "o/r3"), _task("d", "o/r3"),
        _task("e", "o/r7", cwes=()), {**_task("f", "o/r8"), "ground_truth_files": ["new.js"], "added_files": ["new.js"]},
        _task("g", "o/r4", eco="pip"), _task("h", "o/r5", eco="pip"), _task("i", "o/r6", eco="maven")]}
    sizes = {"o/r1": 50, "o/r2": 10, "o/r3": 30, "o/r4": 20, "o/r5": None, "o/r6": 5, "o/r7": 1, "o/r8": 1}
    calls = []
    size = lambda name: calls.append(name) or sizes[name]
    res = eb.select("vlb", data, 4, size=size, mix={"npm": 2, "pip": 1, "maven": 1})
    assert res["selected"] == ["c", "a", "g", "i"]       # broad (b), CWE-less (e), added-only (f), same repo (d) out
    assert res["repos_without_size"] == ["o/r5"] and len(calls) == len(set(calls))       # one gh call per repo
    assert eb.select("vlb", data, 4, size=size, mix={"npm": 2, "pip": 1, "maven": 1}) == res
    with pytest.raises(ValueError, match="mix"):
        eb.select("vlb", data, 5, size=size, mix={"npm": 2, "pip": 1, "maven": 1})


def test_a_plain_run_dir_scores_with_line_and_cwe(tmp_path):
    (tmp_path / "findings.json").write_text(json.dumps({"findings": [
        {"file": "lib/a.js", "function": "f", "line_start": 4, "line_end": 4, "cwe": "CWE-79", "title": "xss",
         "explanation": "e"}]}))
    assert devbench.findings_from(str(tmp_path))[0] == "plain"
    (tmp_path / "run.json").write_text(json.dumps({"arm": "plain-sonnet5"}))
    label, [f] = devbench.findings_from(str(tmp_path))
    assert label == "plain-sonnet5" and (f["file"], f["line"], f["cwe"]) == ("lib/a.js", 4, "CWE-79")


def _usage_ev(i, o, cost):
    return json.dumps({"event": "usage", "detail": json.dumps({"input_tokens": i, "output_tokens": o,
                       "cache_read_input_tokens": 100, "cache_creation_input_tokens": 10, "total_cost_usd": cost})})


def test_orion_usage_sums_every_claude_call_and_a_missing_cost_stays_missing(tmp_path):
    p = tmp_path / "progress.jsonl"
    p.write_text("\n".join([_usage_ev(5, 7, 0.25), json.dumps({"event": "lead", "detail": "x"}), "not json",
                            _usage_ev(1, 2, 0.5)]))
    tot = eb.usage_total(eb.orion_usage(p))
    assert (tot["calls"], tot["uncached_in"], tot["out"], tot["tokens"], tot["cli_cost_usd"]) == (2, 6, 9, 235, 0.75)
    p.write_text(_usage_ev(1, 1, None))
    assert eb.usage_total(eb.orion_usage(p))["cli_cost_usd"] is None
    assert eb.usage_total([])["tokens"] == 0 and eb.usage_total([])["cli_cost_usd"] is None


def test_plain_usage_takes_the_clis_cost_from_the_same_result_events(tmp_path):
    log = tmp_path / "usage.jsonl"
    log.write_text("\n".join([json.dumps({"type": "assistant"}), json.dumps({"type": "result", "total_cost_usd": 1.5})]))
    [b] = eb.plain_usage(log, [{"uncached_in": 3, "cache_read": 0, "cache_write": 0, "out": 4}])
    assert b["cli_cost_usd"] == 1.5 and b["out"] == 4


def _vlb_keys(*ids):
    return {"status": "proposed", "tasks": [_task(t, f"o/{t}") for t in ids]}


def test_run_groups_by_model_retries_a_usage_limit_and_resumes(tmp_path, monkeypatch):
    monkeypatch.setattr(eb, "load_keys", lambda b: _vlb_keys("t1"))
    seen, waits, mirrored = [], [], []
    outcomes = iter(["aborted"] + ["ok"] * 20)

    def arm(name, tree, d, on_line):
        assert os.path.isdir(tree) and not os.path.exists(os.path.join(tree, ".git"))
        seen.append((name, d.parent.name, d.name))
        return {"status": next(outcomes), "usage": [{"uncached_in": 1, "cache_read": 0, "cache_write": 0, "out": 1,
                                                    "cli_cost_usd": 0.1}]}

    kw = dict(root=tmp_path / "runs", mirrors=tmp_path / "m", tree_root=str(tmp_path / "w"), sleep=waits.append,
              _mirror=lambda url, d: mirrored.append(url), _export=lambda m, sha, tree: os.makedirs(tree),
              _resolve=lambda m, ref: ref, _arms={a: arm for a in eb.ARMS})
    counts = eb.run("vlb", ["t1"], ["plain-sonnet5", "orion-sonnet5", "plain-opus5"], resume_wait=7, **kw)
    assert counts == {"ok": 6, "aborted_retries": 1, "failed": 0, "skipped": 0} and waits == [7]
    assert [s[0] for s in seen] == ["plain-sonnet5", "plain-sonnet5", "orion-sonnet5", "plain-sonnet5",
                                    "orion-sonnet5", "plain-opus5", "plain-opus5"]          # sonnet first, then opus
    assert seen[:2] == [("plain-sonnet5", "vulnerable", "1"), ("plain-sonnet5", "vulnerable", "1.a2")]
    assert mirrored == ["https://github.com/o/t1.git"] and os.listdir(tmp_path / "w") == []   # no tree left behind
    rec = json.loads((tmp_path / "runs/vlb/plain-sonnet5/t1/vulnerable/1.a2/run.json").read_text())
    assert (rec["status"], rec["model"], rec["effort"], rec["attempt"]) == ("ok", "claude-sonnet-5", "xhigh", 2)
    assert eb.run("vlb", ["t1"], ["plain-sonnet5", "orion-sonnet5", "plain-opus5"], **kw)["skipped"] == 6
    (tmp_path / "w2").mkdir()
    (tmp_path / "w2" / "CLAUDE.md").write_text("x")
    with pytest.raises(RuntimeError, match="answer leak"):
        eb.run("vlb", ["t1"], ["plain-opus5"], root=tmp_path / "r2", mirrors=tmp_path / "m",
               tree_root=str(tmp_path / "w2" / "ws"), _mirror=lambda u, d: None, _resolve=lambda m, r: r,
               _export=lambda m, s, t: os.makedirs(t), _arms={a: arm for a in eb.ARMS})
    with pytest.raises(ValueError, match="unknown arm"):
        eb.run("vlb", ["t1"], ["plain-gpt"], **kw)


def test_a_run_that_keeps_hitting_the_limit_is_left_aborted_after_max_attempts(tmp_path, monkeypatch):
    monkeypatch.setattr(eb, "load_keys", lambda b: {"status": "proposed", "bounties": [
        {"id": "s/bounty_0", "system": "s", "repo": "o/s", "commit": "v1.0"}]})
    waits = []
    counts = eb.run("bountybench", ["s/bounty_0"], ["plain-opus5"], root=tmp_path / "r", mirrors=tmp_path / "m",
                    tree_root=str(tmp_path / "w"), sleep=waits.append, max_attempts=2, _mirror=lambda u, d: None,
                    _resolve=lambda m, r: "c" * 40, _export=lambda m, s, t: os.makedirs(t),
                    _arms={"plain-opus5": lambda *a: {"status": "aborted", "usage": []}})
    assert counts == {"ok": 0, "aborted_retries": 1, "failed": 1, "skipped": 0} and len(waits) == 1
    assert (tmp_path / "r/bountybench/plain-opus5/s__bounty_0/vulnerable/1.a2/run.json").is_file()


def test_progress_lines_about_rate_limiting_never_look_like_a_usage_limit(tmp_path):
    assert eb._limit_hit(["  [discover/lead] login is not rate-limited; error messages leak usernames"], None) is None
    assert eb._limit_hit(["Error: Claude AI usage limit reached|1761900000"], None)


class _Proc:
    def __init__(self, rc):
        self.returncode = rc


def _fake_scan(rc, verdicts, events=()):
    def stream(cmd, *, env, cwd, on_line, on_start):
        assert env["ORION_MODEL"] == "claude-opus-5" and env["ORION_EFFORT"] == "xhigh"
        assert "ORION_SHORTLISTS" not in env and env["PYTHONPATH"].split(os.pathsep)[0] == str(eb.ROOT)
        odir = os.path.join(cwd, ".orion", "runs", "sid1", "20261031T000000Z")
        os.makedirs(odir)
        with open(os.path.join(odir, "leads.json"), "w") as f:
            json.dump({"scan_id": "sid1", "leads": []}, f)
        with open(os.path.join(odir, "progress.jsonl"), "w") as f:
            f.write("\n".join([_usage_ev(2, 3, 0.4), *events]))
        for p in (os.path.join(odir, "verdicts.json"), cmd[cmd.index("--json") + 1]):
            with open(p, "w") as f:
                json.dump(verdicts, f)
        on_line("[discover] 4 shapes")
        on_start(_Proc(rc))
        return "ok" if rc == 0 else "crashed"
    return stream


def test_orion_arm_exit_3_is_a_result_and_a_usage_limit_aborts(tmp_path, monkeypatch):
    monkeypatch.setenv("ORION_SHORTLISTS", "1")                 # an operator knob: the arm measures defaults
    cleared = []
    d = tmp_path / "a"
    d.mkdir()
    res = eb.run_orion_arm("orion-opus5", "/t", d, lambda l: None, _stream=_fake_scan(3, [{"decision": "ERROR",
                           "reason": "verifier timed out"}], [json.dumps({"event": "lead", "detail": "login is not rate-limited"})]),
                           _clear=lambda sid: cleared.append(sid) or "cleared")
    assert (res["status"], res["incomplete"], res["scan_id"], res["graph"]) == ("ok", True, "sid1", "cleared")
    assert res["orion_run_dir"] == os.path.join(".orion", "runs", "sid1", "20261031T000000Z")
    assert eb.usage_total(res["usage"])["cli_cost_usd"] == 0.4 and cleared == ["sid1"]
    d2 = tmp_path / "b"
    d2.mkdir()
    res = eb.run_orion_arm("orion-opus5", "/t", d2, lambda l: None, _stream=_fake_scan(0, [{"decision": "ERROR",
                           "reason": "claude: Claude AI usage limit reached|1761900000"}]), _clear=lambda sid: "cleared")
    assert res["status"] == "aborted" and "usage limit" in res["limit_line"]


def _ok(root, arm, tid, snap, **extra):
    d = eb.run_dir_for("vlb", arm, tid, snap, 1, 1, root)
    d.mkdir(parents=True)
    (d / "run.json").write_text(json.dumps({"status": "ok", "arm": arm, "wall_s": 10, "orion_commit": "abc",
                                            "usage": [{"uncached_in": 1, "out": 1, "cli_cost_usd": 0.5}], **extra}))
    return d


def _verdict(decision, line):
    lead = {"index": 0, "shape": "A", "text": "XSS", "evidence": "e", "confidence": "HIGH", "file": "lib/a.js",
            "line_start": line, "cwe": "CWE-79"}
    return {"lead": lead, "decision": decision, "reason": "r"}, lead


def test_score_compares_plain_orion_and_orion_leads_with_missing_tasks_excluded(tmp_path, monkeypatch):
    monkeypatch.setattr(eb, "load_keys", lambda b: _vlb_keys("t1", "t2"))
    root = tmp_path / "runs"
    hit = {"findings": [{"file": "lib/a.js", "function": None, "line_start": 4, "line_end": 4, "cwe": "CWE-79",
                         "title": "xss", "explanation": "e"}]}
    (_ok(root, "plain-sonnet5", "t1", "vulnerable") / "findings.json").write_text(json.dumps(hit))
    (_ok(root, "plain-sonnet5", "t1", "patched") / "findings.json").write_text(json.dumps(hit))   # still flags it
    for snap, decision in (("vulnerable", "CONFIRM"), ("patched", "REJECT")):
        d = _ok(root, "orion-sonnet5", "t1", snap, orion_run_dir=".orion/runs/s/t")
        odir = d / ".orion" / "runs" / "s" / "t"
        odir.mkdir(parents=True)
        v, lead = _verdict(decision, 4)
        (odir / "verdicts.json").write_text(json.dumps([v]))
        (odir / "leads.json").write_text(json.dumps({"scan_id": "s", "leads": [lead]}))
    res = eb.score("vlb", ["t1", "t2"], ["plain-sonnet5", "orion-sonnet5", "orion-sonnet5:leads"], root=root)
    p, o, l = (res["arms"][a] for a in ("plain-sonnet5", "orion-sonnet5", "orion-sonnet5:leads"))
    assert p["missing"] == ["t2"] and p["summary"]["n"] == 1                 # t2 never ran: excluded, not a miss
    assert (p["summary"]["l2"], p["summary"]["silent"], p["summary"]["pair_correct"]) == (1, 0, 0)
    assert (o["summary"]["l2"], o["summary"]["silent"], o["summary"]["pair_correct"]) == (1, 1, 1)
    assert (l["summary"]["silent"], l["summary"]["pair_correct"]) == (0, 0)  # the verifier is what kept it silent
    assert o["cost"]["cli_cost_usd"] == 1.0 and o["cost"]["wall_s"] == 20 and o["orion_commits"] == ["abc"]
    t = eb.table(res)
    assert "| orion-sonnet5:leads |" in t and "(same run)" in t and "| plain-sonnet5 | 1 | 1 |" in t
