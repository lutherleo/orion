"""Token-free tests for the trace contract and the tracer parsers (V8 coverage + cpu-profile, Go
covdata). Pure parses over literal blobs -- no Node, no `go`.
"""
from __future__ import annotations

import dataclasses

import pytest

from orion.runtime.go_tracer import module_path, parse_covdata_textfmt
from orion.runtime.trace import (
    Hit,
    ObservedCall,
    ObservedDispatch,
    ObservedMethod,
    RuntimeTrace,
    TraceAccumulator,
    from_wire,
    to_wire,
)
from orion.runtime.v8_tracer import parse_cpu_profile, parse_v8_coverage

REPO = "/repo"
URL = "file:///repo"


def _reader(sources):
    return lambda rel: sources.get(rel)


# ── contract ───────────────────────────────────────────────────────────
def test_types_are_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        ObservedCall("a.py", 1, "b.py", 2).hits = 5  # type: ignore[misc]


def test_empty_trace_is_valid():
    assert RuntimeTrace().is_empty()
    assert from_wire({}).is_empty() and from_wire({"calls": None}).is_empty() and from_wire(None).is_empty()


def test_wire_roundtrip():
    t = RuntimeTrace(
        coverage=(Hit("a.py", 3, 2),),
        methods=(ObservedMethod("h", "c.py", 3, 4),),
        calls=(ObservedCall("a.py", 1, "b.py", 2, 3, "f", "g"),),
        dispatches=(ObservedDispatch("a.py", 1, "Impl.g", "b.py", 2),
                    ObservedDispatch("a.py", 1, "cb", "b.py", None)),
    )
    assert from_wire(to_wire(t)) == t


def test_accumulator_sums_and_dedups():
    acc = TraceAccumulator()
    step = RuntimeTrace(coverage=(Hit("a", 1, 2),), methods=(ObservedMethod("f", "a", 1, 1),),
                        calls=(ObservedCall("a", 1, "b", 2),),
                        dispatches=(ObservedDispatch("a", 1, "g", "b", 2),))
    acc.add(step)
    acc.add(step)
    t = acc.freeze()
    assert t.coverage == (Hit("a", 1, 4),)
    assert t.methods == (ObservedMethod("f", "a", 1, 2),)
    assert t.calls[0].hits == 2
    assert len(t.dispatches) == 1
    assert acc.line_count() == 1
    assert step.merge(step) == t


# ── V8 coverage -> lines + functions ───────────────────────────────────
def test_v8_parse_basic():
    src = "a();\nb();\nc();\n"  # lines at bytes 0,5,10
    blob = {"result": [{
        "url": f"{URL}/app/x.js",
        "functions": [{"functionName": "", "ranges": [
            {"startOffset": 0, "endOffset": 4, "count": 2},   # line 1
            {"startOffset": 5, "endOffset": 9, "count": 0},   # line 2, NOT executed -> skipped
            {"startOffset": 10, "endOffset": 13, "count": 1}, # line 3
        ]}],
    }]}
    trace, malformed = parse_v8_coverage([blob], REPO, _reader({"app/x.js": src}))
    assert {(h.file_path, h.line): h.hit_count for h in trace.coverage} == {("app/x.js", 1): 2,
                                                                            ("app/x.js", 3): 1}
    assert malformed == 0


def test_v8_innermost_range_marks_every_executed_line():
    """A function body line with no range of its own inherits the enclosing count; a count-0 block
    carves its lines out. The naive start-line-only read would mark just line 1."""
    src = "function f(x) {\n  a();\n  if (x) {\n    b();\n  }\n  c();\n}\nf(0);\n"
    block_start, block_end = src.index("{\n    b"), src.index("}\n  c") + 1
    blob = {"result": [{"url": f"{URL}/m.js", "functions": [
        {"functionName": "", "ranges": [{"startOffset": 0, "endOffset": len(src), "count": 1}]},
        {"functionName": "f", "ranges": [
            {"startOffset": 0, "endOffset": src.index("\nf(0)"), "count": 1},
            {"startOffset": block_start, "endOffset": block_end, "count": 0},
        ]},
    ]}]}
    trace, _ = parse_v8_coverage([blob], REPO, _reader({"m.js": src}))
    lines = {h.line for h in trace.coverage}
    assert {1, 2, 3, 6, 8} <= lines          # header, a(), the if, c(), the module call
    assert 4 not in lines                    # b() sits in the count-0 block
    assert ObservedMethod("f", "m.js", 1, 1) in trace.methods   # exact invocation count


def test_v8_parse_skips_node_modules_and_internals():
    blob = {"result": [
        {"url": "node:internal/bootstrap", "functions": [{"ranges": [{"startOffset": 0, "endOffset": 1, "count": 1}]}]},
        {"url": f"{URL}/node_modules/x/i.js", "functions": [{"ranges": [{"startOffset": 0, "endOffset": 1, "count": 1}]}]},
    ]}
    trace, _ = parse_v8_coverage([blob], REPO, _reader({}))
    assert trace.is_empty()


def test_v8_parse_malformed_counted_not_crash():
    blobs = [
        {"result": "not a list"},
        {"result": [{"url": f"{URL}/a.js", "functions": [{"ranges": [{"startOffset": "bad", "count": 1}]}]}]},
        {"nokey": 1},
    ]
    trace, malformed = parse_v8_coverage(blobs, REPO, _reader({"a.js": "x\n"}))
    assert trace.coverage == ()
    assert malformed == 3


# ── V8 cpu-profile -> calls ────────────────────────────────────────────
def test_cpu_profile_calls():
    prof = {"nodes": [
        {"id": 1, "callFrame": {"url": f"{URL}/a.js", "lineNumber": 9, "functionName": "h"}, "children": [2]},
        {"id": 2, "callFrame": {"url": f"{URL}/b.js", "lineNumber": 4, "functionName": "s"}, "children": []},
        {"id": 3, "callFrame": {"url": "node:internal", "lineNumber": 0}, "children": [1]},
    ]}
    trace, dropped = parse_cpu_profile(prof, REPO)
    assert [(c.caller_file, c.caller_line, c.callee_file, c.callee_line, c.callee_name)
            for c in trace.calls] == [("a.js", 10, "b.js", 5, "s")]   # 0-based -> 1-based
    assert dropped == 1                                               # node:internal -> a.js


# ── Go covdata ─────────────────────────────────────────────────────────
def test_go_covdata_parse():
    text = ("mode: set\n"
            "example.com/app/handler.go:12.2,14.16 2 3\n"
            "example.com/app/handler.go:20.2,20.10 1 0\n"
            "garbage line\n")
    trace = parse_covdata_textfmt(text, module_prefix="example.com/app")
    assert {(h.file_path, h.line): h.hit_count for h in trace.coverage} == {
        ("handler.go", 12): 3, ("handler.go", 13): 3, ("handler.go", 14): 3}


def test_go_module_prefix_read_from_go_mod(tmp_path):
    (tmp_path / "go.mod").write_text("// c\nmodule example.com/tiny\n\ngo 1.21\n")
    assert module_path(str(tmp_path)) == "example.com/tiny"
    assert module_path(str(tmp_path / "missing")) == ""
