"""Read-only Neo4j access: the grounding tool.

Every agent claim has to be backed by a query result from here. This is the single tool that keeps
the agents honest: they must query for a vulnerability, not assert it.

The MCP server (orion/mcp_server.py) wraps `run_cypher` as the read-only tool the agents call.
The safety contract lives here: read only, scoped to one scan_id. Two layers keep it read-only:
a write-keyword text guard (a friendly early error), and -- the real enforcement -- every
`run_cypher` runs in a READ-access session, so Neo4j itself refuses any write the text guard misses.
`clear_scan` is the one write, and it is NOT agent-reachable — only the harness calls it, at the
start of a build.
"""
from __future__ import annotations

import re

from neo4j import GraphDatabase, unit_of_work

from . import config
from .graph import scans
from .graph.schema import NODE_KEY

_WRITE = re.compile(r"\b(CREATE|MERGE|DELETE|SET|REMOVE|DROP|DETACH)\b", re.IGNORECASE)
# Read clauses that still reach OUTSIDE the graph or batch-execute: LOAD CSV can fetch a URL (so a
# prompt-injected agent could exfiltrate graph data in the query string) or read server files;
# FOREACH / IN TRANSACTIONS / PERIODIC COMMIT exist only to drive writes.
_ESCAPE = re.compile(r"\bLOAD\s+CSV\b|\bFOREACH\b|\bIN\s+TRANSACTIONS\b|\bPERIODIC\s+COMMIT\b",
                     re.IGNORECASE)
# `CALL name(` / `CALL name` -- a procedure call. `CALL {` (a subquery) is not matched.
_PROC_CALL = re.compile(r"\bCALL\s+([A-Za-z_][\w.]*)", re.IGNORECASE)
# Read-only procedures an agent legitimately needs (schema introspection, index lookups).
_ALLOWED_PROCS = re.compile(
    r"^db\.(schema\.\w+|labels|relationshipTypes|propertyKeys|index\.fulltext\.query\w*|"
    r"index\.vector\.queryNodes)$", re.IGNORECASE)
# Cypher string literals ('...' or "..."), escaped-quote aware.
_STRING_LITERAL = re.compile(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"")
# ONE left-to-right lexer pass over strings, comments and backtick identifiers together, so a quote
# inside a backtick identifier (`'`) or a comment cannot open a fake "string" that hides the clause
# after it, and a `//` inside a string is not a comment. Whichever token starts first wins.
_LEXEME = re.compile(
    _STRING_LITERAL.pattern + r"|(?P<line>//[^\n]*)|(?P<block>/\*.*?\*/)|(?P<bt>`[^`]*`)", re.DOTALL
)
# Placeholders: word-shaped, so a backticked label or name still reads as an identifier.
BT_PLACEHOLDER = "_bt_"
STR_PLACEHOLDER = "_str_"


def _lex(query: str) -> tuple[str, list[tuple[int, int]]]:
    """Blank `query`: string literal -> `_str_`, comment -> one space, backtick identifier -> `_bt_`.
    Returns (blanked text, spans) where spans[i] is the (start, end) slice of `query` that produced
    character i, so a slice of the blanked text can be shown to the user exactly as they wrote it.
    An unterminated quote/backtick/comment is left in place (callers fail closed on it)."""
    out: list[str] = []
    spans: list[tuple[int, int]] = []
    pos = 0
    for m in _LEXEME.finditer(query):
        for k in range(pos, m.start()):
            out.append(query[k])
            spans.append((k, k + 1))
        if m.group("line") is not None or m.group("block") is not None:
            rep = " "
        elif m.group("bt") is not None:
            rep = BT_PLACEHOLDER
        else:
            rep = STR_PLACEHOLDER
        for ch in rep:
            out.append(ch)
            spans.append((m.start(), m.end()))
        pos = m.end()
    for k in range(pos, len(query)):
        out.append(query[k])
        spans.append((k, k + 1))
    return "".join(out), spans


def blank(query: str) -> str:
    """`query` with strings, comments and backtick identifiers blanked in one pass (see `_lex`)."""
    return _lex(query)[0]


def _has_write_keyword(query: str) -> bool:
    """True if `query` contains a Cypher write clause outside any string literal, comment or backtick
    identifier. Blanking literals keeps a legitimate READ query that searches source text for DML
    words -- e.g. `WHERE c.code CONTAINS 'SET role=admin'` (a natural SQLi-hunting query for a
    security scanner) -- from being falsely rejected. Pure/testable. This is an early, friendly
    error only; the read transaction in `GraphDB.run_cypher` is what actually enforces it."""
    return bool(_WRITE.search(blank(query)))


def blocked_reason(query: str) -> str | None:
    """Why `query` must not run, or None. Pure/testable. The pre-check in front of a server-enforced
    read transaction (run_cypher): it turns the common mistakes into a readable error for the agent
    and blocks the reads that escape the graph, which a read transaction alone would allow. Every
    check runs on the `blank`ed text, so a quote inside a comment cannot hide a clause after it."""
    if _has_write_keyword(query):
        return "read-only: write keywords are blocked"
    q = blank(query)
    m = _ESCAPE.search(q)
    if m:
        return f"read-only: {' '.join(m.group(0).upper().split())} is blocked"
    for proc in _PROC_CALL.findall(q):
        if not _ALLOWED_PROCS.match(proc):
            return (f"read-only: procedure {proc} is blocked (allowed: db.schema.*, db.labels, "
                    "db.relationshipTypes, db.propertyKeys, db.index.fulltext/vector queries)")
    return None


def scope_problem(query: str, scan_id: str) -> str | None:
    """Agent-boundary policy: a query that MATCHes must be scoped -- by `$scan_id` or by this scan's
    id inlined as a literal -- or it silently reads every other scan in the database too. Returns an
    actionable error, or None. Not enforced for Orion's own internal reads (a cross-scan integrity
    check is a legitimate harness query)."""
    q = blank(query)
    scoped = "$scan_id" in q or (bool(scan_id) and scan_id in query)
    if re.search(r"\bMATCH\b", q, re.IGNORECASE) and not scoped:
        return ("unscoped query: filter every MATCH by scan_id, e.g. "
                "MATCH (c:CpgCall {scan_id:$scan_id}) ... (the tool binds $scan_id for you)")
    return None


class GraphDB:
    """A thin read-only wrapper over the scan graph (plus a single-scan clear for reloads)."""

    def __init__(self) -> None:
        self._driver = GraphDatabase.driver(config.NEO4J_URI, auth=config.NEO4J_AUTH)

    def close(self) -> None:
        self._driver.close()

    def ping(self) -> bool:
        with self._driver.session(database=config.NEO4J_DATABASE) as s:
            return s.run("RETURN 1 AS ok").single()["ok"] == 1

    def files(self, scan_id: str) -> list[str]:
        """The BFS scaffold: the file list handed to the agent up front (deterministic)."""
        with self._driver.session(database=config.NEO4J_DATABASE) as s:
            rows = s.run(
                "MATCH (f:CpgFile {scan_id:$scan_id}) RETURN f.file_path AS p ORDER BY p",
                scan_id=scan_id,
            )
            return [r["p"] for r in rows]

    def node_count(self, scan_id: str) -> int:
        """Total nodes persisted for this scan: the graph (NODE_KEY labels) plus its semantic-index
        Chunks -- the same set the old label-less count saw, minus the scan's OrionScan record. Used
        to size the discovery timeout to graph size (see config.discover_timeout). Returns 0 for an
        unknown/empty scan rather than raising."""
        with self._driver.session(database=config.NEO4J_DATABASE) as s:
            return sum(scans.count_scan(s, scan_id, (*NODE_KEY, "Chunk")).values())

    def clear_scan(self, scan_id: str) -> dict[str, int]:
        """Single-scan lifecycle: remove everything for this scan -- every SCAN_LABELS label, incl. its
        semantic Chunks and its OrionScan record (unlike persist._clear, which keeps both) -- in
        batched per-label transactions. Idempotent; returns {label: nodes deleted}.

        Not agent-reachable — agents go through `run_cypher`, which blocks writes. The harness
        calls this directly at the start of a build so each scan is self-contained.
        """
        return scans.delete_scan(self._driver, scan_id, scans.SCAN_LABELS)

    def schema(self) -> dict:
        """Node labels/properties and relationship types/properties, via Neo4j's built-in
        `db.schema.*` procedures (no APOC — Orion's compose stack doesn't install it). This is
        Orion's own frozen CPG vocabulary (schema.py's NODE_KEY set), not the target repo's
        structure, so the shape is identical regardless of which repo was scanned; unlike
        run_cypher/semantic_search it is intentionally not scan_id-scoped. The :OrionScan
        bookkeeping record (graph/scans.py) is not part of that vocabulary and is left out."""
        with self._driver.session(database=config.NEO4J_DATABASE) as s:
            node_properties = [dict(r) for r in s.run("CALL db.schema.nodeTypeProperties()")
                               if scans.RECORD not in r["nodeLabels"]]
            relationship_properties = [dict(r) for r in s.run("CALL db.schema.relTypeProperties()")]
            return {
                "node_properties": node_properties,
                "relationship_properties": relationship_properties,
            }

    def run_cypher(self, scan_id: str, query: str, limit: int = 50, *,
                   timeout: float | None = None, bounded: bool = False) -> dict:
        """One read-only query. Returns {row_count, rows} or {error}. The scan_id is bound as a
        parameter so the agent's query is always scoped to one scan.

        Read-only is enforced by the SERVER, not just the pre-check: the query runs in a read
        transaction, so Neo4j rejects any write -- a write procedure included. It carries a timeout
        (`timeout` seconds, default config.CYPHER_TIMEOUT) so a runaway cartesian product cannot pin
        the database.

        Default (the Claude agents' path): rows are streamed -- only the first `limit` are kept, while
        `row_count` still counts them all -- and `record.data()` renders nodes/relationships as plain
        JSON-safe values.
        `bounded=True` (the Orion-LM path): fetches at most `limit` records and discards the rest
        instead of reading them, and returns {row_count, rows, truncated, query_type}, where
        query_type is the planner's verdict ("r" read, "w"/"rw" write, "s" schema) -- for an
        `EXPLAIN` query, that of the explained query. Rows are `dict(record)`, as the lm tools expect.
        Any failure returns {error}."""
        reason = blocked_reason(query)
        if reason:
            return {"error": reason}

        # A managed transaction takes its timeout from @unit_of_work -- the driver REJECTS a
        # Query(..., timeout=) object inside execute_read ("only supported for session.run").
        @unit_of_work(timeout=config.CYPHER_TIMEOUT if timeout is None else timeout)
        def _read(tx) -> dict:
            result = tx.run(query, scan_id=scan_id)
            if bounded:
                rows = [dict(r) for r in result.fetch(max(int(limit), 0))]
                truncated = result.peek() is not None
                summary = result.consume()
                return {"row_count": len(rows), "rows": rows, "truncated": truncated,
                        "query_type": summary.query_type}
            rows, count = [], 0
            for record in result:
                count += 1
                if count <= limit:
                    rows.append(record.data())
            return {"row_count": count, "rows": rows}

        try:
            with self._driver.session(database=config.NEO4J_DATABASE) as s:
                return s.execute_read(_read)
        except Exception as exc:  # surface the Cypher error straight back to the agent
            return {"error": f"{exc.__class__.__name__}: {exc}"}
