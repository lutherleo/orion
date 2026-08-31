"""Host side of the Python tracer: run a driver in a sandboxed subprocess, read back its ObservedTrace.

The actual tracing happens in ``_boot_py`` inside the child process (so Orion never executes target
code in its own process). This module just wires the bootstrap through a `Sandbox`, points it at a
sidecar file, and parses the result. A timeout, a crash, or a missing/garbled sidecar all yield an
empty-but-valid `ObservedTrace` plus the `RunResult` — the caller logs the skip; nothing is fabricated.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

from . import trace as trace_mod
from .runner import RunResult, Sandbox, SubprocessSandbox


def trace(driver_path: str, root: str, *, sandbox: Sandbox | None = None,
          timeout: float | None = 120.0,
          python_exe: str | None = None) -> tuple[trace_mod.ObservedTrace, RunResult]:
    """Run `driver_path` under the tracer, scoped to `root`, and return (trace, run_result).

    `sandbox` defaults to `SubprocessSandbox` (timeout + temp cwd). `python_exe` defaults to the
    current interpreter so the child shares this venv (orion importable for `-m orion.dynamic._boot_py`).
    """
    sandbox = sandbox or SubprocessSandbox()
    python_exe = python_exe or sys.executable
    fd, out_path = tempfile.mkstemp(prefix="orion_trace_", suffix=".json")
    os.close(fd)
    try:
        cmd = [python_exe, "-m", "orion.dynamic._boot_py",
               os.path.abspath(driver_path), os.path.abspath(root), out_path]
        # The child runs with cwd=target root, so the `orion` package must be importable via
        # PYTHONPATH, not cwd — an editable install's finder may not expose a freshly-added
        # subpackage otherwise. Point at the dir CONTAINING the `orion` package.
        env = {"PYTHONPATH": os.pathsep.join(
            [_orion_parent()] + ([os.environ["PYTHONPATH"]] if os.environ.get("PYTHONPATH") else []))}
        result = sandbox.run(cmd, cwd=os.path.abspath(root), timeout=timeout, env=env)
        observed = _read_trace(out_path)
        return observed, result
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass


def _orion_parent() -> str:
    """The directory containing the `orion` package (so `-m orion.dynamic._boot_py` resolves in a
    child regardless of cwd or an editable-install finder's package map)."""
    import orion
    return os.path.dirname(os.path.dirname(os.path.abspath(orion.__file__)))


def _read_trace(out_path: str) -> trace_mod.ObservedTrace:
    """Parse the sidecar into an ObservedTrace; an empty/missing/corrupt file is an empty trace."""
    try:
        with open(out_path, encoding="utf-8") as f:
            return trace_mod.from_wire(json.load(f))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return trace_mod.ObservedTrace()
