"""Graph-as-memory Q&A: does Claude answer code-STRUCTURE questions better through Orion's graph than by grep?

Public memory benchmarks (LongMemEval, LoCoMo) are prose or chat logs, so they cannot test a code graph. This module
tests the graph-as-memory layer directly, Graphwalks-style: questions about where code is, what calls what, which
entry points reach which sinks, and what the repo declares, asked of the same model through two access paths:

  * graph -- Orion's three read-only MCP tools only (`run_cypher`, `semantic_search`, `get_schema`), scoped to the
    fixture's scan; no file tools;
  * grep  -- Read / Grep / Glob only, on a `git archive` export of the fixture (tracked files: no .git, no
    node_modules, no cpg.bin); no graph.

Questions are generated from Orion's own NodeGoat and PyGoat graphs in WSL's Neo4j, so they are new by
construction. Six families, each with a gold Cypher query (stored on the question) that computes the gold set:

  lookup        which file and line defines function X
  calls         the call sites of X / the repo functions X calls
  entry_sink    which of Orion's entry points contain a sink of kind K fed by untrusted input (FLOWS_TO)
  flow_path     the calls whose results flow into a CandidateFlow's sink (the FLOWS_TO ancestors of the sink)
  deps          the declared dependency names and versions (all / major version 0)
  routes_noauth routes registered without an auth check

The graph lies by omission (calls nested in arrow functions assigned to object properties get no CONTAINS_CALL
edge; Joern names those functions `<lambda>N`; name-based RESOLVES_TO can bind the wrong method), so every gold item
is checked against the cited source line, and where the family allows it the gold SET is checked for completeness
against a lexical scan of the export (e.g. every `X(` line in the repo for the call sites of X). A candidate whose
gold cannot be confirmed is dropped and counted; candidates are tried in a seeded order until two per family and
fixture pass. The lexical checks are deliberately conservative, so the surviving questions are biased toward code
the graph models well: a pilot on them says whether the graph helps where it is right, not how often it is right.

The templates are AI-written, so questions.json is marked "status": "proposed" until a human reviews them. All
numbers are dev evidence (NodeGoat and PyGoat are public and old). A session that fails (error, timeout, max
turns) is never a correct answer; its tokens are counted when the CLI reported them, else they are missing.

Each question has a `rev`. Re-generating keeps it when what the model is shown is unchanged and bumps it when a
template rewording or a new pick changes it; sessions are keyed by (id, rev), so `run` re-asks only the changed
questions, and `score` leaves the older revs' sessions out of the table (their usage stays in the total).

    python -m eval.graphqa questions [--build]     # WSL: Neo4j + fixtures; writes eval/dataset/graphqa/questions.json
    python -m eval.graphqa run [--jobs 1]          # WSL: 24 x 2 paths x 2 models claude -p sessions (spends usage)
    python -m eval.graphqa score                   # token-free: results JSON + markdown table from the session log
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATASET = ROOT / "eval" / "dataset" / "graphqa" / "questions.json"
RESULTS = ROOT / "eval" / "results" / "graphqa-pilot.json"
TABLE = ROOT / "eval" / "results" / "graphqa-pilot.md"
SESSIONS = ROOT / "eval" / "results" / "graphqa-pilot.sessions.jsonl"

FIXTURES = {"nodegoat": "fixtures/NodeGoat", "pygoat": "fixtures/pygoat"}
FAMILIES = ("lookup", "calls", "entry_sink", "flow_path", "deps", "routes_noauth")
PER_FAMILY = 2                       # questions per family per fixture (pilot: 2 x 6 x 2 = 24)
SEED = 20261031
MODELS = ("claude-sonnet-5", "claude-opus-5")
PATHS = ("graph", "grep")
EFFORT = "xhigh"
MAX_TURNS = 15
TIMEOUT = 900                        # seconds per session
LIMIT_WAIT = 1800                    # seconds to wait after a usage-limit message
MAX_LIMIT_WAITS = 16

LANG_EXTS = {"js": (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"), "py": (".py",)}
# Callee names this common are never trusted as a resolution of a first-party function (`get`, `save`, ...):
# name-based RESOLVES_TO binds `dict.get` to a view's `get` method.
STOP_NAMES = frozenset({
    "get", "post", "put", "delete", "patch", "save", "update", "find", "findOne", "render", "send", "info",
    "error", "warning", "warn", "debug", "log", "main", "run", "handle", "index", "init", "__init__", "__str__",
    "__call__", "__reduce__", "function", "wrapper", "setUp", "test", "call", "apply", "bind", "then"})


# ───────────────────────────── answer normalisation and scoring ─────────────────────────────

def _strip_quotes(s: str) -> str:
    return str(s).strip().strip("`'\"").strip()


def norm_path(p: str, roots: tuple[str, ...] = ()) -> str:
    p = _strip_quotes(p).replace("\\", "/")
    for r in sorted((r.replace("\\", "/").rstrip("/") for r in roots if r), key=len, reverse=True):
        if p.startswith(r + "/"):
            p = p[len(r) + 1:]
            break
    while p.startswith("./"):
        p = p[2:]
    return p.lstrip("/")


_LOC = re.compile(r"^(.*?)(?::|#L|\s+line\s+|\s*\(line\s+)(\d+)", re.I)


def norm_loc(s: str, roots: tuple[str, ...] = ()) -> str:
    """`path/to/file:LINE` (a range or column after the line is dropped)."""
    m = _LOC.match(_strip_quotes(s))
    if not m:
        return norm_path(s, roots)
    return f"{norm_path(m.group(1), roots)}:{int(m.group(2))}"


def norm_name(s: str, roots: tuple[str, ...] = ()) -> str:
    s = _strip_quotes(s)
    s = re.sub(r"^(?:async\s+)?(?:def|function|class)\s+", "", s)
    s = re.sub(r"\(.*$", "", s).strip()
    return re.split(r"\.|::|#", s)[-1].strip()


def _norm_version(v: str) -> str:
    v = _strip_quotes(v)
    v = re.sub(r"^[=^~<>!\s]+", "", v)
    return v[1:] if re.match(r"^v\d", v) else v


def norm_dep(s: str, roots: tuple[str, ...] = ()) -> str:
    """`name@version`, name lower-cased (pip's `_` = `-`), version without range operators (^ ~ == ...)."""
    s = _strip_quotes(s)
    m = re.match(r"^\s*([A-Za-z0-9_.\-]+)\s*(==|~=|>=|<=|!=)\s*(\S+)", s)
    if m:
        name, ver = m.group(1), m.group(3)
    elif "@" in s[1:]:
        i = s.rindex("@")
        name, ver = s[:i], s[i + 1:]
    else:
        parts = s.split(None, 1)
        name, ver = parts[0], (parts[1] if len(parts) > 1 else "")
    return f"{name.strip().lower().replace('_', '-')}@{_norm_version(ver)}"


_HTTP = ("GET", "POST", "PUT", "DELETE", "PATCH", "ALL")


def norm_route(s: str, roots: tuple[str, ...] = ()) -> str:
    """`METHOD /path` or `/path`: method upper-cased, the path with one leading slash and no trailing one."""
    s = _strip_quotes(s)
    method = ""
    head = s.split(None, 1)
    if head and head[0].upper() in _HTTP:
        method, s = head[0].upper(), (head[1] if len(head) > 1 else "")
    path = "/" + _strip_quotes(s).strip("/")
    return f"{method} {path}" if method else path


NORMALIZERS = {"loc": norm_loc, "name": norm_name, "dep": norm_dep, "route": norm_route}


def normalize(items, kind: str, roots: tuple[str, ...] = ()) -> list[str]:
    fn = NORMALIZERS[kind]
    out = {fn(x, roots) for x in items if isinstance(x, str)}   # "" is a real Django route ("/")
    return sorted(x for x in out if x)


def score_answer(answer: list[str], gold: list[str]) -> dict:
    """Set precision / recall / F1 / exact match. An empty answer scores 0 (gold is never empty)."""
    a, g = set(answer), set(gold)
    hit = len(a & g)
    p = hit / len(a) if a else 0.0
    r = hit / len(g) if g else 0.0
    f1 = 2 * p * r / (p + r) if p + r else 0.0
    return {"precision": p, "recall": r, "f1": f1, "exact": a == g, "hits": hit}


# ───────────────────────────── source reading (the gold checks) ─────────────────────────────

def code_only(text: str, lang: str, *, keep_strings: bool = True) -> list[str]:
    """`text` split into lines with comments blanked (and string contents too unless `keep_strings`), the
    line count preserved. A small state machine: JS `//` and `/* */`, '...' "..." `...`; Python `#`, and
    '...' "..." with triple quotes. Regex literals and template `${}` nesting are not modelled."""
    out: list[str] = []
    buf: list[str] = []
    i, n = 0, len(text)
    state, quote = "code", ""
    while i < n:
        ch = text[i]
        if ch == "\n":
            out.append("".join(buf))
            buf = []
            if state == "line":
                state = "code"
            elif state == "str" and quote in ("'", '"') and lang == "js":
                state = "code"      # an unterminated JS string ends at the line (never spans silently)
            elif state == "str" and len(quote) == 1 and lang == "py":
                state = "code"
            i += 1
            continue
        if state == "code":
            two, three = text[i:i + 2], text[i:i + 3]
            if lang == "js" and two == "//":
                state = "line"
                i += 2
                continue
            if lang == "js" and two == "/*":
                state = "block"
                i += 2
                continue
            if lang == "py" and ch == "#":
                state = "line"
                i += 1
                continue
            if lang == "py" and three in ('"""', "'''"):
                state, quote = "str", three
                buf.append(three)
                i += 3
                continue
            if ch in ("'", '"') or (lang == "js" and ch == "`"):
                state, quote = "str", ch
                buf.append(ch)
                i += 1
                continue
            buf.append(ch)
            i += 1
        elif state == "line":
            i += 1
        elif state == "block":
            if text[i:i + 2] == "*/":
                state = "code"
                i += 2
            else:
                i += 1
        else:   # str
            if ch == "\\" and i + 1 < n and text[i + 1] != "\n":
                buf.append(text[i:i + 2] if keep_strings else "  ")
                i += 2
                continue
            if text[i:i + len(quote)] == quote:
                buf.append(quote)
                state = "code"
                i += len(quote)
                continue
            buf.append(ch if keep_strings else " ")
            i += 1
    out.append("".join(buf))
    return out


class SourceTree:
    """The exported fixture: tracked files only, read lazily."""

    def __init__(self, root: str | Path, lang: str):
        self.root = Path(root)
        self.lang = lang
        self._raw: dict[str, list[str]] = {}
        self._code: dict[tuple[str, bool], list[str]] = {}
        self._files: list[str] | None = None

    def files(self) -> list[str]:
        if self._files is None:
            exts = LANG_EXTS[self.lang]
            found = []
            for p in sorted(self.root.rglob("*")):
                rel = p.relative_to(self.root).as_posix()
                if p.is_file() and p.suffix in exts and "node_modules/" not in rel + "/":
                    found.append(rel)
            self._files = found
        return self._files

    def text(self, rel: str) -> str | None:
        try:
            return (self.root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    def raw(self, rel: str) -> list[str]:
        if rel not in self._raw:
            t = self.text(rel)
            self._raw[rel] = t.split("\n") if t is not None else []
        return self._raw[rel]

    def code(self, rel: str, *, keep_strings: bool = True) -> list[str]:
        key = (rel, keep_strings)
        if key not in self._code:
            t = self.text(rel)
            self._code[key] = code_only(t, self.lang, keep_strings=keep_strings) if t is not None else []
        return self._code[key]

    def line(self, rel: str, line: int, *, code: bool = False) -> str | None:
        lines = self.code(rel) if code else self.raw(rel)
        return lines[line - 1] if isinstance(line, int) and 1 <= line <= len(lines) else None


_ID = r"[A-Za-z_$][\w$]*"


def def_patterns(name: str, lang: str) -> list[re.Pattern]:
    n = re.escape(name)
    if lang == "py":
        return [re.compile(rf"^\s*(?:async\s+)?def\s+{n}\s*\(")]
    return [re.compile(rf"\bfunction\s*\*?\s*{n}\s*\("),
            re.compile(rf"(?<![\w$]){n}\s*[:=]\s*(?:async\s+)?function\b"),
            re.compile(rf"(?<![\w$]){n}\s*[:=]\s*(?:async\s*)?\([^)]*\)\s*=>"),
            re.compile(rf"(?<![\w$]){n}\s*[:=]\s*(?:async\s+)?{_ID}\s*=>"),
            re.compile(rf"^\s*(?:async\s+|static\s+)*{n}\s*\([^)]*\)\s*\{{")]


_JS_DEF_NAMES = [re.compile(rf"\bfunction\s*\*?\s*({_ID})\s*\("),
                 re.compile(rf"({_ID})\s*[:=]\s*(?:async\s+)?function\b"),
                 re.compile(rf"({_ID})\s*[:=]\s*(?:async\s*)?\([^)]*\)\s*=>"),
                 re.compile(rf"({_ID})\s*[:=]\s*(?:async\s+)?{_ID}\s*=>")]
_PY_DEF_NAMES = [re.compile(r"^\s*(?:async\s+)?def\s+(\w+)\s*\(")]


def defined_names(tree: SourceTree) -> dict[str, int]:
    """Every function name the source defines, read lexically, with its number of definition lines (the graph
    names `this.f = function` `<lambda>N`, so its own names miss these)."""
    pats = _PY_DEF_NAMES if tree.lang == "py" else _JS_DEF_NAMES
    names: dict[str, int] = {}
    for rel in tree.files():
        for line in tree.code(rel, keep_strings=False):
            found = {m.group(1) for p in pats for m in p.finditer(line)}
            for nm in found:
                names[nm] = names.get(nm, 0) + 1
    return names


def def_sites(tree: SourceTree, name: str) -> set[tuple[str, int]]:
    pats = def_patterns(name, tree.lang)
    return {(rel, i) for rel in tree.files()
            for i, line in enumerate(tree.code(rel, keep_strings=False), 1) if any(p.search(line) for p in pats)}


_CALL_RX: dict[str, re.Pattern] = {}


def call_rx(name: str) -> re.Pattern:
    if name not in _CALL_RX:
        _CALL_RX[name] = re.compile(rf"(?<![\w$]){re.escape(name)}\s*\(")
    return _CALL_RX[name]


def call_sites(tree: SourceTree, name: str) -> set[tuple[str, int]]:
    """Lines (comments and strings blanked) that call `name(`, definition lines excluded."""
    rx, defs = call_rx(name), def_patterns(name, tree.lang)
    return {(rel, i) for rel in tree.files()
            for i, line in enumerate(tree.code(rel, keep_strings=False), 1)
            if rx.search(line) and not any(p.search(line) for p in defs)}


def _indent(s: str) -> int:
    return len(s) - len(s.lstrip(" \t"))


def body_extent(tree: SourceTree, rel: str, line: int) -> tuple[int, int] | None:
    """(first, last) line of the function defined at `rel:line`, inclusive, or None if it can't be read.
    Python: the indented block after the signature. JS: from the def line to the brace that closes the body
    (strings and comments blanked), or the line itself for an arrow function with an expression body."""
    lines = tree.code(rel, keep_strings=False)
    if not (1 <= line <= len(lines)):
        return None
    if tree.lang == "py":
        start = line - 1
        base = _indent(lines[start])
        depth, i = 0, start
        while True:     # the signature may span lines
            depth += sum(lines[i].count(c) for c in "([{") - sum(lines[i].count(c) for c in ")]}")
            if depth <= 0 or i + 1 >= len(lines):
                break
            i += 1
        last = i
        for j in range(i + 1, len(lines)):
            s = lines[j]
            if not s.strip():
                continue
            if _indent(s) <= base:
                break
            last = j
        return line, last + 1
    depth_paren, closed, arrow = 0, False, False
    for i in range(line - 1, len(lines)):
        s = lines[i]
        k = 0
        while k < len(s):
            ch = s[k]
            if ch == "(":
                depth_paren += 1
            elif ch == ")":
                depth_paren -= 1
                if depth_paren == 0:
                    closed = True
            elif s[k:k + 2] == "=>" and depth_paren == 0:
                arrow = True
                rest = s[k + 2:].strip()
                if rest and not rest.startswith("{"):
                    return line, i + 1        # expression-bodied arrow function
                k += 2
                continue
            elif ch == "{" and depth_paren == 0 and (closed or arrow):
                return line, _match_brace(lines, i, k)
            k += 1
    return None


def _match_brace(lines: list[str], i: int, k: int) -> int:
    depth = 0
    for a in range(i, len(lines)):
        for ch in (lines[a][k:] if a == i else lines[a]):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return a + 1
    return len(lines)


# ───────────────────────────── the graph side ─────────────────────────────

# Every query is anchored to one scan ($scan_id), as Orion's agents must anchor theirs. `item` is the gold
# answer item; the other columns are the evidence the source check reads.
CYPHER = {
    "files": "MATCH (f:CpgFile {scan_id:$scan_id}) RETURN f.file_path AS file",
    "methods": ("MATCH (m:CpgMethod {scan_id:$scan_id}) WHERE NOT m.is_external "
                "RETURN m.name AS name, m.full_name AS full_name, m.file_path AS file, m.line AS line"),
    "resolved": ("MATCH (c:CpgCall {scan_id:$scan_id})-[:RESOLVES_TO]->(x:CpgMethod {scan_id:$scan_id}) "
                 "WHERE NOT x.is_external RETURN x.full_name AS full_name, count(DISTINCT c) AS n"),
    "lookup": ("MATCH (m:CpgMethod {scan_id:$scan_id, name:$name}) WHERE NOT m.is_external "
               "RETURN DISTINCT m.file_path + ':' + toString(m.line) AS item, m.file_path AS file, m.line AS line"),
    "callers": ("MATCH (c:CpgCall {scan_id:$scan_id})-[:RESOLVES_TO]->(x:CpgMethod {scan_id:$scan_id, full_name:$full_name}) "
                "RETURN DISTINCT c.file_path + ':' + toString(c.line) AS item, c.file_path AS file, c.line AS line"),
    "callees": ("MATCH (x:CpgMethod {scan_id:$scan_id, full_name:$full_name}) "
                "MATCH (m:CpgMethod {scan_id:$scan_id}) WHERE m.full_name = x.full_name "
                "OR m.full_name STARTS WITH x.full_name + ':' OR m.full_name STARTS WITH x.full_name + '.' "
                "MATCH (m)-[:CONTAINS_CALL]->(c:CpgCall {scan_id:$scan_id})-[:RESOLVES_TO]->(t:CpgMethod {scan_id:$scan_id}) "
                "WHERE NOT t.is_external AND NOT t.name CONTAINS '<' "
                "RETURN DISTINCT t.name AS item, c.file_path AS file, c.line AS line"),
    "entrypoints": ("MATCH (e:EntryPoint {scan_id:$scan_id})-[:ENTERS_AT]->(m:CpgMethod {scan_id:$scan_id}) "
                    "RETURN DISTINCT m.name AS name, m.full_name AS full_name, m.file_path AS file, m.line AS line"),
    "sink_kinds": ("MATCH (cf:CandidateFlow {scan_id:$scan_id}) RETURN cf.sink_category AS kind, count(*) AS n"),
    "entry_sink": ("MATCH (cf:CandidateFlow {scan_id:$scan_id, sink_category:$kind}) "
                   "MATCH (s:CpgCall {scan_id:$scan_id, uid:cf.sink_uid}) WHERE s.name IN $sink_names "
                   "MATCH (a:CpgCall {scan_id:$scan_id})-[:FLOWS_TO*0..6]->(s) "
                   "MATCH (am:CpgMethod {scan_id:$scan_id})-[:CONTAINS_CALL]->(a) "
                   "MATCH (:EntryPoint {scan_id:$scan_id})-[:ENTERS_AT]->(m:CpgMethod {scan_id:$scan_id}) "
                   "WHERE m.full_name IN $candidates AND (am = m OR am.full_name STARTS WITH m.full_name + ':' "
                   "OR am.full_name STARTS WITH m.full_name + '.') "
                   "RETURN DISTINCT m.name AS item, m.full_name AS full_name, s.file_path AS file, s.line AS line"),
    "flows": ("MATCH (cf:CandidateFlow {scan_id:$scan_id}) MATCH (s:CpgCall {scan_id:$scan_id, uid:cf.sink_uid}) "
              "RETURN cf.rank AS rank, cf.sink_category AS kind, s.uid AS sink_uid, s.file_path AS file, "
              "s.line AS line, s.name AS name, s.code AS code ORDER BY cf.rank"),
    "flow_path": ("MATCH (s:CpgCall {scan_id:$scan_id, uid:$sink_uid}) "
                  "MATCH p = (:CpgCall {scan_id:$scan_id})-[:FLOWS_TO*1..6]->(s) "
                  "WHERE all(x IN nodes(p) WHERE single(y IN nodes(p) WHERE y = x)) "
                  "UNWIND nodes(p) AS n "
                  "RETURN DISTINCT n.file_path + ':' + toString(n.line) AS item, n.file_path AS file, n.line AS line, "
                  "n.name AS name"),
    "deps": ("MATCH (d:Dependency {scan_id:$scan_id}) WHERE $version_rx IS NULL OR d.version =~ $version_rx "
             "RETURN d.name + '@' + d.version AS item, d.name AS name, d.version AS version"),
    "routes_express": ("MATCH (c:CpgCall {scan_id:$scan_id, file_path:$file}) "
                       "WHERE c.name IN ['get','post','put','delete','patch','all'] AND c.code STARTS WITH 'app.' "
                       "AND all(t IN $need WHERE c.code CONTAINS t) AND none(t IN $forbid WHERE c.code CONTAINS t) "
                       "RETURN toUpper(c.name) + ' ' + split(c.code, '\"')[1] AS item, c.file_path AS file, "
                       "c.line AS line"),
    "routes_django": ("MATCH (c:CpgCall {scan_id:$scan_id, file_path:$urls, name:'path'}) WHERE c.code CONTAINS 'views.' "
                      "WITH c, trim(split(split(split(c.code, 'views.')[1], ',')[0], ')')[0]) AS view "
                      "MATCH (:CpgMethod {scan_id:$scan_id, file_path:$views, name:view}) "
                      "OPTIONAL MATCH (d:CpgCall {scan_id:$scan_id, file_path:$views}) "
                      "WHERE d.name IN ['authentication_decorator','login_required','csrf_exempt'] "
                      "AND d.code CONTAINS '(def ' + view + '(' "
                      "WITH c, view, collect(DISTINCT d.name) AS decs "
                      "WHERE none(x IN decs WHERE x IN $forbid) AND all(x IN $need WHERE x IN decs) "
                      "RETURN CASE substring(c.code, 5, 1) WHEN '\"' THEN split(c.code, '\"')[1] "
                      "ELSE split(c.code, \"'\")[1] END AS item, c.file_path AS file, c.line AS line, view"),
}


class Graph:
    """Read-only access to one scan, through the same driver settings Orion uses."""

    def __init__(self, scan_id: str):
        from neo4j import GraphDatabase

        from orion import config
        self.scan_id = scan_id
        self._db = config.NEO4J_DATABASE
        self._driver = GraphDatabase.driver(config.NEO4J_URI, auth=config.NEO4J_AUTH)

    def rows(self, key: str, **params) -> list[dict]:
        with self._driver.session(database=self._db) as s:
            return s.execute_read(lambda tx: [r.data() for r in tx.run(CYPHER[key], scan_id=self.scan_id, **params)])

    def close(self) -> None:
        self._driver.close()


# ───────────────────────────── question families ─────────────────────────────

class Ctx:
    """What a family generator needs for one fixture."""

    def __init__(self, fixture: str, graph, tree: SourceTree, profile: dict, rng: random.Random):
        self.fixture, self.g, self.tree, self.profile, self.rng = fixture, graph, tree, profile, rng
        self._methods: list[dict] | None = None
        self._defined: set[str] | None = None

    @property
    def methods(self) -> list[dict]:
        if self._methods is None:
            self._methods = [m for m in self.g.rows("methods") if m.get("name") and m.get("file")]
        return self._methods

    def named(self) -> list[dict]:
        """First-party methods with a real name (not `<lambda>N`, `:program`, `<module>`, `<metaClassAdapter>`)."""
        return [m for m in self.methods if "<" not in m["name"] and not m["name"].startswith(":")
                and isinstance(m.get("line"), int)]

    def name_count(self, name: str) -> int:
        return sum(1 for m in self.methods if m["name"] == name)

    @property
    def defined(self) -> dict[str, int]:
        """Repo-defined function names -> definition count (lexical, or the graph's count if higher)."""
        if self._defined is None:
            d = defined_names(self.tree)
            for m in self.named():
                d[m["name"]] = max(d.get(m["name"], 0), self.name_count(m["name"]))
            self._defined = d
        return self._defined


def _q(ctx: Ctx, family: str, kind: str, text: str, gold: list[str], key: str, params: dict, check: dict) -> dict:
    return {"fixture": ctx.fixture, "family": family, "answer_kind": kind, "question": text,
            "gold": normalize(gold, kind), "gold_cypher": CYPHER[key], "gold_params": params, "check": check}


def _loc(file, line) -> str:
    return f"{file}:{line}"


def _evidence(tree: SourceTree, file: str, line: int) -> dict:
    return {"at": _loc(file, line), "source": (tree.line(file, line) or "").strip()[:160]}


def _shuffled(ctx: Ctx, pool: list) -> list:
    pool = list(pool)
    ctx.rng.shuffle(pool)
    return pool


class Drop(Exception):
    """A candidate whose gold could not be confirmed against the source (counted)."""


class Skip(Exception):
    """A candidate that is not a question at all: no gold rows, a degenerate flow, a repeated answer set
    (tallied apart from the drops)."""


def gen_lookup(ctx: Ctx, n: int):
    pool = [m for m in ctx.named() if len(m["name"]) >= 5 and m["name"] not in STOP_NAMES]
    for m in _shuffled(ctx, sorted(pool, key=lambda m: (m["file"], m["line"]))):
        def build(m=m):
            params = {"name": m["name"]}
            rows = ctx.g.rows("lookup", **params)
            gold = {(r["file"], r["line"]) for r in rows}
            lexical = def_sites(ctx.tree, m["name"])
            if not gold:
                raise Skip("no gold rows")
            for f, l in gold:
                if (f, l) not in lexical:
                    raise Drop(f"{_loc(f, l)} is not a definition of {m['name']} in the source")
            if lexical != gold:
                extra = sorted(_loc(*x) for x in lexical - gold)
                raise Drop(f"source defines {m['name']} at {extra[:3]} too")
            site = ("the line of its `def`" if ctx.tree.lang == "py" else
                    f"the line of the `function` keyword, or of the name in an assignment such as "
                    f"`{m['name']} = function ...` / `{m['name']} = (...) => ...`")
            text = (f"In which file, and on which line, is the function `{m['name']}` defined? Give each definition "
                    f"site as `path/to/file:LINE` ({site}). If it is defined more than once, list every site.")
            return _q(ctx, "lookup", "loc", text, [r["item"] for r in rows], "lookup", params,
                      {"items": [_evidence(ctx.tree, f, l) for f, l in sorted(gold)],
                       "completeness": "every lexical definition site of the name in the export equals the gold set"})
        yield m["name"], build


def gen_calls(ctx: Ctx, n: int):
    """Alternates callers and callees questions (one of each per fixture at the pilot size)."""
    counts = {r["full_name"]: r["n"] for r in ctx.g.rows("resolved")}
    pool = [m for m in ctx.named() if len(m["name"]) >= 5 and m["name"] not in STOP_NAMES
            and ctx.name_count(m["name"]) == 1]
    callers = [m for m in pool if counts.get(m["full_name"], 0) >= 1]
    callers.sort(key=lambda m: (-min(counts[m["full_name"]], 3), m["file"], m["line"]))
    callees = sorted(pool, key=lambda m: (m["file"], m["line"]))

    def caller_q(m):
        params = {"full_name": m["full_name"]}
        rows = ctx.g.rows("callers", **params)
        gold = {(r["file"], r["line"]) for r in rows}
        if not gold:
            raise Skip("no gold rows")
        if ctx.defined.get(m["name"], 0) > 1:
            raise Drop(f"{m['name']} is defined more than once in the source, so its calls are ambiguous")
        rx = call_rx(m["name"])
        for f, l in gold:
            if not rx.search(ctx.tree.line(f, l, code=True) or ""):
                raise Drop(f"{_loc(f, l)} has no `{m['name']}(` call in the source")
        lexical = call_sites(ctx.tree, m["name"])
        if lexical != gold:
            raise Drop(f"source calls {m['name']} at {sorted(_loc(*x) for x in lexical - gold)[:3]} too"
                       if lexical - gold else "a gold call site is a definition line")
        new = f" Count `new {m['name']}(...)` as a call." if ctx.tree.lang == "js" else ""
        text = (f"List every place in this repository where the function `{m['name']}` (defined at "
                f"{_loc(m['file'], m['line'])}) is called. Give each call site as `path/to/file:LINE`.{new} Do not "
                f"list the definition itself.")
        return _q(ctx, "calls", "loc", text, [r["item"] for r in rows], "callers", params,
                  {"items": [_evidence(ctx.tree, f, l) for f, l in sorted(gold)], "direction": "callers",
                   "completeness": "every lexical `name(` call line in the export equals the gold set"})

    def callee_q(m):
        params = {"full_name": m["full_name"]}
        rows = ctx.g.rows("callees", **params)
        gold = {r["item"] for r in rows}
        if not gold - {m["name"]}:
            raise Skip("no gold callee besides itself")
        common = sorted(gold & STOP_NAMES)
        if common:   # name-based RESOLVES_TO: `request.POST.get(` bound to a view's `get`
            raise Drop(f"gold callee names too common to trust: {common[:4]}")
        ext = body_extent(ctx.tree, m["file"], m["line"])
        if ext is None:
            raise Drop("cannot read the function body")
        code = ctx.tree.code(m["file"], keep_strings=False)
        own = def_patterns(m["name"], ctx.tree.lang)
        lexical = set()
        for name in ctx.defined:
            if name in STOP_NAMES:
                continue
            rx = call_rx(name)
            for i in range(ext[0], ext[1] + 1):
                s = code[i - 1]
                if rx.search(s) and not (name == m["name"] and any(p.search(s) for p in own)):
                    lexical.add(name)
                    break
        for r in rows:
            if not (ext[0] <= r["line"] <= ext[1]) or r["file"] != m["file"]:
                raise Drop(f"gold call {r['item']} at {_loc(r['file'], r['line'])} is outside the body {ext}")
            if not call_rx(r["item"]).search(ctx.tree.line(r["file"], r["line"], code=True) or ""):
                raise Drop(f"{_loc(r['file'], r['line'])} has no `{r['item']}(` call in the source")
        ambiguous = sorted(x for x in lexical | gold if ctx.defined.get(x, 0) > 1)
        if ambiguous:
            raise Drop(f"callee names defined more than once in the repo: {ambiguous[:4]}")
        if lexical != gold:
            raise Drop(f"body calls {sorted(lexical - gold)[:4]} too" if lexical - gold
                       else f"gold callees {sorted(gold - lexical)[:4]} not called lexically")
        new = " and count `new X(...)` as a call to X" if ctx.tree.lang == "js" else ""
        text = (f"Which functions defined in this repository does `{m['name']}` (defined at "
                f"{_loc(m['file'], m['line'])}) call? Include calls made inside functions nested within its body"
                f"{new}. Ignore built-in, library and third-party functions. Answer with the called functions' names.")
        return _q(ctx, "calls", "name", text, sorted(gold), "callees", params,
                  {"items": [dict(_evidence(ctx.tree, r["file"], r["line"]), callee=r["item"]) for r in rows],
                   "direction": "callees", "body": list(ext),
                   "completeness": "the repo-defined names called lexically inside the body equal the gold set"})

    # Alternate the two directions; once one has its share of passing questions it waits while the other still
    # has candidates, so the picks are one callers and one callees question whenever both pools allow it.
    pools = {"callers": iter(callers), "callees": iter(_shuffled(ctx, callees))}
    builders = {"callers": caller_q, "callees": callee_q}
    passed = {"callers": 0, "callees": 0}
    quota = (n + 1) // 2
    while pools:
        for d in ("callers", "callees"):
            if d not in pools:
                continue
            other = "callees" if d == "callers" else "callers"
            if passed[d] >= quota and other in pools and passed[other] < quota:
                continue
            m = next(pools[d], None)
            if m is None:
                del pools[d]
                continue

            def build(m=m, d=d):
                q = builders[d](m)
                passed[d] += 1
                return q
            yield f"{d}:{m['name']}", build


def _sink_calls(profile: dict, kind: str) -> list[str]:
    """The profile's sink hints for `kind` that name a CALL (`$where` is an operator inside find's argument)."""
    return [str(h) for h in profile["sink_hints"].get(kind, ()) if re.fullmatch(r"[A-Za-z_]\w*", str(h))]


def _sink_rx(profile: dict, kind: str) -> list[re.Pattern]:
    hints = {str(h): h for h in profile["sink_hints"].get(kind, ())}
    return [getattr(hints[n], "rx", None) or call_rx(n) for n in _sink_calls(profile, kind)]


def _sink_names(profile: dict, kind: str) -> str:
    names = [f"`{h}`" for h in _sink_calls(profile, kind)]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " or " + names[-1]


def _entry_candidates(ctx: Ctx) -> list[dict]:
    """Orion's entry points with a unique real name (so an answer by name is unambiguous)."""
    eps = [e for e in ctx.g.rows("entrypoints") if e.get("name") and "<" not in e["name"]
           and isinstance(e.get("line"), int)]
    names = [e["name"] for e in eps]
    return sorted((e for e in eps if names.count(e["name"]) == 1 and ctx.name_count(e["name"]) == 1),
                  key=lambda e: (e["file"], e["line"]))


def gen_entry_sink(ctx: Ctx, n: int):
    cands = _entry_candidates(ctx)
    present = {r["kind"] for r in ctx.g.rows("sink_kinds")}
    pool = []
    for kind in (k for k in ctx.profile["sink_hints"] if k in present and _sink_calls(ctx.profile, k)):
        params = {"kind": kind, "candidates": [e["full_name"] for e in cands],
                  "sink_names": _sink_calls(ctx.profile, kind)}
        rows = ctx.g.rows("entry_sink", **params) if cands else []
        pool.append((kind, params, rows))
    # smallest answer set first: a 50-name answer is a different task from a 3-name one
    pool.sort(key=lambda t: (not t[2], len({r["item"] for r in t[2]})))
    for kind, params, rows in pool:
        def build(kind=kind, params=params, rows=rows):
            gold = {r["item"] for r in rows}
            if not gold:
                raise Skip("graph gold empty: no listed entry point reaches this sink kind")
            if len(gold) > 20:
                raise Skip("answer set larger than 20 names")
            pats = _sink_rx(ctx.profile, kind)
            for r in rows:
                if not any(p.search(ctx.tree.line(r["file"], r["line"], code=True) or "") for p in pats):
                    raise Drop(f"sink {_loc(r['file'], r['line'])} has no {kind} call in the source")
            lexical, evidence = set(), []
            for e in cands:
                ext = body_extent(ctx.tree, e["file"], e["line"])
                if ext is None:
                    raise Drop(f"cannot read the body of entry point {e['name']}")
                code = ctx.tree.code(e["file"], keep_strings=False)
                hits = [i for i in range(ext[0], ext[1] + 1) if any(p.search(code[i - 1]) for p in pats)]
                if hits:
                    lexical.add(e["name"])
                    evidence.append(_evidence(ctx.tree, e["file"], hits[0]) | {"entry_point": e["name"]})
            if lexical != gold:
                raise Drop(f"entry points with a {kind} call in their body {sorted(lexical - gold)[:4]} lack a graph "
                           f"flow" if lexical - gold else f"gold {sorted(gold - lexical)[:4]} have no {kind} call")
            listing = "\n".join(f"  - {e['name']} ({_loc(e['file'], e['line'])})" for e in cands)
            # rev 2: the names are the WHOLE sink list (the gold is `s.name IN $sink_names`). Written as examples
            # ("a code_exec sink call (`eval`, ...)"), every model in the pilot also counted subprocess.Popen.
            first = _sink_calls(ctx.profile, kind)[0]
            text = (f"Orion marks these functions as the repository's entry points (where untrusted input enters):\n"
                    f"{listing}\nWhich of them contain -- in their own body or in a function nested inside it -- a "
                    f"call to {_sink_names(ctx.profile, kind)} (Orion's {kind} sinks) whose argument carries "
                    f"untrusted input (HTTP request data, or the entry point's own parameters)? Only those names "
                    f"count: a method of any object with that name counts (`x.{first}(...)`), a call to any other "
                    f"function does not, even one with the same effect. Answer with entry-point names from the list.")
            return _q(ctx, "entry_sink", "name", text, sorted(gold), "entry_sink", params,
                      {"items": evidence, "kind": kind,
                       "completeness": "the listed entry points whose body has a lexical sink call equal the gold "
                                       "set (taint itself is the graph's claim)"})
        yield kind, build


_ASSIGN = {"py": re.compile(r"^\s*([A-Za-z_][\w.]*(?:\s*,\s*[A-Za-z_][\w.]*)*)\s*[+\-*/]?=(?!=)\s*(.*)$"),
           "js": re.compile(r"^\s*(?:(?:const|let|var)\s+)?([A-Za-z_$][\w$.]*(?:\s*,\s*[A-Za-z_$][\w$.]*)*|\{[^}]*\}|"
                            r"\[[^\]]*\])\s*[+\-*/]?=(?![=>])\s*(.*)$")}
_HAS_CALL = re.compile(r"[\w$\])]\s*\(")


def _assignment(line: str, lang: str) -> tuple[set[str], str] | None:
    m = _ASSIGN[lang].match(line)
    if not m:
        return None
    lhs = {x.split(".")[0] for x in re.findall(r"[A-Za-z_$][\w$.]*", m.group(1))} - {"const", "let", "var"}
    return lhs, m.group(2)


def _rebinds(line: str, lang: str, name: str) -> bool:
    """Whether the assignment on `line` binds `name` itself, not only an attribute of it (`contrib.userId = ...`
    changes `contrib` but keeps its earlier value)."""
    m = _ASSIGN[lang].match(line)
    return bool(m) and name in re.findall(r"[A-Za-z_$][\w$.]*", m.group(1))


# a function literal passed as a call argument: `dao.get(id, (err, rows) => {`, `find(q, function (e, r) {`
_CALLBACK = re.compile(r"[\w$\])]\s*\((?:[^()]|\([^()]*\))*?(?:\bfunction\s*[\w$]*\s*\(([^()]*)\)|\(([^()]*)\)\s*=>|"
                       r"(?<![\w$.])([A-Za-z_$][\w$]*)\s*=>)")


def _callbacks(code: list[str], ln: int) -> list[tuple[set[str], int]]:
    """JS function literals passed as a call argument on line `ln`: each one's parameter names and the last line of
    its body (the matching brace, or the statement's end for an expression body)."""
    s, out = code[ln - 1], []
    for m in _CALLBACK.finditer(s):
        params = set(re.findall(r"[A-Za-z_$][\w$]*", next(g for g in m.groups() if g is not None)))
        k = s.find("{", m.end())
        last = (_match_brace(code, ln - 1, k) if k >= 0 and not s[m.end():k].strip()
                else ln + _statement(code, ln).count("\n"))
        out.append((params, last))
    return out


_KEYWORDS = frozenset({"const", "let", "var", "return", "new", "await", "if", "else", "for", "while", "in", "of",
                       "not", "and", "or", "is", "None", "True", "False", "null", "true", "false", "this", "self",
                       "lambda", "function", "typeof", "async", "yield"})


def _reads(text: str) -> set[str]:
    """Variable names `text` reads: identifiers that are not called, not an attribute after a dot (a JS spread,
    `...contrib`, is a read), not keywords."""
    return {m.group(1) for m in re.finditer(r"(?<![\w$])(?<![^.]\.)([A-Za-z_$][\w$]*)(?![\w$])(?!\s*\()", text)
            } - _KEYWORDS


def _statement(code: list[str], line: int, max_lines: int = 20) -> str:
    """The text from `line` until its brackets balance (a call that spans lines), at most `max_lines` lines."""
    depth, out = 0, []
    for i in range(line - 1, min(len(code), line - 1 + max_lines)):
        s = code[i]
        out.append(s)
        depth += sum(s.count(c) for c in "([{") - sum(s.count(c) for c in ")]}")
        if depth <= 0:
            break
    return "\n".join(out)


def _enclosing_start(tree: SourceTree, rel: str, line: int) -> int:
    """First line of the OUTERMOST function in `rel` whose body holds `line` (1 at module level)."""
    code = tree.code(rel, keep_strings=False)
    best = None
    for i in range(line, 0, -1):
        s = code[i - 1]
        if (re.match(r"^\s*(?:async\s+)?def\s", s) if tree.lang == "py" else re.search(r"\bfunction\b|=>", s)):
            ext = body_extent(tree, rel, i)
            if ext and ext[0] <= line <= ext[1]:
                best = i
    return best or 1


def flow_gaps(tree: SourceTree, rel: str, gold_lines: set[int], sink_line: int) -> list[int]:
    """Lines a lexical def-use pass puts on the flow into `sink_line` but the gold lacks. Two passes, both intra-file
    and first-line-only, so they can only make a question drop, never add gold:
      * backward, from the variables the sink reads, through their latest assignment in the enclosing function,
        recursively: an assignment whose right side has a call is a flow line (`name = request.POST.get('name')`
        feeding `sql_query = "..." + name` feeding `print(sql_query)`). An attribute assignment (`contrib.userId =
        userId`) adds its reads but the search goes on for the variable's own binding; in JS a variable bound as the
        parameter of a callback passed to a call is that call's result, so the call's line is a flow line
        (`dao.getByUserId(userId, (error, contrib) => {` feeding `res.render(..., {...contrib})`: FLOWS_TO follows
        return values only, so the graph never has it);
      * forward, from the first gold line to the sink: an assignment whose right side has a call and reads a
        variable assigned on an earlier flow line (`res = res.decode()` between `res, err = command_out(...)` and
        `str(res)`)."""
    code = tree.code(rel, keep_strings=False)
    if not (1 <= sink_line <= len(code)):
        return []
    lang = tree.lang
    expected: set[int] = set()
    start = _enclosing_start(tree, rel, sink_line)
    stmt = _statement(code, sink_line)
    sink = _assignment(stmt.split("\n", 1)[0], lang)
    work = [(sink_line, _reads(stmt.split("=", 1)[1] if sink else stmt))]
    seen: set[int] = set()
    while work:
        at, names = work.pop()
        for v in names:
            for ln in range(at - 1, start - 1, -1):
                a = _assignment(code[ln - 1], lang)
                if a and v in a[0]:
                    if ln not in seen:
                        seen.add(ln)
                        if _HAS_CALL.search(a[1]):
                            expected.add(ln)
                        work.append((ln, _reads(a[1])))
                    if _rebinds(code[ln - 1], lang, v):
                        break
                    continue
                if lang == "js" and any(v in ps and last >= at for ps, last in _callbacks(code, ln)):
                    expected.add(ln)
                    break
    tainted: set[str] = set()
    for ln in range(min(gold_lines | {sink_line}), sink_line):
        a = _assignment(code[ln - 1], lang)
        if ln in gold_lines:
            if a:
                tainted |= a[0]
            continue
        if a and _HAS_CALL.search(a[1]) and _reads(a[1]) & tainted:
            expected.add(ln)
            tainted |= a[0]
    return sorted(expected - gold_lines - {sink_line})


def gen_flow_path(ctx: Ctx, n: int):
    seen: set[str] = set()
    for f in ctx.g.rows("flows"):
        if f["kind"] not in ctx.profile["sink_hints"] or not isinstance(f.get("line"), int):
            continue
        at = _loc(f["file"], f["line"])
        if at in seen:
            continue
        seen.add(at)

        def build(f=f, at=at):
            params = {"sink_uid": f["sink_uid"]}
            rows = ctx.g.rows("flow_path", **params)
            lines = {(r["file"], r["line"]) for r in rows}
            if len(lines) < 2:
                raise Skip("fewer than two lines on the flow")
            # the sink itself must be a call of its kind (as in entry_sink): the plain `log` hint also files
            # `app.get("/login", ...)` as a log sink, and a question built on that would assert a false premise
            if not any(p.search(ctx.tree.line(f["file"], f["line"], code=True) or "")
                       for p in _sink_rx(ctx.profile, f["kind"])):
                raise Drop(f"sink {at} has no {f['kind']} call in the source")
            for r in rows:
                if r["file"] != f["file"]:
                    raise Drop(f"the flow leaves {f['file']}")
                src = ctx.tree.line(r["file"], r["line"], code=True)
                if not src or not src.strip():
                    raise Drop(f"{_loc(r['file'], r['line'])} is empty in the source")
                nm = r.get("name") or ""
                if not nm.startswith("<") and not re.search(rf"(?<![\w$]){re.escape(nm)}\b", src):
                    raise Drop(f"{_loc(r['file'], r['line'])} has no `{nm}` call in the source")
            gaps = flow_gaps(ctx.tree, f["file"], {b for _, b in lines}, f["line"])
            if gaps:
                raise Drop(f"the source also carries the data through lines {gaps[:4]}, which the gold lacks")
            code0 = (ctx.tree.line(f["file"], f["line"]) or "").strip()
            text = (f"In {f['file']}, the call on line {f['line']} -- `{code0[:120]}` -- is a {f['kind']} sink for "
                    f"untrusted data. List every line holding a function or method call whose result is passed -- "
                    f"directly, or through variables and further calls -- into the arguments of this sink call. Give "
                    f"each as `path/to/file:LINE`, and include the sink's own line.")
            return _q(ctx, "flow_path", "loc", text, [r["item"] for r in rows], "flow_path", params,
                      {"items": [_evidence(ctx.tree, a, b) for a, b in sorted(lines)], "rank": f["rank"],
                       "kind": f["kind"], "completeness": "a lexical def-use pass between the first gold line and "
                                                          "the sink finds no flow line the gold lacks (first lines "
                                                          "of assignments and JS callback parameters only; "
                                                          "approximate)"})
        yield f"rank{f['rank']}:{at}", build


def manifest_deps(tree: SourceTree, manifest: str) -> dict[str, str] | None:
    """The manifest's own declarations, parsed independently of orion/graph/deps.py: normalised `name@version` ->
    the version text as declared (operator included, e.g. `^4.13.4`, `==4.2`)."""
    text = tree.text(manifest)
    if text is None:
        return None
    out: dict[str, str] = {}
    if manifest.endswith("package.json"):
        data = json.loads(text)
        for sec in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
            for name, ver in (data.get(sec) or {}).items():
                out.setdefault(norm_dep(f"{name}@{ver}"), str(ver))
        return out
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        m = re.match(r"^([A-Za-z0-9._-]+)\s*(?:\[[^\]]*\])?\s*((?:==|~=|>=|<=|!=|<|>)\s*[^;,\s]+)?", line)
        if m:
            spec = re.sub(r"\s+", "", m.group(2) or "")
            out.setdefault(norm_dep(f"{m.group(1)}@{spec}"), spec)
    return out


_DEP_VARIANTS = (
    ("all", None, "every package declared"),
    ("major0", r"^[=^~<>!\s]*v?0(\..*)?$", "the packages whose declared version (ignoring range operators such as "
                                            "^ ~ ==) has major version 0, i.e. starts with `0.`"),
    ("exact", r"^=*\s*v?\d+(\.\d+)*$", "the packages pinned to one exact numeric version (no range operator such "
                                       "as ^ or ~, no tag or URL)"),
)


def gen_deps(ctx: Ctx, n: int):
    manifest = ctx.profile["manifest"]
    golds: list[set] = []
    for name, rx, what in _DEP_VARIANTS:
        def build(name=name, rx=rx, what=what):
            params = {"version_rx": rx}
            rows = ctx.g.rows("deps", **params)
            gold = set(normalize([r["item"] for r in rows], "dep"))
            if not gold:
                raise Skip("no gold rows")
            if any(gold == g for g in golds):
                raise Skip("same answer set as an earlier deps question")
            declared = manifest_deps(ctx.tree, manifest)
            if declared is None:
                raise Drop(f"{manifest} is not in the export")
            if rx is not None:   # the variant's regex on the declared text, as the Cypher applies it to d.version
                declared = {d: v for d, v in declared.items() if re.fullmatch(rx, v)}
            if set(declared) != gold:
                raise Drop(f"{manifest} declares {sorted(set(declared) ^ gold)[:4]} differently")
            golds.append(gold)
            text = (f"From {ctx.profile['manifest_desc']}, list {what}. Give each as `name@version` with the version "
                    f"as declared (e.g. `{ctx.profile['dep_example']}`; a range operator such as ^, ~ or == may be "
                    f"kept or dropped).")
            return _q(ctx, "deps", "dep", text, sorted(gold), "deps", params,
                      {"items": [{"at": manifest, "declared": d} for d in sorted(gold)], "variant": name,
                       "completeness": f"an independent parse of {manifest} gives the same set"})
        yield name, build


_EXPRESS_ROUTE = re.compile(r"\bapp\.(get|post|put|delete|patch|all)\s*\(\s*([\"'`])([^\"'`]*)\2")
_DJANGO_PATH = re.compile(r"\bpath\s*\(\s*([\"'])([^\"']*)\1\s*,\s*views\.(\w+)")


def _express_routes(tree: SourceTree, rel: str) -> list[tuple[str, int, str]]:
    """(item, line, call text) for every app.METHOD("path", ...) registration outside comments."""
    code = tree.code(rel)
    out = []
    for i, s in enumerate(code, 1):
        for m in _EXPRESS_ROUTE.finditer(s):
            depth, j, text = 0, i - 1, ""   # the whole call, which may span lines
            start = m.start()
            while j < len(code):
                seg = code[j][start:] if j == i - 1 else code[j]
                for ch in seg:
                    text += ch
                    if ch == "(":
                        depth += 1
                    elif ch == ")":
                        depth -= 1
                        if depth == 0:
                            break
                if depth == 0:
                    break
                text += "\n"
                j += 1
            out.append((norm_route(f"{m.group(1)} {m.group(3)}"), i, text))
    return out


def _django_views(tree: SourceTree, rel: str) -> dict[str, list[tuple[int, set[str]]]]:
    """Top-level `def`s of `rel` -> [(def line, decorator names)]."""
    code = tree.code(rel)
    out: dict[str, list] = {}
    for i, s in enumerate(code, 1):
        m = re.match(r"^def\s+(\w+)\s*\(", s)
        if not m:
            continue
        decs, j = set(), i - 2
        while j >= 0 and (code[j].strip().startswith("@") or not code[j].strip()):
            if code[j].strip().startswith("@"):
                decs.add(re.match(r"@([\w.]+)", code[j].strip()).group(1).split(".")[-1])
            j -= 1
        out.setdefault(m.group(1), []).append((i, decs))
    return out


def gen_routes(ctx: Ctx, n: int):
    r = ctx.profile["routes"]
    for name, need, forbid, what in r["variants"]:
        def build(name=name, need=need, forbid=forbid, what=what):
            if r["style"] == "express":
                params = {"file": r["file"], "need": list(need), "forbid": list(forbid)}
                rows = ctx.g.rows("routes_express", **params)
                lexical = {item for item, _, text in _express_routes(ctx.tree, r["file"])
                           if all(t in text for t in need) and not any(t in text for t in forbid)}
                gold = set(normalize([x["item"] for x in rows], "route"))
                for x in rows:
                    src = ctx.tree.line(x["file"], x["line"], code=True) or ""
                    if not _EXPRESS_ROUTE.search(src):
                        raise Drop(f"{_loc(x['file'], x['line'])} registers no route in the source")
                text = (f"In {r['file']}, routes are registered with `app.get(...)` / `app.post(...)`. Which of these "
                        f"route registrations {what}? Ignore commented-out code. Answer as `METHOD /path` "
                        f"(e.g. `GET /login`).")
            else:
                params = {"urls": r["urls"], "views": r["views"], "need": list(need), "forbid": list(forbid)}
                rows = ctx.g.rows("routes_django", **params)
                views = _django_views(ctx.tree, r["views"])
                lexical = set()
                for i, s in enumerate(ctx.tree.code(r["urls"]), 1):
                    for m in _DJANGO_PATH.finditer(s):
                        defs = views.get(m.group(3))
                        if not defs:
                            continue
                        if len(defs) > 1:
                            raise Drop(f"view {m.group(3)} is defined {len(defs)} times in {r['views']}")
                        decs = defs[0][1]
                        if all(t in decs for t in need) and not any(t in decs for t in forbid):
                            lexical.add(norm_route(m.group(2)))
                gold = set(normalize([x["item"] for x in rows], "route"))
                for x in rows:
                    src = ctx.tree.line(x["file"], x["line"], code=True) or ""
                    if f"views.{x['view']}" not in src:
                        raise Drop(f"{_loc(x['file'], x['line'])} does not route to views.{x['view']} in the source")
                text = (f"{r['urls']} maps URL patterns to views with `path('<route>', views.<view>, ...)`. Consider "
                        f"the patterns whose view is a function defined in {r['views']}. Which of them point at a "
                        f"view that {what}? Answer with the route strings as written in `path(...)` (use `/` for the "
                        f"empty route).")
            if not gold:
                raise Skip("graph gold empty")
            if lexical != gold:
                raise Drop(f"source routes differ: {sorted(lexical ^ gold)[:4]}")
            return _q(ctx, "routes_noauth", "route", text, sorted(gold),
                      "routes_express" if r["style"] == "express" else "routes_django", params,
                      {"items": [_evidence(ctx.tree, x["file"], x["line"]) | {"route": x["item"]} for x in rows],
                       "variant": name, "completeness": "a lexical parse of the route file(s) gives the same set"})
        yield name, build


GENERATORS = {"lookup": gen_lookup, "calls": gen_calls, "entry_sink": gen_entry_sink,
              "flow_path": gen_flow_path, "deps": gen_deps, "routes_noauth": gen_routes}

FIXTURE_PROFILES = {
    "nodegoat": {
        "lang": "js", "profile": "express", "manifest": "package.json",
        "manifest_desc": "package.json (dependencies, devDependencies, peerDependencies and optionalDependencies)",
        "dep_example": "express@^4.13.4",
        "routes": {"style": "express", "file": "app/routes/index.js", "variants": [
            ("no-login", (), ("isLoggedIn",), "do NOT pass the `isLoggedIn` middleware"),
            ("login-no-admin", ("isLoggedIn",), ("isAdmin",), "pass `isLoggedIn` but NOT `isAdmin`"),
        ]},
    },
    "pygoat": {
        "lang": "py", "profile": "generic", "manifest": "requirements.txt",
        "manifest_desc": "the repository's top-level requirements.txt", "dep_example": "Django@4.2",
        "routes": {"style": "django", "urls": "introduction/urls.py", "views": "introduction/views.py", "variants": [
            ("no-auth", (), ("authentication_decorator", "login_required"),
             "has neither the `@authentication_decorator` nor the `@login_required` decorator"),
            ("no-auth-csrf-exempt", ("csrf_exempt",), ("authentication_decorator", "login_required"),
             "has the `@csrf_exempt` decorator but neither `@authentication_decorator` nor `@login_required`"),
        ]},
    },
}


def _profile(fixture: str) -> dict:
    from orion.graph import profiles
    p = dict(FIXTURE_PROFILES[fixture])
    p["sink_hints"] = {"express": profiles.EXPRESS, "generic": profiles.GENERIC}[p["profile"]].sink_hints
    return p


def generate(fixture: str, graph, tree: SourceTree, *, per_family: int = PER_FAMILY, seed: int = SEED,
             families=FAMILIES, profile: dict | None = None, log=print) -> tuple[list[dict], list[dict], dict]:
    """(questions, drops, skips) for one fixture. Each family's candidates are tried in a seeded order until
    `per_family` pass the gold check. A candidate is a question only when the graph gives it a non-empty gold set;
    such a question whose gold fails the source check is a drop. A candidate that is no question at all (empty
    graph gold, a one-line flow, a repeated answer set) is a skip, tallied by "family: reason"."""
    profile = profile or _profile(fixture)
    questions, drops, skips = [], [], {}
    for fam in families:
        ctx = Ctx(fixture, graph, tree, profile, random.Random(f"{seed}:{fixture}:{fam}"))
        got = 0
        for label, build in GENERATORS[fam](ctx, per_family):
            if got >= per_family:
                break
            try:
                q = build()
            except Skip as s:
                key = f"{fam}: {s}"
                skips[key] = skips.get(key, 0) + 1
                continue
            except Drop as d:
                drops.append({"fixture": fixture, "family": fam, "candidate": label, "reason": str(d)})
                continue
            got += 1
            q["id"] = f"{fixture}-{fam}-{got}"
            questions.append(q)
        if got < per_family:
            log(f"{fixture}/{fam}: only {got} of {per_family} questions passed the gold check")
    return questions, drops, skips


# ───────────────────────────── building the inputs (WSL) ─────────────────────────────

def _git(repo: str | Path, *args) -> str:
    # safe.directory: from WSL the Windows checkout (/mnt/c/...) is owned by another uid; this only reads it
    return subprocess.run(["git", "-c", "safe.directory=*", "-C", str(repo), *args], capture_output=True, text=True,
                          check=True).stdout.strip()


def export_tree(repo: Path, cache: Path) -> Path:
    """`git archive HEAD` of `repo` into `cache/<name>-<sha12>` (reused when present)."""
    sha = _git(repo, "rev-parse", "HEAD")
    dest = cache / f"{repo.name}-{sha[:12]}"
    if not (dest / ".graphqa-export").exists():
        dest.mkdir(parents=True, exist_ok=True)
        tar = subprocess.run(["git", "-C", str(repo), "archive", "--format=tar", "HEAD"], capture_output=True, check=True)
        subprocess.run(["tar", "-x", "-C", str(dest)], input=tar.stdout, check=True)
        (dest / ".graphqa-export").write_text(sha + "\n")
    return dest


def ensure_scan(repo: Path, *, build: bool, log=print) -> str:
    """A usable claude-engine scan of `repo` (built with stream=True + the semantic index when --build), else raise."""
    from orion import embed, graph_build
    from orion.graph import scans
    sid = graph_build.scan_id_for(str(repo))
    why = scans.check_usable(sid, engine="claude", repo=str(repo))
    if why:
        if not build:
            raise SystemExit(f"{repo}: {why} (pass --build to build it)")
        log(f"building {repo} (scan {sid})")
        graph_build.build(str(repo), None, None, stream=True, on_batch=lambda b: embed.index(str(repo), sid, batch=b))
        why = scans.check_usable(sid, engine="claude", repo=str(repo))
        if why:
            raise SystemExit(f"{repo}: still unusable after the build: {why}")
    g = Graph(sid)
    try:
        with g._driver.session(database=g._db) as s:
            chunks = s.run("MATCH (c:Chunk {scan_id:$sid}) RETURN count(c) AS n", sid=sid).single()["n"]
    finally:
        g.close()
    if not chunks:
        if not build:
            raise SystemExit(f"{repo}: scan {sid} has no semantic index (pass --build to index it)")
        log(f"indexing {repo} for semantic_search")
        embed.index(str(repo), sid)
    return sid


def cmd_questions(args) -> int:
    cache = Path(args.cache).expanduser()
    out = {"status": "proposed",
           "note": "AI-written templates and picks; a human must review every question before a result counts.",
           "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "orion_commit": _git(ROOT, "rev-parse", "HEAD"), "seed": args.seed, "per_family": args.per_family,
           "fixtures": {}, "questions": [], "drops": {}}
    all_drops = []
    for fixture, rel in FIXTURES.items():
        repo = (Path(args.fixtures_root).expanduser() / rel).resolve() if args.fixtures_root else ROOT / rel
        sid = ensure_scan(repo, build=args.build)
        export = export_tree(repo, cache)
        prof = _profile(fixture)
        tree = SourceTree(export, prof["lang"])
        g = Graph(sid)
        try:
            gfiles = {r["file"] for r in g.rows("files")}
            missing = sorted(f for f in gfiles if not (export / f).is_file())
            if missing:
                print(f"{fixture}: {len(missing)} graph files are not tracked in the export, e.g. {missing[:3]}")
            qs, drops, skips = generate(fixture, g, tree, per_family=args.per_family, seed=args.seed, profile=prof)
        finally:
            g.close()
        out["fixtures"][fixture] = {"repo": str(repo), "commit": _git(repo, "rev-parse", "HEAD"), "scan_id": sid,
                                    "export": str(export), "lang": prof["lang"], "graph_files": len(gfiles),
                                    "graph_files_not_tracked": missing, "skipped_candidates": skips}
        out["questions"].extend(dict(q, status="proposed", scan_id=sid) for q in qs)
        all_drops.extend(drops)
        print(f"{fixture}: {len(qs)} questions, {len(drops)} candidates dropped, "
              f"{sum(skips.values())} skipped (not a question)")
    by_family: dict[str, int] = {}
    for d in all_drops:
        by_family[d["family"]] = by_family.get(d["family"], 0) + 1
    out["drops"] = {"total": len(all_drops), "by_family": by_family, "detail": all_drops}
    path = Path(args.out)
    old = (json.loads(path.read_text(encoding="utf-8")).get("questions") or []) if path.exists() else []
    carry_revs(out["questions"], old)
    old_rev = {p["id"]: p.get("rev", 1) for p in old}
    changed = sorted(q["id"] for q in out["questions"] if q["id"] in old_rev and q["rev"] != old_rev[q["id"]])
    if changed:
        print(f"reworded or replaced since the last file (new rev, their sessions run again): {', '.join(changed)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {len(out['questions'])} questions ({len(all_drops)} dropped) to {path}")
    return 0


# ───────────────────────────── the two access paths ─────────────────────────────

ANSWER_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["answer"],
                 "properties": {"answer": {"type": "array", "items": {"type": "string"}}}}

_FORMAT = {
    "loc": "each item a location `path/to/file:LINE`, the path relative to the repository root",
    "name": "each item a bare function name, without arguments or a module/class prefix",
    "dep": "each item `name@version`",
    "route": "each item a route as asked",
}


def graph_system(scan_id: str) -> str:
    from orion.strategies import SCHEMA
    return f"""You answer questions about the structure of ONE code repository. You cannot read its files: you see it
ONLY through Orion's code graph, with three read-only tools:
  - mcp__orion__run_cypher(query, scan_id): run ONE read-only Cypher query. Every query must be anchored to the
    scan: filter with `{{scan_id:$scan_id}}` (or `WHERE x.scan_id = $scan_id`) and pass scan_id="{scan_id}".
  - mcp__orion__semantic_search(query, scan_id, k): code chunks nearest to `query` by meaning; pass
    scan_id="{scan_id}".
  - mcp__orion__get_schema(): the graph's labels and properties.

{SCHEMA}

File paths in the graph (`file_path`) are relative to the repository root; `line` is 1-based. Answer only from
what the tools return. When done, return the required JSON object: {{"answer": [<strings>]}}. Return an empty
list if you find nothing; never guess an item you did not see."""


def grep_system(repo: str) -> str:
    return f"""You answer questions about the structure of ONE code repository, checked out at {repo}. You have ONLY
these read-only tools: Read, Grep and Glob. There is no code graph and no database: read the actual source.
Give file paths relative to the repository root ({repo}); lines are 1-based. Answer only from source you read.
When done, return the required JSON object: {{"answer": [<strings>]}}. Return an empty list if you find nothing;
never guess an item you did not see."""


def budget_note(budget: int) -> str:
    """Told to both paths when a question set states a budget, so a long session ends in its best answer, not nothing."""
    return (f"\n\nYou have at most {budget} rounds of tool calls. Make several calls in one round where you can. When "
            "you are close to the limit, stop and return your best answer from what you have seen: a session that runs "
            "out returns nothing.")


def question_message(q: dict) -> str:
    return f"{q['question']}\n\nAnswer format: a JSON object {{\"answer\": [...]}}, {_FORMAT[q['answer_kind']]}."


def carry_revs(new: list[dict], old: list[dict]) -> None:
    """Give each question a `rev`: the previous file's when what a model is shown (the message and the scan) is
    unchanged, else one more (1 for a new id). Sessions are keyed by (id, rev), so a reworded or replaced question
    runs again and its old sessions stop counting, while an unchanged one keeps its sessions."""
    prev = {q["id"]: q for q in old}
    for q in new:
        p = prev.get(q["id"])
        if p is None:
            q["rev"] = 1
        else:
            same = (question_message(p), p.get("scan_id")) == (question_message(q), q.get("scan_id"))
            q["rev"] = p.get("rev", 1) + (0 if same else 1)


def _skey(qid: str, rev, model: str, path: str) -> tuple:
    return qid, rev or 1, model, path       # records written before revs existed are rev 1


# A session cut off mid-way ends "You've hit your 5-hour limit · resets 12:10am" (captured 2026-11-02, CLI 2.1.316), which
# eval.run's signatures miss: without this, a re-run cut off the same way would be scored as the model's failure.
_PLAN_LIMIT = re.compile(r"· resets \d|\bweekly limit", re.I)      # the error tail can cut anything before "· resets"


def _limit_hit(error: str, usage: list[dict]) -> bool:
    """A usage limit, as eval.run classifies one, in the error text run_agent salvaged (the stdout tail of a
    non-zero exit). An `is_error` result that exits 0 reaches us only as its subtype, so a session whose result
    is an error and whose usage shows no output token at all (the model never ran) is treated the same way:
    paused and re-run, never scored as the model's failure."""
    from eval.run import classify_match
    if classify_match("failed", error.split(" | "), source="claude")[0] == "aborted":
        return True
    if _PLAN_LIMIT.search(error):
        return True
    return (error.startswith("agent reported error") and not any(s in error for s in _OUT_OF_TURNS)
            and all(not u.get("output_tokens") for u in usage))


def run_session(q: dict, fixture: dict, model: str, path: str, *, agent=None, max_turns: int = MAX_TURNS,
                budget: int | None = None, timeout: int = TIMEOUT, effort: str = EFFORT) -> dict:
    """One `claude -p` session for question `q` through `path`. Returns the session record (never raises)."""
    from orion import claude_cli, config
    agent = agent or claude_cli.run_agent
    usage: list[dict] = []
    tools = {"cypher": 0}

    def on_event(ev):
        if ev.get("event") == "usage":
            try:
                usage.append(json.loads(ev.get("detail") or "{}"))
            except ValueError:
                pass
        elif ev.get("event") == "tool":
            tools["cypher"] += 1

    config.MODEL = model    # claude_cli reads config.MODEL; one model is run at a time (see cmd_run)
    if path == "graph":
        kw = dict(system=graph_system(q["scan_id"]), extra_allowed=("mcp__orion__get_schema",), use_mcp=True)
    else:
        kw = dict(system=grep_system(fixture["export"]), add_dir=fixture["export"],
                  extra_allowed=("Read", "Grep", "Glob"), use_mcp=False)
    if budget:
        kw["system"] += budget_note(budget)
    t0 = time.monotonic()
    result = agent(str(uuid.uuid4()), message=question_message(q), json_schema=ANSWER_SCHEMA, on_event=on_event,
                   max_turns=max_turns, timeout=timeout, retries=0, effort=effort, **kw)
    wall = time.monotonic() - t0
    rec = {"qid": q["id"], "rev": q.get("rev", 1), "model": model, "path": path, "wall_s": round(wall, 1),
           "usage": usage, "max_turns": max_turns, "budget": budget,
           "cypher_calls": tools["cypher"] if path == "graph" else None,
           "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    if not isinstance(result, dict) or "_error" in result:
        err = (result or {}).get("_error", "no result") if isinstance(result, dict) else "no result"
        rec.update(status="aborted" if _limit_hit(err, usage) else "failed", error=err[:1200], answer=None)
    elif not isinstance(result.get("answer"), list):
        rec.update(status="failed", error="structured output had no `answer` list", answer=None)
    else:
        rec.update(status="ok", error=None, answer=[str(x) for x in result["answer"]])
    return rec


_OUT_OF_TURNS = ("max_turns", "maximum number of turns")


def _transient(rec: dict) -> bool:
    e = rec.get("error") or ""
    return rec["status"] == "failed" and not any(s in e for s in (*_OUT_OF_TURNS, "timed out", "no `answer`"))


def load_sessions(path: Path) -> dict[tuple, dict]:
    out = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                out[_skey(r["qid"], r.get("rev"), r["model"], r["path"])] = r
    return out


def run_all(data: dict, *, models=MODELS, paths=PATHS, sessions_path: Path = SESSIONS, jobs: int = 1,
            agent=None, sleep=time.sleep, limit_wait: int = LIMIT_WAIT, max_waits: int = MAX_LIMIT_WAITS,
            only: set | None = None, max_turns: int = MAX_TURNS, budget: int | None = None, log=print) -> dict:
    """Run every (question, model, path) not already finished in the session log. Model by model (claude_cli's
    model is a module global), question by question, both paths per question, so a usage limit leaves paired
    comparisons. A usage limit pauses every worker for `limit_wait` s and re-runs the session; any other failure is
    retried once after 30 s unless it is a max-turns / timeout failure (a real outcome); the final record is
    appended to the log. Re-running skips sessions recorded `ok` or `failed`."""
    done = load_sessions(sessions_path)
    lock = threading.Lock()
    pause = {"until": 0.0, "waits": 0}
    stats = {"ran": 0, "skipped": 0}

    def write(rec):
        with lock:
            with open(sessions_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
            done[_skey(rec["qid"], rec.get("rev"), rec["model"], rec["path"])] = rec

    def one(q, model, path):
        attempt = 0
        while True:
            while time.time() < pause["until"]:
                sleep(min(60, max(0.0, pause["until"] - time.time())))
            attempt += 1
            rec = run_session(q, data["fixtures"][q["fixture"]], model, path, agent=agent, max_turns=max_turns,
                              budget=budget)
            rec["attempt"] = attempt
            tag = f"{q['id']} {model} {path}"
            if rec["status"] == "aborted":
                write(rec)
                with lock:
                    pause["waits"] += 1
                    if pause["waits"] > max_waits:
                        log(f"{tag}: usage limit again after {max_waits} waits; stopping (resume later)")
                        return False
                    pause["until"] = max(pause["until"], time.time() + limit_wait)
                log(f"{tag}: usage limit ({(rec['error'] or '')[:100]}); waiting {limit_wait}s")
                continue
            if _transient(rec) and attempt == 1:
                write(dict(rec, status="retried"))
                log(f"{tag}: failed ({(rec['error'] or '')[:100]}); retrying once in 30 s")
                sleep(30)
                continue
            write(rec)
            sc = ""
            if rec["status"] == "ok":
                a = normalize(rec["answer"], q["answer_kind"], _roots(data, q))
                s = score_answer(a, q["gold"])
                sc = f" f1={s['f1']:.2f}{' exact' if s['exact'] else ''}"
            log(f"{tag}: {rec['status']}{sc} ({rec['wall_s']}s)")
            return True

    from concurrent.futures import ThreadPoolExecutor
    for model in models:
        todo = []
        for q in data["questions"]:
            if only and q["id"] not in only:
                continue
            for path in paths:
                prev = done.get(_skey(q["id"], q.get("rev"), model, path))
                if prev and prev["status"] in ("ok", "failed"):
                    stats["skipped"] += 1
                    continue
                todo.append((q, model, path))
        log(f"{model}: {len(todo)} sessions to run")
        with ThreadPoolExecutor(max_workers=max(1, jobs)) as ex:
            results = list(ex.map(lambda t: one(*t), todo))
        stats["ran"] += len(todo)
        if not all(results):
            log("stopped on repeated usage limits; re-run the same command to resume")
            break
    return stats


def cmd_run(args) -> int:
    data = json.loads(Path(args.questions).read_text(encoding="utf-8"))
    models = tuple(m for m in args.models.split(",") if m)
    paths = tuple(p for p in args.paths.split(",") if p)
    only = set(args.only.split(",")) if args.only else None
    Path(args.sessions).parent.mkdir(parents=True, exist_ok=True)
    stats = run_all(data, models=models, paths=paths, sessions_path=Path(args.sessions), jobs=args.jobs,
                    limit_wait=args.limit_wait, only=only)
    print(f"ran {stats['ran']} sessions, skipped {stats['skipped']} already finished")
    return 0


# ───────────────────────────── scoring ─────────────────────────────

_TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


def session_usage(rec: dict) -> dict:
    """Tokens and CLI cost summed over the session's usage events; a field no event reported is None."""
    out = {}
    for k in _TOKEN_FIELDS + ("total_cost_usd",):
        vals = [u.get(k) for u in rec.get("usage") or [] if isinstance(u.get(k), (int, float))]
        out[k] = (round(sum(vals), 6) if k == "total_cost_usd" else int(sum(vals))) if vals else None
    parts = [out[k] for k in _TOKEN_FIELDS]
    out["tokens"] = sum(parts) if all(p is not None for p in parts) else None
    out["num_turns"] = sum(u.get("num_turns") or 0 for u in rec.get("usage") or []) or None
    return out


def _roots(data: dict, q: dict) -> tuple[str, ...]:
    fx = data["fixtures"].get(q["fixture"], {})
    return tuple(r for r in (fx.get("export"), fx.get("repo")) if r)


def score(data: dict, sessions: list[dict]) -> dict:
    """Scores per (model, path) row, overall and per family. Each question's FINAL record (its last `ok` / `failed`
    one) counts once; a question left only `aborted` / `retried` (an unfinished run) scores as a failure and is
    counted as incomplete. Usage spent on every attempt is added to the row's totals, never dropped. Sessions of an
    earlier rev of a question (reworded or replaced since) are not scored; they stay in the total usage, counted as
    superseded."""
    qs = {(q["id"], q.get("rev", 1)): q for q in data["questions"]}
    families = [f for f in FAMILIES if any(q["family"] == f for q in data["questions"])]
    families += sorted({q["family"] for q in data["questions"]} - set(families))
    final: dict[tuple, dict] = {}
    spent: dict[tuple, list[dict]] = {}
    superseded = []
    for r in sessions:
        k = _skey(r["qid"], r.get("rev"), r["model"], r["path"])
        if k[:2] not in qs:
            superseded.append(r)
            continue
        spent.setdefault((r["model"], r["path"]), []).append(r)
        cur = final.get(k)
        if r["status"] in ("ok", "failed") or cur is None or cur["status"] not in ("ok", "failed"):
            final[k] = r
    rows = []
    per_session = []
    keys = sorted({(m, p) for (_, _, m, p) in final}, key=lambda mp: (MODELS.index(mp[0]) if mp[0] in MODELS else 9,
                                                                      PATHS.index(mp[1]) if mp[1] in PATHS else 9))
    for model, path in keys:
        recs = [(qs[qk], final[qk + (model, path)]) for qk in qs if qk + (model, path) in final]
        scored = []
        for q, r in recs:
            if r["status"] == "ok":
                s = score_answer(normalize(r["answer"], q["answer_kind"], _roots(data, q)), q["gold"])
            else:
                s = {"precision": 0.0, "recall": 0.0, "f1": 0.0, "exact": False, "hits": 0}
            u = session_usage(r)
            scored.append((q, r, s))
            per_session.append({"qid": q["id"], "rev": q.get("rev", 1), "family": q["family"],
                                "fixture": q["fixture"], "model": model,
                                "path": path, "status": r["status"], **s, "tokens": u["tokens"],
                                "cost_usd": u["total_cost_usd"], "wall_s": r.get("wall_s"),
                                "answer": r.get("answer"), "error": r.get("error")})
        usage_all = [session_usage(r) for r in spent[(model, path)]]
        tok = [u["tokens"] for u in usage_all]
        cost = [u["total_cost_usd"] for u in usage_all]
        n_exact = sum(1 for _, _, s in scored if s["exact"])
        ok = [s for _, r, s in scored if r["status"] == "ok"]

        def mean(xs):
            return round(sum(xs) / len(xs), 4) if xs else None

        tok_known = [t for t in tok if t is not None]
        cost_known = [c for c in cost if c is not None]
        row = {"model": model, "path": path, "n": len(scored),
               "failed": sum(1 for _, r, _ in scored if r["status"] != "ok"),
               "incomplete": sum(1 for _, r, _ in scored if r["status"] not in ("ok", "failed")),
               "precision": mean([s["precision"] for _, _, s in scored]),
               "recall": mean([s["recall"] for _, _, s in scored]),
               "f1": mean([s["f1"] for _, _, s in scored]), "exact": n_exact,
               "f1_completed": mean([s["f1"] for s in ok]),
               "tokens": sum(tok_known) if tok_known else None, "tokens_missing": len(tok) - len(tok_known),
               "cost_usd": round(sum(cost_known), 4) if cost_known else None,
               "cost_missing": len(cost) - len(cost_known), "sessions_billed": len(usage_all),
               "tokens_per_correct": round(sum(tok_known) / n_exact) if n_exact and tok_known else None,
               "cost_per_correct": round(sum(cost_known) / n_exact, 4) if n_exact and cost_known else None,
               "wall_s": round(sum(r.get("wall_s") or 0 for _, r, _ in scored), 1),
               "families": {}}
        for fam in families:
            fs = [s for q, _, s in scored if q["family"] == fam]
            row["families"][fam] = {"n": len(fs), "f1": mean([s["f1"] for s in fs]),
                                    "exact": sum(1 for s in fs if s["exact"])}
        rows.append(row)
    def usage_of(recs):
        tok = [session_usage(r)["tokens"] for r in recs]
        cost = [session_usage(r)["total_cost_usd"] for r in recs]
        return {"sessions": len(recs), "tokens": sum(t for t in tok if t is not None),
                "tokens_missing": sum(1 for t in tok if t is None),
                "cost_usd": round(sum(c for c in cost if c is not None), 4),
                "cost_missing": sum(1 for c in cost if c is None)}

    total = usage_of(sessions)
    total["superseded"] = usage_of(superseded)
    return {"status": data.get("status", "proposed"), "questions": len(qs), "families": families, "rows": rows,
            "drops": {k: v for k, v in (data.get("drops") or {}).items() if k != "detail"},
            "total_usage": total, "sessions": per_session}


def _f(x, nd=2):
    return "--" if x is None else f"{x:.{nd}f}"


def table(res: dict) -> str:
    lines = [f"Graph-as-memory Q&A pilot -- {res['questions']} questions (status: {res['status']}; dev evidence, "
             f"AI-written templates). Failed sessions score 0; `--` = not reported.", "",
             "| model | path | n | failed | P | R | F1 | exact | F1 (completed) | tokens | $ CLI | tokens / exact | "
             "$ / exact | wall s |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in res["rows"]:
        tok = "--" if r["tokens"] is None else f"{r['tokens']:,}" + (f" ({r['tokens_missing']} missing)"
                                                                     if r["tokens_missing"] else "")
        tpc = "--" if r["tokens_per_correct"] is None else f"{r['tokens_per_correct']:,}"
        failed = f"{r['failed']}" + (f" ({r['incomplete']} unfinished)" if r.get("incomplete") else "")
        lines.append(f"| {r['model']} | {r['path']} | {r['n']} | {failed} | {_f(r['precision'])} | "
                     f"{_f(r['recall'])} | {_f(r['f1'])} | {r['exact']}/{r['n']} | {_f(r['f1_completed'])} | {tok} | "
                     f"{_f(r['cost_usd'])} | {tpc} | {_f(r['cost_per_correct'], 3)} | {r['wall_s']:.0f} |")
    families = res.get("families") or FAMILIES
    lines += ["", "Per family: F1 / exact (of n).", "",
              "| model | path | " + " | ".join(families) + " |", "|---|---|" + "---|" * len(families)]
    for r in res["rows"]:
        cells = [f"{_f(r['families'][f]['f1'])} / {r['families'][f]['exact']} of {r['families'][f]['n']}"
                 for f in families]
        lines.append(f"| {r['model']} | {r['path']} | " + " | ".join(cells) + " |")
    d, t = res.get("drops") or {}, res["total_usage"]
    lines += ["", f"Gold-check drops while generating: {d.get('total', '--')} candidates "
                  f"({', '.join(f'{k} {v}' for k, v in sorted((d.get('by_family') or {}).items())) or 'none'}).",
              f"Total usage: {t['sessions']} claude -p sessions (incl. retried / aborted attempts), "
              f"{t['tokens']:,} tokens ({t['tokens_missing']} sessions without a usage report), "
              f"${t['cost_usd']:.2f} as reported by the CLI ({t['cost_missing']} without a cost)."]
    sup = t.get("superseded") or {}
    if sup.get("sessions"):
        lines.append(f"Of that, {sup['sessions']} sessions ({sup['tokens']:,} tokens, ${sup['cost_usd']:.2f}) answered "
                     f"an earlier rev of a question that was reworded or replaced since; they are not scored above.")
    return "\n".join(lines) + "\n"


def cmd_score(args) -> int:
    data = json.loads(Path(args.questions).read_text(encoding="utf-8"))
    sessions = [json.loads(l) for l in Path(args.sessions).read_text(encoding="utf-8").splitlines() if l.strip()]
    res = score(data, sessions)
    res["questions_file"] = os.path.relpath(args.questions, ROOT)
    res["sessions_file"] = os.path.relpath(args.sessions, ROOT)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(res, indent=1) + "\n", encoding="utf-8")
    md = table(res)
    Path(args.table).write_text(md, encoding="utf-8")
    print(md)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m eval.graphqa", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    q = sub.add_parser("questions", help="generate + gold-check the questions (WSL: Neo4j + fixtures)")
    q.add_argument("--build", action="store_true", help="build a fixture's graph / semantic index if unusable")
    q.add_argument("--fixtures-root", default="", help="folder holding fixtures/ (default: this checkout)")
    q.add_argument("--cache", default=os.path.join(os.environ.get("XDG_CACHE_HOME") or "~/.cache", "orion", "graphqa"))
    q.add_argument("--seed", type=int, default=SEED)
    q.add_argument("--per-family", type=int, default=PER_FAMILY)
    q.add_argument("--out", default=str(DATASET))
    r = sub.add_parser("run", help="run the claude -p sessions (spends usage)")
    r.add_argument("--questions", default=str(DATASET))
    r.add_argument("--sessions", default=str(SESSIONS))
    r.add_argument("--models", default=",".join(MODELS))
    r.add_argument("--paths", default=",".join(PATHS))
    r.add_argument("--jobs", type=int, default=1)
    r.add_argument("--limit-wait", type=int, default=LIMIT_WAIT)
    r.add_argument("--only", default="", help="comma-separated question ids (a smoke run)")
    s = sub.add_parser("score", help="score the session log (token-free)")
    s.add_argument("--questions", default=str(DATASET))
    s.add_argument("--sessions", default=str(SESSIONS))
    s.add_argument("--out", default=str(RESULTS))
    s.add_argument("--table", default=str(TABLE))
    args = ap.parse_args(argv)
    return {"questions": cmd_questions, "run": cmd_run, "score": cmd_score}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
