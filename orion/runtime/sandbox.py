"""Execute a command for the runtime stage behind a swappable Sandbox seam.

Two implementations behind one `Sandbox.run` -> `RunResult` contract:

- `SubprocessSandbox` -- a plain child process with a wall-clock timeout and a working directory. NO
  security boundary: the code runs on the host with the user's rights. Fine for trusted targets.
- `DockerSandbox` -- the same command inside a throwaway container: no network, a read-only root
  and read-only source, all capabilities dropped, bounded memory/CPU/processes, and only the work
  dir writable. This is the floor for AGENT-WRITTEN harness scripts: the agent that writes them reads
  the target's source, which may carry a prompt injection, so its output is treated as untrusted
  code. Orion already needs Docker for Neo4j.

A run that times out is NOT an error to the caller — it returns a `RunResult(timed_out=True)` with
whatever stdout/stderr was salvaged, matching the stage's "failure is a logged skip, never a crash"
contract.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from functools import lru_cache
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


# ─────────────────────────────── Docker ───────────────────────────────

DEFAULT_IMAGES = {"py": "python:3.12-slim", "js": "node:22-slim"}
_IMAGE_ENV = {"py": "ORION_SANDBOX_PY_IMAGE", "js": "ORION_SANDBOX_NODE_IMAGE"}
_START_GRACE = 15.0          # seconds on top of the run's own timeout for container start/teardown


def default_image(language: str) -> str:
    return os.environ.get(_IMAGE_ENV.get(language, ""), "") or DEFAULT_IMAGES.get(language, "")


@lru_cache(maxsize=1)
def docker_available() -> bool:
    """True when a Docker CLI is on PATH AND its daemon answers. Cached per process."""
    exe = shutil.which("docker")
    if exe is None:
        return False
    try:
        return subprocess.run([exe, "info"], capture_output=True, timeout=15).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


@lru_cache(maxsize=None)
def _ensure_image(docker: str, image: str) -> None:
    """Pull `image` once per process if it is not present locally. Best-effort: on failure the run
    itself reports the error."""
    try:
        if subprocess.run([docker, "image", "inspect", image], capture_output=True,
                          timeout=30).returncode != 0:
            subprocess.run([docker, "pull", image], capture_output=True, timeout=900)
    except (OSError, subprocess.SubprocessError):
        pass


@dataclass(frozen=True)
class Mount:
    host: str
    container: str
    read_only: bool = True


def _key(path: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


class DockerSandbox:
    """Run a command inside a locked-down, throwaway container.

    Host paths in the command, its cwd and its env values are rewritten to container paths through
    the mounts (longest host prefix wins); a host Python/Node interpreter becomes the image's own
    `python` / `node`. Never raises: a missing docker CLI is exit 127, a timeout kills the container
    (killing only the CLI client would leave it running) and reports `timed_out`."""

    def __init__(self, image: str, mounts: Sequence[Mount], *, docker: str = "docker",
                 memory: str = "2g", cpus: str = "2", pids: int = 256) -> None:
        self.image = image
        self.mounts = sorted(mounts, key=lambda m: len(_key(m.host)), reverse=True)
        self._docker = docker
        self._limits = ("--memory", memory, "--cpus", cpus, "--pids-limit", str(pids))

    @classmethod
    def for_harness(cls, image: str, *, repo: str, work: str, script: str,
                    runtime_dir: str | None = None) -> "DockerSandbox":
        """The harness mount set: source read-only at /src, the work dir writable at /work, Orion's
        stdlib-only bootstraps read-only at /orion, and the script's own dir when it lives elsewhere
        (an agent-written driver sits in a temp dir)."""
        runtime_dir = runtime_dir or os.path.dirname(os.path.abspath(__file__))
        mounts = [Mount(repo, "/src"), Mount(str(work), "/work", read_only=False),
                  Mount(runtime_dir, "/orion")]
        script_dir = _key(os.path.dirname(os.path.abspath(script)))
        if not any(script_dir == _key(m.host) or script_dir.startswith(_key(m.host) + os.sep)
                   for m in mounts):
            mounts.append(Mount(os.path.dirname(os.path.abspath(script)), "/harness"))
        return cls(image, mounts)

    def to_container(self, value: str) -> str:
        """A host path under a mount, rewritten to its container path; anything else unchanged."""
        if not value or not os.path.isabs(value):
            return value
        k = _key(value)
        for m in self.mounts:
            base = _key(m.host)
            if k == base or k.startswith(base + os.sep):
                rel = os.path.relpath(k, base).replace(os.sep, "/")
                return m.container if rel == "." else f"{m.container}/{rel}"
        return value

    @staticmethod
    def _interpreter(exe: str) -> str | None:
        name = os.path.basename(exe).lower()
        name = name[:-4] if name.endswith(".exe") else name
        if name.startswith("python"):
            return "python"
        return "node" if name == "node" else None

    def build_command(self, cmd: Sequence[str], *, cwd: str | None, env: dict[str, str] | None,
                      name: str) -> list[str]:
        """The full `docker run` argv. Pure -- unit-tested without Docker."""
        argv = [self._docker, "run", "--rm", "--name", name, "--network", "none", "--read-only",
                "--tmpfs", "/tmp:rw,size=256m", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges", *self._limits]
        if hasattr(os, "getuid"):          # files the container writes to /work stay the user's
            argv += ["--user", f"{os.getuid()}:{os.getgid()}"]
        for m in reversed(self.mounts):
            argv += ["-v", f"{os.path.abspath(m.host)}:{m.container}" + (":ro" if m.read_only else "")]
        if cwd:
            argv += ["-w", self.to_container(cwd)]
        for k, v in (env or {}).items():
            argv += ["-e", f"{k}={self.to_container(v)}"]
        argv.append(self.image)
        if cmd:
            argv.append(self._interpreter(cmd[0]) or self.to_container(cmd[0]))
            argv += [self.to_container(a) for a in cmd[1:]]
        return argv

    def run(self, cmd: Sequence[str], *, cwd: str | None = None,
            timeout: float | None = None, env: dict[str, str] | None = None) -> RunResult:
        name = f"orion-sbx-{uuid.uuid4().hex[:12]}"
        argv = self.build_command(cmd, cwd=cwd, env=env, name=name)
        _ensure_image(self._docker, self.image)   # a first-run pull must not eat the run's timeout
        try:
            proc = subprocess.run(argv, capture_output=True, text=True,
                                  timeout=None if timeout is None else timeout + _START_GRACE)
            return RunResult(stdout=proc.stdout or "", stderr=proc.stderr or "",
                             exit_code=proc.returncode, timed_out=False)
        except subprocess.TimeoutExpired as exc:
            try:
                subprocess.run([self._docker, "kill", name], capture_output=True, timeout=30)
            except (OSError, subprocess.SubprocessError):
                pass
            out = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            err = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            return RunResult(stdout=out, stderr=err, exit_code=None, timed_out=True)
        except OSError as exc:
            return RunResult(stdout="", stderr=f"failed to launch {self._docker!r}: {exc}",
                             exit_code=127, timed_out=False)
