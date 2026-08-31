"""The language-neutral trace contract (orion/dynamic/trace.py). Token-free, no runtime, no DB.

These types are the seam between the language tracers (Python/JS) and the graph merge, so the tests
pin exactly the fields every producer must fill and every consumer may rely on. Hand-built traces —
no interpreter needed.
"""
from __future__ import annotations

import dataclasses

import pytest

from orion.dynamic.trace import (
    ObservedCall,
    ObservedDispatch,
    ObservedMethod,
    ObservedTrace,
)


def test_types_are_frozen():
    """The contract is immutable: a consumer can hold a frame without a producer mutating it later."""
    call = ObservedCall("a.py", 1, "caller", "b.py", 2, "callee")
    with pytest.raises(dataclasses.FrozenInstanceError):
        call.caller_name = "other"  # type: ignore[misc]


def test_observed_call_fields():
    c = ObservedCall(
        caller_file="app/a.py", caller_line=10, caller_name="handler",
        callee_file="lib/b.py", callee_line=99, callee_name="sink",
    )
    assert c.caller_file == "app/a.py"
    assert c.callee_name == "sink"
    assert c.callee_line == 99


def test_observed_dispatch_fields():
    d = ObservedDispatch(
        call_site_file="app/a.py", call_site_line=10,
        resolved_callee_name="ConcreteImpl.run",
        resolved_callee_file="lib/impl.py", resolved_callee_line=42,
    )
    assert d.resolved_callee_name == "ConcreteImpl.run"
    assert d.call_site_line == 10


def test_observed_method_fields():
    m = ObservedMethod(name="reflected", file="dyn.py", line=7)
    assert (m.name, m.file, m.line) == ("reflected", "dyn.py", 7)


def test_empty_trace_is_valid():
    """A run that observed nothing is a valid trace (honest 'harness exercised nothing'), not an error."""
    t = ObservedTrace()
    assert t.calls == ()
    assert t.dispatches == ()
    assert t.methods == ()
    assert t.is_empty()


def test_trace_holds_tuples_and_reports_nonempty():
    t = ObservedTrace(
        calls=(ObservedCall("a.py", 1, "f", "b.py", 2, "g"),),
        dispatches=(ObservedDispatch("a.py", 1, "Impl.g", "b.py", 2),),
        methods=(ObservedMethod("h", "c.py", 3),),
    )
    assert not t.is_empty()
    assert isinstance(t.calls, tuple)
    assert t.calls[0].callee_name == "g"
    assert t.dispatches[0].resolved_callee_name == "Impl.g"
    assert t.methods[0].name == "h"


def test_dispatch_resolved_line_optional():
    """A JS require-hook may know the target name/file but not its exact line — line is optional."""
    d = ObservedDispatch("a.py", 1, "cb", "b.py", None)
    assert d.resolved_callee_line is None


def test_wire_roundtrip():
    """to_wire → from_wire is lossless: the trace crosses the subprocess boundary intact."""
    from orion.dynamic.trace import from_wire, to_wire

    t = ObservedTrace(
        calls=(ObservedCall("a.py", 1, "f", "b.py", 2, "g"),),
        dispatches=(ObservedDispatch("a.py", 1, "Impl.g", "b.py", 2),),
        methods=(ObservedMethod("h", "c.py", 3),),
    )
    assert from_wire(to_wire(t)) == t


def test_from_wire_tolerates_missing_sections():
    """A truncated/empty sidecar (missing keys) yields an empty-but-valid trace, never a crash."""
    from orion.dynamic.trace import from_wire

    assert from_wire({}).is_empty()
    assert from_wire({"calls": None}).is_empty()
