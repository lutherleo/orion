"""A scan never loses work: leads land on disk after discovery, each verdict as it completes, and
`--resume <run_dir>` finishes an interrupted run verifying ONLY what is missing (or ERRORed).
`--fail-on` turns verdicts into a CI exit code. Token-free: the real CLI and the real verify_all run,
with discovery, the graph and the `claude -p` call stubbed."""
from __future__ import annotations

import json
import os
import threading
import time

import pytest

from orion import claude_cli, cli, config, discover, embed, graphdb, monitor, runlock
from orion.graph import scans
from orion.contracts import Lead

LEADS = [Lead(index=i, shape="A", text=f"lead-{i} sqli in app/{i}.js", evidence="q", confidence="HIGH")
         for i in range(3)]


class _NoGraph:
    def node_count(self, scan_id):
        return 10

    def close(self):
        pass


@pytest.fixture
def run_dir(tmp_path, monkeypatch):
    d = tmp_path / "run"
    d.mkdir()
    monkeypatch.setattr(cli, "_run_dir", lambda scan_id: str(d))
    monkeypatch.setattr(embed, "ensure_exploit_index", lambda: False)
    monkeypatch.setattr(graphdb, "GraphDB", _NoGraph)
    monkeypatch.setattr(scans, "check_usable", lambda *a, **k: None)   # the --scan-id record gate
    return d


def _agent(decisions: dict, calls: list):
    """A fake run_agent: the verdict for `lead-N` comes from decisions[N] (ERROR -> failure)."""
    def run_agent(session_id, system, message, **kw):
        n = int(message.split("lead-")[1].split()[0])
        calls.append(n)
        d = decisions[n]
        return {"_error": "boom"} if d == "ERROR" else {"decision": d, "reason": f"r{n}"}
    return run_agent


def test_fresh_scan_writes_leads_and_each_verdict(run_dir, monkeypatch):
    calls: list = []
    monkeypatch.setattr(discover, "discover", lambda *a, **k: list(LEADS))
    monkeypatch.setattr(claude_cli, "run_agent", _agent({0: "CONFIRM", 1: "ERROR", 2: "REJECT"}, calls))

    rc = cli.main(["scan", "--scan-id", "sid", "--quiet", "--fail-on", "confirm"])

    assert rc == 1                                          # a CONFIRM exists -> CI gate trips
    meta = json.loads((run_dir / "leads.json").read_text())
    assert meta["scan_id"] == "sid" and len(meta["leads"]) == 3
    logged = [json.loads(line) for line in (run_dir / "verdicts.jsonl").read_text().splitlines()]
    assert sorted(v["decision"] for v in logged) == ["CONFIRM", "ERROR", "REJECT"]
    assert len(json.loads((run_dir / "verdicts.json").read_text())) == 3
    assert "CONFIRM" in (run_dir / "report.txt").read_text()
    sarif = json.loads((run_dir / "results.sarif").read_text())
    assert sarif["version"] == "2.1.0"          # always written (prose-only leads: nothing located)


def test_resume_verifies_only_missing_or_errored_leads(run_dir, monkeypatch):
    monkeypatch.setattr(discover, "discover", lambda *a, **k: list(LEADS))
    monkeypatch.setattr(claude_cli, "run_agent", _agent({0: "CONFIRM", 1: "ERROR", 2: "REJECT"}, []))
    cli.main(["scan", "--scan-id", "sid", "--quiet"])
    with open(run_dir / "verdicts.jsonl", "a") as f:
        f.write('{"lead": {"index": 2')                       # a torn line from a crash mid-write

    calls: list = []
    monkeypatch.setattr(discover, "discover", lambda *a, **k: pytest.fail("resume must not rediscover"))
    monkeypatch.setattr(claude_cli, "run_agent", _agent({1: "INCONCLUSIVE"}, calls))
    rc = cli.main(["scan", "--resume", str(run_dir), "--quiet", "--fail-on", "inconclusive"])

    assert calls == [1]                                     # only the ERRORed lead is re-verified
    final = {v["lead"]["index"]: v["decision"]
             for v in json.loads((run_dir / "verdicts.json").read_text())}
    assert final == {0: "CONFIRM", 1: "INCONCLUSIVE", 2: "REJECT"}
    assert rc == 1


def test_fail_on_levels():
    from orion.contracts import Verdict
    vs = [Verdict(lead=LEADS[0], decision="INCONCLUSIVE", reason="")]
    assert cli._exit_code(vs, "none") == 0
    assert cli._exit_code(vs, "confirm") == 0
    assert cli._exit_code(vs, "inconclusive") == 1


def test_resume_without_leads_file_exits_2(tmp_path, capsys):
    assert cli.main(["scan", "--resume", str(tmp_path)]) == 2
    assert "cannot resume" in capsys.readouterr().err


def test_resume_refuses_a_run_from_another_engine(run_dir, monkeypatch, capsys):
    # One model per scan: an lm run dir is never finished by the claude verifier, and vice versa.
    (run_dir / "leads.json").write_text(json.dumps(
        {"scan_id": "sid", "repo": None, "engine": "lm", "model": "m", "leads": []}))
    assert cli.main(["scan", "--resume", str(run_dir), "--quiet"]) == 2
    assert "--engine lm run" in capsys.readouterr().err


def test_lm_run_dir_records_its_engine_and_model(tmp_path, monkeypatch):
    from orion.lm import engine as lm_engine
    d = tmp_path / "run"
    d.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(cli, "_run_dir", lambda scan_id: str(d))
    monkeypatch.setattr(embed, "ensure_exploit_index", lambda: False)
    monkeypatch.setattr(graphdb, "GraphDB", _NoGraph)
    monkeypatch.setattr(scans, "check_usable", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_load_lm_model", lambda path, **kw: object())
    lead = Lead(index=0, shape="LM", text="t", evidence="e", confidence="HIGH", file="a.js", line_start=1)
    monkeypatch.setattr(lm_engine, "discover_lm", lambda *a, **k: [lead])
    assert cli.main(["scan", str(repo), "--scan-id", "sid", "--engine", "lm", "--model", "m", "--quiet"]) == 0
    meta = json.loads((d / "leads.json").read_text())
    assert (meta["engine"], meta["model"], len(meta["leads"])) == ("lm", "m", 1)
    logged = [json.loads(line) for line in (d / "verdicts.jsonl").read_text().splitlines()]
    assert [v["decision"] for v in logged] == ["INCONCLUSIVE"]


def test_a_claude_run_records_its_model_and_verify_settings(run_dir, monkeypatch):
    """What `orion ui` needs to resume the run as it began (one model per scan): leads.json says so."""
    monkeypatch.setattr(discover, "discover", lambda *a, **k: list(LEADS))
    monkeypatch.setattr(claude_cli, "run_agent", _agent({0: "CONFIRM", 1: "REJECT", 2: "REJECT"}, []))
    monkeypatch.setattr(config, "MODEL", "opus")
    monkeypatch.setattr(config, "SEMANTIC_MODE", config.SEMANTIC_MODE)    # --semantic-mode sets both; restored
    monkeypatch.setenv("ORION_SEMANTIC_MODE", "vector")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:11434")
    cli.main(["scan", "--scan-id", "sid", "--quiet", "--verify-routing", "--semantic-mode", "hybrid",
              "--fail-on", "confirm"])
    meta = json.loads((run_dir / "leads.json").read_text())
    assert (meta["engine"], meta["model"]) == ("claude", "opus")
    assert meta["settings"] == {"verify_routing": True, "verify_context": False, "semantic_mode": "hybrid",
                                "fail_on": "confirm", "endpoint": True}             # the URL itself is not kept
    assert "127.0.0.1" not in (run_dir / "leads.json").read_text()


def test_resume_is_refused_while_another_scan_writes_the_run(run_dir, monkeypatch, capsys):
    monkeypatch.setattr(discover, "discover", lambda *a, **k: list(LEADS))
    monkeypatch.setattr(claude_cli, "run_agent", _agent({0: "CONFIRM", 1: "ERROR", 2: "REJECT"}, []))
    cli.main(["scan", "--scan-id", "sid", "--quiet"])
    held = runlock.acquire(run_dir)                  # a scan still running in another terminal
    try:
        monkeypatch.setattr(claude_cli, "run_agent", lambda *a, **k: pytest.fail("a held run dir is not verified"))
        assert cli.main(["scan", "--resume", str(run_dir), "--quiet"]) == 2
        assert "being written by another scan" in capsys.readouterr().err
    finally:
        held.close()
    assert runlock.held(run_dir) is False            # and the first scan released it when it returned


def test_resume_says_so_when_the_model_changed(run_dir, monkeypatch):
    monkeypatch.setattr(discover, "discover", lambda *a, **k: list(LEADS))
    monkeypatch.setattr(claude_cli, "run_agent", _agent({0: "CONFIRM", 1: "ERROR", 2: "REJECT"}, []))
    monkeypatch.setattr(config, "MODEL", "opus")
    cli.main(["scan", "--scan-id", "sid", "--quiet"])
    monkeypatch.setattr(config, "MODEL", "haiku")
    monkeypatch.setattr(claude_cli, "run_agent", _agent({1: "REJECT"}, []))
    cli.main(["scan", "--resume", str(run_dir), "--quiet"])
    events = [json.loads(line) for line in (run_dir / "progress.jsonl").read_text().splitlines() if line.strip()]
    assert any(e.get("event") == "warn" and "ORION_MODEL=opus" in e.get("detail", "") for e in events)


def _until_stopped(seconds: float = 10.0) -> None:
    deadline = time.monotonic() + seconds
    while not claude_cli.interrupted() and time.monotonic() < deadline:
        time.sleep(0.01)


def test_ctrl_c_under_watch_stops_and_keeps_the_run_resumable(run_dir, monkeypatch):
    """`monitor.tail` swallows a Ctrl-C and returns: the scan must still stop. No session the Ctrl-C killed is
    retried, no ERROR is logged for the leads it cut short, and no verdicts.json makes the run look finished."""
    started, launched = threading.Event(), []

    def run_once(cmd, *, timeout, on_event):           # a claude -p that runs until the Ctrl-C kills it
        launched.append(cmd[-1].split("lead-")[1].split()[0])
        started.set()
        _until_stopped()
        return {"_error": "claude -p exited 130"}

    monkeypatch.setattr(discover, "discover", lambda *a, **k: list(LEADS))
    monkeypatch.setattr(claude_cli, "_run_once", run_once)
    monkeypatch.setattr(monitor, "tail", lambda d, stop: started.wait(10))   # returns early, stop unset
    with pytest.raises(KeyboardInterrupt):
        cli.main(["scan", "--scan-id", "sid", "--watch"])
    assert claude_cli.interrupted() and launched and len(launched) == len(set(launched))   # none retried
    assert (run_dir / "leads.json").exists() and not (run_dir / "verdicts.json").exists()
    log = run_dir / "verdicts.jsonl"
    assert not log.exists() or log.read_text().strip() == ""


def test_ctrl_c_under_watch_during_discovery_saves_no_leads(run_dir, monkeypatch):
    """An unfinished discovery must not be saved as final: --resume never re-discovers."""
    started = threading.Event()

    def discovering(*a, **k):
        started.set()
        _until_stopped()
        return LEADS[:1]                                # what the shapes had when the Ctrl-C came

    monkeypatch.setattr(discover, "discover", discovering)
    monkeypatch.setattr(claude_cli, "_run_once", lambda *a, **k: pytest.fail("a session started after the stop"))
    monkeypatch.setattr(monitor, "tail", lambda d, stop: started.wait(10))
    with pytest.raises(KeyboardInterrupt):
        cli.main(["scan", "--scan-id", "sid", "--watch"])
    assert not (run_dir / "leads.json").exists()


def test_resume_takes_the_lock_before_it_reads_the_run(run_dir, monkeypatch, capsys):
    """A scan still finishing the run could log verdicts between a read and a later lock."""
    (run_dir / "leads.json").write_text(json.dumps({"scan_id": "sid", "repo": None, "leads": []}))
    held = runlock.acquire(run_dir)
    monkeypatch.setattr(cli, "_load_run", lambda d: pytest.fail("read while another scan holds the run"))
    try:
        assert cli.main(["scan", "--resume", str(run_dir), "--quiet"]) == 2
    finally:
        held.close()
    assert "being written by another scan" in capsys.readouterr().err


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root may write a read-only file")
def test_resume_refuses_a_run_dir_it_cannot_write(run_dir, monkeypatch, capsys):
    """It would pay for verifier sessions, then fail to keep their verdicts."""
    monkeypatch.setattr(discover, "discover", lambda *a, **k: list(LEADS))
    monkeypatch.setattr(claude_cli, "run_agent", _agent({0: "CONFIRM", 1: "ERROR", 2: "REJECT"}, []))
    cli.main(["scan", "--scan-id", "sid", "--quiet"])
    log = run_dir / "verdicts.jsonl"
    os.chmod(log, 0o444)
    try:
        monkeypatch.setattr(claude_cli, "run_agent", lambda *a, **k: pytest.fail("paid for a verdict it cannot keep"))
        assert cli.main(["scan", "--resume", str(run_dir), "--quiet"]) == 2
        assert "cannot write" in capsys.readouterr().err
    finally:
        os.chmod(log, 0o644)
