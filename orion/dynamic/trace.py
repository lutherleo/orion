"""The language-neutral runtime-trace contract — the seam between a language tracer and the graph.

A tracer (Python's ``tracer_py``, JS's ``tracer_js``) observes a run and produces exactly one
``ObservedTrace``. The graph merge (``merge.py``) consumes exactly that shape. Keeping the contract
here — frozen, with no interpreter or DB imports — lets the two ends agree without seeing each other,
the same discipline ``contracts.py`` enforces for the static pipeline.

Three observations, mapped 1:1 to the three static blind spots (design §1):

- ``ObservedCall``     — a caller/callee pair that actually executed (the "graph lies by omission"
  arrow-function calls Joern never linked).
- ``ObservedDispatch`` — the CONCRETE target a dynamic/virtual call site reached ("pointers switch").
- ``ObservedMethod``   — a function that executed but has no static ``CpgMethod`` (reflection/eval/
  monkey-patch — the "newer nodes than before").

Line numbers are 1-based to match Joern/``CpgMethod.line``. A field is Optional only where a real
tracer legitimately cannot know it (e.g. a JS require-hook that has a target name but not its line).
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ObservedCall:
    """A caller→callee edge seen at runtime. Maps to an OBSERVED_CALL relationship in the graph.

    Both endpoints carry their method DEFINITION line (``co_firstlineno``), not the call-site line:
    (file, def-line) is the one identity key Joern's ``CpgMethod`` and the tracer agree on — names and
    qualnames diverge across the two systems, but a def's source line does not. The graph merge matches
    on exactly this pair.
    """
    caller_file: str
    caller_line: int          # caller method's DEFINITION line
    caller_name: str
    callee_file: str
    callee_line: int          # callee method's DEFINITION line
    callee_name: str


@dataclass(frozen=True)
class ObservedDispatch:
    """The concrete target a dynamic call site resolved to at runtime. Maps to OBSERVED_DISPATCH.

    ``call_site_*`` locate the call in the caller's source (used to match the static ``CpgCall``);
    ``resolved_callee_*`` name the method actually reached. ``resolved_callee_line`` is Optional: a
    tracer may know the target's name and file but not its defining line.
    """
    call_site_file: str
    call_site_line: int
    resolved_callee_name: str
    resolved_callee_file: str
    resolved_callee_line: int | None = None


@dataclass(frozen=True)
class ObservedMethod:
    """A function that executed but has no static counterpart. Maps to an :ObservedMethod node."""
    name: str
    file: str
    line: int


@dataclass(frozen=True)
class ObservedTrace:
    """The whole result of one traced run. Empty is valid — it means the harness exercised nothing
    new, an honest outcome, never an error."""
    calls: tuple[ObservedCall, ...] = field(default_factory=tuple)
    dispatches: tuple[ObservedDispatch, ...] = field(default_factory=tuple)
    methods: tuple[ObservedMethod, ...] = field(default_factory=tuple)

    def is_empty(self) -> bool:
        return not (self.calls or self.dispatches or self.methods)


# --------------------------------------------------------------------------------------------------
# Wire format. The Python tracer runs the target in a SUBPROCESS (real timeout + isolation, and Orion
# never executes target code in its own process), so the trace must cross a process boundary as JSON.
# The format lives with the contract so both ends agree on it. Tuples-of-fields keep it compact.
# --------------------------------------------------------------------------------------------------
def to_wire(trace: ObservedTrace) -> dict:
    """Serialize an ObservedTrace to a JSON-safe dict (the subprocess bootstrap writes this)."""
    return {
        "calls": [[c.caller_file, c.caller_line, c.caller_name,
                   c.callee_file, c.callee_line, c.callee_name] for c in trace.calls],
        "dispatches": [[d.call_site_file, d.call_site_line, d.resolved_callee_name,
                        d.resolved_callee_file, d.resolved_callee_line] for d in trace.dispatches],
        "methods": [[m.name, m.file, m.line] for m in trace.methods],
    }


def from_wire(obj: dict) -> ObservedTrace:
    """Rebuild an ObservedTrace from `to_wire`'s dict (the host reads this back). Tolerant of a
    missing/None section so a truncated or empty sidecar yields an empty-but-valid trace."""
    obj = obj or {}
    return ObservedTrace(
        calls=tuple(ObservedCall(*row) for row in (obj.get("calls") or [])),
        dispatches=tuple(ObservedDispatch(*row) for row in (obj.get("dispatches") or [])),
        methods=tuple(ObservedMethod(*row) for row in (obj.get("methods") or [])),
    )
