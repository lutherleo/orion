"""Stopping a scan stops it: Ctrl-C, or `orion ui`'s Stop, is SIGINT to the scan's whole process group, so the
`claude -p` sessions already running are killed by it too. A killed session is not a transient failure: run_agent
must neither retry it on a fresh session nor start new ones, or the scan keeps running (and spending) after it was
told to stop. Token-free: subprocess.run and run_agent are faked; nothing is signalled."""
from __future__ import annotations

import asyncio
import subprocess
import threading

import pytest

from orion import claude_cli, discover, verify
from orion.contracts import Lead


@pytest.fixture(autouse=True)
def fresh_stop(monkeypatch):
    """Every test starts with a scan that has not been interrupted (the real flag is process-wide)."""
    monkeypatch.setattr(claude_cli, "_STOP", threading.Event())


def _failed(cmd, **kw):
    return subprocess.CompletedProcess(args=cmd, returncode=130, stdout="", stderr="")   # killed by SIGINT


def test_no_session_starts_once_the_scan_is_interrupted(monkeypatch):
    launched = []
    monkeypatch.setattr(claude_cli.subprocess, "run", lambda cmd, **kw: launched.append(cmd))
    claude_cli.interrupt()
    out = claude_cli.run_agent("sid", "sys", "msg", retries=2)
    assert launched == [] and "interrupted" in out["_error"] and claude_cli.interrupted()


def test_a_session_killed_by_the_stop_is_not_retried(monkeypatch):
    launched, slept = [], []

    def run(cmd, **kw):
        launched.append(cmd)
        claude_cli.interrupt()            # the Stop lands while this session runs, and kills it
        return _failed(cmd)

    monkeypatch.setattr(claude_cli.subprocess, "run", run)
    monkeypatch.setattr(claude_cli.time, "sleep", slept.append)
    events: list[dict] = []
    out = claude_cli.run_agent("sid", "sys", "msg", retries=2, on_event=events.append)
    assert len(launched) == 1 and slept == []                 # no fresh session, no backoff
    assert "exited 130" in out["_error"]
    assert not any("retrying" in str(e.get("detail")) for e in events)


def test_an_interrupt_during_the_backoff_cancels_the_retry(monkeypatch):
    launched = []
    monkeypatch.setattr(claude_cli.subprocess, "run", lambda cmd, **kw: launched.append(cmd) or _failed(cmd))
    monkeypatch.setattr(claude_cli.time, "sleep", lambda s: claude_cli.interrupt())    # Stop while it waits
    out = claude_cli.run_agent("sid", "sys", "msg", retries=2)
    assert len(launched) == 1 and "not retried: the scan is stopping" in out["_error"]


def test_without_an_interrupt_a_failure_is_still_retried(monkeypatch):
    launched = []
    monkeypatch.setattr(claude_cli.subprocess, "run", lambda cmd, **kw: launched.append(cmd) or _failed(cmd))
    monkeypatch.setattr(claude_cli.time, "sleep", lambda s: None)
    claude_cli.run_agent("sid", "sys", "msg", retries=2)
    assert len(launched) == 3 and not claude_cli.interrupted()


def _cancel_while_running(coro_factory, started: threading.Event, release: threading.Event) -> None:
    """Run the coroutine, cancel it once a worker thread is inside run_agent (what asyncio.run does on the first
    SIGINT), then let the worker finish -- as the killed claude -p would."""
    async def main():
        task = asyncio.ensure_future(coro_factory())
        await asyncio.to_thread(started.wait, 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()

    asyncio.run(main())


def test_cancelling_verification_marks_the_scan_interrupted():
    started, release = threading.Event(), threading.Event()

    def run_agent(session_id, system, message, **kw):
        started.set()
        release.wait(10)
        return {"_error": "claude -p exited 130"}

    leads = [Lead(index=i, shape="A", text=f"claim-{i}", evidence="e", confidence="HIGH") for i in range(3)]
    _cancel_while_running(
        lambda: verify._verify_all_async("sid", leads, ".", lambda e: None, run_agent, 1, lambda *a: None),
        started, release)
    assert claude_cli.interrupted()


def test_cancelling_discovery_marks_the_scan_interrupted(monkeypatch):
    started, release = threading.Event(), threading.Event()

    def run_agent(*a, **kw):
        started.set()
        release.wait(10)
        return {"_error": "claude -p exited 130"}

    monkeypatch.setattr(claude_cli, "run_agent", run_agent)
    _cancel_while_running(lambda: discover._discover_async("sid", lambda e: None), started, release)
    assert claude_cli.interrupted()
