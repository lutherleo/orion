"""A scan never loses work: leads land on disk after discovery, each verdict as it completes, and
`--resume <run_dir>` finishes an interrupted run verifying ONLY what is missing (or ERRORed).
`--fail-on` turns verdicts into a CI exit code. Token-free: the real CLI and the real verify_all run,
with discovery, the graph and the `claude -p` call stubbed."""
from __future__ import annotations

import json

import pytest

from orion import claude_cli, cli, discover, embed, graphdb
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
