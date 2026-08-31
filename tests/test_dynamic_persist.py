"""The two-clear invariant + dynamic persist plumbing, as PURE logic (no Neo4j).

The invariant that keeps static and dynamic from wiping each other: dynamic labels live in
DYNAMIC_NODE_KEY, never in NODE_KEY, so persist._clear (scoped to NODE_KEY) can't touch them, and
persist._clear_dynamic (scoped to DYNAMIC_NODE_KEY + origin='dynamic' edges) can't touch static.
Here we assert the label partitioning and that the job-builders emit the right CREATE for the
dynamic label/edges — the DB writes themselves are exercised by the @slow end-to-end.
"""
from __future__ import annotations

from orion.graph import persist
from orion.graph.schema import ALL_NODE_KEY, DYNAMIC_NODE_KEY, NODE_KEY, Batch


def test_static_and_dynamic_label_sets_are_disjoint():
    assert set(NODE_KEY) & set(DYNAMIC_NODE_KEY) == set()
    assert "ObservedMethod" in DYNAMIC_NODE_KEY
    assert "ObservedMethod" not in NODE_KEY            # static clear can never reach it
    assert ALL_NODE_KEY == {**NODE_KEY, **DYNAMIC_NODE_KEY}


def test_node_jobs_builds_create_for_observed_method():
    b = Batch("s1")
    b.emit_node("ObservedMethod",
                {"uid": "u1", "name": "f", "file_path": "a.py", "line": 3, "origin": "dynamic"})
    jobs = persist._node_jobs(b.nodes, DYNAMIC_NODE_KEY)
    assert jobs, "expected a node job for the ObservedMethod"
    cypher, rows = jobs[0]
    assert "CREATE (n:`ObservedMethod`)" in cypher
    assert rows[0]["props"]["uid"] == "u1"


def test_edge_jobs_builds_match_create_for_observed_edges():
    b = Batch("s1")
    b.emit_edge("OBSERVED_CALL", "CpgMethod", {"full_name": "a.caller"},
                "ObservedMethod", {"uid": "u1"}, {"origin": "dynamic"})
    jobs = persist._edge_jobs(b.edges)
    cypher, rows = jobs[0]
    assert "[r:`OBSERVED_CALL`]" in cypher
    assert "MATCH (a:`CpgMethod`" in cypher and "MATCH (b:`ObservedMethod`" in cypher
    assert rows[0]["props"]["origin"] == "dynamic"
    assert rows[0]["fk"]["scan_id"] == "s1" and rows[0]["tk"]["scan_id"] == "s1"   # B2 stamp


def test_static_node_rows_unaffected_by_dynamic_key_addition():
    """A static batch still keys/dedups exactly as before (ALL_NODE_KEY is a superset of NODE_KEY)."""
    b = Batch("s1")
    b.emit_node("CpgMethod", {"full_name": "a.f", "name": "f"})
    b.emit_node("CpgMethod", {"full_name": "a.f", "name": "f", "line": 3})   # same key, union props
    rows = persist._node_rows(b.nodes)          # default ALL_NODE_KEY
    assert len(rows["CpgMethod"]) == 1          # collapsed to one, line unioned in
    assert rows["CpgMethod"][0]["props"]["line"] == 3
