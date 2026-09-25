"""End-to-end harness runs: a pinned driver script executed under the real tracer in a subprocess,
then collected. Token-free (no Claude, no Neo4j). The Node half skips when Node is unavailable.
"""
from __future__ import annotations

import shutil
import sys
import textwrap

import pytest

from orion.runtime import engine
from orion.runtime.harness import HarnessDriver
from orion.runtime.py_tracer import PyTracer
from orion.runtime.sandbox import RunResult, SubprocessSandbox
from orion.runtime.v8_tracer import V8Tracer


def _run(tmp_path, driver_name, tracer, language, timeout=60):
    work = tmp_path / "work"
    work.mkdir()
    drv = HarnessDriver(str(tmp_path), language, tracer, harness_file=str(tmp_path / driver_name),
                        timeout=timeout)
    target = drv.start(str(tmp_path), work, [])
    seeds = drv.seeds(None, "s")
    trace = engine.run(drv, tracer, target, seeds, budget=10)
    return trace.merge(tracer.collect(work, str(tmp_path)))


# ── Python ─────────────────────────────────────────────────────────────
def _py_target(root):
    (root / "target.py").write_text(textwrap.dedent("""\
        class Base:
            def handle(self, x):
                return x

        class Impl(Base):
            def handle(self, x):
                return sink(x)

        def sink(x):
            return x * 2

        def run(obj, v):
            return obj.handle(v)

        # a call nested in a lambda assigned to a dict value -- the static "lies by omission" shape
        DISPATCH = {"go": lambda v: sink(v)}
    """))
    (root / "driver.py").write_text(textwrap.dedent("""\
        import json, target
        for _ in range(3):
            target.run(target.Impl(), 5)   # dispatches to Impl.handle -> sink
        target.DISPATCH["go"](7)           # lambda -> sink
    """))


def test_py_harness_captures_methods_calls_dispatch_and_lines(tmp_path):
    _py_target(tmp_path)
    t = _run(tmp_path, "driver.py", PyTracer(), "py")
    methods = {(m.file_path, m.line): m for m in t.methods}
    assert methods[("target.py", 9)].name == "sink"
    assert methods[("target.py", 9)].hit_count == 4               # 3 via Impl.handle + 1 via lambda
    assert methods[("target.py", 6)].hit_count == 3               # Impl.handle, exact invocations
    assert any(c.callee_line == 9 and c.caller_line == 6 for c in t.calls)   # Impl.handle -> sink
    assert any(c.callee_line == 9 and c.caller_name.endswith("<lambda>") for c in t.calls)
    assert any(d.callee_line == 6 and d.site_file == "target.py" for d in t.dispatches)
    lines = {(h.file_path, h.line) for h in t.coverage}
    assert ("target.py", 7) in lines and ("target.py", 10) in lines      # executed bodies
    assert ("target.py", 3) not in lines                                  # Base.handle never ran
    # Scoped to the target: no stdlib (json) and never the driver's absolute path.
    assert {m.file_path for m in t.methods} <= {"target.py", "driver.py"}


def test_py_settrace_fallback_matches_monitoring(tmp_path, monkeypatch):
    """Interpreters without sys.monitoring (<3.12) use the settrace engine: same observations."""
    _py_target(tmp_path)
    fast = _run(tmp_path, "driver.py", PyTracer(), "py")
    monkeypatch.setenv("ORION_PY_TRACER", "settrace")
    (tmp_path / "work").rename(tmp_path / "work-fast")
    slow = _run(tmp_path, "driver.py", PyTracer(), "py")
    assert set(slow.methods) == set(fast.methods)
    assert set(slow.calls) == set(fast.calls)
    assert set(slow.dispatches) == set(fast.dispatches)
    assert {(h.file_path, h.line) for h in slow.coverage} == {(h.file_path, h.line) for h in fast.coverage}


def test_py_driver_exception_still_yields_partial_trace(tmp_path):
    (tmp_path / "target.py").write_text("def f():\n    return 1\n")
    (tmp_path / "driver.py").write_text("import target\ntarget.f()\nraise RuntimeError('boom')\n")
    t = _run(tmp_path, "driver.py", PyTracer(), "py")
    assert any(m.name == "f" for m in t.methods)


def test_py_timeout_yields_empty_trace_not_crash(tmp_path):
    (tmp_path / "driver.py").write_text("import time\nwhile True:\n    time.sleep(0.1)\n")
    assert _run(tmp_path, "driver.py", PyTracer(), "py", timeout=1.5).is_empty()


# ── Node ───────────────────────────────────────────────────────────────
@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_js_harness_captures_exact_counts_and_calls(tmp_path):
    (tmp_path / "target.js").write_text(textwrap.dedent("""\
        function sink(x) {
          let s = 0;
          for (let i = 0; i < 200; i++) { s += (x * 3 + i) % 7; }
          return s;
        }
        function handle(req) { return sink(req.q); }
        function never() { return 1; }
        module.exports = { sink, handle, never };
    """))
    (tmp_path / "driver.js").write_text(textwrap.dedent("""\
        const t = require('./target.js');
        let acc = 0;
        for (let i = 0; i < 20000; i++) { acc += t.handle({ q: i }); }
        if (acc < 0) console.log(acc);
    """))
    t = _run(tmp_path, "driver.js", V8Tracer(settle_ms=0), "js")
    methods = {m.name: m for m in t.methods}
    assert methods["handle"].hit_count == 20000          # precise coverage: exact, not sampled
    assert "never" not in methods
    lines = {(h.file_path, h.line) for h in t.coverage}
    assert ("target.js", 3) in lines and ("target.js", 7) not in lines
    assert t.calls and all(c.callee_line >= 1 for c in t.calls)


def test_missing_node_is_a_failed_run_not_a_crash(tmp_path):
    (tmp_path / "driver.js").write_text("")
    t = _run(tmp_path, "driver.js", V8Tracer(node_exe=str(tmp_path / "no-such-node")), "js")
    assert t.is_empty()


# ── the sandbox floor ──────────────────────────────────────────────────
def test_sandbox_reports_exit_timeout_and_env():
    sb = SubprocessSandbox()
    r = sb.run([sys.executable, "-c", "print('hello')"], timeout=30)
    assert isinstance(r, RunResult) and r.exit_code == 0 and "hello" in r.stdout
    assert sb.run([sys.executable, "-c", "import sys; sys.exit(3)"], timeout=30).exit_code == 3
    r = sb.run([sys.executable, "-c", "import time; time.sleep(10)"], timeout=0.5)
    assert r.timed_out and r.exit_code is None
    r = sb.run([sys.executable, "-c", "import os; print(os.environ.get('ORION_X'))"],
               timeout=30, env={"ORION_X": "42"})
    assert "42" in r.stdout
    assert sb.run(["/no/such/exe-orion"], timeout=5).exit_code == 127
