"""Precomputed shortlists for discovery shapes B, C and D: what `pathfind` does for shape A.

Shape A judges ranked :CandidateFlow nodes instead of hand-writing FLOWS_TO Cypher. Shapes B/C/D had
no such list: each agent spent its turns searching. This module finds their candidates at BUILD
time, lexically, over the first-party source files the graph already knows (CpgFile paths) plus the
:Dependency nodes, and emits one node per candidate:

  (:CandidateFinding {scan_id, uid, shape, kind, file_path, line, cwe, code, detail, rank})

  B  absence of a control  -- the profile's `controls` checklist: each control is `present` (where),
                              `commented_out`, `absent` (not found anywhere), or explicitly `disabled`.
  C  disabled/reverted fix -- a COMMENT with a hedge word (fix, todo, insecure, disabled, ...) near a
                              security term, plus any commented-out code right under it.
  D  pattern-in-data       -- regex literals with nested quantifiers (static ReDoS heuristic), regexes
                              built from a variable, and the declared dependencies.

Comments matter here: the graph carries call code, not comments, so shape C could not see a
"// Fix for A3 - XSS" block at all before. `rank` is per shape (0 = best) and unique within it.

Pure over the batch + repo files (no Neo4j). It only ADDS nodes of a new NODE_KEY label, so the
taint-parity tripwires are untouched, and it never raises: an unreadable file is skipped. Every row
is a CANDIDATE the agent re-checks; the detectors are deliberately lexical and generous.
"""
from __future__ import annotations

import hashlib
import os
import re

from . import schema

MAX_PER_SHAPE = 60          # rows persisted per shape, best-ranked first (like pathfind's _MAX_FLOWS)
_MAX_FILE_BYTES = 1_000_000
_MAX_LINE_CHARS = 400       # longer lines are minified/generated code, not something a human wrote
_CODE_SNIPPET = 200
_PER_CONTROL_HITS = 3       # locations kept per (control, kind)

_SKIP_DIRS = {"node_modules", "bower_components", "vendor", "site-packages", ".venv", "venv",
              "__pycache__", ".git", "test", "tests", "__tests__", "spec", "e2e"}
_HASH_COMMENT_EXT = {".py", ".rb", ".sh", ".bash", ".pl", ".r", ".yaml", ".yml", ".toml"}

# Shape C: hedge words (weight) and the security vocabulary that must sit near one.
_HEDGE = re.compile(
    r"\b(vulnerab\w*|insecure|unsafe|disabled?|bypass\w*|hack|fix(?:me|es|ed)?|todo|xxx|"
    r"workaround|temporar\w*|do not ship|remove (?:this|before))\b", re.IGNORECASE)
_STRONG_HEDGE = re.compile(r"vulnerab|insecure|unsafe|disable|bypass|hack", re.IGNORECASE)
_SECURITY = re.compile(
    r"(auth|passw|passwd|secret|token|csrf|xss|saniti[sz]|escap|inject|sql|crypt|hash|session|"
    r"cookie|permission|privilege|role|admin|redirect|ssl|tls|cert|https|owasp|security|"
    r"validat|whitelist|allowlist|traversal|deserial|eval\b|exec\b|regex|dos\b|header)", re.IGNORECASE)
_CODE_LIKE = re.compile(r"[;{}()=]|^\s*(?:return|if|var|let|const|def|import|from)\b")
_C_WINDOW = 2               # lines either side searched for a security term / commented-out code

# Shape D: where regexes come from.
_JS_REGEX_LITERAL = re.compile(
    r"(?:^|[=(,:;!&|?{}\[\s])/((?![*/])(?:\\.|\[(?:\\.|[^\]\\])*\]|[^/\\\n\[])+)/[a-z]*")
_REGEX_CALL = re.compile(
    r"(?:\bRegExp|\bre\.(?:compile|match|search|fullmatch|sub|subn|findall|finditer|split)|"
    r"Pattern\.compile|regexp\.(?:MustCompile|Compile)|\bRegex)\s*\(\s*(.*)")
_STRING_LITERAL = re.compile(r"""^[rbuf]{0,2}(["'`])((?:\\.|(?!\1).)*)\1""")


def _uid(scan_id: str, shape: str, kind: str, file_path, line, detail: str) -> str:
    key = f"{scan_id}|CandidateFinding|{shape}|{kind}|{file_path or ''}|{line or ''}|{detail}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


# ── source access ───────────────────────────────────────────────────────
def _skip_path(rel: str) -> bool:
    parts = re.split(r"[\\/]", rel.lower())
    return any(p in _SKIP_DIRS for p in parts[:-1]) or parts[-1].endswith((".min.js", ".bundle.js"))


def source_files(batch: schema.Batch, repo: str) -> list[tuple[str, str]]:
    """(repo-relative path, absolute path) for every first-party CpgFile that exists on disk, sorted.
    Joern's pseudo-files (`<unknown>`, `<includes>`) and dependency/test dirs are skipped."""
    seen: dict[str, str] = {}
    for label, props in batch.nodes:
        if label != "CpgFile":
            continue
        fp = props.get("file_path")
        if not isinstance(fp, str) or not fp or fp.startswith("<"):
            continue
        full = fp if os.path.isabs(fp) else os.path.join(repo, fp)
        rel = os.path.relpath(full, repo).replace("\\", "/") if os.path.isabs(fp) else fp.replace("\\", "/")
        if rel.startswith("../") or _skip_path(rel) or rel in seen:
            continue
        if os.path.isfile(full):
            seen[rel] = full
    return sorted(seen.items())


def _read_lines(path: str) -> list[str] | None:
    try:
        if os.path.getsize(path) > _MAX_FILE_BYTES:
            return None
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read().splitlines()
    except OSError:
        return None


def split_comment(line: str, in_block: bool, style: str) -> tuple[str, str, bool]:
    """Split one source line into (code, comment, still_in_block_comment). `style` is "c" (// and
    /* */) or "hash" (#). Quote-aware within the line, so a `//` inside a string (a URL) stays code.
    Strings spanning lines (template literals, triple quotes) are not tracked -- a lexical tool."""
    code: list[str] = []
    comment: list[str] = []
    i, n = 0, len(line)
    quote: str | None = None
    while i < n:
        if in_block:
            j = line.find("*/", i)
            if j < 0:
                comment.append(line[i:])
                return "".join(code), " ".join(comment), True
            comment.append(line[i:j])
            i, in_block = j + 2, False
            continue
        ch = line[i]
        if quote:
            code.append(ch)
            if ch == "\\" and i + 1 < n:
                code.append(line[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in "\"'`":
            quote = ch
        elif style == "c" and line.startswith("//", i):
            comment.append(line[i + 2:])
            break
        elif style == "c" and line.startswith("/*", i):
            in_block = True
            i += 2
            continue
        elif style == "hash" and ch == "#":
            comment.append(line[i + 1:])
            break
        code.append(ch)
        i += 1
    return "".join(code), " ".join(comment), in_block


def _split_file(rel: str, lines: list[str]) -> list[tuple[int, str, str, str]]:
    """[(lineno, raw, code, comment)] for every line short enough to be hand-written."""
    style = "hash" if os.path.splitext(rel)[1].lower() in _HASH_COMMENT_EXT else "c"
    out = []
    in_block = False
    for lineno, raw in enumerate(lines, start=1):
        if len(raw) > _MAX_LINE_CHARS:
            continue
        code, comment, in_block = split_comment(raw, in_block, style)
        out.append((lineno, raw, code, comment))
    return out


def _snippet(text: str) -> str:
    return text.strip()[:_CODE_SNIPPET]


# ── shape B: controls checklist ─────────────────────────────────────────
def controls_rows(files: list[tuple[str, list]], profile) -> list[dict]:
    """One or more rows per control in `profile.controls`, ranked disabled -> commented_out ->
    absent -> present, then by checklist order."""
    rows: list[dict] = []
    order = {"disabled": 0, "commented_out": 1, "absent": 2, "present": 3}
    for ci, control in enumerate(getattr(profile, "controls", ()) or ()):
        present = [re.compile(p, re.IGNORECASE) for p in control.present]
        disabled = [re.compile(p, re.IGNORECASE) for p in control.disabled]
        hits: dict[str, list[tuple[str, int, str]]] = {"present": [], "commented_out": [], "disabled": []}
        for rel, split in files:
            for lineno, raw, code, comment in split:
                if any(p.search(code) for p in disabled):
                    hits["disabled"].append((rel, lineno, raw))
                if any(p.search(code) for p in present):
                    hits["present"].append((rel, lineno, raw))
                elif comment and any(p.search(comment) for p in present):
                    hits["commented_out"].append((rel, lineno, raw))
        kinds: list[str] = ["disabled"] if hits["disabled"] else []
        if hits["present"]:
            kinds.append("present")
        elif hits["commented_out"]:
            kinds.append("commented_out")
        elif present:
            kinds.append("absent")
        for kind in kinds:
            if kind == "absent":
                rows.append({"kind": kind, "file_path": None, "line": None, "cwe": control.cwe, "code": "",
                             "detail": f"{control.name}: no use found in first-party source", "_k": (order[kind], ci)})
                continue
            note = {"disabled": "explicitly switched off", "present": "in use",
                    "commented_out": "only appears inside a comment"}[kind]
            for rel, lineno, raw in hits[kind][:_PER_CONTROL_HITS]:
                rows.append({"kind": kind, "file_path": rel, "line": lineno, "cwe": control.cwe,
                             "code": _snippet(raw), "detail": f"{control.name}: {note}",
                             "_k": (order[kind], ci, rel, lineno)})
    rows.sort(key=lambda r: r["_k"])
    return rows


# ── shape C: hedge comments near security code ──────────────────────────
def hedge_rows(files: list[tuple[str, list]]) -> list[dict]:
    """A comment with a hedge word and a security term within `_C_WINDOW` lines, scored by hedge
    strength, the security term, and commented-out code right below it. Consecutive hits in one
    comment block collapse into the first line."""
    rows: list[dict] = []
    for rel, split in files:
        by_line = {ln: (raw, code, comment) for ln, raw, code, comment in split}
        last_hit = -10
        for lineno, raw, _code, comment in split:
            m = _HEDGE.search(comment) if comment else None
            if not m:
                continue
            if lineno - last_hit <= _C_WINDOW + 1:
                last_hit = lineno
                continue
            # The comment's own security term first (it names the bug), then the surrounding lines.
            window = [by_line[lineno]] + [by_line[k] for k in range(lineno - _C_WINDOW, lineno + _C_WINDOW + 1)
                                          if k in by_line and k != lineno]
            term = next((t.group(0) for _r, c, cm in window for t in [_SECURITY.search(cm + " " + c)] if t), None)
            if term is None:
                continue
            below = [by_line[k][2].strip() for k in range(lineno + 1, lineno + _C_WINDOW + 2)
                     if k in by_line and by_line[k][2].strip() and not by_line[k][1].strip()]
            disabled_code = [c for c in below if _CODE_LIKE.search(c)]
            score = (3 if _STRONG_HEDGE.search(m.group(0)) else 2) + 2 + (2 if disabled_code else 0)
            detail = f"hedge '{m.group(0).lower()}' near '{term.lower()}'"
            if disabled_code:
                detail += f"; commented-out code: {disabled_code[0][:80]}"
            rows.append({"kind": "commented_out_fix" if disabled_code else "hedge_comment",
                         "file_path": rel, "line": lineno, "cwe": None, "code": _snippet(raw),
                         "detail": detail, "_k": (-score, rel, lineno)})
            last_hit = lineno
    rows.sort(key=lambda r: r["_k"])
    return rows


# ── shape D: regexes and dependencies ───────────────────────────────────
def _quantifier_at(p: str, i: int) -> int:
    """Length of an UNBOUNDED quantifier (+, *, {n,}, {n,m}) starting at p[i], else 0. `?` and an
    exact `{n}` are bounded and do not count."""
    if i >= len(p):
        return 0
    if p[i] in "+*":
        return 1
    m = re.match(r"\{\d*,\d*\}", p[i:])
    return len(m.group(0)) if m else 0


def nested_quantifier(pattern: str) -> bool:
    """True when a quantified group itself contains a quantifier -- (a+)+, (\\w+\\s?)*, ((x*))+ --
    the classic catastrophic-backtracking shape. Escapes and character classes are skipped, so
    `[a+]+` and `\\(a+\\)+` do not count. A heuristic: it misses overlapping alternations."""
    stack: list[bool] = []          # per open group: does it contain a quantifier?
    i, n = 0, len(pattern)
    while i < n:
        ch = pattern[i]
        if ch == "\\":
            i += 2
            q = _quantifier_at(pattern, i)
            if q and stack:
                stack[-1] = True
            i += q
            continue
        if ch == "[":
            j = i + 1
            if j < n and pattern[j] == "^":
                j += 1
            if j < n and pattern[j] == "]":
                j += 1
            while j < n and pattern[j] != "]":
                j += 2 if pattern[j] == "\\" else 1
            i = j + 1
        elif ch == "(":
            stack.append(False)
            i += 1
            if i < n and pattern[i] == "?":          # (?:  (?=  (?<name>  -- a modifier, not a quantifier
                i += 1
            continue
        elif ch == ")":
            inner = stack.pop() if stack else False
            i += 1
            q = _quantifier_at(pattern, i)
            if q and inner:
                return True
            if stack and (inner or q):
                stack[-1] = True
            i += q
            continue
        else:
            i += 1
        q = _quantifier_at(pattern, i)
        if q and stack:
            stack[-1] = True
        i += q
    return False


def _regexes_in(code: str) -> list[tuple[str, str]]:
    """(kind, regex text) found in one line of code: literal regexes, and the first argument of a
    regex constructor -- a string literal, or a variable (a regex built from data)."""
    out = [("literal", m.group(1)) for m in _JS_REGEX_LITERAL.finditer(code)]
    for m in _REGEX_CALL.finditer(code):
        arg = m.group(1).lstrip()
        lit = _STRING_LITERAL.match(arg)
        if lit:
            out.append(("literal", lit.group(2)))
        elif arg and (arg[0].isalpha() or arg[0] == "_") and not arg.startswith(("r'", 'r"')):
            out.append(("dynamic", re.split(r"[),]", arg, maxsplit=1)[0].strip()[:80]))
    return out


def pattern_rows(files: list[tuple[str, list]], batch: schema.Batch) -> list[dict]:
    """ReDoS-shaped regex literals first, then regexes built from a variable, then every declared
    dependency (name@version) for the components-with-known-vulnerabilities check."""
    rows: list[dict] = []
    for rel, split in files:
        for lineno, raw, code, _comment in split:
            for kind, rx in _regexes_in(code):
                if kind == "literal" and nested_quantifier(rx):
                    rows.append({"kind": "redos_regex", "file_path": rel, "line": lineno, "cwe": "CWE-1333",
                                 "code": _snippet(raw), "detail": f"nested quantifier in /{rx[:80]}/",
                                 "_k": (0, rel, lineno)})
                elif kind == "dynamic":
                    rows.append({"kind": "dynamic_regex", "file_path": rel, "line": lineno, "cwe": "CWE-1333",
                                 "code": _snippet(raw), "detail": f"regex built from `{rx}`",
                                 "_k": (1, rel, lineno)})
    deps = sorted({(str(p.get("name")), str(p.get("version") or "")) for label, p in batch.nodes
                   if label == "Dependency" and p.get("name")})
    for name, version in deps:
        rows.append({"kind": "dependency", "file_path": None, "line": None, "cwe": "CWE-1104", "code": "",
                     "detail": f"{name}@{version}" if version else name, "_k": (2, name)})
    rows.sort(key=lambda r: r["_k"])
    return rows


# ── entry point ─────────────────────────────────────────────────────────
def shortlists(batch: schema.Batch, profile, repo: str | None) -> dict:
    """Emit ranked :CandidateFinding nodes for shapes B, C and D onto `batch`. Returns
    {"B": n, "C": n, "D": n, "files": n} (rows emitted per shape, files scanned). Never raises on
    file problems; with no repo checkout only the dependency rows are produced."""
    files: list[tuple[str, list]] = []
    for rel, full in (source_files(batch, repo) if repo else []):
        lines = _read_lines(full)
        if lines is not None:
            files.append((rel, _split_file(rel, lines)))
    out = {"files": len(files)}
    # No files read means nothing can be called absent: B (and C) need source to say anything.
    for shape, rows in (("B", controls_rows(files, profile) if files else []), ("C", hedge_rows(files)),
                        ("D", pattern_rows(files, batch))):
        rows = rows[:MAX_PER_SHAPE]
        for rank, row in enumerate(rows):
            row.pop("_k", None)
            props = {"uid": _uid(batch.scan_id, shape, row["kind"], row["file_path"], row["line"], row["detail"]),
                     "shape": shape, "rank": rank, **{k: v for k, v in row.items() if v is not None}}
            batch.emit_node("CandidateFinding", props)
        out[shape] = len(rows)
    return out
