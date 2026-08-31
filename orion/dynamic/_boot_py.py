"""In-subprocess bootstrap: run a driver under `sys.settrace`, record the ObservedTrace, dump it.

This runs INSIDE the sandboxed child process (never in Orion's own process), invoked as
``python -m orion.dynamic._boot_py <driver> <root> <out_json>``. It installs a call-event tracer,
executes the driver as ``__main__``, and — in a ``finally``, so a driver exception never loses the
partial trace — writes ``trace.to_wire`` JSON to ``out_json`` for the host (`tracer_py`) to read back.

Why ``settrace`` and not ``sys.monitoring``: settrace is one code path that works on 3.10–3.12+
identically. On a new interpreter ``sys.monitoring`` is faster and is the documented upgrade; the
observation semantics here (one record per caller→callee frame transition) are the same either way.

Scope: only frames whose CALLEE file lives under ``root`` are recorded, so the trace is bounded to
the target repo exactly as the static graph is — stdlib/site-packages noise is dropped.
"""
from __future__ import annotations

import json
import os
import runpy
import sys
import threading

# Distinct-record caps: bound memory on a hot loop. Observations past the cap are dropped (the trace
# is honestly a lower bound anyway, design §10). Generous enough for real handlers.
_MAX_RECORDS = 100_000


class _Recorder:
    """Accumulates distinct calls / dispatches / executed methods, filtered to `root`."""

    def __init__(self, root: str) -> None:
        self.root = os.path.abspath(root)
        self._calls: dict[tuple, int] = {}       # (caller_file,line,name,callee_file,line,name) -> hits
        self._dispatches: set[tuple] = set()      # (site_file,site_line,resolved_name,res_file,res_line)
        self._methods: dict[tuple, tuple] = {}    # (file,firstline,name) -> (name,file,firstline)

    def _under_root(self, filename: str | None) -> bool:
        # Reject pseudo-filenames first: frozen/builtin/exec code reports co_filename like
        # "<frozen runpy>" or "<string>". os.path.abspath would join those onto cwd (which IS root
        # during a run), falsely matching — so a "<..." name is never under root. Real target source
        # is imported with an absolute path, so a direct normpath comparison suffices.
        if not filename or filename.startswith("<"):
            return False
        try:
            return os.path.normpath(filename).startswith(self.root + os.sep)
        except (ValueError, OSError):
            return False

    def on_call(self, frame) -> None:
        code = frame.f_code
        callee_file = code.co_filename
        if not self._under_root(callee_file):
            return
        callee_line = code.co_firstlineno
        callee_name = _qualname(code)

        # Every executed target method is a candidate :ObservedMethod; merge decides which lack a
        # static CpgMethod. Cheap dedup by (file, firstline, name).
        mkey = (callee_file, callee_line, callee_name)
        if mkey not in self._methods and len(self._methods) < _MAX_RECORDS:
            self._methods[mkey] = (callee_name, callee_file, callee_line)

        caller = frame.f_back
        if caller is not None:
            caller_file = caller.f_code.co_filename
            caller_def_line = caller.f_code.co_firstlineno   # DEFINITION line — the merge identity key
            call_site_line = caller.f_lineno                 # the call SITE — anchors a CpgCall
            caller_name = _qualname(caller.f_code)
            ckey = (caller_file, caller_def_line, caller_name,
                    callee_file, callee_line, callee_name)
            if ckey in self._calls:
                self._calls[ckey] += 1
            elif len(self._calls) < _MAX_RECORDS:
                self._calls[ckey] = 1

            # Dispatch heuristic: a bound-method callee is where polymorphism / "pointers switch"
            # lives — the concrete method this exact call site reached. The call SITE line anchors it
            # to a static CpgCall; the callee's DEF line identifies the resolved method.
            if _is_bound_method(code, frame) and len(self._dispatches) < _MAX_RECORDS:
                self._dispatches.add(
                    (caller_file, call_site_line, callee_name, callee_file, callee_line))

    def to_wire(self) -> dict:
        return {
            "calls": [list(k) for k in self._calls],
            "dispatches": [list(d) for d in self._dispatches],
            "methods": [list(v) for v in self._methods.values()],
        }


def _qualname(code) -> str:
    """A stable method name: prefer co_qualname (3.11+, carries the class) over co_name."""
    return getattr(code, "co_qualname", None) or code.co_name


def _is_bound_method(code, frame) -> bool:
    """True when the frame looks like a method call (first local param is `self`). Cheap and
    interpreter-version agnostic; good enough to flag the polymorphic call sites."""
    return bool(code.co_varnames) and code.co_varnames[0] == "self" and "self" in frame.f_locals


def main(argv: list[str]) -> int:
    driver, root, out_path = argv[1], argv[2], argv[3]
    rec = _Recorder(root)

    def _trace(frame, event, arg):
        if event == "call":
            try:
                rec.on_call(frame)
            except Exception:  # noqa: BLE001 — a tracer bug must never break the traced run
                pass
        return None  # no per-line tracing; the global hook still fires on every new frame's 'call'

    threading.settrace(_trace)   # cover threads the driver spawns
    sys.settrace(_trace)
    try:
        runpy.run_path(driver, run_name="__main__")
    except SystemExit:
        pass  # a driver calling sys.exit() is fine; keep the trace
    except Exception:  # noqa: BLE001 — driver failures are expected; we still emit the partial trace
        pass
    finally:
        sys.settrace(None)
        threading.settrace(None)  # type: ignore[arg-type]
        try:
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(rec.to_wire(), f)
        except OSError:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
