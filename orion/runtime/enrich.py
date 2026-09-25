"""The runtime stage pipeline: select -> start -> drive -> collect -> correlate -> write -> report.

One entry point for both surfaces: `orion scan --runtime` calls it right after the static build, and
`orion trace` calls it against a graph an earlier scan built. Best-effort by contract: an unsupported
target, an unbootable app, a missing toolchain or a driver error is a `runtime` warn/error event and a
None return -- the scan continues and the graph is left exactly as it was.
"""
from __future__ import annotations

import datetime as _dt
import os
import shutil
import tempfile
from pathlib import Path

from ..graphdb import GraphDB
from . import correlate, engine, report, targets, writeback

_PROGRESS_EVERY = 25   # inputs between drive-progress events


def _event(event: str, detail: str = "") -> dict:
    return {"ts": _dt.datetime.now(_dt.timezone.utc).isoformat(), "phase": "runtime",
            "shape": None, "lead": None, "turn": None, "event": event, "detail": detail}


def _has_static_graph(db: GraphDB, scan_id: str) -> bool:
    res = db.run_cypher(scan_id, "MATCH (m:CpgMethod {scan_id:$scan_id}) RETURN count(m) AS c", limit=1)
    rows = res.get("rows") if isinstance(res, dict) else None
    return bool(rows) and int(rows[0].get("c") or 0) > 0


def enrich(scan_id: str, repo: str, on_event=None, *, budget: int = 200, driver: str = "auto",
           language: str | None = None, harness_file: str | None = None,
           timeout: float = 120.0, sandbox: str = "auto") -> dict | None:
    """Run the runtime stage for `scan_id`. Returns the metric dict, or None when skipped/failed.
    Never raises."""
    def emit(ev: str, detail: str = "") -> None:
        if on_event is not None:
            on_event(_event(ev, detail))

    try:
        sel = targets.select(repo, driver=driver, language=language, harness_file=harness_file,
                             timeout=timeout, on_event=on_event, sandbox=sandbox)
        if sel is None:
            emit("warn", "no runtime driver fits this repo (add .orion/runtime.json or pass "
                         "--driver/--harness-file); skipping, graph unchanged")
            return None
        drv, tracer = sel
        work = Path(tempfile.mkdtemp(prefix="orion_runtime_"))
        db = GraphDB()
        try:
            if not _has_static_graph(db, scan_id):
                emit("warn", f"no static graph for scan_id {scan_id}; run `orion scan` first")
                return None
            seeds = drv.seeds(db, scan_id)
            if not seeds:
                emit("warn", f"{type(drv).__name__} produced no inputs; skipping, graph unchanged")
                return None
            emit("start", f"{type(drv).__name__} + {type(tracer).__name__}: {len(seeds)} seeds, "
                          f"budget {budget} inputs")

            def on_step(i, inp, new):
                if i % _PROGRESS_EVERY == 0 or new:
                    emit("tool", f"input {i}: {inp.label}" + (f" (+{new} new lines)" if new else ""))

            target = drv.start(repo, work, tracer.build_flags())
            try:
                trace = engine.run(drv, tracer, target, seeds, budget=budget, on_step=on_step)
            finally:
                drv.stop(target)
            # What the loop did not collect: everything, for a server that flushes on exit.
            trace = trace.merge(tracer.collect(work, repo))
            emit("tool", f"observed {len(trace.coverage)} lines, {len(trace.methods)} methods, "
                         f"{len(trace.calls)} calls, {len(trace.dispatches)} dispatches")
            if trace.is_empty():
                # Keep any previous enrichment: an empty run is no evidence the old one is stale.
                emit("warn", "empty trace -- the drive exercised nothing observable; graph unchanged")
                return None
            plan = correlate.correlate(trace, correlate.load_static_index(scan_id), scan_id)
        finally:
            db.close()
            if not os.environ.get("ORION_KEEP_RUNTIME_WORK"):
                shutil.rmtree(work, ignore_errors=True)

        metric = writeback.apply_plan(scan_id, plan)
        metric["method_samples"] = report.samples(plan)
        report.emit(metric, on_event)
        return metric
    except Exception as exc:  # noqa: BLE001 -- best-effort stage; never abort the scan
        emit("error", f"runtime stage failed, continuing without it: {type(exc).__name__}: {exc}")
        return None
