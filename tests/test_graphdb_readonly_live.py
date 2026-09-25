"""Server-enforced read-only behaviour of GraphDB.run_cypher against a real Neo4j. Skips when Neo4j
is not reachable. The pure pre-check is covered by tests/test_graphdb_guard.py; this proves the
SERVER refuses a write even when the pre-check is bypassed, and that timeout/streaming hold.
"""
from __future__ import annotations

import json

import pytest

from orion import graphdb
from orion.graphdb import GraphDB

SID = "test-readonly-live"


@pytest.fixture
def db():
    try:
        d = GraphDB()
        if not d.ping():
            pytest.skip("Neo4j not reachable")
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Neo4j not reachable: {exc}")
    with d._driver.session() as s:
        s.run("UNWIND range(1, 120) AS i CREATE (:CpgFile {scan_id:$sid, uid:toString(i), "
              "file_path:'f' + i})", sid=SID)
    yield d
    with d._driver.session() as s:
        s.run("MATCH (n {scan_id:$sid}) DETACH DELETE n", sid=SID)
    d.close()


def test_server_rejects_a_write_even_if_the_precheck_is_bypassed(db, monkeypatch):
    monkeypatch.setattr(graphdb, "blocked_reason", lambda q: None)
    res = db.run_cypher(SID, "CREATE (n:Nope {scan_id:$scan_id}) RETURN n")
    assert "error" in res
    with db._driver.session() as s:
        assert s.run("MATCH (n:Nope) RETURN count(n) AS c").single()["c"] == 0


def test_row_count_counts_past_the_limit_and_rows_are_json(db):
    res = db.run_cypher(SID, "MATCH (f:CpgFile {scan_id:$scan_id}) RETURN f", limit=10)
    assert res["row_count"] == 120 and len(res["rows"]) == 10
    json.dumps(res)                                   # nodes rendered as plain dicts
    assert res["rows"][0]["f"]["scan_id"] == SID


def test_runaway_query_times_out(db, monkeypatch):
    monkeypatch.setattr(graphdb.config, "CYPHER_TIMEOUT", 0.5)
    res = db.run_cypher(SID, "MATCH (a:CpgFile {scan_id:$scan_id}), (b:CpgFile {scan_id:$scan_id}), "
                             "(c:CpgFile {scan_id:$scan_id}), (d:CpgFile {scan_id:$scan_id}), "
                             "(e:CpgFile {scan_id:$scan_id}) "
                             "RETURN count(*) AS n")
    assert "error" in res
