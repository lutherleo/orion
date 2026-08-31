"""Host side of the JS tracer: run a Node driver under the CPU profiler, read back its ObservedTrace.

Mirrors tracer_py: the tracing happens in `_boot_js.js` inside a sandboxed Node subprocess (Orion
never runs target JS in its own process); this module wires the bootstrap through a `Sandbox`, points
it at a sidecar file, and parses the wire JSON. A missing Node binary, a timeout, a crash, or a
garbled sidecar all yield an empty-but-valid `ObservedTrace` plus the `RunResult` — the caller logs
the skip; nothing is fabricated.

The JS trace is SAMPLED (V8 CPU profiler), so it is a lower bound — matching the delta's semantics.
Attribution (file+line) is exact for whatever is sampled; dispatch is not emitted in this first cut.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile

from . import trace as trace_mod
from .runner import RunResult, Sandbox, SubprocessSandbox

_BOOT_JS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_boot_js.js")


def _resolve_node(node_exe: str | None) -> str | None:
    """Find the Node binary: explicit arg, then $ORION_NODE, then PATH. None if unavailable."""
    if node_exe:
        return node_exe
    return os.environ.get("ORION_NODE") or shutil.which("node")


def trace(driver_path: str, root: str, *, sandbox: Sandbox | None = None,
          timeout: float | None = 120.0,
          node_exe: str | None = None) -> tuple[trace_mod.ObservedTrace, RunResult]:
    """Run `driver_path` under the Node CPU profiler, scoped to `root`; return (trace, run_result).

    `sandbox` defaults to `SubprocessSandbox`. `node_exe` overrides the Node binary (else $ORION_NODE
    or PATH). If Node cannot be found, returns an empty trace and a RunResult flagged as a failure so
    the caller reports a clean skip."""
    node = _resolve_node(node_exe)
    if node is None:
        return trace_mod.ObservedTrace(), RunResult(
            stdout="", stderr="node executable not found (install Node or set ORION_NODE)",
            exit_code=127, timed_out=False)

    sandbox = sandbox or SubprocessSandbox()
    fd, out_path = tempfile.mkstemp(prefix="orion_trace_js_", suffix=".json")
    os.close(fd)
    try:
        cmd = [node, _BOOT_JS, os.path.abspath(driver_path), os.path.abspath(root), out_path]
        result = sandbox.run(cmd, cwd=os.path.abspath(root), timeout=timeout)
        return _read_trace(out_path), result
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass


def _read_trace(out_path: str) -> trace_mod.ObservedTrace:
    try:
        with open(out_path, encoding="utf-8") as f:
            return trace_mod.from_wire(json.load(f))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return trace_mod.ObservedTrace()
