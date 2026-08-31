"""Frame→graph mapping (orion/dynamic/merge.build_batch). Pure — hand-built index + trace, no DB.

Pins the three decisions: an executed method with no static CpgMethod becomes an :ObservedMethod; an
OBSERVED_CALL is emitted only when BOTH endpoints resolve (plumbing frames are dropped, not
fabricated); an OBSERVED_DISPATCH anchors on the static CpgCall at the call site.
"""
from __future__ import annotations

from orion.dynamic.merge import build_batch, index_from_rows
from orion.dynamic.trace import ObservedCall, ObservedDispatch, ObservedMethod, ObservedTrace

ROOT = "/repo"
SID = "scan1"


def _idx(methods=(), calls=()):
    return index_from_rows(
        [{"f": f, "l": l, "fn": fn} for (f, l, fn) in methods],
        [{"f": f, "l": l, "uid": uid} for (f, l, uid) in calls],
    )


def _nodes(batch, label):
    return [p for lbl, p in batch.nodes if lbl == label]


def _edges(batch, rtype):
    return [e for e in batch.edges if e[0] == rtype]


def test_unmatched_method_becomes_observed_method():
    idx = _idx(methods=[("app/known.py", 5, "app.known.f")])
    trace = ObservedTrace(methods=(
        ObservedMethod("f", "/repo/app/known.py", 5),        # exists statically -> NOT new
        ObservedMethod("reflected", "/repo/app/dyn.py", 9),   # no static match -> new
    ))
    batch, stats = build_batch(trace, SID, idx, ROOT)
    obs = _nodes(batch, "ObservedMethod")
    assert stats.new_methods == 1
    assert len(obs) == 1
    assert obs[0]["name"] == "reflected"
    assert obs[0]["file_path"] == "app/dyn.py"     # relativized + forward-slashed
    assert obs[0]["origin"] == "dynamic"


def test_observed_call_between_two_static_methods():
    idx = _idx(methods=[("app/a.py", 1, "app.a.caller"), ("app/b.py", 2, "app.b.callee")])
    trace = ObservedTrace(calls=(
        ObservedCall("/repo/app/a.py", 1, "caller", "/repo/app/b.py", 2, "callee"),))
    batch, stats = build_batch(trace, SID, idx, ROOT)
    calls = _edges(batch, "OBSERVED_CALL")
    assert stats.observed_calls == 1 and len(calls) == 1
    rtype, fl, fk, tl, tk, props = calls[0]
    assert (fl, tl) == ("CpgMethod", "CpgMethod")
    assert fk["full_name"] == "app.a.caller" and tk["full_name"] == "app.b.callee"
    assert props["origin"] == "dynamic"


def test_call_to_runtime_only_method_targets_observed_node():
    idx = _idx(methods=[("app/a.py", 1, "app.a.caller")])   # callee has NO static method
    trace = ObservedTrace(
        methods=(ObservedMethod("dynh", "/repo/app/dyn.py", 3),),
        calls=(ObservedCall("/repo/app/a.py", 1, "caller", "/repo/app/dyn.py", 3, "dynh"),),
    )
    batch, stats = build_batch(trace, SID, idx, ROOT)
    (_, fl, fk, tl, tk, _), = _edges(batch, "OBSERVED_CALL")
    assert fl == "CpgMethod" and tl == "ObservedMethod"        # edge lands on the new node
    assert tk["uid"] == _nodes(batch, "ObservedMethod")[0]["uid"]


def test_call_with_unresolved_endpoint_is_dropped_not_fabricated():
    idx = _idx(methods=[("app/a.py", 1, "app.a.caller")])
    # callee is a plumbing frame with no static node and no ObservedMethod (never in trace.methods)
    trace = ObservedTrace(calls=(
        ObservedCall("/repo/app/a.py", 1, "caller", "/usr/lib/runpy.py", 88, "_run_code"),))
    batch, stats = build_batch(trace, SID, idx, ROOT)
    assert _edges(batch, "OBSERVED_CALL") == []
    assert stats.dropped_calls == 1


def test_dispatch_anchors_on_static_call_and_targets_resolved_method():
    idx = _idx(
        methods=[("app/impl.py", 4, "app.impl.Impl.handle")],
        calls=[("app/a.py", 6, "call-uid-xyz")],
    )
    trace = ObservedTrace(dispatches=(
        ObservedDispatch("/repo/app/a.py", 6, "Impl.handle", "/repo/app/impl.py", 4),))
    batch, stats = build_batch(trace, SID, idx, ROOT)
    (_, fl, fk, tl, tk, props), = _edges(batch, "OBSERVED_DISPATCH")
    assert stats.observed_dispatches == 1
    assert fl == "CpgCall" and fk["uid"] == "call-uid-xyz"
    assert tl == "CpgMethod" and tk["full_name"] == "app.impl.Impl.handle"
    assert props["origin"] == "dynamic"


def test_dispatch_without_static_call_is_dropped():
    idx = _idx(methods=[("app/impl.py", 4, "app.impl.Impl.handle")])   # no CpgCall at the site
    trace = ObservedTrace(dispatches=(
        ObservedDispatch("/repo/app/a.py", 6, "Impl.handle", "/repo/app/impl.py", 4),))
    batch, stats = build_batch(trace, SID, idx, ROOT)
    assert _edges(batch, "OBSERVED_DISPATCH") == []
    assert stats.dropped_dispatches == 1


def test_synthetic_twin_loses_to_real_method_on_line_collision():
    """Joern emits `Foo.handle` AND `Foo.handle<metaClassAdapter>` at the same (file,line). A dispatch
    must resolve to the REAL method regardless of row order."""
    for rows in (
        [{"f": "app/h.py", "l": 6, "fn": "app.Foo.handle"},
         {"f": "app/h.py", "l": 6, "fn": "app.Foo.handle<metaClassAdapter>"}],
        [{"f": "app/h.py", "l": 6, "fn": "app.Foo.handle<metaClassAdapter>"},   # reversed order
         {"f": "app/h.py", "l": 6, "fn": "app.Foo.handle"}],
    ):
        idx = index_from_rows(rows, [])
        assert idx.method_at("app/h.py", 6) == "app.Foo.handle"


def test_operator_call_loses_to_real_call_on_line_collision():
    """A call site has the real call and a Joern `<operator>.fieldAccess` at the same line; anchor on
    the real call."""
    idx = index_from_rows([], [
        {"f": "app/h.py", "l": 9, "uid": "op", "name": "<operator>.fieldAccess"},
        {"f": "app/h.py", "l": 9, "uid": "real", "name": "handle"}])
    assert idx.call_at("app/h.py", 9) == "real"


def test_basename_fallback_when_relpath_layout_differs():
    """If Joern stored an absolute-ish path but the tracer's relpath differs, basename+line matches."""
    idx = index_from_rows([{"f": "/abs/build/app/x.py", "l": 2, "fn": "app.x.f"}], [])
    trace = ObservedTrace(methods=(ObservedMethod("f", "/repo/app/x.py", 2),))
    batch, stats = build_batch(trace, SID, idx, ROOT)
    assert stats.new_methods == 0     # matched via basename x.py + line 2, so NOT counted as new
