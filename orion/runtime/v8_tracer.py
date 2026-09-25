"""V8/Node tracer: PRECISE coverage (exact call counts) + a sampled CPU profile (call edges).

One tracer, two ways in, identical dumps:

- a long-running SERVER (HttpDriver): `NODE_V8_COVERAGE=<dir>` + `--cpu-prof` via NODE_OPTIONS on the
  target's own `npm start`, plus a preload that turns SIGTERM/SIGINT into exit(0) so both flush;
- a harness SCRIPT (HarnessDriver): `_boot_js.js` runs the script under the inspector, taking
  precise coverage and a CPU profile, and writes them into the SAME two dirs in the same formats.

So `collect` has one parse path. Coverage gives line hits AND per-function invocation counts (a
function's first range is the function itself -- exact, not sampled); the profile's parent->child
frames give caller->callee edges (sampled, so a lower bound). Parsers are PURE over the JSON.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from urllib.parse import unquote, urlparse

from .correlate import build_line_starts, offset_to_line, relativize
from .trace import Hit, ObservedCall, ObservedMethod, RuntimeTrace, TraceAccumulator

COVERAGE_SUBDIR = "v8-coverage"
CPUPROF_SUBDIR = "cpu-prof"
_BOOT_JS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_boot_js.js")


def _url_to_path(url: str) -> str | None:
    """A V8 script url (file: URL or plain path) as a filesystem path; None for node:/other schemes."""
    if not url or url.startswith("node:"):
        return None
    if url.startswith("file:"):
        path = unquote(urlparse(url).path)
        # file:///C:/x -> /C:/x on Windows; drop the leading slash before a drive letter.
        if len(path) > 2 and path[0] == "/" and path[2] == ":":
            path = path[1:]
        return path
    return None if "://" in url else url


def _script_relpath(url: str, repo: str) -> str | None:
    """Repo-relative path for a first-party script; None for internals, node_modules, or outside."""
    path = _url_to_path(url)
    if not path or "node_modules" in path:
        return None
    return relativize(path, repo)


def _line_counts(ranges: list[tuple[int, int, int]], starts: list[int]) -> dict[int, int]:
    """Per-line execution count from one script's ranges: a line takes the count of the INNERMOST
    range containing its first byte. V8 ranges nest lexically (a function inside the module, a
    block inside a function), and a count-0 inner block is how V8 marks code that did NOT run -- so
    crediting only a range's start line (the naive read) would miss every other executed line.
    One sweep: ranges sorted outer-first, a stack of the ranges open at the current line."""
    ranges.sort(key=lambda r: (r[0], -r[1]))
    out: dict[int, int] = {}
    stack: list[tuple[int, int, int]] = []
    ri = 0
    for lineno, s in enumerate(starts, start=1):
        while stack and stack[-1][1] <= s:
            stack.pop()
        while ri < len(ranges) and ranges[ri][0] <= s:
            r = ranges[ri]
            ri += 1
            while stack and stack[-1][1] <= r[0]:
                stack.pop()
            if r[1] > s:
                stack.append(r)
        if stack and stack[-1][2] > 0:
            out[lineno] = stack[-1][2]
    return out


def parse_v8_coverage(blobs: list[dict], repo: str, read_source) -> tuple[RuntimeTrace, int]:
    """V8 coverage blobs -> (trace of line hits + executed functions, malformed_count).

    Blob shape: `{result: [{url, functions: [{functionName, ranges: [{startOffset, endOffset,
    count}]}]}]}`. `read_source(relpath)` supplies file text so byte offsets become lines. A
    function's FIRST range is the function itself, so its count is the exact invocation count.
    Malformed entries are counted and skipped, never raised."""
    acc = TraceAccumulator()
    malformed = 0
    starts_cache: dict[str, list[int] | None] = {}

    for blob in blobs:
        results = blob.get("result") if isinstance(blob, dict) else None
        if not isinstance(results, list):
            malformed += 1
            continue
        for entry in results:
            if not isinstance(entry, dict):
                malformed += 1
                continue
            rel = _script_relpath(entry.get("url", ""), repo)
            if rel is None:
                continue
            if rel not in starts_cache:
                src = read_source(rel)
                starts_cache[rel] = build_line_starts(src) if src is not None else None
            starts = starts_cache[rel]
            if starts is None:
                continue
            ranges: list[tuple[int, int, int]] = []
            methods: list[ObservedMethod] = []
            for fn in entry.get("functions") or []:
                for i, rng in enumerate((fn.get("ranges") if isinstance(fn, dict) else None) or []):
                    try:
                        start, end, count = int(rng["startOffset"]), int(rng["endOffset"]), int(rng["count"])
                    except (KeyError, TypeError, ValueError):
                        malformed += 1
                        continue
                    ranges.append((start, end, count))
                    if i == 0 and count > 0:
                        methods.append(ObservedMethod(fn.get("functionName") or "", rel,
                                                      offset_to_line(starts, start), count))
            hits = tuple(Hit(rel, ln, n) for ln, n in _line_counts(ranges, starts).items())
            acc.add(RuntimeTrace(coverage=hits, methods=tuple(methods)))
    return acc.freeze(), malformed


def parse_cpu_profile(profile: dict, repo: str) -> tuple[RuntimeTrace, int]:
    """A V8 CPU profile -> (trace of caller->callee calls, dropped_count). `callFrame.lineNumber` is
    the function's 0-based DEFINITION line (+1 here), so endpoints resolve exactly. A link with a
    frame outside the repo is dropped and counted."""
    nodes = profile.get("nodes") if isinstance(profile, dict) else None
    if not isinstance(nodes, list):
        return RuntimeTrace(), 1
    frames: dict[int, tuple[str, int, str] | None] = {}
    for n in nodes:
        if not (isinstance(n, dict) and "id" in n):
            continue
        cf = n.get("callFrame")
        loc = None
        if isinstance(cf, dict) and isinstance(cf.get("lineNumber"), int):
            rel = _script_relpath(cf.get("url", ""), repo)
            if rel is not None:
                loc = (rel, cf["lineNumber"] + 1, cf.get("functionName") or "")
        frames[n["id"]] = loc

    acc = TraceAccumulator()
    dropped = 0
    calls: list[ObservedCall] = []
    for n in nodes:
        if not isinstance(n, dict):
            continue
        parent = frames.get(n.get("id"))
        for cid in n.get("children") or []:
            child = frames.get(cid)
            if parent is None or child is None:
                dropped += 1
                continue
            calls.append(ObservedCall(parent[0], parent[1], child[0], child[1], 1, parent[2], child[2]))
    acc.add(RuntimeTrace(calls=tuple(calls)))
    return acc.freeze(), dropped


def _fs_reader(repo: str):
    def read(rel: str) -> str | None:
        try:
            return (Path(repo) / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
    return read


def resolve_node(node_exe: str | None = None) -> str | None:
    """The Node binary: explicit arg, then $ORION_NODE, then PATH. None if unavailable."""
    return node_exe or os.environ.get("ORION_NODE") or shutil.which("node")


class V8Tracer:
    """Tracer for Node targets (server or harness script)."""

    def __init__(self, node_exe: str | None = None, settle_ms: int = 500) -> None:
        self._node = node_exe
        self._settle_ms = settle_ms

    def build_flags(self) -> list[str]:
        return []  # no rebuild for JS

    def _dirs(self, work: Path) -> tuple[Path, Path]:
        cov, prof = Path(work) / COVERAGE_SUBDIR, Path(work) / CPUPROF_SUBDIR
        cov.mkdir(parents=True, exist_ok=True)
        prof.mkdir(parents=True, exist_ok=True)
        return cov, prof

    def launch_env(self, work: Path) -> dict[str, str]:
        """Env for a SERVER started by its own command. Coverage and the CPU profile flush only on a
        clean exit, so a preload traps SIGTERM/SIGINT and exits 0; NODE_OPTIONS carries it into the
        `node` child of `npm start`."""
        cov, prof = self._dirs(work)
        preload = Path(work) / "orion_exit_flush.cjs"
        preload.write_text("for (const s of ['SIGTERM', 'SIGINT', 'SIGBREAK']) "
                           "process.on(s, () => process.exit(0));\n")
        return {"NODE_V8_COVERAGE": str(cov),
                "NODE_OPTIONS": f'--cpu-prof --cpu-prof-dir="{prof}" --require "{preload}"'}

    def script_command(self, script: str, repo: str, work: Path) -> tuple[list[str], dict[str, str]]:
        """argv + env to run a harness SCRIPT under `_boot_js`, dumping into the same dirs."""
        node = resolve_node(self._node)
        if node is None:
            raise FileNotFoundError("node executable not found (install Node or set ORION_NODE)")
        cov, prof = self._dirs(work)
        return ([node, _BOOT_JS, os.path.abspath(script), os.path.abspath(repo), str(cov), str(prof)],
                {"ORION_JS_SETTLE_MS": str(self._settle_ms)})

    def reset(self, work: Path) -> None:
        """Drop consumed dumps so the next collect sees only the next run's."""
        for d in (Path(work) / COVERAGE_SUBDIR, Path(work) / CPUPROF_SUBDIR):
            if d.is_dir():
                for f in d.iterdir():
                    f.unlink(missing_ok=True)

    def collect(self, work: Path, repo: str) -> RuntimeTrace:
        cov_dir, prof_dir = Path(work) / COVERAGE_SUBDIR, Path(work) / CPUPROF_SUBDIR
        acc = TraceAccumulator()
        blobs = []
        for f in sorted(cov_dir.glob("*.json")) if cov_dir.is_dir() else []:
            try:
                blobs.append(json.loads(f.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        if blobs:
            acc.add(parse_v8_coverage(blobs, repo, _fs_reader(repo))[0])
        for f in sorted(prof_dir.glob("*.cpuprofile")) if prof_dir.is_dir() else []:
            try:
                acc.add(parse_cpu_profile(json.loads(f.read_text(encoding="utf-8")), repo)[0])
            except (OSError, ValueError):
                continue
        return acc.freeze()
