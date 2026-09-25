"""Python tracer (host side): build the `_boot_py` command for a harness script and read its dumps.

The tracing itself happens in `_boot_py` inside the sandboxed child. Each run writes one sidecar
under <work>/py-trace/; `collect` folds every sidecar present, `reset` removes them, so consecutive
runs are disjoint. A missing/garbled sidecar (timeout, crash) is an empty trace, never an error.
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

from .trace import RuntimeTrace, TraceAccumulator, from_wire

TRACE_SUBDIR = "py-trace"


def _orion_parent() -> str:
    """The directory containing the `orion` package, so `-m orion.runtime._boot_py` resolves in a
    child whose cwd is the target (an editable install's finder may not expose it otherwise)."""
    import orion
    return os.path.dirname(os.path.dirname(os.path.abspath(orion.__file__)))


class PyTracer:
    """Tracer for Python harness scripts."""

    def __init__(self, python_exe: str | None = None) -> None:
        self._python = python_exe or sys.executable

    def build_flags(self) -> list[str]:
        return []

    def launch_env(self, work: Path) -> dict[str, str]:
        return {}

    def script_command(self, script: str, repo: str, work: Path) -> tuple[list[str], dict[str, str]]:
        out_dir = Path(work) / TRACE_SUBDIR
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"{uuid.uuid4().hex}.json"
        pythonpath = os.pathsep.join(filter(None, [_orion_parent(), os.environ.get("PYTHONPATH")]))
        return ([self._python, "-m", "orion.runtime._boot_py",
                 os.path.abspath(script), os.path.abspath(repo), str(out)],
                {"PYTHONPATH": pythonpath})

    def reset(self, work: Path) -> None:
        d = Path(work) / TRACE_SUBDIR
        if d.is_dir():
            for f in d.glob("*.json"):
                f.unlink(missing_ok=True)

    def collect(self, work: Path, repo: str) -> RuntimeTrace:
        d = Path(work) / TRACE_SUBDIR
        acc = TraceAccumulator()
        for f in sorted(d.glob("*.json")) if d.is_dir() else []:
            try:
                acc.add(from_wire(json.loads(f.read_text(encoding="utf-8"))))
            except (OSError, ValueError, TypeError):
                continue
        return acc.freeze()   # _boot_py already wrote repo-relative, forward-slash paths
