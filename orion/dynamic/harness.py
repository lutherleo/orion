"""The harness-generation agent: a `claude -p` session that writes a driver exercising the target.

Orion's discovery/verify fleet is agents-querying-the-graph; the dynamic layer keeps that grain — an
agent reads the scan's :EntryPoint nodes (and real source, sandboxed to the target via --add-dir) and
writes a small driver that imports the target and CALLS those entry points, so the tracer has
something to observe. The agent stays read-only: it returns the driver as structured output and this
module writes it to a temp file — it never gets Write/Bash/Edit.

Fallible-by-contract (CLAUDE.md): a `_error` sentinel, an empty driver, or a malformed result is a
logged skip that yields no harness (None), never a crash and never a fabricated script. The caller
then reports an empty delta. `--harness-file` bypasses this agent entirely with a pinned driver.
"""
from __future__ import annotations

import tempfile
import uuid
from dataclasses import dataclass

from .. import claude_cli, config
from ..contracts import OnEvent
from ..graphdb import GraphDB

# Structured-output contract for the agent. `driver` is the whole script; `language` echoes the
# target stack; `notes` is a short free-text rationale surfaced in the run log.
HARNESS_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "driver": {"type": "string", "description": "the complete driver script source"},
        "language": {"type": "string", "enum": ["py", "js"]},
        "notes": {"type": "string"},
    },
    "required": ["driver"],
}

_SYSTEM = """\
You write a SELF-CONTAINED driver script that exercises a target program's entry points so a runtime
tracer can observe real behavior (which concrete methods run, which dynamic-dispatch targets are hit).
You are given the target's entry-point functions (from a code graph) and read-only access to its
source. You have `mcp__orion__run_cypher` to read more of the graph and Read/Grep/Glob to read source.

Write a driver that:
- imports the target's modules and CALLS the entry-point functions/handlers with plausible, BENIGN
  inputs (simple strings/dicts/numbers; for web handlers, construct minimal fake request objects if
  the framework allows, otherwise call the underlying function directly);
- wraps EACH call in its own try/except so one failing call never stops the rest — maximize how many
  distinct entry points execute;
- imports only what the target needs; does not require network access, external services, or a real
  database if avoidable (stub or wrap in try/except).

HARD SAFETY RULES: the driver must NOT delete or modify files, make outbound network requests, spawn
shells, or perform any destructive or irreversible action. Exercising code paths only. If an entry
point cannot be driven safely, skip it (leave a comment) rather than forcing it.

Return ONLY the structured object: `driver` (the full script), `language` ("py" or "js"), `notes`
(one or two sentences on what you drove and what you skipped). Do not include markdown fences."""

_SUFFIX = {"py": ".py", "js": ".js"}


@dataclass
class HarnessResult:
    """The outcome of harness generation. `path` is None on any skip; `error` says why."""
    path: str | None
    language: str
    notes: str = ""
    error: str = ""


def load_entry_points(scan_id: str, limit: int = 200) -> list[dict]:
    """The scan's entry points (name/full_name/file/line) via the read-only graph tool. Best-effort:
    a graph error yields [] so harness generation degrades to a skip rather than crashing."""
    db = GraphDB()
    try:
        res = db.run_cypher(
            scan_id,
            "MATCH (e:EntryPoint {scan_id:$scan_id})-[:ENTERS_AT]->(m:CpgMethod) "
            "RETURN m.full_name AS full_name, m.name AS name, m.file_path AS file, m.line AS line "
            "ORDER BY file, line",
            limit=limit,
        )
    finally:
        db.close()
    return res.get("rows", []) if isinstance(res, dict) else []


def _format_entry_points(rows: list[dict]) -> str:
    if not rows:
        return "(no :EntryPoint nodes found — infer entry points from the source you can read)"
    return "\n".join(
        f"- {r.get('name') or r.get('full_name')}  ({r.get('file')}:{r.get('line')})  "
        f"full_name={r.get('full_name')}" for r in rows)


def generate(scan_id: str, repo: str, language: str, on_event: OnEvent | None = None,
             *, timeout: int = 300) -> HarnessResult:
    """Ask the agent for a driver and write it to a temp file. Returns a HarnessResult (path=None on
    any skip). `language` is the tracer language ("py"/"js"); `repo` is the --add-dir source sandbox."""
    entries = load_entry_points(scan_id)
    if on_event is not None:
        on_event({"ts": "", "phase": "dynamic", "shape": None, "lead": None, "turn": None,
                  "event": "start", "detail": f"harness agent: {len(entries)} entry points, lang={language}"})

    message = (
        f"Target repo: {repo}\nLanguage: {language}\nscan_id: {scan_id}\n\n"
        f"Entry points to exercise:\n{_format_entry_points(entries)}\n\n"
        "Write the driver now. Prefer driving the most entry points possible, safely.")

    result = claude_cli.run_agent(
        session_id=str(uuid.uuid4()),
        system=_SYSTEM,
        message=message,
        json_schema=HARNESS_SCHEMA,
        add_dir=repo,
        extra_allowed=("Read", "Grep", "Glob"),
        on_event=(lambda ev: on_event({**_base_ev(), **ev})) if on_event else None,
        max_turns=config.MAX_TURNS,
        timeout=timeout,
        retries=1,
    )

    if "_error" in result:
        _warn(on_event, f"harness generation failed, skipping dynamic layer: {result['_error'][:200]}")
        return HarnessResult(path=None, language=language, error=result["_error"])

    driver = result.get("driver")
    if not isinstance(driver, str) or not driver.strip():
        _warn(on_event, "harness agent returned no driver, skipping dynamic layer")
        return HarnessResult(path=None, language=language, error="empty driver")

    fd, path = tempfile.mkstemp(prefix="orion_harness_", suffix=_SUFFIX.get(language, ".txt"))
    import os
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(driver)
    notes = result.get("notes") if isinstance(result.get("notes"), str) else ""
    if on_event is not None:
        on_event({**_base_ev(), "event": "done", "detail": f"harness written ({len(driver)} chars): {notes[:160]}"})
    return HarnessResult(path=path, language=language, notes=notes)


def _base_ev() -> dict:
    return {"ts": "", "phase": "dynamic", "shape": None, "lead": None, "turn": None,
            "event": "tool", "detail": ""}


def _warn(on_event: OnEvent | None, detail: str) -> None:
    if on_event is not None:
        on_event({**_base_ev(), "event": "warn", "detail": detail})
