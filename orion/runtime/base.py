"""Seam interfaces for the runtime stage: how a target is DRIVEN and how it is TRACED.

A run is one (Driver, Tracer) pair chosen by `targets.select`:

    driver  = HOW to exercise the target  -- HarnessDriver (a script calling the entry points),
                                             HttpDriver (boot a web app, fuzz its routes),
                                             ProcessDriver (build an exe, fuzz its argv/stdin)
    tracer  = HOW to observe it           -- PyTracer (sys.monitoring), V8Tracer (precise coverage
                                             + cpu profile), GoCoverTracer (`go build -cover`)

Everything here is pure data or a `Protocol`, so a fake satisfies either seam in a token-free test.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .trace import RuntimeTrace


@dataclass
class Input:
    """One thing to feed the target. http: verb+path+body+headers. process: argv+stdin.
    script: argv[0] is the driver script to run under the tracer."""
    kind: str                      # "http" | "process" | "script"
    label: str = ""                # human-readable, for progress events
    verb: str = "GET"
    path: str = "/"
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)
    argv: tuple[str, ...] = ()
    stdin: bytes = b""


@dataclass
class Response:
    """The observable result of one Input. The engine never judges it; coverage is the feedback."""
    ok: bool = True
    status: int = 0
    detail: str = ""
    timed_out: bool = False


@dataclass
class RunningTarget:
    """Opaque handle a Driver returns from start() and consumes in send()/stop()."""
    kind: str
    repo: str                       # source root the tracer relativizes against
    work: Path                      # dump dir the tracer writes/reads
    base_url: str = ""
    exe_path: str = ""
    handle: object | None = None    # e.g. a Popen, held by the driver


class Tracer(Protocol):
    """Instrument a target and parse what it dumped into a RuntimeTrace.

    `collect` returns what was dumped SINCE the last `reset`, so per-input traces are disjoint and a
    running sum never double counts."""

    def launch_env(self, work: Path) -> dict[str, str]: ...
    def build_flags(self) -> list[str]: ...
    def collect(self, work: Path, repo: str) -> RuntimeTrace: ...
    def reset(self, work: Path) -> None: ...


class Driver(Protocol):
    """Boot and drive one target shape. The ONLY execution seam.

    `feedback`: True when each send() leaves its trace on disk right away (a process exits per
    input), so the engine can collect per input and steer mutation by NEW coverage. False for a
    long-running server, which only flushes on exit: the engine then drives blind and the pipeline
    collects once after stop(). `mutable`: False when inputs are whole scripts, not fuzzable data."""

    feedback: bool
    mutable: bool

    def start(self, repo: str, work: Path, build_flags: list[str]) -> RunningTarget: ...
    def seeds(self, db, scan_id: str) -> list[Input]: ...
    def send(self, target: RunningTarget, inp: Input) -> Response: ...
    def stop(self, target: RunningTarget) -> None: ...
