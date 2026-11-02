"""Harder questions for the graph-as-memory pilot: multi-hop and whole-repo, with the gold read from the SOURCE.

`eval.graphqa` v2 saturated: lookup-sized questions on two small repos, and a gold set that had to come out of the
graph and survive a source check, so the graph was right by construction. This set changes both:

  * Questions need several hops or a sweep of the whole repo (below), and a third, larger fixture is added.
  * The gold is computed from the source by an oracle that never reads the graph: Python's `ast` for Python (a
    syntactic call graph, by the rule the question states), a lexical pass for JS. The graph's own answer, from the
    plain Cypher a user would write, is stored next to it as `reference` and only labels the question (`graph_agrees`).
    A question the graph gets wrong stays in the set, so this measures how often the graph is right as well.

  reach_down  the repo functions F reaches through calls, at any depth
  reach_up    the repo functions that reach a function, or a named library call, at any depth
  chain       the functions on any call chain from A to B
  hot         the module-level functions called from at least K different functions
  dead        the module-level functions under a folder that nothing else in the code mentions
  pair        the functions that call X and also Y, or X but not Y (any receiver)

The same templates and two access paths as `eval.graphqa` (its `run` and `score` take --questions / --sessions), so
the same rules hold: the questions are AI-written ("status": "proposed" until a human reviews them), everything is dev
evidence, a failed session is never a correct answer, and a number nobody reported is missing, not estimated.

    python -m eval.graphqa_hard edges                   # WSL: token-free, how the graph's call edges match the oracle
    python -m eval.graphqa_hard questions [--build]     # WSL: writes eval/dataset/graphqa/questions.hard.json
    python -m eval.graphqa_hard run [--models a,b]      # WSL: claude -p sessions (spends usage)
    python -m eval.graphqa_hard score                   # token-free: results JSON + markdown table
"""
from __future__ import annotations

import argparse
import ast
import builtins
import json
import os
import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from eval import graphqa as gq
from eval.graphqa import STOP_NAMES, Drop, Graph, Skip, SourceTree, _JS_DEF_NAMES, body_extent, call_rx

DUNDER = re.compile(r"^__\w+__$")
_BUILTINS = frozenset(dir(builtins))
_JS_NOT_CALLS = frozenset({"if", "for", "while", "switch", "catch", "function", "return", "typeof", "super", "with"})


@dataclass
class Func:
    fid: str                        # file:line of the definition
    name: str
    file: str
    line: int
    end: int
    cls: str | None = None          # the class that holds it (Python), else None
    calls: set[str] = field(default_factory=set)        # fids of the repo functions it calls, by the question's rule
    named: set[str] = field(default_factory=set)        # the name of every call in its body, on any receiver
    maybe: set[str] = field(default_factory=set)        # repo names it calls on an object the rule does not follow

    @property
    def module_level(self) -> bool:
        return self.cls is None


@dataclass
class CallGraph:
    funcs: dict[str, Func] = field(default_factory=dict)
    unparsed: list[str] = field(default_factory=list)   # files the oracle could not read: their calls are unknown

    def callers(self) -> dict[str, set[str]]:
        out: dict[str, set[str]] = {f: set() for f in self.funcs}
        for f in self.funcs.values():
            for t in f.calls:
                if t in out:
                    out[t].add(f.fid)
        return out

    def down(self, fid: str) -> dict[str, int]:
        """Function -> fewest calls from `fid`, without `fid` itself (recursion adds nothing)."""
        return _bfs({f.fid: f.calls for f in self.funcs.values()}, {fid})

    def up(self, fids) -> dict[str, int]:
        """Function -> fewest calls to any of `fids`, the fids themselves left out."""
        return _bfs(self.callers(), set(fids))

    def by_name(self, name: str) -> list[Func]:
        return [f for f in self.funcs.values() if f.name == name]


def _bfs(next_of: dict[str, set[str]], start: set[str]) -> dict[str, int]:
    """Function -> fewest steps from `start`; the start functions themselves are never listed."""
    seen: dict[str, int] = {}
    frontier, depth = set(start), 0
    while frontier:
        depth += 1
        frontier = {t for f in frontier for t in next_of.get(f, ())} - set(seen) - set(start)
        for t in frontier:
            seen[t] = depth
    return seen


# ───────────────────────────── the Python oracle ─────────────────────────────

PY_RULE = ("What counts as a call: `f(...)`, `module.f(...)`, `Class.f(...)`, `self.f(...)`, `cls.f(...)` and "
           "`super().f(...)`, followed through the imports and through the class (then its base classes defined in "
           "this repository) that the code names. Not a call: a call on any other object (`obj.f(...)`, "
           "`self.attr.f(...)`, `Class().f(...)`), creating an instance (`Class(...)`), a decorator, a function passed "
           "as a value, a dunder method. A call inside a function nested in F counts as F's own, and so does one inside an "
           "f-string's braces.")


class _Module:
    def __init__(self, name: str, rel: str, is_pkg: bool):
        self.name, self.rel, self.is_pkg = name, rel, is_pkg
        self.funcs: dict[str, list[str]] = {}               # module-level name -> fids (try/except variants: several)
        self.classes: dict[str, str] = {}                   # name -> class id
        self.imports: dict[str, tuple[str, str | None]] = {}    # alias -> (module, symbol or None)
        self.stars: list[str] = []


class _Class:
    def __init__(self, cid: str, module: _Module, node: ast.ClassDef):
        self.cid, self.module, self.node = cid, module, node
        self.methods: dict[str, str] = {}
        self.bases: list[str] = []


def _module_name(rel: str) -> tuple[str, bool]:
    p = PurePosixPath(rel)
    parts = list(p.with_suffix("").parts)
    is_pkg = parts[-1] == "__init__"
    return ".".join(parts[:-1] if is_pkg else parts), is_pkg


def _absolute(mod: _Module, node: ast.ImportFrom) -> str:
    if not node.level:
        return node.module or ""
    base = mod.name.split(".") if mod.name else []
    drop = node.level - (1 if mod.is_pkg else 0)
    base = base[:len(base) - drop] if drop > 0 else base
    return ".".join(base + ([node.module] if node.module else []))


def _add_import(mod: _Module, table: dict, node) -> None:
    if isinstance(node, ast.Import):
        for a in node.names:
            if a.asname:
                table[a.asname] = (a.name, None)
            else:   # `import a.b.c` binds `a`; the dotted form is read back from the receiver's own text
                table[a.name.split(".")[0]] = (a.name.split(".")[0], None)
        return
    src = _absolute(mod, node)
    for a in node.names:
        if a.name == "*":
            mod.stars.append(src)
        else:
            table[a.asname or a.name] = (src, a.name)


def _dotted(node) -> list[str] | None:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return parts[::-1]
    return None


def _top_level(body):
    """Statements at module or class level, looking inside if / try / with / for, not inside a def or a class."""
    for st in body:
        yield st
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for fname in ("body", "orelse", "finalbody"):
            yield from _top_level(getattr(st, fname, []) or [])
        for h in getattr(st, "handlers", []) or []:
            yield from _top_level(h.body)


class _PyOracle:
    def __init__(self, tree: SourceTree, *, files: list[str] | None = None):
        self.tree = tree
        self.cg = CallGraph()
        self.modules: dict[str, _Module] = {}
        self.by_rel: dict[str, _Module] = {}
        self.classes: dict[str, _Class] = {}
        self.nodes: dict[str, tuple] = {}
        self.names: set[str] = set()
        self.parsed: dict[str, ast.Module] = {}
        self.files = files if files is not None else tree.files()

    def build(self) -> CallGraph:
        for rel in self.files:
            try:
                self.parsed[rel] = ast.parse(self.tree.text(rel) or "")
            except (SyntaxError, ValueError):
                self.cg.unparsed.append(rel)
                continue
            name, is_pkg = _module_name(rel)
            mod = _Module(name, rel, is_pkg)
            self.modules[name] = mod
            self.by_rel[rel] = mod
        for rel, m in self.parsed.items():
            self._declare(self.by_rel[rel], m)
        for c in self.classes.values():
            self._link_bases(c)
        for fid, (node, mod, cls) in self.nodes.items():
            self._resolve_body(self.cg.funcs[fid], node, mod, cls)
        return self.cg

    def _func(self, node, mod: _Module, cls: str | None) -> str:
        fid = f"{mod.rel}:{node.lineno}"
        self.cg.funcs[fid] = Func(fid, node.name, mod.rel, node.lineno, node.end_lineno or node.lineno,
                                  cls=cls.split(":")[-1] if cls else None)
        self.nodes[fid] = (node, mod, cls)
        self.names.add(node.name)
        return fid

    def _declare(self, mod: _Module, m: ast.Module) -> None:
        for st in _top_level(m.body):
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                mod.funcs.setdefault(st.name, []).append(self._func(st, mod, None))
            elif isinstance(st, ast.ClassDef):
                self._declare_class(mod, st)
            elif isinstance(st, (ast.Import, ast.ImportFrom)):
                _add_import(mod, mod.imports, st)

    def _declare_class(self, mod: _Module, node: ast.ClassDef) -> None:
        cid = f"{mod.rel}:{node.name}"
        c = _Class(cid, mod, node)
        self.classes[cid] = c
        mod.classes[node.name] = cid
        for st in _top_level(node.body):
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                c.methods[st.name] = self._func(st, mod, cid)
            elif isinstance(st, ast.ClassDef):
                self._declare_class(mod, st)

    def _link_bases(self, c: _Class) -> None:
        for b in c.node.bases:
            parts = _dotted(b)
            kind, val = self._lookup(parts, c.module, {}) if parts else (None, None)
            if kind == "class":
                c.bases.append(val)

    def _file_module(self, dotted: str) -> _Module | None:
        return self.modules.get(dotted)

    def _symbol(self, modname: str, name: str, seen=None) -> tuple[str | None, object]:
        """What `name` is in module `modname`, following re-exports: ("func", [fids]), ("class", cid), ("module", m)."""
        seen = seen or set()
        if (modname, name) in seen:
            return None, None
        seen.add((modname, name))
        mod = self._file_module(modname)
        if mod is None:
            sub = f"{modname}.{name}"
            return ("module", sub) if self._file_module(sub) or self._is_pkg_prefix(sub) else (None, None)
        if name in mod.funcs:
            return "func", mod.funcs[name]
        if name in mod.classes:
            return "class", mod.classes[name]
        if name in mod.imports:
            src, sym = mod.imports[name]
            if sym is None:
                return ("module", src) if self._file_module(src) or self._is_pkg_prefix(src) else (None, None)
            kind, val = self._symbol(src, sym, seen)
            if kind:
                return kind, val
            sub = f"{src}.{sym}"
            return ("module", sub) if self._file_module(sub) or self._is_pkg_prefix(sub) else (None, None)
        sub = f"{modname}.{name}"
        if self._file_module(sub) or self._is_pkg_prefix(sub):
            return "module", sub
        for star in mod.stars:
            kind, val = self._symbol(star, name, seen)
            if kind:
                return kind, val
        return None, None

    def _is_pkg_prefix(self, dotted: str) -> bool:
        return any(m.startswith(dotted + ".") for m in self.modules)

    def _lookup(self, parts: list[str], mod: _Module, local_imports: dict) -> tuple[str | None, object]:
        """What a dotted name means from inside `mod`, without a local variable in the way."""
        head, rest = parts[0], parts[1:]
        if head in local_imports or head in mod.imports:
            src, sym = local_imports.get(head) or mod.imports[head]
            if sym is None:
                kind, val = ("module", src) if self._file_module(src) or self._is_pkg_prefix(src) else (None, None)
            else:
                kind, val = self._symbol(src, sym)
                if not kind:
                    sub = f"{src}.{sym}"
                    kind, val = ("module", sub) if self._file_module(sub) or self._is_pkg_prefix(sub) else (None, None)
        elif head in mod.funcs:
            kind, val = "func", mod.funcs[head]
        elif head in mod.classes:
            kind, val = "class", mod.classes[head]
        else:
            kind, val = None, None
            for star in mod.stars:
                kind, val = self._symbol(star, head)
                if kind:
                    break
        for part in rest:
            if kind == "module":
                kind, val = self._symbol(val, part)
            elif kind == "class":
                fid = self._method(val, part)
                kind, val = ("func", [fid]) if fid else (None, None)
            else:
                return None, None
        return kind, val

    def _method(self, cid: str, name: str, seen=None, *, own: bool = True) -> str | None:
        seen = seen or set()
        if cid in seen or cid not in self.classes:
            return None
        seen.add(cid)
        c = self.classes[cid]
        if own and name in c.methods:
            return c.methods[name]
        for b in c.bases:
            fid = self._method(b, name, seen)
            if fid:
                return fid
        return None

    def _resolve_body(self, f: Func, node, mod: _Module, cid: str | None) -> None:
        local_imports: dict = {}
        bound: set[str] = {a.arg for a in node.args.args + node.args.kwonlyargs + node.args.posonlyargs}
        for extra in (node.args.vararg, node.args.kwarg):
            if extra:
                bound.add(extra.arg)
        body = ast.Module(body=node.body, type_ignores=[])
        calls = []
        for n in ast.walk(body):
            if isinstance(n, (ast.Import, ast.ImportFrom)):
                _add_import(mod, local_imports, n)
            elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                bound.add(n.id)
            elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                bound.add(n.name)
                if not isinstance(n, ast.ClassDef):
                    bound |= {a.arg for a in n.args.args + n.args.kwonlyargs + n.args.posonlyargs}
            elif isinstance(n, ast.Lambda):
                bound |= {a.arg for a in n.args.args + n.args.kwonlyargs + n.args.posonlyargs}
            elif isinstance(n, ast.ExceptHandler) and n.name:
                bound.add(n.name)
            elif isinstance(n, ast.Call):
                calls.append(n)
        bound -= set(local_imports)
        first = node.args.posonlyargs + node.args.args
        static = any(isinstance(d, ast.Name) and d.id == "staticmethod" for d in node.decorator_list)
        receiver = first[0].arg if cid and first and not static else None
        for call in calls:
            self._resolve_call(f, call, mod, cid, bound, local_imports, receiver)

    def _resolve_call(self, f, call, mod, cid, bound, local_imports, receiver) -> None:
        fn = call.func
        if isinstance(fn, ast.Name):
            f.named.add(fn.id)
            if fn.id in bound:
                return
            kind, val = self._lookup([fn.id], mod, local_imports)
            if kind == "func":
                f.calls.update(x for x in val if not DUNDER.match(self.cg.funcs[x].name))
            return
        if not isinstance(fn, ast.Attribute):
            return
        attr = fn.attr
        f.named.add(attr)
        if DUNDER.match(attr):
            return
        recv = fn.value
        if isinstance(recv, ast.Call) and isinstance(recv.func, ast.Name) and recv.func.id == "super" and cid:
            target = self._method(cid, attr, own=False)
            if target:
                f.calls.add(target)
            return
        if isinstance(recv, ast.Name) and recv.id == receiver and cid:
            target = self._method(cid, attr)
            if target:
                f.calls.add(target)
            return
        parts = _dotted(recv)
        if parts and parts[0] not in bound:
            kind, val = self._lookup(parts + [attr], mod, local_imports)
            if kind == "func":
                f.calls.update(val)
                return
            if kind:
                return      # `pkg.Class(...)`: creating an instance
        f.maybe.add(attr)   # kept only when the repo defines the name (`_prune_maybes`)


def py_callgraph(tree: SourceTree, *, files: list[str] | None = None) -> CallGraph:
    o = _PyOracle(tree, files=files)
    cg = o.build()
    for f in cg.funcs.values():
        f.maybe &= o.names
    return cg


# ───────────────────────────── the JS oracle (lexical) ─────────────────────────────

JS_RULE = ("What counts as a call: `f(...)`, `obj.f(...)` on any object and `new f(...)`, where `f` is a function name "
           "defined exactly once in this repository's own code. Not a call: a function passed as a value. A call inside "
           "an anonymous callback counts as the named function around it. A named function (assigned to `this.f`, to an "
           "object property or to a variable) is its own function, not part of the function around it.")


def js_callgraph(tree: SourceTree, *, files: list[str] | None = None) -> CallGraph:
    cg = CallGraph()
    files = files if files is not None else tree.files()
    defs: list[Func] = []
    for rel in files:
        for i, line in enumerate(tree.code(rel, keep_strings=False), 1):
            for nm in sorted({m.group(1) for p in _JS_DEF_NAMES for m in p.finditer(line)}):
                ext = body_extent(tree, rel, i)
                if ext is None:
                    continue
                defs.append(Func(f"{rel}:{i}", nm, rel, i, ext[1]))
    counts: dict[str, int] = {}
    for d in defs:
        counts[d.name] = counts.get(d.name, 0) + 1
    for d in defs:
        cg.funcs[d.fid] = d
    targets = {d.name: d.fid for d in defs if counts[d.name] == 1}
    per_file: dict[str, list[Func]] = {}
    for d in defs:
        per_file.setdefault(d.file, []).append(d)
    for rel, here in per_file.items():
        for d in here:      # nested in another named function: not a top-level function
            around = [o for o in here if o is not d and o.line < d.line and d.end <= o.end]
            if around:
                d.cls = max(around, key=lambda o: o.line).name
    any_call = re.compile(r"([A-Za-z_$][\w$]*)\s*\(")
    for rel in files:
        here = sorted(per_file.get(rel, []), key=lambda d: (d.line, -d.end))
        code = tree.code(rel, keep_strings=False)
        for i, line in enumerate(code, 1):
            owner = None
            for d in here:      # the innermost named function holding this line
                if d.line <= i <= d.end:
                    owner = d
            if owner is None:
                continue
            for name in {m.group(1) for m in any_call.finditer(line)} - _JS_NOT_CALLS:
                if name == owner.name and owner.line == i:
                    continue
                if any(d.line == i and d.name == name for d in here):
                    continue    # the definition line of a named function expression
                owner.named.add(name)
                if name in targets and call_rx(name).search(line):
                    owner.calls.add(targets[name])
    return cg


# ───────────────────────────── the graph's own call graph (the reference answer) ─────────────────────────────

GRAPH_METHODS = ("MATCH (m:CpgMethod {scan_id:$scan_id}) WHERE NOT m.is_external "
                 "RETURN m.name AS name, m.full_name AS full_name, m.file_path AS file, m.line AS line")
GRAPH_CALLS = ("MATCH (m:CpgMethod {scan_id:$scan_id})-[:CONTAINS_CALL]->(c:CpgCall {scan_id:$scan_id}) "
               "OPTIONAL MATCH (c)-[:RESOLVES_TO]->(t:CpgMethod {scan_id:$scan_id}) "
               "RETURN m.full_name AS caller, c.name AS name, t.full_name AS callee, "
               "coalesce(t.is_external, true) AS external")
gq.CYPHER.update({"hard_methods": GRAPH_METHODS, "hard_calls": GRAPH_CALLS})


def _parent(full: str) -> str | None:
    m = re.match(r"^(.*)[.:][^.:]*$", full)
    return m.group(1) if m else None


def graph_callgraph(methods: list[dict], calls: list[dict], oracle: CallGraph) -> tuple[CallGraph, dict[str, int]]:
    """The call graph the plain query gives: CONTAINS_CALL then RESOLVES_TO. A nested function or lambda is folded into
    the function around it unless the oracle knows its line as a function (JS: `this.f = function` is `<lambda>N` in
    the graph), and Joern's pseudo-methods (`<module>`, `<body>`, `f<metaClassAdapter>`) call nothing here. Also returns
    how many calls resolve to each function from anywhere but an adapter, module level included."""
    known = {m["full_name"]: m for m in methods if m.get("file") and isinstance(m.get("line"), int)}

    def real(m) -> bool:
        return "<" not in m["name"] and not m["name"].startswith(":")

    def anc(full):      # the nearest enclosing real method
        p = _parent(full)
        while p:
            if p in known and real(known[p]):
                return p
            p = _parent(p)
        return None

    owner: dict[str, str] = {}
    cg = CallGraph()
    for full, m in known.items():
        fid = f"{m['file']}:{m['line']}"
        if fid in oracle.funcs and ("<lambda" in m["name"] or real(m)):
            o = oracle.funcs[fid]
            cg.funcs.setdefault(fid, Func(fid, o.name, m["file"], m["line"], o.end, cls=o.cls))
            owner[full] = fid
        elif real(m) and anc(full) is None:
            cls = _parent(full)
            cls = None if cls is None or cls.endswith(("<module>", ":program")) else cls.rsplit(".", 1)[-1]
            cg.funcs.setdefault(fid, Func(fid, m["name"], m["file"], m["line"], m["line"], cls=cls))
            owner[full] = fid

    def resolve(full):
        while full and full not in owner:
            if full in known and "metaClass" in known[full]["name"]:
                return None
            full = _parent(full)
        return owner.get(full) if full else None

    incoming: dict[str, int] = {}
    for r in calls:
        if r["caller"] in known and "metaClass" in known[r["caller"]]["name"]:
            continue    # the `handle(self, *args)` an adapter holds is not a call in the source
        target = None if r.get("external") or not r.get("callee") else resolve(r["callee"])
        if target:
            incoming[target] = incoming.get(target, 0) + 1
        caller = resolve(r["caller"])
        if caller is None:
            continue
        f = cg.funcs[caller]
        name = str(r.get("name") or "")
        if not name.startswith("<"):
            f.named.add(name.rsplit(".", 1)[-1] if ":" in name else name)
        if target and not DUNDER.match(cg.funcs[target].name):
            f.calls.add(target)
    return cg, incoming


def edge_stats(oracle: CallGraph, ref: CallGraph) -> dict:
    """How the graph's call edges compare with the oracle's, between the functions both know. A graph-only edge whose
    name the oracle saw on another object (`obj.f(...)`) is not wrong, only outside the question's rule."""
    both = set(oracle.funcs) & set(ref.funcs)
    o = {(f, t) for f in both for t in oracle.funcs[f].calls if t in both}
    g = {(f, t) for f in both for t in ref.funcs[f].calls if t in both}
    on_object = {(f, t) for f, t in g - o if oracle.funcs[t].name in oracle.funcs[f].maybe}
    return {"oracle_functions": len(oracle.funcs), "graph_functions": len(ref.funcs), "both": len(both),
            "oracle_edges": len(o), "graph_edges": len(g), "common": len(o & g),
            "oracle_only": len(o - g), "graph_only": len(g - o), "graph_only_on_an_object": len(on_object),
            "recall": round(len(o & g) / len(o), 4) if o else None,
            "precision": round(len(o & g) / len(g), 4) if g else None}


# ───────────────────────────── the families ─────────────────────────────

MIN_GOLD, MAX_GOLD = 3, 20
PER_FAMILY = 2
SEED = 20261101
FAMILIES = ("reach_down", "reach_up", "chain", "hot", "dead", "pair")

ROOT = gq.ROOT
DATASET = ROOT / "eval" / "dataset" / "graphqa" / "questions.hard.json"
RESULTS = ROOT / "eval" / "results" / "graphqa-hard.json"
TABLE = ROOT / "eval" / "results" / "graphqa-hard.md"
SESSIONS = ROOT / "eval" / "results" / "graphqa-hard.sessions.jsonl"
MAX_TURNS, BUDGET = 30, 25     # smoke at 15 round trips: 6 of 8 sessions ran out and returned nothing

FIXTURES = {
    "nodegoat": {"root": "fixtures", "rel": "NodeGoat", "lang": "js", "min_depth": 2,
                 "skip": ("app/assets/", "test/"), "not_followed": ("insert", "test"),
                 "scope": "Only the server code counts: ignore `app/assets/` and `test/`. "},
    "pygoat": {"root": "fixtures", "rel": "pygoat", "lang": "py", "min_depth": 2},
    # Scanned from src/: Joern binds `from black.nodes import f` to black/nodes.py only when that path is the file's own.
    # From src/black the graph sees 0.51 of black's calls, from src/ 0.92 (poetry 0.75, 0.89). The clone is sparse, so the
    # graph has only black/, while the export has blackd/ and blib2to3/ as well.
    "black": {"root": "repos", "rel": "psf__black/src", "lang": "py", "min_depth": 3,
              "skip": ("blackd/", "blib2to3/"), "scope": "Only the `black/` folder counts: ignore `blackd/` and `blib2to3/`. "},
    "poetry": {"root": "repos", "rel": "python-poetry__poetry/src", "lang": "py", "min_depth": 3},
}


class Ctx:
    def __init__(self, fixture: str, tree: SourceTree, cg: CallGraph, ref: CallGraph | None, incoming: dict | None,
                 spec: dict, rng: random.Random, files: list[str]):
        self.fixture, self.tree, self.cg, self.ref, self.incoming = fixture, tree, cg, ref, incoming or {}
        self.spec, self.rng, self.files = spec, rng, files
        self.rule = (PY_RULE if spec["lang"] == "py" else JS_RULE)
        if spec.get("not_followed"):
            self.rule += " " + " and ".join(f"`{n}`" for n in spec["not_followed"]) + " are not followed: the code " \
                         "also calls a library's method by those names."
        self.lead = spec.get("scope", "")
        self.candidates = sorted(cg.funcs.values(), key=lambda f: f.fid)

    def shuffled(self, pool) -> list:
        pool = list(pool)
        self.rng.shuffle(pool)
        return pool

    def names(self, fids) -> list[str]:
        names = [self.cg.funcs[f].name for f in fids]
        if len(set(names)) != len(names):
            raise Skip("two functions in the answer share a name")
        return names


def _size_ok(gold, low: int = MIN_GOLD, high: int = MAX_GOLD) -> None:
    if not low <= len(gold) <= high:
        raise Skip(f"answer size outside {low}..{high}")


def _ref_names(ref: CallGraph, fids) -> list[str]:
    return sorted({ref.funcs[f].name for f in fids if f in ref.funcs})


def _q(ctx: Ctx, family: str, text: str, gold: list[str], reference: list[str] | None, check: dict) -> dict:
    gold = gq.normalize(gold, "name")
    out = {"fixture": ctx.fixture, "family": family, "answer_kind": "name", "question": ctx.lead + text, "gold": gold,
           "check": dict(check, oracle="python ast" if ctx.spec["lang"] == "py" else "lexical")}
    if reference is None:
        out["reference"], out["graph_agrees"] = None, False
    else:
        ans = gq.normalize(reference, "name")
        s = gq.score_answer(ans, gold)
        out["reference"] = {"answer": ans, "f1": round(s["f1"], 4), "exact": s["exact"]}
        out["graph_agrees"] = s["exact"]
    return out


def gen_reach_down(ctx: Ctx):
    for f in ctx.shuffled(ctx.candidates):
        def build(f=f):
            d = ctx.cg.down(f.fid)
            _size_ok(d)
            if max(d.values()) < ctx.spec["min_depth"]:
                raise Skip("too shallow")
            gold = ctx.names(d)
            ref = None
            if ctx.ref and f.fid in ctx.ref.funcs:
                ref = _ref_names(ctx.ref, ctx.ref.down(f.fid))
            text = (f"Start at the function `{f.name}` (defined at {f.fid}). Follow its calls to functions defined in "
                    f"this repository, then the calls those functions make, and so on to any depth. Which functions can "
                    f"it reach? Do not list `{f.name}` itself. {ctx.rule} Answer with the reached functions' names.")
            return _q(ctx, "reach_down", text, gold, ref,
                      {"start": f.fid, "depth": {ctx.cg.funcs[k].name: v for k, v in sorted(d.items())},
                       "objects_not_followed": sorted({m for k in [f.fid, *d] for m in ctx.cg.funcs[k].maybe})})
        yield f.fid, build


def gen_reach_up(ctx: Ctx):
    """Alternates a repo function and a library call as the target."""
    repo_names = {f.name for f in ctx.cg.funcs.values()}
    named: dict[str, set[str]] = {}
    for f in ctx.cg.funcs.values():
        for n in f.named:
            if n not in repo_names and n not in _BUILTINS and not DUNDER.match(n) and n not in STOP_NAMES and len(n) > 3:
                named.setdefault(n, set()).add(f.fid)

    def repo_q(f):
        u = ctx.cg.up([f.fid])
        _size_ok(u)
        if max(u.values()) < ctx.spec["min_depth"]:
            raise Skip("too shallow")
        gold = ctx.names(u)
        ref = _ref_names(ctx.ref, ctx.ref.up([f.fid])) if ctx.ref and f.fid in ctx.ref.funcs else None
        text = (f"Which functions in this repository can reach the function `{f.name}` (defined at {f.fid}) through a "
                f"chain of calls of any length: they call it, or call a function that calls it, and so on? Do not list "
                f"`{f.name}` itself. {ctx.rule} Answer with the functions' names.")
        return _q(ctx, "reach_up", text, gold, ref, {"target": f.fid, "depth": {ctx.cg.funcs[k].name: v
                                                                                for k, v in sorted(u.items())}})

    def lib_q(name):
        direct = named[name]
        u = ctx.cg.up(direct)
        gold = ctx.names(direct | set(u))
        _size_ok(gold)
        if not u or max(u.values()) < ctx.spec["min_depth"] - 1:
            raise Skip("too shallow")
        ref = None
        if ctx.ref:
            rd = {f.fid for f in ctx.ref.funcs.values() if name in f.named}
            ref = _ref_names(ctx.ref, rd | set(ctx.ref.up(rd)))
        text = (f"No function named `{name}` is defined in this repository. Which functions in this repository can reach "
                f"a call to `{name}`, either in their own body or through a chain of calls of any length to other "
                f"functions defined in this repository? The call to `{name}` itself may be on any object (`{name}(...)` "
                f"or `obj.{name}(...)`). For the chain: {ctx.rule} Answer with the functions' names.")
        return _q(ctx, "reach_up", text, gold, ref, {"library_call": name, "direct": sorted(direct),
                                                     "depth": {ctx.cg.funcs[k].name: v for k, v in sorted(u.items())}})

    pools = [iter(ctx.shuffled(ctx.candidates)), iter(ctx.shuffled(sorted(named)))]
    builders = [repo_q, lib_q]
    i = 0
    while pools[0] is not None or pools[1] is not None:
        i = 1 - i
        if pools[i] is None:
            continue
        c = next(pools[i], None)
        if c is None:
            pools[i] = None
            continue

        def build(c=c, i=i):
            return builders[i](c)
        yield (c.fid if i == 0 else f"lib:{c}"), build


def gen_chain(ctx: Ctx):
    pairs = []
    for f in ctx.candidates:
        d = ctx.cg.down(f.fid)
        pairs += [(f.fid, t) for t, depth in d.items() if depth >= max(ctx.spec["min_depth"], 2)]
    for a, b in ctx.shuffled(sorted(pairs)):
        def build(a=a, b=b):
            A, B = ctx.cg.funcs[a], ctx.cg.funcs[b]
            on = (set(ctx.cg.down(a)) | {a}) & (set(ctx.cg.up([b])) | {b})
            hops = ctx.cg.down(a)[b]
            if len(on) < hops + 2:
                raise Skip("only one chain")
            _size_ok(on, 4)
            gold = ctx.names(on)
            ref = None
            if ctx.ref and a in ctx.ref.funcs and b in ctx.ref.funcs:
                ron = (set(ctx.ref.down(a)) | {a}) & (set(ctx.ref.up([b])) | {b})
                ref = _ref_names(ctx.ref, ron) if b in ctx.ref.down(a) else []
            text = (f"Consider every chain of calls from `{A.name}` (defined at {a}) to `{B.name}` (defined at {b}), "
                    f"going only through functions defined in this repository, of any length. Which functions lie on at "
                    f"least one such chain? Include both ends. {ctx.rule} Answer with the functions' names.")
            return _q(ctx, "chain", text, gold, ref, {"from": a, "to": b, "shortest_calls": hops})
        yield f"{a}->{b}", build


def gen_hot(ctx: Ctx):
    """Fan-in over the module-level functions, then fan-out over every function, each at the highest K that gives an
    answer of a usable size."""
    cg = ctx.cg
    callers = cg.callers()
    py = ctx.spec["lang"] == "py"
    fan_in = {f.fid: len(callers[f.fid] - {f.fid}) for f in cg.funcs.values() if f.module_level or not py}
    fan_out = {f.fid: len(f.calls - {f.fid}) for f in cg.funcs.values()}

    def at(values, k):
        return {f for f, n in values.items() if n >= k}

    def usable(gold) -> bool:
        return MIN_GOLD <= len(gold) <= MAX_GOLD and len({cg.funcs[f].name for f in gold}) == len(gold)

    def build_in():
        for k in range(max(fan_in.values(), default=0), 1, -1):
            gold = at(fan_in, k)
            if not usable(gold):
                continue
            ref = None
            if ctx.ref:
                rc = ctx.ref.callers()
                ref = sorted({f.name for f in ctx.ref.funcs.values()
                              if (f.module_level or not py) and len(rc[f.fid] - {f.fid}) >= k})
            what = "module-level functions (not methods)" if py else "functions"
            text = (f"Which {what} defined in this repository are called from at least {k} "
                    f"different functions or methods of this repository, not counting themselves? Count calling "
                    f"functions, not call sites; code at module level does not count as a caller. {ctx.rule} Answer with "
                    f"the functions' names.")
            return _q(ctx, "hot", text, ctx.names(gold), ref,
                      {"variant": "fan-in", "k": k, "counts": {cg.funcs[f].name: fan_in[f] for f in sorted(gold)}})
        raise Skip("no K gives a usable fan-in answer")

    def build_out():
        for k in range(max(fan_out.values(), default=0), 1, -1):
            gold = at(fan_out, k)
            if not usable(gold):
                continue
            ref = None
            if ctx.ref:
                ref = sorted({f.name for f in ctx.ref.funcs.values() if len(f.calls - {f.fid}) >= k})
            text = (f"Which functions or methods defined in this repository call at least {k} different functions defined "
                    f"in this repository, not counting themselves? {ctx.rule} Answer with the functions' names.")
            return _q(ctx, "hot", text, ctx.names(gold), ref,
                      {"variant": "fan-out", "k": k, "counts": {cg.funcs[f].name: fan_out[f] for f in sorted(gold)}})
        raise Skip("no K gives a usable fan-out answer")

    yield "fan-in", build_in
    yield "fan-out", build_out


def _fstring_names(text: str):
    """Names used inside f-string braces, which the string blanking hides: `f"{pluralize(n)}"` mentions pluralize."""
    try:
        parsed = ast.parse(text)
    except (SyntaxError, ValueError):
        return
    for n in ast.walk(parsed):
        if isinstance(n, ast.JoinedStr):
            for v in ast.walk(n):
                if isinstance(v, ast.Name):
                    yield v.id, v.lineno
                elif isinstance(v, ast.Attribute):
                    yield v.attr, v.lineno


def mentions(tree: SourceTree, files: list[str]) -> dict[str, set[tuple[str, int]]]:
    """Identifier -> the (file, line) pairs where it appears in code (comments and strings blanked)."""
    out: dict[str, set[tuple[str, int]]] = {}
    for rel in files:
        for i, line in enumerate(tree.code(rel, keep_strings=False), 1):
            for name in set(re.findall(r"[A-Za-z_$][\w$]*", line)):
                out.setdefault(name, set()).add((rel, i))
        if tree.lang == "py":
            for name, i in _fstring_names(tree.text(rel) or ""):
                out.setdefault(name, set()).add((rel, i))
    return out


def gen_dead(ctx: Ctx):
    """Module-level functions first, then methods, each within a folder that gives an answer of a usable size."""
    seen = mentions(ctx.tree, ctx.files)
    fs = [f for f in ctx.cg.funcs.values() if not DUNDER.match(f.name)]

    def unmentioned(f):
        return seen.get(f.name, set()) <= {(f.file, f.line)}

    def folders(pool):
        out = {""}
        for f in pool:
            parts = f.file.split("/")[:-1]
            out |= {"/".join(parts[:i]) for i in range(1, len(parts) + 1)}
        return sorted(out)

    py = ctx.spec["lang"] == "py"
    kinds = (("module-level functions (not methods)", True), ("methods (not module-level functions)", False)) if py else (
        ("top-level functions (not ones nested in another function)", True),
        ("functions nested in another function (a `this.f = ...` included)", False))
    for kind, top in kinds:
        pool = [f for f in fs if f.module_level == top]
        for d in ctx.shuffled(folders(pool)) if pool else ():
            def build(kind=kind, top=top, pool=pool, d=d):
                scope = [f for f in pool if not d or f.file.startswith(d + "/")]
                gold = {f.fid for f in scope if unmentioned(f)}
                _size_ok(gold)
                if len(scope) < 2 * len(gold):
                    raise Skip("most of the folder is the answer")
                ref = None
                if ctx.ref:
                    rs = [f for f in ctx.ref.funcs.values() if f.module_level == top
                          and (not d or f.file.startswith(d + "/")) and not DUNDER.match(f.name)]
                    ref = sorted({f.name for f in rs if not ctx.incoming.get(f.fid)})
                where = f"under `{d}/`" if d else "in this repository"
                fstring = ", and so is code inside an f-string's braces" if py else ""
                lang = "Python" if py else "JavaScript"
                text = (f"Consider the {kind} defined {where}. Which of them are never mentioned anywhere else in this "
                        f"repository's {lang} code? That is, the function's name appears on no other line of code in any "
                        f"file: a call, an import or a plain reference is a mention{fstring}; comments and "
                        f"plain strings are not. Answer with the functions' names.")
                return _q(ctx, "dead", text, ctx.names(gold), ref, {"folder": d, "of": len(scope), "kind": kind})
            yield f"{'top' if top else 'nested'}:{d}", build


def gen_pair(ctx: Ctx):
    """X and Y, then X and not Y: names that many functions call, on any receiver."""
    named: dict[str, set[str]] = {}
    for f in ctx.cg.funcs.values():
        for n in f.named:
            if n not in _BUILTINS and not DUNDER.match(n) and n not in STOP_NAMES and len(n) > 3:
                named.setdefault(n, set()).add(f.fid)
    common = sorted(n for n, fs in named.items() if len(fs) >= MIN_GOLD)
    pairs = [(x, y) for x in common for y in common if x != y]
    ctx.rng.shuffle(pairs)
    have = {"both": 0, "not": 0}
    for variant in ("both", "not"):
        for x, y in pairs:
            if variant == "both" and x > y:
                continue

            def build(variant=variant, x=x, y=y):
                if have[variant]:
                    raise Skip("this variant has its question")
                a, b = named[x], named[y]
                gold = a & b if variant == "both" else a - b
                _size_ok(gold)
                if variant == "not" and len(a & b) < 2:
                    raise Skip("Y never appears with X")
                names = ctx.names(gold)
                ref = None
                if ctx.ref:
                    ra = {f.fid for f in ctx.ref.funcs.values() if x in f.named}
                    rb = {f.fid for f in ctx.ref.funcs.values() if y in f.named}
                    ref = _ref_names(ctx.ref, ra & rb if variant == "both" else ra - rb)
                how = f"call both `{x}` and `{y}`" if variant == "both" else f"call `{x}` but never call `{y}`"
                text = (f"Which functions or methods defined in this repository {how}? A call by that name on any "
                        f"receiver counts (`{x}(...)`, `obj.{x}(...)`), in the function's own body or in a function "
                        f"nested in it; code at module level does not count. Answer with the functions' names.")
                have[variant] += 1
                return _q(ctx, "pair", text, names, ref, {"variant": variant, "x": x, "y": y})
            yield f"{variant}:{x},{y}", build


GENERATORS = {"reach_down": gen_reach_down, "reach_up": gen_reach_up, "chain": gen_chain, "hot": gen_hot,
              "dead": gen_dead, "pair": gen_pair}


def source_files(tree: SourceTree, spec: dict) -> list[str]:
    return [f for f in tree.files() if not f.startswith(tuple(spec.get("skip", ())) or ("\0",))]


def oracle_graph(tree: SourceTree, spec: dict) -> CallGraph:
    files = source_files(tree, spec)
    if spec["lang"] == "py":
        return py_callgraph(tree, files=files)
    cg = js_callgraph(tree, files=files)
    drop = {f.fid for f in cg.funcs.values() if f.name in spec.get("not_followed", ())}
    for f in cg.funcs.values():
        f.calls -= drop
    return cg


def generate(fixture: str, tree: SourceTree, cg: CallGraph, ref: CallGraph | None, incoming: dict | None, spec: dict,
             *, per_family: int = PER_FAMILY, seed: int = SEED, families=FAMILIES, log=print) -> tuple[list, dict]:
    """(questions, skips). Candidates of each family are tried in a seeded order until `per_family` give a question. The
    gold is the oracle's, so a graph that disagrees never removes a question: it only sets `graph_agrees`. A skip is a
    candidate that is no question (too shallow, an answer too small or too large, two answers sharing a name)."""
    questions, skips, used = [], {}, set()
    files = source_files(tree, spec)
    for fam in families:
        ctx = Ctx(fixture, tree, cg, ref, incoming, spec, random.Random(f"{seed}:{fixture}:{fam}"), files)
        got = 0
        for label, build in GENERATORS[fam](ctx):
            if got >= per_family:
                break
            try:
                q = build()
                if tuple(q["gold"]) in used:
                    raise Skip("same answer set as an earlier question")
            except (Skip, Drop) as s:
                key = f"{fam}: {s}"
                skips[key] = skips.get(key, 0) + 1
                continue
            used.add(tuple(q["gold"]))
            got += 1
            q["id"] = f"{fixture}-{fam}-{got}"
            q["candidate"] = label
            questions.append(q)
        if got < per_family:
            log(f"{fixture}/{fam}: only {got} of {per_family} questions")
    return questions, skips


# ───────────────────────────── the commands (WSL) ─────────────────────────────

def _repo(name: str, args) -> Path:
    spec = FIXTURES[name]
    base = args.fixtures_root if spec["root"] == "fixtures" else args.repos_root
    return (Path(base).expanduser() / spec["rel"]).resolve()


def _load(name: str, args) -> tuple:
    """(repo, scan id, export, tree, oracle, graph call graph, incoming) for one fixture."""
    spec = FIXTURES[name]
    repo = _repo(name, args)
    sid = gq.ensure_scan(repo, build=args.build)
    export = gq.export_tree(repo, Path(args.cache).expanduser())
    tree = SourceTree(export, spec["lang"])
    cg = oracle_graph(tree, spec)
    g = Graph(sid)
    try:
        ref, incoming = graph_callgraph(g.rows("hard_methods"), g.rows("hard_calls"), cg)
    finally:
        g.close()
    return repo, sid, export, tree, cg, ref, incoming


def cmd_edges(args) -> int:
    for name in args.fixtures.split(","):
        _, _, _, _, cg, ref, _ = _load(name, args)
        print(name, json.dumps(edge_stats(cg, ref)))
    return 0


def _stamp(scan_id: str) -> str | None:
    from orion.graph import scans
    return (scans.get_record(scan_id) or {}).get("built_at")


def cmd_questions(args) -> int:
    out = {"status": "proposed",
           "note": "AI-written templates and picks; a human must review every question before a result counts. Gold is "
                   "the source oracle's; `reference` is the plain Cypher's answer.",
           "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "orion_commit": gq._git(ROOT, "rev-parse", "HEAD"), "seed": args.seed, "per_family": args.per_family,
           "fixtures": {}, "questions": []}
    for name in args.fixtures.split(","):
        spec = FIXTURES[name]
        repo, sid, export, tree, cg, ref, incoming = _load(name, args)
        qs, skips = generate(name, tree, cg, ref, incoming, spec, per_family=args.per_family, seed=args.seed)
        out["fixtures"][name] = {"repo": str(repo), "commit": gq._git(repo, "rev-parse", "HEAD"), "scan_id": sid,
                                 "built_at": str(_stamp(sid)), "export": str(export), "lang": spec["lang"],
                                 "source_files": len(source_files(tree, spec)), "unparsed": cg.unparsed,
                                 "edges": edge_stats(cg, ref), "skipped_candidates": skips}
        out["questions"].extend(dict(q, status="proposed", scan_id=sid) for q in qs)
        print(f"{name}: {len(qs)} questions, {sum(1 for q in qs if q['graph_agrees'])} where the plain query is exact")
    path = Path(args.out)
    old = (json.loads(path.read_text(encoding="utf-8")).get("questions") or []) if path.exists() else []
    gq.carry_revs(out["questions"], old)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {len(out['questions'])} questions to {path}")
    return 0


def check_scans(data: dict, stamp=_stamp) -> list[str]:
    """Fixtures whose scan was rebuilt since the questions were written: graph sessions would read another graph."""
    return [f"{n}: built {fx.get('built_at')}, now {stamp(fx['scan_id'])}" for n, fx in data["fixtures"].items()
            if fx.get("built_at") and str(stamp(fx["scan_id"])) != fx["built_at"]]


def select_fixtures(data: dict, fixtures: str) -> dict:
    """`data` narrowed to the comma-separated fixtures ("" = all of them)."""
    if not fixtures:
        return data
    names = set(fixtures.split(","))
    return dict(data, fixtures={k: v for k, v in data["fixtures"].items() if k in names},
                questions=[q for q in data["questions"] if q["fixture"] in names])


def cmd_run(args) -> int:
    data = select_fixtures(json.loads(Path(args.questions).read_text(encoding="utf-8")), args.fixtures)
    changed = check_scans(data)
    if changed:
        raise SystemExit("a scan changed since the questions were written:\n  " + "\n  ".join(changed))
    Path(args.sessions).parent.mkdir(parents=True, exist_ok=True)
    stats = gq.run_all(data, models=tuple(m for m in args.models.split(",") if m),
                       paths=tuple(p for p in args.paths.split(",") if p), sessions_path=Path(args.sessions),
                       jobs=args.jobs, limit_wait=args.limit_wait, max_turns=args.max_turns, budget=args.budget,
                       only=set(args.only.split(",")) if args.only else None)
    print(f"ran {stats['ran']} sessions, skipped {stats['skipped']} already finished")
    changed = check_scans(data)
    if changed:
        print("WARNING: a scan changed during the run:\n  " + "\n  ".join(changed))
        return 3
    return 0


def agreement(data: dict, res: dict) -> dict:
    """Exact answers per (model, path), split by whether the plain Cypher query gets the question right; plus that
    query itself as a row (no model, no tokens)."""
    agrees = {q["id"]: bool(q.get("graph_agrees")) for q in data["questions"]}
    rows = []
    for r in res["rows"]:
        here = [s for s in res["sessions"] if (s["model"], s["path"]) == (r["model"], r["path"])]
        rows.append({"model": r["model"], "path": r["path"],
                     "plain_right": [sum(1 for s in here if s["exact"] and agrees[s["qid"]]),
                                     sum(1 for s in here if agrees[s["qid"]])],
                     "plain_wrong": [sum(1 for s in here if s["exact"] and not agrees[s["qid"]]),
                                     sum(1 for s in here if not agrees[s["qid"]])]})
    refs = [q.get("reference") or {} for q in data["questions"]]
    return {"rows": rows, "plain_query": {"exact": sum(1 for x in refs if x.get("exact")), "n": len(refs),
                                          "f1": round(sum(x.get("f1") or 0 for x in refs) / len(refs), 4) if refs else None}}


def cmd_score(args) -> int:
    data = select_fixtures(json.loads(Path(args.questions).read_text(encoding="utf-8")), args.fixtures)
    ids = {q["id"] for q in data["questions"]}
    sessions = [json.loads(x) for x in Path(args.sessions).read_text(encoding="utf-8").splitlines() if x.strip()]
    res = gq.score(data, [r for r in sessions if r["qid"] in ids])
    res["fixtures_scored"] = sorted(data["fixtures"])
    res["questions_file"] = os.path.relpath(args.questions, ROOT)
    res["sessions_file"] = os.path.relpath(args.sessions, ROOT)
    res["agreement"] = agreement(data, res)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(res, indent=1) + "\n", encoding="utf-8")
    a = res["agreement"]
    lines = [gq.table(res), f"The plain Cypher query alone (no model): {a['plain_query']['exact']} of "
                            f"{a['plain_query']['n']} exact, mean F1 {a['plain_query']['f1']}.", "",
             "Exact answers, split by whether that query is right on the question.", "",
             "| model | path | query right | query wrong |", "|---|---|---|---|"]
    lines += [f"| {r['model']} | {r['path']} | {r['plain_right'][0]} of {r['plain_right'][1]} | "
              f"{r['plain_wrong'][0]} of {r['plain_wrong'][1]} |" for r in a["rows"]]
    md = "\n".join(lines) + "\n"
    Path(args.table).write_text(md, encoding="utf-8")
    print(md)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m eval.graphqa_hard", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    def inputs(p):
        p.add_argument("--build", action="store_true", help="build a fixture's graph / semantic index if unusable")
        p.add_argument("--fixtures-root", default="~/orion/fixtures", help="folder holding NodeGoat and pygoat")
        p.add_argument("--repos-root", default="~/bench-data/repoqa/repos", help="folder of RepoQA's clones")
        p.add_argument("--fixtures", default=",".join(FIXTURES))
        p.add_argument("--cache", default=os.path.join(os.environ.get("XDG_CACHE_HOME") or "~/.cache", "orion", "graphqa"))
    inputs(sub.add_parser("edges", help="how the graph's call edges match the oracle (token-free)"))
    q = sub.add_parser("questions", help="generate the questions (WSL: Neo4j + the fixtures; token-free)")
    inputs(q)
    q.add_argument("--seed", type=int, default=SEED)
    q.add_argument("--per-family", type=int, default=PER_FAMILY)
    q.add_argument("--out", default=str(DATASET))
    r = sub.add_parser("run", help="run the claude -p sessions (spends usage)")
    r.add_argument("--questions", default=str(DATASET))
    r.add_argument("--sessions", default=str(SESSIONS))
    r.add_argument("--models", default=",".join(gq.MODELS))
    r.add_argument("--paths", default=",".join(gq.PATHS))
    r.add_argument("--jobs", type=int, default=1)
    r.add_argument("--limit-wait", type=int, default=gq.LIMIT_WAIT)
    r.add_argument("--max-turns", type=int, default=MAX_TURNS, help="the hard stop, in model round trips")
    r.add_argument("--budget", type=int, default=BUDGET, help="the limit the agent is told (0: say nothing)")
    r.add_argument("--only", default="", help="comma-separated question ids (a smoke run)")
    r.add_argument("--fixtures", default="", help="comma-separated fixtures (default: every fixture in the file)")
    s = sub.add_parser("score", help="score the session log (token-free)")
    s.add_argument("--fixtures", default="", help="comma-separated fixtures (default: every fixture in the file)")
    s.add_argument("--questions", default=str(DATASET))
    s.add_argument("--sessions", default=str(SESSIONS))
    s.add_argument("--out", default=str(RESULTS))
    s.add_argument("--table", default=str(TABLE))
    args = ap.parse_args(argv)
    return {"edges": cmd_edges, "questions": cmd_questions, "run": cmd_run, "score": cmd_score}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
