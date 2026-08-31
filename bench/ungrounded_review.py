"""Ungrounded LLM security review — the PLAN2 control (arms B and C).

The honest "same task, minus the graph" baseline: one `claude -p` session reads the target's SOURCE
with Read/Grep/Glob (sandboxed via --add-dir), NO Orion MCP graph tools, NO verifier, and emits leads
in the SAME `strategies.LEADS_JSON_SCHEMA` shape so `bench.scoring` scores them identically to Orion's
CONFIRMED findings. Arm B runs this with the local model; arm C with `claude-opus-5` — set via
`ORION_MODEL` (claude_cli reads `config.MODEL`).

This isolates grounding's contribution: any recall gap between an Orion arm and this arm on the same
model is what the graph + fixed shapes + independent verifier bought. Findings here are UNVERIFIED
(single pass) — that is the point of the control; report them as-is, false positives included.
"""
from __future__ import annotations

import uuid

from orion import claude_cli, config
from orion.contracts import OnEvent
from orion.strategies import LEADS_JSON_SCHEMA

_SYSTEM = """You are a senior application-security auditor doing a source-code review of ONE repository
for exploitable vulnerabilities. You have ONLY these read tools: Read, Grep, Glob. There is NO code
graph, NO database — read the actual source.

Systematically hunt for the OWASP-style vulnerability classes: injection (SQL/NoSQL/command/template/
code/XXE), cross-site scripting, SSRF, insecure deserialization, broken access control / IDOR, broken
authentication & weak password handling, cryptographic failures, security misconfiguration (debug on,
hardcoded secrets, missing security headers), unvalidated redirects, sensitive-data exposure, CSRF,
ReDoS, and vulnerable/outdated dependencies. Trace untrusted input (request params/body/query, CLI
args, file contents) to a dangerous sink.

GROUNDING RULE: every finding must be backed by actual source you read this session — cite the file
and the concrete code. Do NOT invent findings or report a class you did not locate in the code. Be
precise: name the vulnerability class explicitly and the file it lives in.

When done, emit the required structured JSON: a "leads" array, each item
{"shape":"A","text":<the finding: vuln CLASS + the file, specific enough to re-derive>,
 "evidence":<the file path + the concrete code/line you read that proves it>,
 "confidence":"LOW"|"MEDIUM"|"HIGH"}. Use shape "A" for all items (the schema requires one). If you
found nothing, return an empty "leads" array — never invent one."""


def review(repo: str, on_event: OnEvent | None = None, *,
           timeout: int | None = None, max_turns: int | None = None) -> dict:
    """Run one ungrounded review session over `repo`. Returns the raw structured result
    ({"leads":[...]} or {"_error":...}). The model is whatever `config.MODEL` (env ORION_MODEL) is,
    so arm B (local) and arm C (opus) differ only by that env var. Token usage flows through on_event."""
    message = (
        f"Review the repository at {repo} for exploitable security vulnerabilities. Read the source "
        f"with Read/Grep/Glob, trace untrusted input to dangerous sinks, and report every distinct "
        f"vulnerability you can substantiate from the code. Begin now.")
    return claude_cli.run_agent(
        session_id=str(uuid.uuid4()),
        system=_SYSTEM,
        message=message,
        json_schema=LEADS_JSON_SCHEMA,
        add_dir=repo,
        extra_allowed=("Read", "Grep", "Glob"),
        on_event=on_event,
        max_turns=max_turns if max_turns is not None else config.MAX_TURNS,
        timeout=timeout if timeout is not None else config.VERIFY_TIMEOUT,
        retries=1,
        use_mcp=False,          # the whole point: no graph
    )


def findings_from_result(result: dict) -> list[tuple[str, str]]:
    """Map the agent's {"leads":[...]} into (text, evidence) pairs for bench.scoring. Defensive: a
    malformed item is dropped, an `_error` result yields []. Never fabricates."""
    if not isinstance(result, dict) or "_error" in result:
        return []
    raw = result.get("leads")
    if not isinstance(raw, list):
        return []
    out: list[tuple[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        text = item.get("text") or ""
        evidence = item.get("evidence") or ""
        if text or evidence:
            out.append((text, evidence))
    return out
