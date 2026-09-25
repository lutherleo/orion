"""Go tracer: `go build -cover` + `go tool covdata textfmt`.

Coverage only (line-based, no call tree), so Go enrichment is `executed`/`hit_count` props without
OBSERVED_CALL edges. Each process run writes into GOCOVERDIR; `reset` empties it, so `collect` after
one input sees exactly that input's coverage -- real per-input feedback for the engine, and summed
per-input traces never double count.

`parse_covdata_textfmt` is PURE over the tool's text output; only `collect` runs the tool.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from .trace import Hit, RuntimeTrace

COVERDATA_SUBDIR = "covdata"
_MODULE_RE = re.compile(r"^\s*module\s+(\S+)", re.M)


def module_path(repo: str) -> str:
    """The `module` path declared in <repo>/go.mod ('' if absent). Coverage reports files as
    <module>/<relpath>; stripping it yields the repo-relative path the graph uses."""
    try:
        m = _MODULE_RE.search((Path(repo) / "go.mod").read_text(encoding="utf-8"))
    except OSError:
        return ""
    return m.group(1) if m else ""


def parse_covdata_textfmt(text: str, module_prefix: str = "") -> RuntimeTrace:
    """`file:startLine.startCol,endLine.endCol numStmts count` lines -> line hits. A count > 0
    marks every line of the block executed; the module prefix is stripped. Malformed lines skip."""
    per_line: dict[tuple[str, int], int] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("mode:"):
            continue
        try:
            location, _numstmts, count_s = line.rsplit(" ", 2)
            count = int(count_s)
            file_part, span = location.rsplit(":", 1)
            start_span, end_span = span.split(",", 1)
            start_line = int(start_span.split(".", 1)[0])
            end_line = int(end_span.split(".", 1)[0])
        except (ValueError, IndexError):
            continue
        if count <= 0:
            continue
        rel = file_part
        if module_prefix and rel.startswith(module_prefix):
            rel = rel[len(module_prefix):].lstrip("/")
        for ln in range(start_line, end_line + 1):
            per_line[(rel, ln)] = per_line.get((rel, ln), 0) + count
    return RuntimeTrace(coverage=tuple(Hit(f, l, c) for (f, l), c in per_line.items()))


class GoCoverTracer:
    """Tracer for Go executables built from the scanned source."""

    def __init__(self, module_prefix: str | None = None) -> None:
        self._module_prefix = module_prefix     # None = read it from go.mod at collect time

    def build_flags(self) -> list[str]:
        return ["-cover"]

    def launch_env(self, work: Path) -> dict[str, str]:
        d = Path(work) / COVERDATA_SUBDIR
        d.mkdir(parents=True, exist_ok=True)
        return {"GOCOVERDIR": str(d)}

    def reset(self, work: Path) -> None:
        d = Path(work) / COVERDATA_SUBDIR
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True, exist_ok=True)

    def collect(self, work: Path, repo: str) -> RuntimeTrace:
        d = Path(work) / COVERDATA_SUBDIR
        if not d.is_dir() or not any(d.iterdir()):
            return RuntimeTrace()
        out = Path(work) / "covdata.txt"
        try:
            r = subprocess.run(["go", "tool", "covdata", "textfmt", "-i", str(d), "-o", str(out)],
                               capture_output=True, text=True, timeout=120, check=False)
            if r.returncode != 0:
                return RuntimeTrace()
            text = out.read_text(encoding="utf-8")
        except (OSError, subprocess.SubprocessError):
            return RuntimeTrace()
        prefix = self._module_prefix if self._module_prefix is not None else module_path(repo)
        return parse_covdata_textfmt(text, prefix)
