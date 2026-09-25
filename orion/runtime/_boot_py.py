"""In-subprocess bootstrap: run a harness driver under a tracer, dump the trace wire JSON.

Runs INSIDE the sandboxed child (never in Orion's own process) as
``python -m orion.runtime._boot_py <driver> <root> <out_json>``. It executes the driver as
``__main__`` and -- in a ``finally``, so a raising driver never loses the partial trace -- writes
``trace.to_wire``-shaped JSON with REPO-RELATIVE paths.

Recorded, for code under ``root`` only (stdlib/site-packages never enter the trace):
  methods     -- (name, file, def-line) -> invocation count
  calls       -- caller def-line -> callee def-line, with a count
  dispatches  -- a bound-method call: the call SITE line -> the concrete method reached
  coverage    -- executed lines (marked once: hit_count 1; method counts carry the frequency)

Engine: ``sys.monitoring`` (3.12+) when available. PY_START returns DISABLE for any code object
outside ``root``, so library code costs one callback per function EVER, not per call; LINE events
are switched on per target code object only and DISABLEd after a line's first hit. Older
interpreters fall back to ``sys.settrace`` with a local line tracer confined to target frames.
"""
from __future__ import annotations

import json
import os
import runpy
import sys
import threading

_MAX_RECORDS = 200_000   # per-table cap on DISTINCT records: bounds memory on a pathological run


class _Recorder:
    def __init__(self, root: str) -> None:
        self.root = os.path.realpath(root)
        self._rel: dict[str, str | None] = {}
        # Keyed WITH the name: a `def f():` on line 1 shares (file, line) with the file's <module>.
        self.methods: dict[tuple[str, int, str], int] = {}      # (file, def, name) -> hits
        self.calls: dict[tuple, int] = {}                       # (cfile, cdef, cname, file, def, name) -> hits
        self.dispatches: set[tuple] = set()
        self.lines: set[tuple[str, int]] = set()

    def rel(self, filename: str) -> str | None:
        """Repo-relative forward-slash path for a code object's file, or None if outside root.
        Pseudo-files ("<frozen runpy>", "<string>") are never target code. Cached per filename."""
        try:
            return self._rel[filename]
        except KeyError:
            pass
        out = None
        if filename and not filename.startswith("<"):
            try:
                p = os.path.realpath(filename)
                if p.startswith(self.root + os.sep):
                    out = os.path.relpath(p, self.root).replace(os.sep, "/")
            except (ValueError, OSError):
                out = None
        self._rel[filename] = out
        return out

    def on_start(self, code, frame) -> None:
        """A target function started executing in `frame`."""
        rel = self._rel[code.co_filename]
        name = getattr(code, "co_qualname", None) or code.co_name
        key = (rel, code.co_firstlineno, name)
        if key in self.methods:
            self.methods[key] += 1
        elif len(self.methods) < _MAX_RECORDS:
            self.methods[key] = 1

        caller = frame.f_back
        if caller is None:
            return
        ccode = caller.f_code
        crel = self.rel(ccode.co_filename)
        if crel is None:
            return
        ckey = (crel, ccode.co_firstlineno, getattr(ccode, "co_qualname", None) or ccode.co_name,
                rel, code.co_firstlineno, name)
        if ckey in self.calls:
            self.calls[ckey] += 1
        elif len(self.calls) < _MAX_RECORDS:
            self.calls[ckey] = 1
        # A bound-method call is where polymorphism lives: record the concrete method this exact
        # call SITE reached (the site line anchors a static CpgCall).
        if (code.co_argcount and code.co_varnames[0] == "self"
                and len(self.dispatches) < _MAX_RECORDS):
            self.dispatches.add((crel, caller.f_lineno, name, rel, code.co_firstlineno))

    def to_wire(self) -> dict:
        return {
            "coverage": [[f, ln, 1] for f, ln in self.lines],
            "methods": [[name, f, ln, n] for (f, ln, name), n in self.methods.items()],
            "calls": [[cf, cl, f, ln, n, cn, name]
                      for (cf, cl, cn, f, ln, name), n in self.calls.items()],
            "dispatches": [list(d) for d in self.dispatches],
        }


def _install_monitoring(rec: _Recorder):
    """sys.monitoring engine. Returns an uninstall callable, or None if unavailable/busy."""
    mon = getattr(sys, "monitoring", None)
    if mon is None:
        return None
    tool = next((t for t in (mon.PROFILER_ID, mon.OPTIMIZER_ID, 3, 4)
                 if mon.get_tool(t) is None), None)
    if tool is None:
        return None
    mon.use_tool_id(tool, "orion-runtime")
    E, DISABLE = mon.events, mon.DISABLE
    lines_on: set = set()

    def py_start(code, _offset):
        if rec.rel(code.co_filename) is None:
            return DISABLE                        # never called again for this code object
        if code not in lines_on:
            lines_on.add(code)
            mon.set_local_events(tool, code, E.LINE)
        try:
            rec.on_start(code, sys._getframe(1))  # frame 1 = the function that just started
        except Exception:  # noqa: BLE001 -- a tracer bug must never break the traced run
            pass
        return None

    def line(code, lineno):
        rel = rec.rel(code.co_filename)
        if rel is not None and len(rec.lines) < _MAX_RECORDS:
            rec.lines.add((rel, lineno))
        return DISABLE                            # executed is a boolean per line: once is enough

    mon.register_callback(tool, E.PY_START, py_start)
    mon.register_callback(tool, E.LINE, line)
    mon.set_events(tool, E.PY_START)

    def uninstall():
        mon.set_events(tool, 0)
        for code in lines_on:
            mon.set_local_events(tool, code, 0)
        mon.register_callback(tool, E.PY_START, None)
        mon.register_callback(tool, E.LINE, None)
        mon.free_tool_id(tool)
    return uninstall


def _install_settrace(rec: _Recorder):
    """Fallback engine for <3.12: a global call hook plus a line tracer on target frames only."""
    def local(frame, event, _arg):
        if event == "line" and len(rec.lines) < _MAX_RECORDS:
            rec.lines.add((rec._rel[frame.f_code.co_filename], frame.f_lineno))
        return local

    def hook(frame, event, _arg):
        if event != "call" or rec.rel(frame.f_code.co_filename) is None:
            return None
        try:
            rec.on_start(frame.f_code, frame)
        except Exception:  # noqa: BLE001
            pass
        return local

    threading.settrace(hook)
    sys.settrace(hook)

    def uninstall():
        sys.settrace(None)
        threading.settrace(None)  # type: ignore[arg-type]
    return uninstall


def main(argv: list[str]) -> int:
    driver, root, out_path = argv[1], argv[2], argv[3]
    rec = _Recorder(root)
    sys.path.insert(0, os.path.abspath(root))
    # ORION_PY_TRACER=settrace forces the fallback engine (tests pin both engines agree).
    forced = os.environ.get("ORION_PY_TRACER") == "settrace"
    uninstall = (None if forced else _install_monitoring(rec)) or _install_settrace(rec)
    try:
        runpy.run_path(driver, run_name="__main__")
    except SystemExit:
        pass    # a driver calling sys.exit() is fine; keep the trace
    except Exception:  # noqa: BLE001 -- driver failures are expected; the partial trace still counts
        pass
    finally:
        uninstall()
        try:
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(rec.to_wire(), f)
        except OSError:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
