"""Map an ObservedTrace onto the existing scan graph and emit dynamic nodes/edges into a Batch.

Two halves, split so the mapping logic is pure and unit-testable without a database:

- ``load_static_index`` (I/O): read the scan's CpgMethod / CpgCall locations straight from Neo4j
  (its own driver, NOT the agent-facing run_cypher whose 50-row cap would truncate a real repo).
- ``build_batch`` (pure): given that index + the trace, decide which observed methods are genuinely
  new (:ObservedMethod) and emit OBSERVED_CALL / OBSERVED_DISPATCH edges, all stamped
  ``origin='dynamic'``. Nothing static is read or written here beyond matching endpoints by key.

Identity is (file, definition-line): the one key Joern and the tracer agree on (see trace.py).
Paths are normalized to repo-relative forward-slash form so the tracer's absolute co_filename lines
up with Joern's repo-relative FILENAME; a basename+line fallback covers a path-layout mismatch.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from neo4j import GraphDatabase

from .. import config
from ..graph.schema import Batch, synthesize_uid
from .trace import ObservedTrace


def _norm(path: str) -> str:
    """Forward-slashed, ./-stripped path for stable comparison across OSes and the two producers."""
    p = (path or "").replace("\\", "/")
    return p[2:] if p.startswith("./") else p


def _rel(path: str, root: str) -> str:
    """The tracer's absolute co_filename as a repo-relative path (to match Joern's FILENAME)."""
    try:
        return _norm(os.path.relpath(path, root))
    except (ValueError, OSError):
        return _norm(path)


@dataclass
class StaticIndex:
    """Location→identity lookups for one scan's static graph. `*_by_base` are the basename+line
    fallbacks used only when the repo-relative match misses (a path-layout mismatch)."""
    methods_by_loc: dict[tuple[str, int], str] = field(default_factory=dict)     # (relpath,line)->full_name
    methods_by_base: dict[tuple[str, int], str] = field(default_factory=dict)    # (basename,line)->full_name
    calls_by_loc: dict[tuple[str, int], str] = field(default_factory=dict)       # (relpath,line)->uid
    calls_by_base: dict[tuple[str, int], str] = field(default_factory=dict)      # (basename,line)->uid

    def method_at(self, relpath: str, line: int) -> str | None:
        m = self.methods_by_loc.get((relpath, line))
        if m is not None:
            return m
        return self.methods_by_base.get((os.path.basename(relpath), line))

    def call_at(self, relpath: str, line: int) -> str | None:
        c = self.calls_by_loc.get((relpath, line))
        if c is not None:
            return c
        return self.calls_by_base.get((os.path.basename(relpath), line))


def _method_synth_score(full_name: str) -> int:
    """Lower is more 'real'. Joern emits synthetic twins at the SAME (file, line) as a real method
    (e.g. `Foo.handle` AND `Foo.handle<metaClassAdapter>`, plus `<body>`/`<fakeNew>` shims). On a
    location collision we must resolve a runtime dispatch to the REAL method, not its adapter."""
    fn = full_name or ""
    short = fn.rsplit(".", 1)[-1]
    score = 0
    if "metaClass" in fn or "fakeNew" in fn:
        score += 2
    if "<" in short:                 # <metaClassAdapter>, <body>, <module>, <lambda>-style shims
        score += 1
    return score


def _call_synth_score(name: str) -> int:
    """Lower is more 'real'. A call SITE has both the real call (`handle`) and Joern operator nodes
    (`<operator>.fieldAccess` for the `handler.handle` attribute access) at the same line — anchor a
    dispatch on the real call, not the operator."""
    return 1 if (name or "").startswith("<operator>") else 0


def _prefer(store: dict, key, value, score: int, scores: dict) -> None:
    """Keep `value` for `key` only if its `score` beats the incumbent's (strictly lower); ties keep
    the first seen (deterministic). `scores` tracks the incumbent score per key."""
    if key not in store or score < scores[key]:
        store[key] = value
        scores[key] = score


def index_from_rows(method_rows, call_rows) -> StaticIndex:
    """Build a StaticIndex from row dicts ({f,l,fn} / {f,l,uid,name}). Pure — the unit-test entry
    point. On a (file,line) collision the least-synthetic method / non-operator call wins, so a
    runtime dispatch resolves to the real symbol rather than a Joern shim. Relpath keys are preferred
    over the basename+line fallback."""
    idx = StaticIndex()
    m_loc_s: dict = {}
    m_base_s: dict = {}
    c_loc_s: dict = {}
    c_base_s: dict = {}
    for r in method_rows:
        f, line, fn = _norm(r["f"]), r["l"], r["fn"]
        if not f or line is None or not fn:
            continue
        s = _method_synth_score(fn)
        _prefer(idx.methods_by_loc, (f, line), fn, s, m_loc_s)
        _prefer(idx.methods_by_base, (os.path.basename(f), line), fn, s, m_base_s)
    for r in call_rows:
        f, line, uid = _norm(r["f"]), r["l"], r["uid"]
        if not f or line is None or not uid:
            continue
        s = _call_synth_score(r.get("name") or "")
        _prefer(idx.calls_by_loc, (f, line), uid, s, c_loc_s)
        _prefer(idx.calls_by_base, (os.path.basename(f), line), uid, s, c_base_s)
    return idx


def load_static_index(scan_id: str) -> StaticIndex:
    """Read all CpgMethod/CpgCall locations for `scan_id` (own driver; no 50-row cap). I/O."""
    driver = GraphDatabase.driver(config.NEO4J_URI, auth=config.NEO4J_AUTH)
    try:
        with driver.session(database=config.NEO4J_DATABASE) as s:
            methods = [dict(r) for r in s.run(
                "MATCH (m:CpgMethod {scan_id:$sid}) WHERE m.line IS NOT NULL "
                "RETURN m.file_path AS f, m.line AS l, m.full_name AS fn", sid=scan_id)]
            calls = [dict(r) for r in s.run(
                "MATCH (c:CpgCall {scan_id:$sid}) WHERE c.line IS NOT NULL "
                "RETURN c.file_path AS f, c.line AS l, c.uid AS uid, c.name AS name", sid=scan_id)]
    finally:
        driver.close()
    return index_from_rows(methods, calls)


@dataclass
class MergeStats:
    """What the merge produced, for the run log before delta reads the persisted graph back."""
    new_methods: int = 0
    observed_calls: int = 0
    observed_dispatches: int = 0
    dropped_calls: int = 0        # a call whose caller/callee didn't resolve to any node (e.g. plumbing)
    dropped_dispatches: int = 0   # a dispatch with no static CpgCall to anchor, or unresolved target


def build_batch(trace: ObservedTrace, scan_id: str, index: StaticIndex,
                root: str) -> tuple[Batch, MergeStats]:
    """Pure: turn a trace into a dynamic Batch (:ObservedMethod nodes + OBSERVED_* edges). Returns
    (batch, stats). An endpoint that resolves to neither a static node nor a new ObservedMethod is
    dropped (this is what discards runpy/importlib plumbing frames) and counted, never fabricated."""
    b = Batch(scan_id)
    stats = MergeStats()
    observed_uid: dict[tuple[str, int], str] = {}   # (relpath, def-line) -> ObservedMethod uid

    # Pass 1: which executed methods have NO static CpgMethod? Those become :ObservedMethod nodes.
    for m in trace.methods:
        rel = _rel(m.file, root)
        if index.method_at(rel, m.line) is not None:
            continue                                  # exists statically — not new
        key = (rel, m.line)
        if key in observed_uid:
            continue
        uid = synthesize_uid(scan_id, "ObservedMethod", rel, m.line, 0, m.name)
        observed_uid[key] = uid
        b.emit_node("ObservedMethod",
                    {"uid": uid, "name": m.name, "file_path": rel, "line": m.line, "origin": "dynamic"})
        stats.new_methods += 1

    def _endpoint(file: str, line: int):
        """Resolve a (file, def-line) to a graph endpoint (label, key), or None to drop it."""
        rel = _rel(file, root)
        fn = index.method_at(rel, line)
        if fn is not None:
            return "CpgMethod", {"full_name": fn}
        uid = observed_uid.get((rel, line))
        if uid is not None:
            return "ObservedMethod", {"uid": uid}
        return None

    # Pass 2: OBSERVED_CALL for every caller→callee where BOTH endpoints resolve.
    for c in trace.calls:
        caller = _endpoint(c.caller_file, c.caller_line)
        callee = _endpoint(c.callee_file, c.callee_line)
        if caller is None or callee is None:
            stats.dropped_calls += 1
            continue
        b.emit_edge("OBSERVED_CALL", caller[0], caller[1], callee[0], callee[1], {"origin": "dynamic"})
        stats.observed_calls += 1

    # Pass 3: OBSERVED_DISPATCH from the static CpgCall at the call site to the resolved target.
    for d in trace.dispatches:
        call_uid = index.call_at(_rel(d.call_site_file, root), d.call_site_line)
        target = (_endpoint(d.resolved_callee_file, d.resolved_callee_line)
                  if d.resolved_callee_line is not None else None)
        if call_uid is None or target is None:
            stats.dropped_dispatches += 1
            continue
        b.emit_edge("OBSERVED_DISPATCH", "CpgCall", {"uid": call_uid},
                    target[0], target[1], {"origin": "dynamic"})
        stats.observed_dispatches += 1

    return b, stats
