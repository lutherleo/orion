"""Orchestrate one `orion trace` phase: harness -> tracer -> merge -> persist_dynamic -> delta.

This is the dynamic analogue of cli._pipeline for the static scan: it owns the wiring and the
progress events, nothing more. Every step is best-effort per the layer's contract — a failed harness
or an empty trace yields an honest empty delta, never a crash. Requires that `orion scan` already
built the static graph for `scan_id` (OBSERVED_* edges MATCH static endpoints).
"""
from __future__ import annotations

from datetime import datetime, timezone

from ..contracts import OnEvent, ProgressEvent
from ..graph import persist
from ..graphdb import GraphDB
from . import delta, harness, merge


def _event(event: str, detail: str = "") -> ProgressEvent:
    return {"ts": datetime.now(timezone.utc).isoformat(), "phase": "dynamic", "shape": None,
            "lead": None, "turn": None, "event": event, "detail": detail}


def _has_static_graph(scan_id: str) -> bool:
    """True iff a static build exists for this scan (at least one CpgMethod)."""
    db = GraphDB()
    try:
        res = db.run_cypher(scan_id, "MATCH (m:CpgMethod {scan_id:$scan_id}) RETURN count(m) AS c", limit=1)
        rows = res.get("rows") if isinstance(res, dict) else None
        return bool(rows) and int(rows[0].get("c", 0)) > 0
    finally:
        db.close()


def _run_tracer(language: str, driver_path: str, repo: str, timeout: float):
    """Dispatch to the language tracer. Returns (ObservedTrace, RunResult). JS lands in Layer 5."""
    if language == "py":
        from .tracer_py import trace
        return trace(driver_path, repo, timeout=timeout)
    if language == "js":
        from .tracer_js import trace          # built in Layer 5
        return trace(driver_path, repo, timeout=timeout)
    raise ValueError(f"no dynamic tracer for language {language!r}")


def trace_repo(scan_id: str, repo: str, language: str, *,
               harness_file: str | None = None, timeout: float = 120.0,
               on_event: OnEvent | None = None) -> dict:
    """Run the whole dynamic phase and return the delta summary (delta.compute's dict).

    On any skip (no static graph, no harness, empty trace) the returned summary is a zeroed delta with
    a `note`; the phase never raises for an expected miss. `harness_file` pins a driver and skips the
    agent (and its tokens)."""
    def emit(ev: ProgressEvent) -> None:
        if on_event is not None:
            on_event(ev)

    if not _has_static_graph(scan_id):
        emit(_event("warn", f"no static graph for scan_id {scan_id}; run `orion scan` first — skipping"))
        return _empty("no static graph for scan_id")

    # 1) driver: pinned file or agent-generated
    if harness_file:
        driver_path, notes = harness_file, "(pinned --harness-file)"
        emit(_event("start", f"using pinned harness {harness_file}"))
    else:
        hr = harness.generate(scan_id, repo, language, on_event, timeout=int(timeout) + 180)
        if hr.path is None:
            return _empty(f"harness generation skipped: {hr.error}")
        driver_path, notes = hr.path, hr.notes

    # 2) run the driver under the tracer
    emit(_event("start", f"tracing (lang={language}, timeout={timeout}s)"))
    observed, run_result = _run_tracer(language, driver_path, repo, timeout)
    emit(_event("done" if not run_result.timed_out else "warn",
                f"trace: {len(observed.methods)} methods, {len(observed.calls)} calls, "
                f"{len(observed.dispatches)} dispatches"
                + (" (TIMED OUT — partial)" if run_result.timed_out else "")))
    if observed.is_empty():
        emit(_event("done", "empty trace — harness exercised nothing observable"))
        return _empty("empty trace")

    # 3) map onto the graph and persist the dynamic facts alongside the static graph
    index = merge.load_static_index(scan_id)
    batch, stats = merge.build_batch(observed, scan_id, index, repo)
    emit(_event("done", f"merged: {stats.new_methods} new methods, {stats.observed_calls} calls, "
                        f"{stats.observed_dispatches} dispatches "
                        f"(dropped {stats.dropped_calls} calls / {stats.dropped_dispatches} dispatches)"))
    persist.persist_dynamic(batch)

    # 4) the delta — the headline
    summary = delta.compute(scan_id)
    delta.emit(summary, on_event)
    summary["notes"] = notes
    return summary


def _empty(note: str) -> dict:
    return {"new_methods": 0, "observed_calls": 0, "observed_dispatches": 0,
            "calls_to_new_methods": 0, "method_samples": [], "dispatch_samples": [], "note": note}
