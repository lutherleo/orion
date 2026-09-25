"""The headline: what the runtime saw that the static graph did not. Pure.

Honesty: every number is a LOWER BOUND on runtime behavior -- it names what the drive exercised, never
"all dynamic paths". An empty delta means the drive reached nothing new, not that the code has no
dynamic behavior.
"""
from __future__ import annotations

from datetime import datetime, timezone

from ..contracts import OnEvent, ProgressEvent

SAMPLE = 15   # cap per-category examples so a huge run stays legible

EMPTY = {"calls_marked": 0, "methods_marked": 0, "new_methods": 0, "observed_calls": 0,
         "observed_dispatches": 0, "novel_edges": 0, "unreachable_executed": 0,
         "dropped": {}, "method_samples": []}


def samples(plan) -> list[dict]:
    """A capped, sorted sample of the runtime-only methods a plan creates."""
    rows = sorted(plan.new_methods, key=lambda m: (m["file_path"], m["line"]))[:SAMPLE]
    return [{"name": m["name"], "file": m["file_path"], "line": m["line"]} for m in rows]


def report_text(metric: dict) -> str:
    m = {**EMPTY, **metric}
    lines = [
        "Runtime delta (observed vs static graph) -- a LOWER BOUND on what ran:",
        f"  {m['calls_marked']} calls + {m['methods_marked']} methods marked executed "
        f"({m['unreachable_executed']} of them were statically marked unreachable)",
        f"  {m['new_methods']} runtime-only methods (:ObservedMethod, no static CpgMethod)",
        f"  {m['observed_calls']} OBSERVED_CALL edges ({m['novel_edges']} with no static call path)",
        f"  {m['observed_dispatches']} OBSERVED_DISPATCH edges (concrete target a call site reached)",
    ]
    if m["method_samples"]:
        lines.append("  runtime-only methods (sample):")
        lines += [f"    - {s['name']}  {s['file']}:{s['line']}" for s in m["method_samples"]]
    dropped = {k: v for k, v in (m["dropped"] or {}).items() if v}
    if dropped:
        lines.append("  uncorrelated (dropped, not fabricated): "
                     + ", ".join(f"{k}={v}" for k, v in sorted(dropped.items())))
    if not (m["calls_marked"] or m["methods_marked"] or m["observed_calls"] or m["new_methods"]):
        lines.append("  (empty delta: the drive exercised nothing the graph could correlate)")
    return "\n".join(lines)


def to_events(metric: dict) -> list[ProgressEvent]:
    m = {**EMPTY, **metric}
    return [{
        "ts": datetime.now(timezone.utc).isoformat(), "phase": "runtime", "shape": None,
        "lead": None, "turn": None, "event": "done",
        "detail": (f"{m['calls_marked']} calls + {m['methods_marked']} methods executed; "
                   f"{m['new_methods']} runtime-only methods; {m['observed_calls']} OBSERVED_CALL "
                   f"({m['novel_edges']} novel); {m['observed_dispatches']} OBSERVED_DISPATCH; "
                   f"{m['unreachable_executed']} statically-unreachable nodes proven executed"),
    }]


def emit(metric: dict, on_event: OnEvent | None) -> None:
    if on_event is not None:
        for ev in to_events(metric):
            on_event(ev)
