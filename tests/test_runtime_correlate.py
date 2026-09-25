"""Token-free tests for trace -> graph correlation (orion/runtime/correlate.py). Pure: hand-built
index + trace, no Neo4j.

Pins the resolution order (exact def-line, runtime-only method, containing method, basename
fallback), the synthetic-twin/operator tie-breaks, and that nothing unresolvable is ever fabricated.
"""
from __future__ import annotations

from orion.runtime.correlate import (
    build_line_starts,
    correlate,
    index_from_rows,
    offset_to_line,
    relativize,
)
from orion.runtime.trace import Hit, ObservedCall, ObservedDispatch, ObservedMethod, RuntimeTrace

SID = "scan1"


def _idx(methods=(), calls=()):
    return index_from_rows(
        [{"f": f, "l": l, "fn": fn} for (f, l, fn) in methods],
        [{"f": f, "l": l, "uid": uid, "name": name} for (f, l, uid, name) in
         (c if len(c) == 4 else (*c, "") for c in calls)],
    )


# ── byte offset -> line (V8) ───────────────────────────────────────────
def test_byte_to_line():
    starts = build_line_starts("line1\nline2\nline3\n")
    assert starts == [0, 6, 12, 18]
    assert offset_to_line(starts, 0) == 1
    assert offset_to_line(starts, 5) == 1          # the '\n' of line 1
    assert offset_to_line(starts, 6) == 2
    assert offset_to_line(starts, 13) == 3
    assert offset_to_line(starts, 999) == 4        # past the end -> last line
    assert offset_to_line(starts, -1) == 1         # degenerate -> clamp


def test_byte_to_line_multibyte():
    starts = build_line_starts("é = 1\nx = 2\n")   # 'é' is 2 bytes
    assert starts[1] == 7
    assert offset_to_line(starts, 7) == 2


def test_relativize(tmp_path):
    (tmp_path / "app").mkdir()
    f = tmp_path / "app" / "x.py"
    f.write_text("")
    assert relativize(str(f), str(tmp_path)) == "app/x.py"
    assert relativize("app\\y.py", str(tmp_path)) == "app/y.py"      # already relative, normalized
    assert relativize(str(tmp_path.parent / "other.py"), str(tmp_path)) is None
    assert relativize("<frozen runpy>", str(tmp_path)) is None


# ── method resolution ──────────────────────────────────────────────────
def test_exact_and_containing_method():
    idx = _idx(methods=[("f.js", 10, "f.js::a"), ("f.js", 30, "f.js::b"), ("g.js", 5, "g.js::c")])
    assert idx.method_exact("f.js", 10) == "f.js::a"
    assert idx.method_exact("f.js", 11) is None
    assert idx.method_containing("f.js", 9) is None      # above the first method
    assert idx.method_containing("f.js", 25) == "f.js::a"
    assert idx.method_containing("f.js", 999) == "f.js::b"
    assert idx.method_containing("unknown.js", 5) is None


def test_bad_rows_never_anchor():
    idx = index_from_rows([{"f": "f.js", "l": 10, "fn": "ok"}, {"f": "", "l": 5, "fn": "no_file"},
                           {"f": "f.js", "l": None, "fn": "no_line"}], [])
    assert idx.method_containing("f.js", 12) == "ok"
    assert idx.method_containing("", 5) is None


def test_synthetic_twin_loses_to_real_method_on_line_collision():
    """Joern emits `Foo.handle` AND `Foo.handle<metaClassAdapter>` at one (file,line): resolve to the
    REAL method regardless of row order."""
    for rows in ([("app/h.py", 6, "app.Foo.handle"), ("app/h.py", 6, "app.Foo.handle<metaClassAdapter>")],
                 [("app/h.py", 6, "app.Foo.handle<metaClassAdapter>"), ("app/h.py", 6, "app.Foo.handle")]):
        assert _idx(methods=rows).method_exact("app/h.py", 6) == "app.Foo.handle"


def test_operator_call_sorts_after_real_call():
    idx = _idx(calls=[("app/h.py", 9, "op", "<operator>.fieldAccess"), ("app/h.py", 9, "real", "handle")])
    assert idx.calls_at("app/h.py", 9) == ["real", "op"]


def test_same_line_methods_resolve_by_name():
    """A `def f():` on line 1 shares (file, line) with the file's <module>: the observed name picks
    the right one; without a name the least-synthetic wins."""
    idx = _idx(methods=[("t.py", 1, "t.py:<module>"), ("t.py", 1, "t.py:<module>.f")])
    assert idx.method_exact("t.py", 1, "f") == "t.py:<module>.f"
    assert idx.method_exact("t.py", 1, "<module>") == "t.py:<module>"
    assert idx.method_exact("t.py", 1) == "t.py:<module>.f"
    trace = RuntimeTrace(methods=(ObservedMethod("<module>", "t.py", 1, 1), ObservedMethod("f", "t.py", 1, 5)),
                         calls=(ObservedCall("t.py", 1, "t.py", 1, 1, "<module>", "f"),))
    plan = correlate(trace, idx, SID)
    assert plan.method_hits == {"t.py:<module>": 1, "t.py:<module>.f": 5}
    assert plan.edges_of("OBSERVED_CALL") == {("CpgMethod", "t.py:<module>", "CpgMethod", "t.py:<module>.f"): 1}


def test_basename_fallback_when_path_layout_differs():
    idx = _idx(methods=[("build/app/x.py", 2, "app.x.f")])
    assert idx.method_exact("app/x.py", 2) == "app.x.f"         # unique basename x.py stands in


def test_ambiguous_basename_is_not_guessed():
    idx = _idx(methods=[("a/x.py", 2, "a.x.f"), ("b/x.py", 2, "b.x.f")])
    assert idx.method_exact("c/x.py", 2) is None


# ── coverage -> props ──────────────────────────────────────────────────
def test_coverage_marks_calls_and_hottest_line_of_method():
    idx = _idx(methods=[("f.js", 8, "f.js::a")],
               calls=[("f.js", 10, "uidA"), ("f.js", 10, "uidB"), ("f.js", 12, "uidC")])
    trace = RuntimeTrace(coverage=(Hit("f.js", 10, 3), Hit("f.js", 10, 1), Hit("f.js", 12, 5),
                                   Hit("f.js", 999, 2)))
    plan = correlate(trace, idx, SID)
    assert plan.call_hits == {"uidA": 4, "uidB": 4, "uidC": 5}      # two calls share line 10
    assert plan.method_hits == {"f.js::a": 5}                       # hottest line, not a sum
    assert plan.dropped["coverage_no_call"] == 1                    # line 999


def test_method_invocations_beat_line_counts():
    idx = _idx(methods=[("f.js", 8, "f.js::a")])
    trace = RuntimeTrace(coverage=(Hit("f.js", 9, 50),), methods=(ObservedMethod("a", "f.js", 8, 7),))
    assert correlate(trace, idx, SID).method_hits == {"f.js::a": 7}


def test_line_zero_correlates_to_nothing():
    idx = _idx(calls=[("f.js", 5, "uid5"), ("f.js", 0, "uid0")])
    plan = correlate(RuntimeTrace(coverage=(Hit("f.js", 0, 9),)), idx, SID)
    assert plan.call_hits == {}
    assert plan.dropped["coverage_no_call"] == 1


# ── runtime-only methods ───────────────────────────────────────────────
def test_unmatched_named_method_becomes_observed_method():
    idx = _idx(methods=[("app/known.py", 5, "app.known.f")])
    trace = RuntimeTrace(methods=(
        ObservedMethod("f", "app/known.py", 5),          # exists statically -> NOT new
        ObservedMethod("reflected", "app/dyn.py", 9),    # no static match -> new
        ObservedMethod("", "app/dyn.py", 1),             # unnamed module wrapper -> never a node
        ObservedMethod("<lambda>", "app/dyn.py", 3),     # structural shim -> never a node
    ))
    plan = correlate(trace, idx, SID)
    assert [m["name"] for m in plan.new_methods] == ["reflected"]
    m = plan.new_methods[0]
    assert (m["file_path"], m["line"], m["origin"]) == ("app/dyn.py", 9, "runtime")


# ── calls -> OBSERVED_CALL ─────────────────────────────────────────────
def test_observed_call_between_static_methods_sums_hits():
    idx = _idx(methods=[("f.js", 10, "a"), ("f.js", 30, "b")])
    trace = RuntimeTrace(calls=(
        ObservedCall("f.js", 10, "f.js", 30, 2),     # exact def lines
        ObservedCall("f.js", 16, "f.js", 33, 1),     # call-site lines -> containing methods
        ObservedCall("f.js", 15, "nope.js", 1),      # callee unresolved -> dropped
        ObservedCall("f.js", 15, "f.js", 16),        # a -> a self edge -> dropped
    ))
    plan = correlate(trace, idx, SID)
    assert plan.edges_of("OBSERVED_CALL") == {("CpgMethod", "a", "CpgMethod", "b"): 3}
    assert plan.dropped["call_unresolved"] == 2


def test_call_to_runtime_only_method_targets_observed_node():
    idx = _idx(methods=[("app/a.py", 1, "app.a.caller")])
    trace = RuntimeTrace(methods=(ObservedMethod("dynh", "app/dyn.py", 3),),
                         calls=(ObservedCall("app/a.py", 1, "app/dyn.py", 3),))
    plan = correlate(trace, idx, SID)
    ((fl, fv, tl, tv), n), = plan.edges_of("OBSERVED_CALL").items()
    assert (fl, fv, tl) == ("CpgMethod", "app.a.caller", "ObservedMethod")
    assert tv == plan.new_methods[0]["uid"]


def test_plumbing_frame_is_dropped_not_fabricated():
    idx = _idx(methods=[("app/a.py", 1, "app.a.caller")])
    plan = correlate(RuntimeTrace(calls=(ObservedCall("app/a.py", 1, "runpy.py", 88),)), idx, SID)
    assert plan.edges == {}
    assert plan.dropped["call_unresolved"] == 1


# ── dispatches -> OBSERVED_DISPATCH ────────────────────────────────────
def test_dispatch_anchors_on_real_call_and_targets_resolved_method():
    idx = _idx(methods=[("app/impl.py", 4, "app.impl.Impl.handle")],
               calls=[("app/a.py", 6, "op", "<operator>.fieldAccess"), ("app/a.py", 6, "site", "handle")])
    trace = RuntimeTrace(dispatches=(ObservedDispatch("app/a.py", 6, "Impl.handle", "app/impl.py", 4),))
    plan = correlate(trace, idx, SID)
    assert plan.edges_of("OBSERVED_DISPATCH") == {("CpgCall", "site", "CpgMethod", "app.impl.Impl.handle"): 1}


def test_dispatch_without_static_call_or_line_is_dropped():
    idx = _idx(methods=[("app/impl.py", 4, "app.impl.Impl.handle")])
    trace = RuntimeTrace(dispatches=(ObservedDispatch("app/a.py", 6, "Impl.handle", "app/impl.py", 4),
                                     ObservedDispatch("app/a.py", 6, "cb", "app/impl.py", None)))
    plan = correlate(trace, idx, SID)
    assert plan.edges == {}
    assert plan.dropped["dispatch_unresolved"] == 2
