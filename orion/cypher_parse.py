"""Cypher pattern parsing shared by the two query guards: the Orion-LM checker (orion/lm/tools.py,
strict: every labeled node scoped) and the Claude agents' guard (orion/cypher_guard.py, anchored paths).

Moved verbatim from orion/lm/tools.py on 2026-10-28 so both guards read patterns the same way without
the Claude MCP server importing orion.lm. Everything here works on `graphdb.blank`ed text (strings,
comments and backtick names already replaced) and is pure.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


# A word right before `(` that makes the group a function call -- unless it is one of these
# keywords, after which a pattern can start (`MATCH(n)`, `WHERE NOT(...)`, `ANY SHORTEST(a)`).
_KEYWORDS = frozenset("""
    MATCH OPTIONAL MERGE CREATE WHERE AND OR XOR NOT WHEN THEN ELSE CASE RETURN WITH UNWIND AS IN
    DISTINCT IS SET DELETE DETACH REMOVE ON YIELD ORDER BY SKIP LIMIT OFFSET ASC DESC ASCENDING
    DESCENDING STARTS ENDS CONTAINS SHORTEST GROUPS GROUP ANY ALL NONE SINGLE PATH PATHS WALK TRAIL
    ACYCLIC SIMPLE REPEATABLE DIFFERENT ELEMENTS ELEMENT RELATIONSHIPS RELATIONSHIP NODETACH FILTER
    LET NEXT FINISH NULL TRUE FALSE END
""".split())


# Identifiers: unicode word characters, not starting with a digit. Neo4j also allows characters that
# are not `\w` (currency symbols, combining marks); a group using one fails to parse here and is
# rejected below rather than taken for an expression.
_IDENT = r"(?!\d)\w+"


_NODE_HEAD = re.compile(rf"^\s*(?P<var>{_IDENT})?\s*(?P<labels>(?::\s*{_IDENT}\s*)*)$")


# `x IN list WHERE pred` -- the body of any()/all()/none()/single(); never a node pattern.
_LIST_PRED = re.compile(rf"^\s*{_IDENT}\s+IN\b", re.IGNORECASE)


# Evidence that a parenthesised group is an expression (property access, operator, call, list,
# number, boolean keyword) rather than a node pattern, which can never contain any of these.
_EXPR_HINT = re.compile(r"[.+\-*/%^=<>!(\[]|\b(?:AND|OR|XOR|NOT|IN|CONTAINS|STARTS|ENDS|CASE)\b|^\s*[0-9]",
                        re.IGNORECASE)


# Text allowed between two node patterns of one path (rel bracket already collapsed to `[]`).
_REL_GAP = re.compile(r"^\s*<?-\s*(?:\[\]\s*)?-\s*>?\s*$")


def _word_before(text: str, i: int) -> str:
    """The run of word characters ending right before index i ('' if none)."""
    j = i
    while j > 0 and (text[j - 1].isalnum() or text[j - 1] == "_"):
        j -= 1
    return text[j:i]


def _after_dash(text: str, i: int) -> bool:
    """The last non-space character before index i is `-` (so a `[` at i opens a relationship)."""
    j = i - 1
    while j >= 0 and text[j].isspace():
        j -= 1
    return j >= 0 and text[j] == "-"


def _matching(text: str, i: int) -> int:
    """Index of the bracket closing the one opened at `i` (text is known to be balanced)."""
    depth = 0
    for j in range(i, len(text)):
        if text[j] in "([{":
            depth += 1
        elif text[j] in ")]}":
            depth -= 1
            if depth == 0:
                return j
    return len(text) - 1


def _depth0(s: str) -> str:
    """`s` with the contents of every nested (), [] and {} removed (the brackets themselves kept)."""
    out, depth = [], 0
    for ch in s:
        if ch in "([{":
            if depth == 0:
                out.append(ch)
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth == 0:
                out.append(ch)
        elif depth == 0:
            out.append(ch)
    return "".join(out)


def _top_level_entries(props: str) -> list[str]:
    """Split a `{k:v, ...}` map body on top-level commas (nesting-aware)."""
    inner = props.strip()[1:-1]
    if not inner.strip():
        return []
    out, depth, cur = [], 0, []
    for ch in inner:
        if ch in "{[(":
            depth += 1
        elif ch in "}])":
            depth -= 1
        if ch == "," and depth == 0:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return out


def _split_props(body: str) -> tuple[str, str] | None:
    """(head, props) where props is the single trailing balanced `{...}` map ('' if none);
    None when anything but whitespace follows the map."""
    i = body.find("{")
    if i < 0:
        return body, ""
    j = _matching(body, i)
    if body[j] != "}" or body[j + 1:].strip():
        return None
    return body[:i], body[i:j + 1]


@dataclass
class _Group:
    start: int            # index of "(" in the blanked text
    end: int = -1         # index just past ")"
    parent: int | None = None
    children: list[int] = field(default_factory=list)
    func: bool = False    # a function call's argument list, not a pattern
    node: bool = False
    labeled: bool = False
    scoped: bool = False
    has_node: bool = False  # this group or a descendant is a node pattern


def _groups(text: str) -> tuple[list[_Group], list[int]]:
    """Every parenthesised group (nested ones too), plus the indices of the top-level ones."""
    groups: list[_Group] = []
    top: list[int] = []
    stack: list[int] = []
    for i, ch in enumerate(text):
        if ch == "(":
            g = _Group(start=i, parent=stack[-1] if stack else None)
            groups.append(g)
            idx = len(groups) - 1
            (groups[g.parent].children if g.parent is not None else top).append(idx)
            stack.append(idx)
        elif ch == ")":
            groups[stack.pop()].end = i + 1
    return groups, top


def _is_function_call(text: str, i: int) -> bool:
    """`(` at i directly follows a word that names a function (not a keyword or a number)."""
    word = _word_before(text, i)
    return bool(word) and not word.isdigit() and word.upper() not in _KEYWORDS


def _collapse(gap: str) -> str:
    """Collapse each (possibly nested) [...] in a gap to `[]`."""
    prev = None
    while prev != gap:
        prev, gap = gap, re.sub(r"\[[^\[\]]*\]", "[]", gap)
    return gap
