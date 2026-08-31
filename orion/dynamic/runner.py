"""Execute a command for the dynamic layer behind a swappable Sandbox seam.

Per the operator's decision (design §9), the shipped isolation floor is a wall-clock timeout + a
temp working directory — NO container, NO network/resource isolation. `orion trace` runs the
target repo's code on the host, which is fine for trusted targets and unacceptable for untrusted
third-party code. This module is the ONE seam where a `DockerSandbox` (Orion already requires Docker
for Neo4j) drops in later to raise that floor without touching any caller: everything upstream calls
`Sandbox.run` and reads a `RunResult`, so the implementation swaps freely.

A run that times out is NOT an error to the caller — it returns a `RunResult(timed_out=True)` with
whatever stdout/stderr was salvaged, matching the layer's "failure is a logged skip, never a crash"
contract.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Protocol, Sequence


@dataclass(frozen=True)
class RunResult:
    """The outcome of one sandboxed command. `timed_out` is surfaced separately from `exit_code`
    because a killed process has no meaningful exit code but its partial stdout/stderr still matter."""
    stdout: str
    stderr: str
    exit_code: int | None
    timed_out: bool


class Sandbox(Protocol):
    """The seam. An implementer runs `cmd` and returns a RunResult; it must never raise on a target
    failure/timeout — those are reported through the RunResult, so the caller's skip logic is uniform."""

    def run(self, cmd: Sequence[str], *, cwd: str | None = None,
            timeout: float | None = None, env: dict[str, str] | None = None) -> RunResult:
        ...


class SubprocessSandbox:
    """The shipped floor: a plain child process with a wall-clock timeout and a working directory
    (a fresh temp dir if the caller gives none). No security boundary — see the module docstring."""

    def run(self, cmd: Sequence[str], *, cwd: str | None = None,
            timeout: float | None = None, env: dict[str, str] | None = None) -> RunResult:
        run_cwd = cwd or tempfile.mkdtemp(prefix="orion_trace_")
        run_env = {**os.environ, **(env or {})}
        try:
            proc = subprocess.run(
                list(cmd), cwd=run_cwd, env=run_env,
                capture_output=True, text=True, timeout=timeout)
            return RunResult(stdout=proc.stdout or "", stderr=proc.stderr or "",
                             exit_code=proc.returncode, timed_out=False)
        except subprocess.TimeoutExpired as exc:
            # Salvage whatever the process printed before we killed it (bytes when text failed).
            out = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            err = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            return RunResult(stdout=out, stderr=err, exit_code=None, timed_out=True)
        except OSError as exc:
            # A missing/unrunnable executable (e.g. a bad node path) must be a reported skip, not a
            # raise — the contract is that the sandbox never throws on a target/launch failure.
            return RunResult(stdout="", stderr=f"failed to launch {cmd[0]!r}: {exc}",
                             exit_code=127, timed_out=False)
