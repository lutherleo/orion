"""End-to-end Python tracer (orion/dynamic/tracer_py.py) against a REAL tiny driver+target.

Token-free — it runs a local python subprocess, no Claude, no Neo4j. This is Layer 2's proof: a real
ObservedTrace comes back from actually executing code, including a dispatch through a polymorphic
method (the "pointers switch" case) and the graph-lies-by-omission arrow/lambda call.
"""
from __future__ import annotations

import textwrap

from orion.dynamic.tracer_py import trace


def _write_target_and_driver(root):
    """A target with a base/subclass (polymorphism) and a lambda-wrapped call, plus a driver."""
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

        # a call nested in a lambda assigned to a dict value — the static "lies by omission" shape
        DISPATCH = {"go": lambda v: sink(v)}
    """))
    (root / "driver.py").write_text(textwrap.dedent("""\
        import target
        target.run(target.Impl(), 5)      # dispatches to Impl.handle -> sink
        target.DISPATCH["go"](7)          # lambda -> sink
    """))
    return str(root / "driver.py")


def test_trace_captures_calls_methods_and_dispatch(tmp_path):
    driver = _write_target_and_driver(tmp_path)
    observed, result = trace(driver, str(tmp_path), timeout=60)

    assert result.timed_out is False
    assert not observed.is_empty()

    method_names = {m.name for m in observed.methods}
    # sink executed; the polymorphic Impl.handle executed (qualname on 3.11+, else 'handle').
    assert "sink" in method_names
    assert any("handle" in n for n in method_names)

    # An OBSERVED_CALL into sink exists (from Impl.handle and/or the lambda).
    assert any(c.callee_name == "sink" for c in observed.calls)

    # The dispatch heuristic caught the bound-method call reaching the concrete Impl.handle.
    assert any("handle" in d.resolved_callee_name for d in observed.dispatches)

    # Everything recorded is scoped to the target root (no stdlib noise).
    assert all(str(tmp_path) in m.file for m in observed.methods)


def test_driver_exception_still_yields_partial_trace(tmp_path):
    (tmp_path / "target.py").write_text("def f():\n    return 1\n")
    (tmp_path / "driver.py").write_text(
        "import target\ntarget.f()\nraise RuntimeError('boom')\n")
    observed, result = trace(str(tmp_path / "driver.py"), str(tmp_path), timeout=60)
    # The driver raised, but f() ran before it, so the trace is non-empty (finally-write salvage).
    assert any(m.name == "f" for m in observed.methods)


def test_timeout_yields_empty_trace_not_crash(tmp_path):
    (tmp_path / "driver.py").write_text("import time\nwhile True:\n    time.sleep(0.1)\n")
    observed, result = trace(str(tmp_path / "driver.py"), str(tmp_path), timeout=1.0)
    assert result.timed_out is True
    assert observed.is_empty()
