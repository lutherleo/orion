"""The Claude agents' Cypher policy, applied at the MCP boundary (mcp_server.run_cypher_impl) before a
query reaches Neo4j. Orion's own internal reads never go through it.

Layers, in order (each matched on `graphdb.blank`ed text -- strings, comments and backtick names
removed in ONE lexer pass -- so a quote inside a comment cannot hide the clause after it):
  1. `graphdb.blocked_reason`: write clauses; LOAD CSV (the Neo4j SERVER would fetch the URL, so a
     prompt injection in a scanned repo could send graph data out in it), FOREACH / IN TRANSACTIONS /
     PERIODIC COMMIT; any procedure outside the read-only allowlist (db.schema.*, db.labels, ...).
  2. Admin commands a read transaction may still run: SHOW (lists other sessions' transactions),
     TERMINATE (kills them), USE (switches database).
  3. Scan scope (`scope_problem`, decision D3 2026-10-27): every pattern must be ANCHORED to this
     scan -- see below.
The real write enforcement is the read transaction in GraphDB.run_cypher; these give the agent a
readable error and stop the reads that escape the graph or the scan.

Anchored paths. Neo4j holds every scan (and, in the eval, the PATCHED graph of the same CVE), so a
pattern that is not tied to this scan reads all of them. Edges only ever join nodes of one scan
(schema.emit_edge stamps scan_id on both endpoints; the runtime writer matches both by scan_id), so a
connected pattern is in this scan as soon as ONE of its elements is. An element is tied when
  - its property map holds `scan_id:$scan_id` (or this scan's id as a literal), node or relationship;
  - a top-level WHERE conjunct says `v.scan_id = $scan_id` for its variable (never under OR/XOR/NOT);
  - its variable is tied elsewhere in the same scope (an earlier clause, another tied pattern), and a
    WITH in between carried it (`WITH c`, `WITH c AS d`, `WITH *`).
Each UNION branch and each `{ }` subquery body (CALL/EXISTS/COUNT/COLLECT) is its own scope: nothing
is trusted across it. Index procedures (db.index.*) must filter what they YIELD by scan_id. Anything
the parser cannot read (an inline `(n WHERE ...)`, an unterminated string) is refused with a hint,
never let through. This is deliberately looser than the Orion-LM checker (orion/lm/tools.check_query:
every labeled node scoped, no UNION/CALL/subqueries), whose pattern parsing it shares
(orion/cypher_parse.py); this module never imports orion.lm, so the Claude MCP server does not load
the lm package.
"""
from __future__ import annotations

import re

from .cypher_parse import (_EXPR_HINT, _IDENT, _LIST_PRED, _REL_GAP, _after_dash, _collapse,
                           _depth0, _groups, _is_function_call, _matching, _split_props,
                           _top_level_entries, _word_before)
from .graphdb import BT_PLACEHOLDER, STR_PLACEHOLDER, _lex, blank, blocked_reason

# `SHOW TRANSACTIONS`, `TERMINATE TRANSACTIONS 'id'`, `USE neo4j` -- the command word followed by a
# name, and not a property (`n.use`) or a parameter.
_ADMIN = re.compile(r"(?<![\w.$])(SHOW|TERMINATE|USE)\s+[A-Za-z_`]", re.IGNORECASE)
_ADMIN_MSG = {
    "SHOW": "read-only: SHOW commands are blocked; query the graph only",
    "TERMINATE": "read-only: TERMINATE is blocked",
    "USE": "read-only: USE is blocked; the database is fixed",
}


def admin_command(query: str) -> str | None:
    """The message for the first SHOW / TERMINATE / USE command in `query`, or None. Pure."""
    m = _ADMIN.search(blank(query))
    return _ADMIN_MSG[m.group(1).upper()] if m else None


def claude_problem(query: str, scan_id: str) -> str | None:
    """Why the Claude agents' `query` must not run, or None. Pure/testable."""
    return blocked_reason(query) or admin_command(query) or scope_problem(query, scan_id)


# ── anchored-path scan scoping ──────────────────────────────────────────────────────────────────

MAX_QUERY_CHARS = 20000   # keeps the check cheap; agents' queries are far shorter
_HINT = ("give one node or relationship in each pattern {scan_id:$scan_id} (or WHERE x.scan_id = "
         "$scan_id); what it reaches through relationships is in the same scan")
# A node head for the Claude path: `var`, `var:Label`, `:A:B`, `:A|B`, `:A&!B` (label expressions are
# fine here; only the lm checker refuses them).
_HEAD = re.compile(rf"^\s*(?P<var>{_IDENT})?\s*(?P<labels>:[\w\s:|&!%]*)?$")
_REL_VAR = re.compile(rf"^\s*(?P<var>{_IDENT})?\s*(?::|\*|$)")
_SUBQUERY_OPEN = re.compile(r"\b(?P<kind>EXISTS|COUNT|COLLECT|CALL)\s*(?P<scope>\([^()]*\)\s*)?\{",
                            re.IGNORECASE)
# Keywords are matched only where they cannot be a property (`f.call`, `n.with`) or a parameter.
_KW = r"(?<![.\w$])"
_UNION = re.compile(_KW + r"UNION(?:\s+ALL|\s+DISTINCT)?\b", re.IGNORECASE)
_WITH = re.compile(_KW + r"WITH\b", re.IGNORECASE)
_WHERE = re.compile(_KW + r"WHERE\b", re.IGNORECASE)
# What ends a WHERE body: only clauses that can follow one. SKIP / LIMIT / YIELD never do, so a
# variable or property spelled like them cannot cut the body short and hide an OR after it.
_WHERE_END = re.compile(_KW + r"(?:OPTIONAL\s+MATCH\b|MATCH\b|WITH\b|UNWIND\b|RETURN\b|ORDER\s+BY\b|"
                        r"UNION\b|WHERE\b|CALL\s*[({]|CALL\s+\w+\s*\.)", re.IGNORECASE)
# What ends a WITH projection list (cutting it early only ever carries FEWER variables).
_PROJ_END = re.compile(_KW + r"(?:OPTIONAL\s+MATCH|MATCH|WITH|UNWIND|RETURN|WHERE|ORDER\s+BY|SKIP|LIMIT|"
                       r"UNION|CALL)\b", re.IGNORECASE)
# The clause a WHERE belongs to: a WHERE after OPTIONAL MATCH filters only the optional part, never
# the rows of the patterns before it, so it scopes nothing.
_CLAUSE_START = re.compile(_KW + r"(?:OPTIONAL\s+MATCH|MATCH|WITH|YIELD|UNWIND|RETURN|CALL)\b",
                           re.IGNORECASE)
_OR = re.compile(_KW + r"(?:OR|XOR)\b", re.IGNORECASE)
_AND = re.compile(_KW + r"AND\b", re.IGNORECASE)
_SCOPED_CONJUNCT = re.compile(rf"^(?:(?P<a>{_IDENT})\s*\.\s*scan_id\s*=\s*\$scan_id|"
                              rf"\$scan_id\s*=\s*(?P<b>{_IDENT})\s*\.\s*scan_id)$")
_INDEX_CALL = re.compile(r"\bCALL\s+db\s*\.\s*index\s*\.\s*\w+\s*\.\s*\w+\s*\(", re.IGNORECASE)
_YIELD = re.compile(r"\bYIELD\s+", re.IGNORECASE)


class _Refuse(Exception):
    """A query the scope check must refuse; the message goes back to the agent."""


def _literal_ids_as_param(query: str, scan_id: str) -> str:
    """This scan's id written as a quoted literal counts like `$scan_id` (agents inline it)."""
    if not scan_id or not re.fullmatch(r"[\w.:-]+", scan_id):
        return query
    return re.sub(r"(['\"])" + re.escape(scan_id) + r"\1", "$scan_id", query)


def _depths(text: str) -> list[int]:
    """Bracket depth of every character ((, [ and { together); a closing bracket gets the outer depth."""
    out, d = [], 0
    for ch in text:
        if ch in ")]}":
            d -= 1
        out.append(d)
        if ch in "([{":
            d += 1
    return out


def _top_matches(rx: re.Pattern, text: str, depth: list[int], a: int = 0, b: int | None = None):
    """Matches of `rx` in text[a:b] that start at bracket depth 0."""
    b = len(text) if b is None else b
    return [m for m in rx.finditer(text, a, b) if depth[m.start()] == 0]


def _map_scoped(props: str, what) -> bool:
    """True when a `{...}` map (blanked) holds `scan_id: $scan_id` exactly once. A second scan_id key
    (Neo4j keeps the last one) is refused rather than trusted."""
    values = [val.strip() for key, sep, val in (e.partition(":") for e in _top_level_entries(props))
              if sep and key.strip() == "scan_id"] if props else []
    if len(values) > 1:
        raise _Refuse(f"unscoped query: {what()} has more than one scan_id key; use {{scan_id:$scan_id}} once")
    return values == ["$scan_id"]


def _where_scoped(text: str, depth: list[int], a: int, b: int) -> set[str]:
    """Variables a depth-0 WHERE in text[a:b] pins to this scan with a top-level `v.scan_id = $scan_id`
    conjunct. A WHERE with a top-level OR/XOR pins nothing (the filter would be optional)."""
    out: set[str] = set()
    for w in _top_matches(_WHERE, text, depth, a, b):
        owners = _top_matches(_CLAUSE_START, text, depth, a, w.start())
        if owners and owners[-1].group(0).upper().startswith("OPTIONAL"):
            continue
        ends = [m.start() for m in _top_matches(_WHERE_END, text, depth, w.end(), b)]
        body_end = min(ends) if ends else b
        body = text[w.end():body_end]
        bd = [depth[w.end() + i] for i in range(len(body))]
        if any(bd[m.start()] == 0 for m in _OR.finditer(body)):
            continue
        cuts = [0] + [x for m in _AND.finditer(body) if bd[m.start()] == 0 for x in (m.start(), m.end())] + [len(body)]
        for k in range(0, len(cuts), 2):
            part = body[cuts[k]:cuts[k + 1]].strip()
            while part.startswith("(") and part.endswith(")") and _matching(part, 0) == len(part) - 1:
                part = part[1:-1].strip()
            m = _SCOPED_CONJUNCT.match(part)
            if m:
                out.add(m.group("a") or m.group("b"))
    return out


def _carried(text: str, depth: list[int], at: int, b: int, trusted: set[str]) -> set[str]:
    """The tied variables a `WITH` at `at` carries into the next clause: `WITH c`, `WITH c AS d`, `*`."""
    ends = [m.start() for m in _top_matches(_PROJ_END, text, depth, at + 4, b)]
    items_text = text[at + 4:(min(ends) if ends else b)].strip()
    items_text = re.sub(r"^DISTINCT\b", "", items_text, flags=re.IGNORECASE).strip()
    out: set[str] = set()
    for item in _top_level_entries("{" + items_text + "}"):
        item = item.strip()
        if item == "*":
            out |= trusted
        elif (m := re.fullmatch(rf"({_IDENT})(?:\s+AS\s+({_IDENT}))?", item, re.IGNORECASE)) and m.group(1) in trusted:
            out.add(m.group(2) or m.group(1))
    return out


def _segment(text, depth, a, b, carried: set[str], shown) -> set[str]:
    """Check one clause run text[a:b] (between WITHs); return the variables it ties to the scan."""
    trusted = set(carried) | _where_scoped(text, depth, a, b)
    groups, _top = _groups(text)
    inside = [i for i, g in enumerate(groups) if a <= g.start < b]
    nodes: dict[int, dict] = {}
    for i in inside:
        g = groups[i]
        if _is_function_call(text, g.start):
            g.func = True
            continue
        body = text[g.start + 1:g.end - 1]
        d0 = _depth0(body)
        if _LIST_PRED.match(body):
            continue
        if _WHERE.search(d0):
            raise _Refuse(f"unscoped query: cannot check {shown(g.start, g.end)}: put conditions in a WHERE "
                          f"after the pattern, e.g. MATCH (x:CpgCall {{scan_id:$scan_id}}) WHERE ...")
        split = _split_props(body)
        head = _HEAD.match(split[0]) if split else None
        if head is None:
            if ":" in d0 or not _EXPR_HINT.search(d0):
                raise _Refuse(f"unscoped query: cannot read the pattern {shown(g.start, g.end)}: write nodes "
                              f"as (x:Label {{scan_id:$scan_id}})")
            continue                      # an expression; its child groups are checked on their own
        nodes[i] = {"var": head.group("var"), "scoped": _map_scoped(split[1], lambda: shown(g.start, g.end)),
                    "span": (g.start, g.end)}
    # Chains: sibling node groups joined by a relationship gap form one connected pattern; the
    # relationship brackets in each gap are elements of it too.
    chains: list[list[dict]] = []
    parents = [None] + [i for i in inside]
    for parent in parents:
        sibs = [i for i in inside if groups[i].parent == parent] if parent is not None else \
            [i for i in inside if groups[i].parent is None or groups[i].parent not in inside]
        chain: list[dict] = []
        prev_end = None
        for i in sibs + [None]:
            node = nodes.get(i) if i is not None else None
            if node is not None and chain and prev_end is not None:
                gap = text[prev_end:node["span"][0]]
                if _REL_GAP.match(_collapse(gap)):
                    chain.extend(_rels(text, prev_end, node["span"][0], shown))
                    chain.append(node)
                    prev_end = node["span"][1]
                    continue
            if chain:
                chains.append(chain)
            chain = [node] if node is not None else []
            prev_end = node["span"][1] if node is not None else None
    # Tie chains to the scan until nothing changes (a tied chain ties its variables, which can tie
    # another chain that shares one).
    tied = [False] * len(chains)
    changed = True
    while changed:
        changed = False
        for k, chain in enumerate(chains):
            if not tied[k] and any(e["scoped"] or (e["var"] and e["var"] in trusted) for e in chain):
                tied[k] = changed = True
                trusted |= {e["var"] for e in chain if e["var"]}
    for k, chain in enumerate(chains):
        if not tied[k]:
            a0, b0 = chain[0]["span"][0], chain[-1]["span"][1]
            raise _Refuse(f"unscoped query: {shown(a0, b0)} is not tied to this scan -- {_HINT}")
    # Index procedures return nodes of every scan: what they YIELD must be filtered by scan_id.
    for m in _top_matches(_INDEX_CALL, text, depth, a, b):
        y = _YIELD.search(text, m.end(), b)
        first = re.match(rf"\s*({_IDENT})", text[y.end():b]) if y else None
        if first is None or first.group(1) not in trusted:
            raise _Refuse("unscoped query: an index procedure returns nodes of every scan -- filter them, "
                          "e.g. ... YIELD node, score WHERE node.scan_id = $scan_id")
    return trusted


def _rels(text, a, b, shown) -> list[dict]:
    """The relationship brackets in a gap text[a:b] between two node groups, as chain elements."""
    out = []
    i = text.find("[", a, b)
    while 0 <= i < b:
        j = _matching(text, i)
        if _after_dash(text, i):
            split = _split_props(text[i + 1:j])
            head = _REL_VAR.match(split[0]) if split else None
            out.append({"var": head.group("var") if head else None,
                        "scoped": bool(split) and _map_scoped(split[1], lambda: shown(i, j + 1)),
                        "span": (i, j + 1)})
        i = text.find("[", j + 1, b)
    return out


def _body(text: str, shown, inherited: frozenset = frozenset()) -> None:
    """Check one query body (a whole query, or one subquery's braces): every UNION branch, clause run
    by clause run, carrying tied variables through WITH; then each subquery body on its own.

    `inherited`: outer variables already tied that this body can see. EXISTS / COUNT / COLLECT bodies
    see the enclosing clause's variables; a CALL body sees only what it imports -- `CALL (a, b) {`,
    `CALL (*) {`, or a leading `WITH a` -- so a fresh `(a:X)` inside a plain `CALL { }` is never
    mistaken for the outer `a`."""
    depth = _depths(text)
    work = list(text)
    subs = []
    for m in _SUBQUERY_OPEN.finditer(text):
        i = m.end() - 1
        if depth[m.start()] != 0 or work[i] != "{":
            continue                       # nested deeper: its own body handles it
        j = _matching(text, i)
        subs.append((m, i, j))
        work[i + 1:j] = " " * (j - i - 1)  # the outer scope does not see inside it
    w = "".join(work)
    depth = _depths(w)
    seg_trust: list[tuple[int, int, set[str]]] = []
    cuts = [0] + [x for m in _top_matches(_UNION, w, depth) for x in (m.start(), m.end())] + [len(w)]
    for k in range(0, len(cuts), 2):
        a, b = cuts[k], cuts[k + 1]
        withs = [m.start() for m in _top_matches(_WITH, w, depth, a, b)
                 if _word_before(w, len(w[:m.start()].rstrip())).upper() not in ("STARTS", "ENDS")]
        bounds = [a] + withs + [b]
        carried: set[str] = set(inherited)
        for s in range(len(bounds) - 1):
            trusted = _segment(w, depth, bounds[s], bounds[s + 1], carried, shown)
            seg_trust.append((bounds[s], bounds[s + 1], trusted))
            nxt = bounds[s + 1]
            carried = _carried(w, depth, nxt, b, trusted) if nxt < b else set()
    for m, i, j in subs:
        here = next((t for a, b, t in seg_trust if a <= i < b), set())
        inner = text[i + 1:j]
        scope = (m.group("scope") or "").strip()
        if m.group("kind").upper() != "CALL":
            sees = set(here)
        elif scope:
            names = {n.strip() for n in scope[1:-1].split(",")}
            sees = set(here) if "*" in names else names & here
        else:
            sees = set(here) if re.match(r"\s*WITH\b", inner, re.IGNORECASE) else set()
        _body(inner, lambda x, y, o=i + 1: shown(x + o, y + o), frozenset(sees))


def scope_problem(query: str, scan_id: str) -> str | None:
    """Why `query` is not anchored to this scan (see the module doc), or None. Pure/testable."""
    if len(query) > MAX_QUERY_CHARS:
        return f"query too long to check (max {MAX_QUERY_CHARS} characters)"
    q = _literal_ids_as_param(query, scan_id)
    text, spans = _lex(q)

    def shown(a: int, b: int) -> str:
        s = q[spans[a][0]:spans[b - 1][1]] if b > a else ""
        return s if len(s) <= 120 else s[:120] + "..."

    if "`" in text or "'" in text or '"' in text or "/*" in text:
        return "unscoped query: cannot check it: an unterminated string, comment or backtick name"
    stack: list[str] = []
    for ch in text:
        if ch in "([{":
            stack.append(ch)
        elif ch in ")]}":
            if not stack or stack.pop() != "([{"[")]}".index(ch)]:
                return "unscoped query: cannot check it: unbalanced brackets"
    if stack:
        return "unscoped query: cannot check it: unbalanced brackets"
    try:
        _body(text, shown)
    except _Refuse as exc:
        return str(exc)
    return None
