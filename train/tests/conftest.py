import os

import pytest


@pytest.fixture
def hermetic_git(monkeypatch):
    """Real git, but blind to the developer's global/system config (signing, diff prefixes, hooks) and
    with a fixed identity, so commit/diff tests are reproducible on any machine."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for k, v in (("GIT_AUTHOR_NAME", "t"), ("GIT_AUTHOR_EMAIL", "t@example.com"),
                 ("GIT_COMMITTER_NAME", "t"), ("GIT_COMMITTER_EMAIL", "t@example.com")):
        monkeypatch.setenv(k, v)


@pytest.fixture
def needs_tree_sitter() -> None:
    """Same as tests/'s fixture: without tree-sitter (the `lm` extra) `RepoIndex` indexes no function, so a
    test that reads spans fails or, worse, passes on an empty result."""
    pytest.importorskip("tree_sitter")
