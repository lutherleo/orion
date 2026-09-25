"""HarnessDriver: exercise a target by running a SCRIPT that calls its entry points, under a tracer.

The script is either pinned (`--harness-file`, zero tokens) or written by a read-only `claude -p`
agent that reads the scan's :EntryPoint nodes and the real source (sandboxed to the target via
--add-dir) and returns the driver as structured output -- it never gets Write/Bash/Edit. This is the
driver for library-shaped Python/JS code with no server to boot.

Fallible by contract (CLAUDE.md): a `_error` sentinel, an empty driver, or a malformed result is a
logged skip (no seeds -> the engine drives nothing), never a crash and never a fabricated script.
"""
from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path

from .. import claude_cli, config
from ..contracts import OnEvent
from .base import Input, Response, RunningTarget
from .sandbox import DockerSandbox, Sandbox, SubprocessSandbox, default_image, docker_available

# Structured-output contract for the agent. `driver` is the whole script; `notes` is a short
# rationale surfaced in the run log.
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

_ENTRY_CYPHER = (
    "MATCH (e:EntryPoint {scan_id:$scan_id})-[:ENTERS_AT]->(m:CpgMethod) "
    "RETURN m.full_name AS full_name, m.name AS name, m.file_path AS file, m.line AS line "
    "ORDER BY file, line")


def _ev(event: str, detail: str) -> dict:
    return {"ts": "", "phase": "runtime", "shape": None, "lead": None, "turn": None,
            "event": event, "detail": detail}


def _format_entry_points(rows: list[dict]) -> str:
    if not rows:
        return "(no :EntryPoint nodes found — infer entry points from the source you can read)"
    return "\n".join(
        f"- {r.get('name') or r.get('full_name')}  ({r.get('file')}:{r.get('line')})  "
        f"full_name={r.get('full_name')}" for r in rows)


def generate(db, scan_id: str, repo: str, language: str, on_event: OnEvent | None = None,
             *, timeout: int = 300) -> str | None:
    """Ask the agent for a driver; write it to a temp file and return its path (None on any skip)."""
    res = db.run_cypher(scan_id, _ENTRY_CYPHER, limit=200)
    entries = res.get("rows", []) if isinstance(res, dict) else []
    emit = on_event or (lambda ev: None)
    emit(_ev("start", f"harness agent: {len(entries)} entry points, lang={language}"))

    result = claude_cli.run_agent(
        session_id=str(uuid.uuid4()),
        system=_SYSTEM,
        message=(f"Target repo: {repo}\nLanguage: {language}\nscan_id: {scan_id}\n\n"
                 f"Entry points to exercise:\n{_format_entry_points(entries)}\n\n"
                 "Write the driver now. Prefer driving the most entry points possible, safely."),
        json_schema=HARNESS_SCHEMA,
        add_dir=repo,
        extra_allowed=("Read", "Grep", "Glob"),
        on_event=(lambda ev: emit({**_ev("tool", ""), **ev})) if on_event else None,
        max_turns=config.MAX_TURNS,
        timeout=timeout,
        retries=1,
    )
    if "_error" in result:
        emit(_ev("warn", f"harness generation failed, skipping: {result['_error'][:200]}"))
        return None
    driver = result.get("driver")
    if not isinstance(driver, str) or not driver.strip():
        emit(_ev("warn", "harness agent returned no driver, skipping"))
        return None
    fd, path = tempfile.mkstemp(prefix="orion_harness_", suffix=_SUFFIX.get(language, ".txt"))
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(driver)
    notes = result.get("notes") if isinstance(result.get("notes"), str) else ""
    emit(_ev("done", f"harness written ({len(driver)} chars): {notes[:160]}"))
    return path


class HarnessDriver:
    """Run one harness script per seed under the tracer, each in a sandboxed child process."""

    feedback = True     # every run leaves its own sidecar -> collect per run
    mutable = False     # a script is not fuzzable data

    def __init__(self, repo: str, language: str, tracer, *, harness_file: str | None = None,
                 timeout: float = 120.0, on_event: OnEvent | None = None,
                 sandbox: Sandbox | None = None, isolation: str = "auto",
                 image: str | None = None) -> None:
        """`isolation` is "docker" | "host" | "auto" (Docker when its daemon answers, else host with a
        warning). An explicit `sandbox` instance overrides it (tests). `image` overrides the
        per-language default (ORION_SANDBOX_PY_IMAGE / ORION_SANDBOX_NODE_IMAGE)."""
        self._repo = repo
        self.language = language
        self._tracer = tracer
        self._harness_file = harness_file
        self._timeout = timeout
        self._on_event = on_event
        self._sandbox = sandbox
        self._isolation = isolation
        self._image = image or default_image(language)
        self.use_docker = False

    def _warn(self, detail: str) -> None:
        if self._on_event is not None:
            self._on_event(_ev("warn", detail))

    def start(self, repo: str, work: Path, build_flags: list[str]) -> RunningTarget:
        if self._sandbox is None:
            self.use_docker = self._isolation == "docker" or (
                self._isolation == "auto" and docker_available())
            if self._isolation == "auto" and not self.use_docker:
                self._warn("Docker unavailable: running the harness ON THIS HOST (no isolation). An "
                           "agent-written harness is untrusted code -- start Docker to contain it.")
        return RunningTarget(kind="script", repo=repo, work=Path(work))

    def seeds(self, db, scan_id: str) -> list[Input]:
        path = self._harness_file
        if path is None:
            path = generate(db, scan_id, self._repo, self.language, self._on_event,
                            timeout=int(self._timeout) + 180)
        return [Input(kind="script", label=f"harness {os.path.basename(path)}", argv=(path,))] if path else []

    def _sandbox_for(self, target: RunningTarget, script: str) -> Sandbox:
        if self._sandbox is not None:
            return self._sandbox
        if self.use_docker:
            return DockerSandbox.for_harness(self._image, repo=os.path.abspath(target.repo),
                                             work=str(target.work), script=script)
        return SubprocessSandbox()

    def send(self, target: RunningTarget, inp: Input) -> Response:
        script = inp.argv[0]
        cmd, env = self._tracer.script_command(script, target.repo, target.work)
        sandbox = self._sandbox_for(target, script)
        r = sandbox.run(cmd, cwd=os.path.abspath(target.repo), timeout=self._timeout, env=env)
        return Response(ok=r.exit_code == 0, status=r.exit_code if r.exit_code is not None else -1,
                        detail=(r.stderr or "")[-300:], timed_out=r.timed_out)

    def stop(self, target: RunningTarget) -> None:
        pass
