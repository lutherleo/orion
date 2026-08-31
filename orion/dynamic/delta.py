"""The delta: what the runtime saw that the static graph did not. This is the headline output.

Reads the persisted partition (static + dynamic together, one scan_id) and reports the runtime-only
facts: new :ObservedMethod nodes, OBSERVED_CALL edges with no parallel static call path, and
OBSERVED_DISPATCH targets. ``compute`` is I/O (own driver); ``report_text`` and ``to_events`` are pure.

Honesty (design §10): the delta is a LOWER BOUND on runtime behavior — it names what the harness
exercised, never "all dynamic paths". The report says so; an empty delta means the harness hit
nothing new, not that the code has no dynamic behavior.
"""
from __future__ import annotations

from datetime import datetime, timezone

from neo4j import GraphDatabase

from .. import config
from ..contracts import OnEvent, ProgressEvent

_SAMPLE = 15   # cap per-category examples in the summary so a huge run stays legible


def compute(scan_id: str) -> dict:
    """Read the persisted graph and return the delta summary (counts + capped samples). I/O."""
    driver = GraphDatabase.driver(config.NEO4J_URI, auth=config.NEO4J_AUTH)
    try:
        with driver.session(database=config.NEO4J_DATABASE) as s:
            new_methods = int(s.run(
                "MATCH (m:ObservedMethod {scan_id:$sid}) RETURN count(m) AS c", sid=scan_id).single()["c"])
            n_calls = int(s.run(
                "MATCH ()-[r:OBSERVED_CALL {scan_id:$sid}]->() RETURN count(r) AS c",
                sid=scan_id).single()["c"])
            n_disp = int(s.run(
                "MATCH ()-[r:OBSERVED_DISPATCH {scan_id:$sid}]->() RETURN count(r) AS c",
                sid=scan_id).single()["c"])

            # OBSERVED_CALL edges reaching a runtime-only method — unambiguously new (no static node
            # for the callee at all). The clearest "static never had this" signal.
            calls_to_new = int(s.run(
                "MATCH (:CpgMethod {scan_id:$sid})-[r:OBSERVED_CALL {scan_id:$sid}]->"
                "(:ObservedMethod {scan_id:$sid}) RETURN count(r) AS c", sid=scan_id).single()["c"])

            method_samples = [dict(r) for r in s.run(
                "MATCH (m:ObservedMethod {scan_id:$sid}) "
                "RETURN m.name AS name, m.file_path AS file, m.line AS line "
                "ORDER BY file, line LIMIT $k", sid=scan_id, k=_SAMPLE)]
            disp_samples = [dict(r) for r in s.run(
                "MATCH (c:CpgCall {scan_id:$sid})-[:OBSERVED_DISPATCH {scan_id:$sid}]->(t) "
                "RETURN c.file_path AS site_file, c.line AS site_line, "
                "coalesce(t.full_name, t.name) AS target ORDER BY site_file, site_line LIMIT $k",
                sid=scan_id, k=_SAMPLE)]
    finally:
        driver.close()
    return {
        "scan_id": scan_id,
        "new_methods": new_methods,
        "observed_calls": n_calls,
        "observed_dispatches": n_disp,
        "calls_to_new_methods": calls_to_new,
        "method_samples": method_samples,
        "dispatch_samples": disp_samples,
    }


def report_text(summary: dict) -> str:
    """A human-readable delta report. Pure."""
    lines = [
        "Dynamic trace delta (runtime observed vs static graph) — a LOWER BOUND on what ran:",
        f"  {summary['new_methods']} runtime-only methods (:ObservedMethod, no static CpgMethod)",
        f"  {summary['observed_calls']} OBSERVED_CALL edges "
        f"({summary['calls_to_new_methods']} into a runtime-only method)",
        f"  {summary['observed_dispatches']} OBSERVED_DISPATCH edges "
        f"(concrete target a dynamic call site actually reached)",
    ]
    if summary["method_samples"]:
        lines.append("  runtime-only methods (sample):")
        for m in summary["method_samples"]:
            lines.append(f"    - {m['name']}  {m['file']}:{m['line']}")
    if summary["dispatch_samples"]:
        lines.append("  dispatch targets (sample):")
        for d in summary["dispatch_samples"]:
            lines.append(f"    - {d['site_file']}:{d['site_line']} -> {d['target']}")
    if summary["new_methods"] == 0 and summary["observed_calls"] == 0:
        lines.append("  (empty delta: the harness exercised nothing the static graph lacked)")
    return "\n".join(lines)


def to_events(summary: dict) -> list[ProgressEvent]:
    """One-per-headline `phase:'dynamic'` progress events so a run is watchable (working agreement)."""
    ts = datetime.now(timezone.utc).isoformat()

    def _ev(event: str, detail: str) -> ProgressEvent:
        return {"ts": ts, "phase": "dynamic", "shape": None, "lead": None, "turn": None,
                "event": event, "detail": detail}

    return [
        _ev("done", f"dynamic delta: {summary['new_methods']} runtime-only methods, "
                    f"{summary['observed_calls']} observed calls "
                    f"({summary['calls_to_new_methods']} into runtime-only methods), "
                    f"{summary['observed_dispatches']} observed dispatches"),
    ]


def emit(summary: dict, on_event: OnEvent | None) -> None:
    """Push the delta events to `on_event` if given (best-effort — a monitor hiccup never fatal)."""
    if on_event is None:
        return
    for ev in to_events(summary):
        on_event(ev)
