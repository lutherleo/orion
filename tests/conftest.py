"""Shared skip guards. The token-free suite's contract: a test that needs local infrastructure the
machine lacks SKIPS, never fails. Neo4j and the fixtures are checked inside each test; Joern is
checked here, because a machine can have Neo4j and fixtures/NodeGoat but no Joern (e.g. Windows
reaching the Neo4j in WSL, where Joern lives) -- and a build then raises instead of skipping."""
from __future__ import annotations

import pytest

from orion import config
from orion.graph import joern_adapter


def joern_available() -> bool:
    return joern_adapter._joern_bin("joern-parse").exists()


@pytest.fixture
def needs_joern() -> None:
    """Request this fixture in any test that builds a graph from source (graph_build.build)."""
    if not joern_available():
        pytest.skip(f"Joern not installed (no joern-parse under {config.JOERN_HOME}; set JOERN_HOME)")
