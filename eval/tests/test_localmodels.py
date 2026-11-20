import json
import os

import pytest

from eval import extbench as eb
from eval import localbench as lb
from eval import localmodels as lm

REG = {"tiny": {"mlx": "models/tiny", "gguf": "models/tiny.gguf"}, "maconly": {"mlx": "hf:org/tiny-4bit"},
       "abs": {"gguf": "/nope/x.gguf"}}


def test_registry_file_parses_and_every_entry_has_a_path():
    reg = lm.load_registry()
    assert reg and all(e.get("mlx") or e.get("gguf") for e in reg.values())
    assert lm.arms(reg)[0].startswith("lm-")


def test_model_path_picks_mlx_on_macos_and_gguf_elsewhere():
    assert lm.model_path("lm-tiny", REG, platform="darwin", check=False).endswith(os.path.join("models", "tiny"))
    assert lm.model_path("lm-tiny", REG, platform="linux", check=False).endswith("tiny.gguf")
    assert lm.model_path("lm-maconly", REG, platform="darwin") == "org/tiny-4bit"       # hf: id, not checked
    with pytest.raises(ValueError, match="MLX runs only on Apple silicon"):
        lm.model_path("lm-maconly", REG, platform="win32")
    with pytest.raises(ValueError, match="does not exist"):
        lm.model_path("lm-abs", REG, platform="linux")
    with pytest.raises(ValueError, match="unknown local model"):
        lm.model_path("lm-missing", REG)


def test_scan_cmd_runs_the_lm_engine_with_its_verifier_and_env_drops_option_knobs():
    cmd = lm.scan_cmd("/t", "/m", eb.Path("/r/verdicts.json"))
    assert cmd[1:] == ["-m", "orion", "scan", "/t", "--engine", "lm", "--model", "/m", "--lm-verify", "--json",
                       str(eb.Path("/r/verdicts.json"))]
    env = lm.scan_env({"ORION_SHORTLISTS": "1", "NEO4J_URI": "x"})
    assert "ORION_SHORTLISTS" not in env and env["NEO4J_URI"] == "x" and str(lm.ROOT) in env["PYTHONPATH"]


def test_behaviour_counts_queries_warnings_and_verdicts(tmp_path):
    p = tmp_path / "progress.jsonl"
    evs = [{"phase": "discover", "event": "query"}, {"phase": "discover", "event": "query"},
           {"phase": "discover", "event": "warn", "detail": "malformed: not JSON"},
           {"phase": "verify", "event": "query"}, {"phase": "verify", "event": "verdict", "detail": "REJECT: no"}]
    p.write_text("\n".join(json.dumps(e) for e in evs) + "\nnot json\n")
    b = lm.behaviour(p)
    assert b["graph_queries"] == 3 and b["warn_kinds"] == {"malformed": 1} and b["decisions"] == {"REJECT": 1}
    assert lm.behaviour(tmp_path / "none.jsonl") == {}


def _fake_scan(verdicts, leads, progress, rc=0):
    """A stand-in for `orion scan`: writes what a real lm scan writes under its cwd."""
    class P:
        returncode = rc

    def stream(cmd, *, env, cwd, on_line, on_start=None, **kw):
        assert "--engine" in cmd and "--lm-verify" in cmd
        sd = os.path.join(cwd, ".orion", "runs", "sid", "ts")
        os.makedirs(sd)
        open(os.path.join(sd, "leads.json"), "w").write(json.dumps({"scan_id": "sid", "engine": "lm", "leads": leads}))
        open(os.path.join(sd, "verdicts.json"), "w").write(json.dumps(verdicts))
        open(os.path.join(sd, "progress.jsonl"), "w").write("\n".join(json.dumps(e) for e in progress))
        open(os.path.join(cwd, "verdicts.json"), "w").write(json.dumps(verdicts))
        on_start and on_start(P())
        return "ok" if rc == 0 else "crashed"
    return stream


def test_extbench_lm_arm_records_missing_tokens_and_behaviour(tmp_path, monkeypatch):
    monkeypatch.setattr(lm, "model_path", lambda arm, registry=None, **kw: "/models/tiny")
    stream = _fake_scan([], [], [{"phase": "discover", "event": "query"}], rc=3)
    out = eb.run_lm_arm("lm-tiny", "/tree", tmp_path, lambda l: None, _stream=stream, _clear=lambda s: "cleared")
    assert out["status"] == "ok" and out["incomplete"] and out["usage"] == []
    assert out["lm_model"] == "/models/tiny" and out["behaviour"]["graph_queries"] == 1
    assert eb._cost_row([{"arm": "lm-tiny", "wall_s": 2}], "lm-tiny")["tokens"] is None      # missing, never 0
    assert eb.model_of("lm-tiny") == ("/models/tiny", "greedy")


def test_extbench_refuses_an_unknown_arm_but_accepts_lm_ones_and_lists_them_on_disk(tmp_path, monkeypatch):
    monkeypatch.setattr(eb, "load_keys", lambda b: {"status": "proposed", "tasks": []})
    with pytest.raises(ValueError, match="unknown arm"):
        eb.run("vlb", [], ["gpt-local"], root=tmp_path)
    (tmp_path / "vlb" / "lm-tiny").mkdir(parents=True)
    (tmp_path / "vlb" / "plain-opus5").mkdir()
    assert eb._arms_on_disk("vlb", tmp_path) == ["plain-opus5", "lm-tiny", "lm-tiny:leads"]


def test_localbench_runs_scores_and_skips_ok_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(lm, "model_path", lambda arm, registry=None, **kw: "/models/tiny")
    fx = tmp_path / "NodeGoat"
    fx.mkdir()
    key = lb.devbench.load_keys("nodegoat")["keys"][0]
    site = key["fix_sites"][0]
    hit = {"file": site["file"], "line_start": site["line"], "cwe": key.get("cwe") or "CWE-79", "text": "x"}
    verdicts = [{"decision": "CONFIRM", "reason": "ok", "lead": hit, **hit},
                {"decision": "REJECT", "reason": "no", "lead": {"text": "y"}}]
    stream = _fake_scan(verdicts, [hit, {"text": "y"}], [{"phase": "discover", "event": "query"},
                                                         {"phase": "verify", "event": "verdict",
                                                          "detail": "CONFIRM: ok"}])
    kw = dict(root=tmp_path / "runs", fixtures={"nodegoat": fx}, on_line=lambda l: None, _stream=stream)
    assert lb.run(["lm-tiny"], ["nodegoat"], **kw) == {"ok": 1, "failed": 0, "skipped": 0}
    assert lb.run(["lm-tiny"], ["nodegoat"], **kw)["skipped"] == 1
    rec = json.loads((tmp_path / "runs/nodegoat/lm-tiny/1/run.json").read_text())
    assert rec["usage_total"]["tokens"] is None and rec["behaviour"]["decisions"] == {"CONFIRM": 1}
    res = lb.score(root=tmp_path / "runs")
    s = res["benchmarks"]["nodegoat"]["scores"]
    assert key["id"] in s["lm-tiny"]["l1"] and s["lm-tiny:leads"]["n_findings"] == 2
    assert "lm-tiny" in lb.behaviour_table(res)
    with pytest.raises(ValueError, match="not lm arms"):
        lb.run(["plain-opus5"], ["nodegoat"], **kw)
