"""The Claude agents' Cypher policy, applied at the MCP boundary (mcp_server.run_cypher_impl) before a
query reaches Neo4j. Orion's own internal reads never go through it.

Layers, in order (each matched on `graphdb.blank`ed text -- strings, comments and backtick names
removed in ONE lexer pass -- so a quote inside a comment cannot hide the clause after it):
  1. `graphdb.blocked_reason`: write clauses; LOAD CSV (the Neo4j SERVER would fetch the URL, so a
     prompt injection in a scanned repo could send graph data out in it), FOREACH / IN TRANSACTIONS /
     PERIODIC COMMIT; any procedure outside the read-only allowlist (db.schema.*, db.labels, ...).
  2. Admin commands a read transaction may still run: SHOW (lists other sessions' transactions),
     TERMINATE (kills them), USE (switches database).
  3. Scan scope (`graphdb.scope_problem`).
The real write enforcement is the read transaction in GraphDB.run_cypher; these give the agent a
readable error and stop the reads that escape the graph.

The Orion-LM engine has its own, stricter checker (orion/lm/tools.check_query); this module never
imports orion.lm, so the Claude MCP server does not load the lm package.
"""
from __future__ import annotations

import re

from .graphdb import blank, blocked_reason, scope_problem

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
