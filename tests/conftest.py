"""Shared skip guards. The token-free suite's contract: a test that needs local infrastructure the
machine lacks SKIPS, never fails. Neo4j and the fixtures are checked inside each test; Joern is
checked here, because a machine can have Neo4j and fixtures/NodeGoat but no working Joern (Windows
reaching the Neo4j in WSL, where Joern lives; or a shell without Java on PATH) -- and a build then
raises instead of skipping."""
from __future__ import annotations

import functools
import subprocess

import pytest

from orion import config
from orion.graph import joern_adapter


@functools.lru_cache(maxsize=1)
def joern_problem() -> str | None:
    """Why Joern can't run here, or None. Probed ONCE per session with `joern-parse --help` (v4 has
    no --version), which also catches an installed Joern whose JVM is missing from PATH."""
    parse = joern_adapter._joern_bin("joern-parse")
    if not parse.exists():
        return f"Joern not installed (no joern-parse under {config.JOERN_HOME}; set JOERN_HOME)"
    try:
        rc = subprocess.run([str(parse), "--help"], capture_output=True, timeout=120).returncode
    except (OSError, subprocess.SubprocessError) as exc:
        return f"joern-parse cannot run: {exc}"
    return None if rc == 0 else f"joern-parse --help exited {rc} (is Java on PATH? source bench/env.sh)"


@pytest.fixture
def needs_joern() -> None:
    """Request this fixture in any test that builds a graph from source (graph_build.build)."""
    problem = joern_problem()
    if problem:
        pytest.skip(problem)
