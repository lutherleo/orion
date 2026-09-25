"""Headless `claude -p` driver, shared by discovery and verification.

Real MCP tool-calling (v2.1.214+), not the old `CYPHER:`/`FINAL:` text protocol: each call to
`run_agent` launches ONE `claude -p` subprocess that loops INTERNALLY, calling the read-only
`mcp__orion__run_cypher` (and optionally `mcp__orion__semantic_search`) tool as many times as it
wants, then emits a schema-conformant structured result. We stream-parse its stdout, forward a
`{"event":"tool","detail":<query>}` notification for every Cypher call it makes, and return the
final structured object.

Hard-won rules baked in (see orion-shared-context.md):
  - `--system-prompt` fully REPLACES Claude Code's default system prompt, and must be re-passed on
    EVERY call (there is no resume in this module, but the rule is kept correct regardless).
  - A subprocess non-zero exit, a timeout, `is_error`, or a missing/malformed result is NEVER
    treated as a clean success. It becomes a failure sentinel `{"_error": "..."}` -- the caller
    must not synthesize a lead or verdict from it.
  - `--json-schema` takes an INLINE JSON string, not a file path.
  - stdin is always redirected from /dev/null (avoids the "no stdin data" warning polluting stdout).
  - Discovery agents get ONLY the two `mcp__orion__*` tools; disallow everything else EXCEPT
    whatever the caller names in `extra_allowed` (the verifier adds Read/Grep/Glob/Task/Skill).
    `ToolSearch` is never disallowed -- deferred MCP tools need it to auto-load.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Callable

from . import config

# Transient `claude -p` failures (intermittent API overload under a heavy back-to-back batch of
# subagent-spawning verifications) are retried with exponential backoff on a FRESH session id.
# Base seconds; attempt k waits base * 2**k. Kept here (config.py is frozen) not as magic numbers.
_RETRY_BACKOFF_BASE = 3.0

# cwd for the subprocess: the repo root (so the `orion` package the MCP server imports resolves, and
# any relative --add-dir target resolves against it too, e.g. "fixtures/NodeGoat").
_REPO_ROOT = Path(__file__).resolve().parent.parent

_ALLOWED_BASE = ("mcp__orion__run_cypher", "mcp__orion__semantic_search")
# Everything a discovery agent must NOT have (extra_allowed subtracts from this — the verifier adds
# Read/Grep/Glob/Task/Skill back). `Skill`/`TodoWrite` are here so discovery is held to ONLY the two
# mcp__orion__* tools; `ToolSearch` is intentionally NOT here (deferred MCP tools need it to load).
_BASE_DISALLOWED = (
    "Bash", "Read", "Write", "Edit", "Grep", "Glob",
    "WebFetch", "WebSearch", "Agent", "Task", "Skill", "NotebookEdit", "TodoWrite",
)


def mcp_config() -> str:
    """The `--mcp-config` argument: config.MCP_CONFIG when set (a file path), else an INLINE JSON
    config that launches the Orion MCP server with the interpreter running Orion right now. The old
    default pointed at `.mcp/orion.json`, which hardcodes `./.venv/bin/python` -- absent on Windows
    (`.venv\\Scripts\\python.exe`) and in any install without that exact venv, so the agents' tool
    server silently failed to start."""
    if config.MCP_CONFIG:
        return config.MCP_CONFIG
    return json.dumps({"mcpServers": {"orion": {
        "command": sys.executable, "args": ["-m", "orion.mcp_server"]}}})


def _build_cmd(
    *,
    session_id: str,
    system: str,
    json_schema: str | dict | None,
    add_dir: str | None,
    extra_allowed: tuple[str, ...],
    max_turns: int | None,
    use_mcp: bool = True,
) -> list[str]:
    # use_mcp=False is the UNGROUNDED-review path (PLAN2 arms B/C): no Orion MCP graph tools at all,
    # so the agent works only from what the caller allows (Read/Grep/Glob on the source). The mcp
    # base tools and --mcp-config are dropped entirely, isolating "same model, minus the graph".
    allowed = (list(_ALLOWED_BASE) if use_mcp else []) + list(extra_allowed)
    disallowed = [t for t in _BASE_DISALLOWED if t not in extra_allowed]

    cmd = [
        "claude", "-p",
        "--model", config.MODEL,
        "--effort", config.EFFORT,
    ]
    if use_mcp:
        cmd += ["--mcp-config", mcp_config(), "--strict-mcp-config"]
    cmd += [
        "--output-format", "stream-json",
        "--verbose",
        "--system-prompt", system,          # re-passed every call (non-negotiable)
        "--disable-slash-commands",
        "--permission-mode", "bypassPermissions",
        "--allowedTools", ",".join(allowed),
        "--disallowed-tools", ",".join(disallowed),
        "--session-id", session_id,
    ]
    if json_schema is not None:
        schema_str = json.dumps(json_schema) if isinstance(json_schema, dict) else json_schema
        cmd += ["--json-schema", schema_str]
    if add_dir:
        cmd += ["--add-dir", add_dir]
    if max_turns is not None:
        cmd += ["--max-turns", str(max_turns)]
    return cmd


def parse_stream_events(
    raw: str | list[str],
    on_event: Callable[[dict], None] | None = None,
) -> tuple[list[dict], dict | None]:
    """Parse `claude -p --output-format stream-json` output into (events, final).

    `raw` is either the full stdout text or an iterable of JSONL lines (tests pass either).
    Unparseable lines are skipped, never raised. For every assistant `tool_use` block calling
    `mcp__orion__run_cypher`, `on_event({"event":"tool","detail":<query>})` fires. `final` is the
    last `{"type":"result"}` line seen (or None if the stream never produced one).
    """
    on_event = on_event or (lambda ev: None)
    lines = raw.splitlines() if isinstance(raw, str) else raw

    events: list[dict] = []
    final: dict | None = None
    for line in lines:
        if not isinstance(line, str):
            continue
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        events.append(obj)

        if obj.get("type") == "assistant":
            content = (obj.get("message") or {}).get("content") or []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use" and block.get("name") == "mcp__orion__run_cypher":
                    query = (block.get("input") or {}).get("query", "")
                    on_event({"event": "tool", "detail": query})
        elif obj.get("type") == "result":
            final = obj

    return events, final


def extract_usage(final: dict | None) -> dict | None:
    """Pull the token/cost accounting out of a stream-json `result` event, or None if absent.

    `claude -p --output-format stream-json` puts a `usage` block and `total_cost_usd` on the final
    `{"type":"result"}` line. run_agent otherwise discards this (it only keeps the structured output),
    so the research token ledger (bench/token_ledger.py) would have nothing to aggregate. Kept defensive:
    a missing/oddly-shaped usage yields zeros, never raises. `total_cost_usd` is the CLI's own cost
    figure (may be null through a non-Anthropic proxy — the ledger then prices from tokens itself)."""
    if not isinstance(final, dict):
        return None
    usage = final.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    return {
        "model": final.get("model") or "",
        "input_tokens": int(usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
        "cache_creation_input_tokens": int(usage.get("cache_creation_input_tokens") or 0),
        "cache_read_input_tokens": int(usage.get("cache_read_input_tokens") or 0),
        "total_cost_usd": final.get("total_cost_usd"),
        "num_turns": final.get("num_turns"),
    }


def _emit_usage(final: dict | None, on_event: Callable[[dict], None] | None) -> None:
    """Emit one `{"event":"usage","detail":<json>}` progress event carrying this call's token/cost.
    Best-effort: no usage or no listener is a silent no-op. Threads through the same on_event the tool
    events use, so the run logger records it and the ledger can aggregate it — callers that parse
    structured output ignore an unknown 'usage' event, so nothing downstream breaks."""
    if on_event is None:
        return
    usage = extract_usage(final)
    if usage is None:
        return
    on_event({"event": "usage", "detail": json.dumps(usage)})


def _final_to_result(final: dict | None) -> dict:
    """Robust final-event -> structured-object extraction. NEVER fabricates output: any
    ambiguity (missing result line, is_error, unparseable result) becomes `{"_error": "..."}`."""
    if final is None:
        return {"_error": "no result event found in stream-json output"}
    if final.get("is_error"):
        return {"_error": f"agent reported error: subtype={final.get('subtype')!r}"}

    structured = final.get("structured_output")
    if isinstance(structured, dict):
        return structured

    result_str = final.get("result")
    if isinstance(result_str, str):
        try:
            parsed = json.loads(result_str)
        except json.JSONDecodeError:
            return {"_error": "result field was not valid JSON"}
        if isinstance(parsed, dict):
            return parsed
        return {"_error": "parsed result was not a JSON object"}

    return {"_error": "no structured_output or result field in final event"}


def _run_once(cmd: list[str], *, timeout: int, on_event: Callable[[dict], None] | None) -> dict:
    """One `claude -p` invocation. Returns the parsed structured object on success, else a rich
    failure sentinel. Crucially, stdout is parsed EVEN on a non-zero exit: any tool_use still
    surfaces via `on_event`, and a stdout tail is salvaged into the sentinel so a crash is never
    opaque (the prior code discarded stdout on non-zero exit and left failures unexplainable)."""
    try:
        r = subprocess.run(
            cmd, text=True, capture_output=True, timeout=timeout,
            stdin=subprocess.DEVNULL, cwd=str(_REPO_ROOT),
        )
    except subprocess.TimeoutExpired:
        return {"_error": f"claude -p timed out after {timeout}s"}
    except OSError as exc:
        return {"_error": f"failed to launch claude -p: {exc}"}

    # Parse regardless of exit code: fires tool events + lets us salvage a partial result / tail.
    _events, final = parse_stream_events(r.stdout, on_event)
    # Emit token/cost accounting whenever the stream produced a result event, even on a non-zero exit
    # (a run that failed late still spent tokens the ledger should count).
    _emit_usage(final, on_event)

    if r.returncode != 0:
        stderr = (r.stderr or "").strip()[:300]
        stdout_tail = (r.stdout or "").strip()[-600:]
        return {"_error": (
            f"claude -p exited {r.returncode}"
            f" | stderr: {stderr or '(empty)'}"
            f" | stdout_tail: {stdout_tail or '(empty)'}"
        )}

    return _final_to_result(final)


def run_agent(
    session_id: str,
    system: str,
    message: str,
    *,
    json_schema: str | dict | None = None,
    add_dir: str | None = None,
    extra_allowed: tuple[str, ...] = (),
    on_event: Callable[[dict], None] | None = None,
    max_turns: int | None = None,
    timeout: int | None = None,
    retries: int = 0,
    retry_backoff: float | None = None,
    use_mcp: bool = True,
) -> dict:
    """Run one `claude -p` session with real MCP tool-calling and return its structured result.

    Returns the parsed final structured object (`structured_output`, or `json.loads(result)` as a
    fallback) on success. On ANY subprocess failure, timeout, `is_error`, or missing/malformed
    result, returns a failure sentinel `{"_error": "<why>"}` -- never raises past the caller, never
    fabricates a lead or verdict.

    `retries` (>0) re-runs a FAILED attempt up to that many times, each on a fresh `--session-id`
    and after exponential backoff -- the correct response to a genuinely transient subprocess
    failure (intermittent API overload), not benchmark-fitting: verification is idempotent (a fresh
    session re-derives from the graph + source), so a clean retry is a faithful re-attempt. Each
    retry is surfaced as an "error" progress event so the recovery is visible in the run log.
    """
    call_timeout = config.CALL_TIMEOUT if timeout is None else timeout
    backoff = _RETRY_BACKOFF_BASE if retry_backoff is None else retry_backoff
    attempts = max(1, retries + 1)

    result: dict = {"_error": "no attempt made"}
    for attempt in range(attempts):
        # A retried attempt needs its own session id -- claude -p requires a unique --session-id.
        sid = session_id if attempt == 0 else str(uuid.uuid4())
        cmd = _build_cmd(
            session_id=sid, system=system, json_schema=json_schema,
            add_dir=add_dir, extra_allowed=extra_allowed, max_turns=max_turns,
            use_mcp=use_mcp,
        ) + [message]

        result = _run_once(cmd, timeout=call_timeout, on_event=on_event)
        if "_error" not in result:
            return result

        if attempt < attempts - 1:
            if on_event is not None:
                on_event({
                    "event": "error",
                    "detail": f"transient failure (attempt {attempt + 1}/{attempts}), retrying: "
                              f"{result['_error'][:160]}",
                })
            time.sleep(backoff * (2 ** attempt))

    return result
