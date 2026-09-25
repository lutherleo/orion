"""Read-only Neo4j access: the grounding tool.

Every agent claim has to be backed by a query result from here. Write keywords are blocked, so
an agent (discovery or verifier) can read the graph but never mutate it. This is the single tool
that keeps the agents honest: they must query for a vulnerability, not assert it.

The MCP server (orion/mcp_server.py) wraps `run_cypher` as the read-only tool the agents call.
The safety contract lives here: read only, scoped to one scan_id. `clear_scan` is the one write,
and it is NOT agent-reachable — only the harness calls it, at the start of a build.
"""
from __future__ import annotations

import re

from neo4j import GraphDatabase, Query

from . import config

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
# Cypher string literals ('...' or "..."), escaped-quote aware, plus backtick-quoted names. Blanked
# before every scan so a legitimate READ query that searches source text for DML words -- e.g.
# `WHERE c.code CONTAINS 'SET role=admin'` (a natural SQLi-hunting query for a security scanner) --
# is not falsely rejected. Structural writes (SET n.x=1) are outside any literal and still caught.
_STRING_LITERAL = re.compile(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"|`[^`]*`")


def _strip_literals(query: str) -> str:
    return _STRING_LITERAL.sub("''", query)


def blocked_reason(query: str) -> str | None:
    """Why `query` must not run, or None. Pure/testable. The pre-check in front of a server-enforced
    read transaction (run_cypher): it turns the common mistakes into a readable error for the agent
    and blocks the reads that escape the graph, which a read transaction alone would allow."""
    q = _strip_literals(query)
    if _WRITE.search(q):
        return "read-only: write keywords are blocked"
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
    q = _strip_literals(query)
    scoped = "$scan_id" in q or (bool(scan_id) and scan_id in query)
    if re.search(r"\bMATCH\b", q, re.IGNORECASE) and not scoped:
        return ("unscoped query: filter every MATCH by scan_id, e.g. "
                "MATCH (c:CpgCall {scan_id:$scan_id}) ... (the tool binds $scan_id for you)")
    return None


def _has_write_keyword(query: str) -> bool:
    """True if `query` would be refused by the read-only pre-check. Kept for existing callers."""
    return blocked_reason(query) is not None


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
        """Total nodes persisted for this scan. Used to size the discovery timeout to graph size
        (see config.discover_timeout). Returns 0 for an unknown/empty scan rather than raising."""
        with self._driver.session(database=config.NEO4J_DATABASE) as s:
            rec = s.run(
                "MATCH (n {scan_id:$scan_id}) RETURN count(n) AS c", scan_id=scan_id
            ).single()
            return int(rec["c"]) if rec else 0

    def clear_scan(self, scan_id: str) -> None:
        """Single-scan lifecycle: remove everything for this scan before a reload (idempotent).

        Not agent-reachable — agents go through `run_cypher`, which blocks writes. The harness
        calls this directly at the start of a build so each scan is self-contained.
        """
        with self._driver.session(database=config.NEO4J_DATABASE) as s:
            s.run("MATCH (n {scan_id:$scan_id}) DETACH DELETE n", scan_id=scan_id)

    def schema(self) -> dict:
        """Node labels/properties and relationship types/properties, via Neo4j's built-in
        `db.schema.*` procedures (no APOC — Orion's compose stack doesn't install it). This is
        Orion's own frozen CPG vocabulary (schema.py's NODE_KEY set), not the target repo's
        structure, so the shape is identical regardless of which repo was scanned; unlike
        run_cypher/semantic_search it is intentionally not scan_id-scoped."""
        with self._driver.session(database=config.NEO4J_DATABASE) as s:
            node_properties = [dict(r) for r in s.run("CALL db.schema.nodeTypeProperties()")]
            relationship_properties = [dict(r) for r in s.run("CALL db.schema.relTypeProperties()")]
            return {
                "node_properties": node_properties,
                "relationship_properties": relationship_properties,
            }

    def run_cypher(self, scan_id: str, query: str, limit: int = 50) -> dict:
        """One read-only query. Returns {row_count, rows} or {error}. The scan_id is bound as a
        parameter so the agent's query is always scoped to one scan.

        Read-only is enforced by the SERVER, not just the pre-check: the query runs in a read
        transaction, so Neo4j rejects any write -- a write procedure included. It carries a timeout
        (config.CYPHER_TIMEOUT) so a runaway cartesian product cannot pin the database, and rows are
        streamed: only the first `limit` are kept, while `row_count` still counts them all.
        `record.data()` renders nodes/relationships as plain JSON-safe values."""
        reason = blocked_reason(query)
        if reason:
            return {"error": reason}

        def _read(tx) -> dict:
            result = tx.run(Query(query, timeout=config.CYPHER_TIMEOUT), scan_id=scan_id)
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
