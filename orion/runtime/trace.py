"""The one runtime-trace contract: what every tracer produces and what correlation consumes.

Four observations, each answering a static blind spot:

- ``Hit``              -- a source LINE that executed, with a hit count (-> `executed`/`hit_count`).
- ``ObservedMethod``   -- a function that executed, keyed by its DEFINITION line. If the static graph
                          has no CpgMethod there (reflection/eval/monkey-patch) it becomes an
                          :ObservedMethod node -- "newer nodes than before".
- ``ObservedCall``     -- a caller->callee pair that executed (the arrow-function calls Joern never
                          linked). Endpoints are source locations; a DEFINITION line resolves exactly,
                          any other line resolves to its containing method.
- ``ObservedDispatch`` -- the CONCRETE target a dynamic/virtual call site reached.

Paths are repo-relative and forward-slashed (tracers relativize before handing a trace over), lines
are 1-based to match Joern. Pure data: no interpreter, subprocess or DB imports.

``TraceAccumulator`` is the mutable O(1)-per-item merge the engine folds step traces into; a frozen
``RuntimeTrace`` is only materialized once, at the end (no quadratic tuple rebuild per input).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Hit:
    """One executed source line. `file_path` is repo-relative (matches CpgCall.file_path)."""
    file_path: str
    line: int
    hit_count: int


@dataclass(frozen=True)
class ObservedMethod:
    """A function that executed. `line` is its DEFINITION line."""
    name: str
    file_path: str
    line: int
    hit_count: int = 1


@dataclass(frozen=True)
class ObservedCall:
    """A caller->callee pair seen at runtime. Names are informational; identity is (file, line)."""
    caller_file: str
    caller_line: int
    callee_file: str
    callee_line: int
    hits: int = 1
    caller_name: str = ""
    callee_name: str = ""


@dataclass(frozen=True)
class ObservedDispatch:
    """The concrete target a call site resolved to. `callee_line` is Optional: a tracer may know the
    target's name and file but not its definition line (such a dispatch cannot be anchored)."""
    site_file: str
    site_line: int
    callee_name: str
    callee_file: str
    callee_line: int | None = None


@dataclass(frozen=True)
class RuntimeTrace:
    """The whole result of a run. Empty is valid -- the drive exercised nothing observable."""
    coverage: tuple[Hit, ...] = ()
    methods: tuple[ObservedMethod, ...] = ()
    calls: tuple[ObservedCall, ...] = ()
    dispatches: tuple[ObservedDispatch, ...] = ()

    def is_empty(self) -> bool:
        return not (self.coverage or self.methods or self.calls or self.dispatches)

    def merge(self, other: "RuntimeTrace") -> "RuntimeTrace":
        """Union two traces, summing counts on identical keys. Pure; returns a new trace."""
        acc = TraceAccumulator()
        acc.add(self)
        acc.add(other)
        return acc.freeze()


class TraceAccumulator:
    """Mutable union of traces: hit counts summed per key, dispatches deduped. `add` is O(len(trace))
    -- the engine can fold one step trace per input without re-copying everything seen so far."""

    def __init__(self) -> None:
        self._lines: dict[tuple[str, int], int] = {}
        # Methods and calls are keyed WITH names: two functions can share a definition line (a
        # `def f():` on line 1 and the file's <module>), and must not be merged into one.
        self._methods: dict[tuple[str, int, str], int] = {}
        self._calls: dict[tuple[str, int, str, str, int, str], int] = {}
        self._dispatches: dict[ObservedDispatch, None] = {}

    def add(self, trace: RuntimeTrace) -> None:
        for h in trace.coverage:
            k = (h.file_path, h.line)
            self._lines[k] = self._lines.get(k, 0) + h.hit_count
        for m in trace.methods:
            k = (m.file_path, m.line, m.name)
            self._methods[k] = self._methods.get(k, 0) + m.hit_count
        for c in trace.calls:
            k = (c.caller_file, c.caller_line, c.caller_name, c.callee_file, c.callee_line, c.callee_name)
            self._calls[k] = self._calls.get(k, 0) + c.hits
        for d in trace.dispatches:
            self._dispatches.setdefault(d, None)

    def line_count(self) -> int:
        """Distinct executed lines so far -- O(1), the engine's new-coverage signal."""
        return len(self._lines)

    def freeze(self) -> RuntimeTrace:
        return RuntimeTrace(
            coverage=tuple(Hit(f, l, n) for (f, l), n in self._lines.items()),
            methods=tuple(ObservedMethod(name, f, l, n) for (f, l, name), n in self._methods.items()),
            calls=tuple(ObservedCall(cf, cl, ef, el, n, cn, en)
                        for (cf, cl, cn, ef, el, en), n in self._calls.items()),
            dispatches=tuple(self._dispatches),
        )


# ------------------------------------------------------------------------------------------------
# Wire format for the in-subprocess bootstraps (_boot_py): the target runs in a CHILD process (real
# timeout + isolation; Orion never executes target code in its own process), so the trace crosses a
# process boundary as JSON. Rows are positional lists to keep large traces compact.
# ------------------------------------------------------------------------------------------------
def to_wire(trace: RuntimeTrace) -> dict:
    return {
        "coverage": [[h.file_path, h.line, h.hit_count] for h in trace.coverage],
        "methods": [[m.name, m.file_path, m.line, m.hit_count] for m in trace.methods],
        "calls": [[c.caller_file, c.caller_line, c.callee_file, c.callee_line, c.hits,
                   c.caller_name, c.callee_name] for c in trace.calls],
        "dispatches": [[d.site_file, d.site_line, d.callee_name, d.callee_file, d.callee_line]
                       for d in trace.dispatches],
    }


def from_wire(obj: dict | None) -> RuntimeTrace:
    """Rebuild a trace from `to_wire`'s dict. A missing/None section (truncated sidecar) is empty."""
    obj = obj or {}
    return RuntimeTrace(
        coverage=tuple(Hit(*r) for r in (obj.get("coverage") or [])),
        methods=tuple(ObservedMethod(*r) for r in (obj.get("methods") or [])),
        calls=tuple(ObservedCall(*r) for r in (obj.get("calls") or [])),
        dispatches=tuple(ObservedDispatch(*r) for r in (obj.get("dispatches") or [])),
    )
