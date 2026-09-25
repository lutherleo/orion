"""Correlate a RuntimeTrace onto the scan graph and produce a WritePlan. Pure except `load_static_index`.

Resolution order for a (file, line) that should name a METHOD:

  1. exact definition line   -- (file, def-line) is the one key Joern and a tracer agree on. When
                                several methods share the line (a `def f():` on line 1 and the file's
                                <module>), the one whose short name matches the observed name wins;
                                otherwise the least-synthetic (Joern emits `Foo.handle` AND
                                `Foo.handle<metaClassAdapter>` / `<body>` shims at one location).
  2. a runtime-only method   -- the :ObservedMethod this same trace just created for that location.
  3. containing method       -- CpgMethod carries no end line, so a line belongs to the method with
                                the GREATEST declaration line <= it (the closest preceding def).
  4. basename fallback       -- when the tracer's and Joern's path layouts differ, a basename that is
                                unique in the graph stands in for the relpath (steps 1 and 3).

A trace item that resolves to nothing is COUNTED in `plan.dropped`, never fabricated into a node/edge
(this is what discards runpy/importlib/node-internal plumbing frames).
"""
from __future__ import annotations

import bisect
import os
import re
from dataclasses import dataclass, field

from ..graph.schema import synthesize_uid
from .trace import RuntimeTrace


def norm(path: str) -> str:
    """Forward-slashed, ./-stripped path for stable comparison across OSes and producers."""
    p = (path or "").replace("\\", "/")
    return p[2:] if p.startswith("./") else p


def relativize(path: str, root: str) -> str | None:
    """An absolute tracer path as a repo-relative one, or None when it lies outside `root`.
    A relative input is taken as already repo-relative."""
    if not path or path.startswith("<"):
        return None
    if not os.path.isabs(path):
        return norm(path)
    try:
        rel = os.path.relpath(os.path.realpath(path), os.path.realpath(root))
    except (ValueError, OSError):
        return None
    return None if rel == ".." or rel.startswith(".." + os.sep) else norm(rel)


# ─────────────────────────── byte offset -> line (V8) ───────────────────────────

def build_line_starts(source: str) -> list[int]:
    """Byte offsets at which each line begins (`starts[i]` = offset of line i+1). V8 keys ranges by
    UTF-8 byte offset, so offsets are over the ENCODED bytes -- a multi-byte char must not shift it."""
    data = source.encode("utf-8")
    starts = [0]
    i = data.find(b"\n")
    while i != -1:
        starts.append(i + 1)
        i = data.find(b"\n", i + 1)
    return starts


def offset_to_line(line_starts: list[int], offset: int) -> int:
    """1-based line containing byte `offset` (the count of line starts <= offset)."""
    if offset < 0:
        return 1
    return max(1, bisect.bisect_right(line_starts, offset))


# ─────────────────────────── the static index ───────────────────────────

def _method_synth_score(full_name: str) -> int:
    """Lower is more 'real': penalize Joern's metaClass/fakeNew adapters and `<...>` shims."""
    fn = full_name or ""
    short = fn.rsplit(".", 1)[-1]
    return (2 if ("metaClass" in fn or "fakeNew" in fn) else 0) + (1 if "<" in short else 0)


def _short(name: str) -> str:
    """Last segment of a method name: Joern's `a.py:<module>.Impl.handle` / `a.js::program:handle`
    and a tracer's `Impl.handle` / `handle` all shorten to `handle`."""
    return re.split(r"[.:]", name or "")[-1]


def _call_synth_score(name: str) -> int:
    """Lower is more 'real': a real call beats a Joern `<operator>.*` node on the same line."""
    return 1 if (name or "").startswith("<operator>") else 0


@dataclass
class StaticIndex:
    """Location lookups for one scan's static graph."""
    # file -> (sorted decl lines, parallel best full_name per line)
    method_lines: dict[str, list[int]] = field(default_factory=dict)
    method_names: dict[str, list[str]] = field(default_factory=dict)
    # (file, line) -> CpgCall uids on that line, real calls before operator nodes
    calls: dict[tuple[str, int], list[str]] = field(default_factory=dict)
    # basename -> the one graph file with that basename (ambiguous basenames are absent)
    by_base: dict[str, str] = field(default_factory=dict)
    # (file, line) -> every method declared there, best first -- only where more than one is
    same_line: dict[tuple[str, int], list[str]] = field(default_factory=dict)

    def _file(self, rel: str, table: dict) -> str | None:
        if rel in table:
            return rel
        alt = self.by_base.get(os.path.basename(rel))
        return alt if alt is not None and alt in table else None

    def method_exact(self, rel: str, line: int, name: str = "") -> str | None:
        f = self._file(rel, self.method_lines)
        if f is None:
            return None
        lines = self.method_lines[f]
        i = bisect.bisect_left(lines, line)
        if i == len(lines) or lines[i] != line:
            return None
        if name and (f, line) in self.same_line:
            want = _short(name)
            for fn in self.same_line[(f, line)]:
                if _short(fn) == want:
                    return fn
        return self.method_names[f][i]

    def method_containing(self, rel: str, line: int) -> str | None:
        f = self._file(rel, self.method_lines)
        if f is None:
            return None
        i = bisect.bisect_right(self.method_lines[f], line) - 1
        return self.method_names[f][i] if i >= 0 else None

    def calls_at(self, rel: str, line: int) -> list[str]:
        uids = self.calls.get((rel, line))
        if uids is None:
            alt = self.by_base.get(os.path.basename(rel))
            uids = self.calls.get((alt, line)) if alt is not None else None
        return uids or []


def index_from_rows(method_rows, call_rows) -> StaticIndex:
    """Build a StaticIndex from row dicts ({f, l, fn} / {f, l, uid, name?}). Pure."""
    at: dict[tuple[str, int], list[tuple[int, int, str]]] = {}   # (score, seen-order, fn)
    for r in method_rows:
        f, line, fn = norm(r.get("f") or ""), r.get("l"), r.get("fn")
        if not f or not isinstance(line, int) or not fn:
            continue
        rows_here = at.setdefault((f, line), [])
        if all(fn != x[2] for x in rows_here):
            rows_here.append((_method_synth_score(fn), len(rows_here), fn))

    idx = StaticIndex()
    per_file: dict[str, list[tuple[int, str]]] = {}
    for (f, line), cands in at.items():
        ranked = [fn for _, _, fn in sorted(cands)]      # least synthetic first; ties keep first seen
        per_file.setdefault(f, []).append((line, ranked[0]))
        if len(ranked) > 1:
            idx.same_line[(f, line)] = ranked
    for f, rows in per_file.items():
        rows.sort()
        idx.method_lines[f] = [line for line, _ in rows]
        idx.method_names[f] = [fn for _, fn in rows]

    scored: dict[tuple[str, int], list[tuple[int, str]]] = {}
    for r in call_rows:
        f, line, uid = norm(r.get("f") or ""), r.get("l"), r.get("uid")
        if not f or not isinstance(line, int) or line <= 0 or not uid:
            continue
        scored.setdefault((f, line), []).append((_call_synth_score(r.get("name") or ""), uid))
    idx.calls = {k: [uid for _, uid in sorted(v, key=lambda t: t[0])] for k, v in scored.items()}

    base_count: dict[str, list[str]] = {}
    for f in {*idx.method_lines, *(f for f, _ in idx.calls)}:
        base_count.setdefault(os.path.basename(f), []).append(f)
    idx.by_base = {b: fs[0] for b, fs in base_count.items() if len(fs) == 1}
    return idx


def load_static_index(scan_id: str) -> StaticIndex:
    """Read every CpgMethod/CpgCall location for `scan_id` over its own driver (the agent-facing
    run_cypher caps rows and re-checks for write keywords -- neither belongs on a bulk read)."""
    from neo4j import GraphDatabase

    from .. import config

    driver = GraphDatabase.driver(config.NEO4J_URI, auth=config.NEO4J_AUTH)
    try:
        with driver.session(database=config.NEO4J_DATABASE) as s:
            methods = [dict(r) for r in s.run(
                "MATCH (m:CpgMethod {scan_id:$sid}) "
                "WHERE m.file_path IS NOT NULL AND m.line IS NOT NULL "
                "RETURN m.file_path AS f, m.line AS l, m.full_name AS fn", sid=scan_id)]
            calls = [dict(r) for r in s.run(
                "MATCH (c:CpgCall {scan_id:$sid}) WHERE c.line > 0 "
                "RETURN c.file_path AS f, c.line AS l, c.uid AS uid, c.name AS name", sid=scan_id)]
    finally:
        driver.close()
    return index_from_rows(methods, calls)


# ─────────────────────────── the correlation ───────────────────────────

@dataclass
class WritePlan:
    """What writeback applies. Pure data -- no DB handle, no Cypher.

    - call_hits   : CpgCall.uid -> summed line hit_count (marked executed)
    - method_hits : CpgMethod.full_name -> invocations (from method observations) or, for a method
                    only seen through line coverage, its hottest line's count
    - new_methods : :ObservedMethod node props (runtime-only functions)
    - edges       : (rtype, from_label, from_key, to_label, to_key) -> summed hits; one edge per pair
    - dropped     : trace items that correlated to nothing, by reason (surfaced, never silent)
    """
    call_hits: dict[str, int] = field(default_factory=dict)
    method_hits: dict[str, int] = field(default_factory=dict)
    new_methods: list[dict] = field(default_factory=list)
    edges: dict[tuple[str, str, str, str, str], int] = field(default_factory=dict)
    dropped: dict[str, int] = field(default_factory=dict)

    def edges_of(self, rtype: str) -> dict[tuple[str, str, str, str], int]:
        return {k[1:]: n for k, n in self.edges.items() if k[0] == rtype}


# Label -> the key property an edge endpoint is matched on (CpgMethod by NODE_KEY full_name).
ENDPOINT_KEY = {"CpgMethod": "full_name", "ObservedMethod": "uid", "CpgCall": "uid"}


def correlate(trace: RuntimeTrace, index: StaticIndex, scan_id: str) -> WritePlan:
    """Turn a RuntimeTrace into a WritePlan. Pure."""
    plan = WritePlan()
    dropped = {"coverage_no_call": 0, "coverage_no_method": 0, "call_unresolved": 0,
               "dispatch_unresolved": 0}
    invocations: dict[str, int] = {}
    hottest_line: dict[str, int] = {}

    # Pass 1: executed methods. A definition with no static CpgMethod becomes an :ObservedMethod.
    observed: dict[tuple[str, int, str], str] = {}
    observed_at: dict[tuple[str, int], str] = {}
    for m in trace.methods:
        fn = index.method_exact(m.file_path, m.line, m.name)
        if fn is not None:
            invocations[fn] = invocations.get(fn, 0) + m.hit_count
            continue
        key = (m.file_path, m.line, m.name)
        if not m.name or m.name.startswith("<") or key in observed:
            # An unnamed wrapper or a structural shim (<module>, <lambda>, <listcomp>) is not a
            # runtime-only FUNCTION; its lines still resolve to the containing method below.
            continue
        uid = synthesize_uid(scan_id, "ObservedMethod", m.file_path, m.line, 0, m.name)
        observed[key] = uid
        observed_at.setdefault(key[:2], uid)      # an unnamed call endpoint resolves by location
        plan.new_methods.append({"uid": uid, "name": m.name, "file_path": m.file_path,
                                 "line": m.line, "hit_count": m.hit_count, "origin": "runtime"})

    # Pass 2: line coverage -> executed calls + the containing method.
    for h in trace.coverage:
        uids = index.calls_at(h.file_path, h.line)
        for uid in uids:
            plan.call_hits[uid] = plan.call_hits.get(uid, 0) + h.hit_count
        if not uids:
            dropped["coverage_no_call"] += 1
        fn = index.method_containing(h.file_path, h.line)
        if fn is None:
            dropped["coverage_no_method"] += 1
        elif h.hit_count > hottest_line.get(fn, 0):
            hottest_line[fn] = h.hit_count
    plan.method_hits = {**hottest_line, **invocations}

    def endpoint(file: str, line: int, name: str = "") -> tuple[str, str] | None:
        fn = index.method_exact(file, line, name)
        if fn is not None:
            return "CpgMethod", fn
        uid = observed.get((file, line, name)) if name else observed_at.get((file, line))
        if uid is not None:
            return "ObservedMethod", uid
        fn = index.method_containing(file, line)
        return ("CpgMethod", fn) if fn is not None else None

    # Pass 3: OBSERVED_CALL where BOTH endpoints resolve; a method calling itself is not a new link.
    for c in trace.calls:
        a = endpoint(c.caller_file, c.caller_line, c.caller_name)
        b = endpoint(c.callee_file, c.callee_line, c.callee_name)
        if a is None or b is None or a == b:
            dropped["call_unresolved"] += 1
            continue
        k = ("OBSERVED_CALL", a[0], a[1], b[0], b[1])
        plan.edges[k] = plan.edges.get(k, 0) + c.hits

    # Pass 4: OBSERVED_DISPATCH from the static CpgCall at the call site to the resolved target.
    for d in trace.dispatches:
        sites = index.calls_at(d.site_file, d.site_line)
        target = (endpoint(d.callee_file, d.callee_line, d.callee_name)
                  if d.callee_line is not None else None)
        if not sites or target is None:
            dropped["dispatch_unresolved"] += 1
            continue
        k = ("OBSERVED_DISPATCH", "CpgCall", sites[0], target[0], target[1])
        plan.edges[k] = plan.edges.get(k, 0) + 1

    plan.dropped = dropped
    return plan
