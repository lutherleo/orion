"""The Sandbox seam (orion/dynamic/runner.py). Token-free; spawns short local subprocesses only.

Verifies the shipped floor (SubprocessSandbox) reports success, non-zero exit, and timeout through a
RunResult without ever raising — the contract every caller's skip logic relies on.
"""
from __future__ import annotations

import sys

from orion.dynamic.runner import RunResult, SubprocessSandbox


def test_success_captures_stdout_and_exit_zero():
    sb = SubprocessSandbox()
    r = sb.run([sys.executable, "-c", "print('hello')"], timeout=30)
    assert isinstance(r, RunResult)
    assert r.exit_code == 0
    assert r.timed_out is False
    assert "hello" in r.stdout


def test_nonzero_exit_is_reported_not_raised():
    sb = SubprocessSandbox()
    r = sb.run([sys.executable, "-c", "import sys; sys.exit(3)"], timeout=30)
    assert r.exit_code == 3
    assert r.timed_out is False


def test_timeout_is_reported_not_raised():
    sb = SubprocessSandbox()
    r = sb.run([sys.executable, "-c", "import time; time.sleep(10)"], timeout=0.5)
    assert r.timed_out is True
    assert r.exit_code is None


def test_env_is_passed_through():
    sb = SubprocessSandbox()
    r = sb.run([sys.executable, "-c", "import os; print(os.environ.get('ORION_X'))"],
               timeout=30, env={"ORION_X": "42"})
    assert "42" in r.stdout
