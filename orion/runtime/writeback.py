"""The runtime stage's one writer: apply a WritePlan to the scan partition. The only impure DB module.

Additive by construction -- it never deletes or rewrites a static node or a static edge:

  props   `executed` / `hit_count` on existing CpgCall / CpgMethod nodes
  nodes   :ObservedMethod (RUNTIME_NODE_KEY -- outside NODE_KEY, so the static clear never targets it)
  edges   OBSERVED_CALL / OBSERVED_DISPATCH, one per endpoint pair, {scan_id, origin:'runtime', hits}

Its own idempotent clear removes exactly that and nothing else, so a re-run replaces the previous
enrichment and the static graph -- and its 217/1075 FLOWS_TO parity -- stays byte-for-byte identical.
(A static RE-scan's DETACH DELETE still drops OBSERVED_* edges incident to rebuilt nodes: a new scan
means the code changed and the old runtime facts are stale; re-run the runtime stage after it.)

Writes reuse persist's chunked, parallel, index-backed job runner rather than one giant transaction.
"""
from __future__ import annotations

from neo4j import GraphDatabase

from .. import config
from ..graph import persist
from ..graph.schema import RUNTIME_NODE_KEY, Batch
from .correlate import ENDPOINT_KEY, WritePlan

ORIGIN = "runtime"
RUNTIME_RELS = ("OBSERVED_CALL", "OBSERVED_DISPATCH")

# Every destructive statement, as constants so a token-free test can pin that the clear only ever
# deletes runtime relationships / runtime nodes and REMOVEs runtime props -- label-scoped, never a
# NODE_KEY node, never a full-partition scan.
CLEAR_EDGES = "MATCH ()-[r:OBSERVED_CALL|OBSERVED_DISPATCH {scan_id:$scan_id}]->() DELETE r"
CLEAR_NODES = "MATCH (n:ObservedMethod {scan_id:$scan_id}) DETACH DELETE n"
CLEAR_PROPS = tuple(
    f"MATCH (n:{label} {{scan_id:$scan_id}}) WHERE n.executed IS NOT NULL REMOVE n.executed, n.hit_count"
    for label in ("CpgMethod", "CpgCall"))

SET_CALLS = ("UNWIND $rows AS row MATCH (c:CpgCall {scan_id:row.sid, uid:row.k}) "
             "SET c.executed = true, c.hit_count = row.n")
SET_METHODS = ("UNWIND $rows AS row MATCH (m:CpgMethod {scan_id:row.sid, full_name:row.k}) "
               "SET m.executed = true, m.hit_count = row.n")

# Of the OBSERVED_CALL pairs just written, how many does the STATIC graph already link? Checked per
# written pair (index-backed endpoint lookups), not by loading every static pair in the scan.
_STATIC_LINKED = (
    "UNWIND $rows AS row "
    "MATCH (a:CpgMethod {scan_id:$scan_id, full_name:row.a}) "
    "WHERE EXISTS { (a)-[:CONTAINS_CALL]->(:CpgCall)-[:RESOLVES_TO]->"
    "(:CpgMethod {scan_id:$scan_id, full_name:row.b}) } "
    "RETURN count(*) AS n")
_UNREACHABLE_EXECUTED = (
    "CALL { MATCH (n:CpgMethod {scan_id:$scan_id}) WHERE n.executed AND n.reachable_from_entry = false "
    "RETURN count(n) AS c UNION ALL MATCH (n:CpgCall {scan_id:$scan_id}) "
    "WHERE n.executed AND n.reachable_from_entry = false RETURN count(n) AS c } RETURN sum(c) AS u")


def to_batch(scan_id: str, plan: WritePlan) -> Batch:
    """The plan's new nodes + edges as a schema.Batch (edges keyed the way persist MATCHes them).
    Pure."""
    b = Batch(scan_id)
    for props in plan.new_methods:
        b.emit_node("ObservedMethod", dict(props))
    for (rtype, fl, fv, tl, tv), hits in plan.edges.items():
        b.emit_edge(rtype, fl, {ENDPOINT_KEY[fl]: fv}, tl, {ENDPOINT_KEY[tl]: tv},
                    {"origin": ORIGIN, "hits": hits})
    return b


def _prop_jobs(scan_id: str, plan: WritePlan) -> list[tuple[str, list]]:
    jobs = []
    for cypher, hits in ((SET_CALLS, plan.call_hits), (SET_METHODS, plan.method_hits)):
        rows = [{"sid": scan_id, "k": k, "n": n} for k, n in hits.items()]
        jobs += [(cypher, chunk) for chunk in persist._chunks(rows, config.PERSIST_CHUNK_SIZE)]
    return jobs


def apply_plan(scan_id: str, plan: WritePlan) -> dict:
    """Replace this scan's runtime enrichment with `plan`; return the metric dict."""
    batch = to_batch(scan_id, plan)
    conc = config.PERSIST_CONCURRENCY
    driver = GraphDatabase.driver(config.NEO4J_URI, auth=config.NEO4J_AUTH)
    try:
        with driver.session(database=config.NEO4J_DATABASE) as s:
            persist._ensure_indexes(s, RUNTIME_NODE_KEY)
            for q in (CLEAR_EDGES, CLEAR_NODES, *CLEAR_PROPS):
                s.execute_write(lambda tx, q=q: tx.run(q, scan_id=scan_id).consume())
        persist._run_jobs(driver, _prop_jobs(scan_id, plan), conc)
        persist._run_jobs(driver, persist._node_jobs(batch.nodes, RUNTIME_NODE_KEY), conc)
        persist._run_jobs(driver, persist._edge_jobs(batch.edges), conc)

        calls = [{"a": fv, "b": tv} for (fl, fv, tl, tv) in plan.edges_of("OBSERVED_CALL")
                 if fl == tl == "CpgMethod"]
        with driver.session(database=config.NEO4J_DATABASE) as s:
            linked = s.run(_STATIC_LINKED, scan_id=scan_id, rows=calls).single()["n"] if calls else 0
            unreachable = s.run(_UNREACHABLE_EXECUTED, scan_id=scan_id).single()["u"]
    finally:
        driver.close()

    observed_calls = plan.edges_of("OBSERVED_CALL")
    return {
        "calls_marked": len(plan.call_hits),
        "methods_marked": len(plan.method_hits),
        "new_methods": len(plan.new_methods),
        "observed_calls": len(observed_calls),
        "observed_dispatches": len(plan.edges_of("OBSERVED_DISPATCH")),
        # novel = no static CONTAINS_CALL/RESOLVES_TO path, incl. every edge into a runtime-only method
        "novel_edges": len(observed_calls) - int(linked),
        "unreachable_executed": int(unreachable or 0),
        "dropped": dict(plan.dropped),
    }
