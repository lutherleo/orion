"""`orion trace` pre-flight guards. Token-free: every guard here runs before any heavy import
(no Neo4j, no joern, no `claude` subprocess) and before the runtime stage starts.
"""
from __future__ import annotations

from orion import cli


def test_missing_repo_path_exits_2_cleanly(capsys):
    rc = cli.main(["trace", "/no/such/path/orion-trace-xyzzy"])
    assert rc == 2
    assert "repo path not found" in capsys.readouterr().err


def test_no_repo_and_no_scan_id_exits_2(capsys):
    rc = cli.main(["trace"])
    assert rc == 2
    assert "provide a repo path or --scan-id" in capsys.readouterr().err


def test_bad_language_exits_2(tmp_path, capsys):
    rc = cli.main(["trace", str(tmp_path), "--language", "ruby"])
    assert rc == 2
    assert "unknown --language" in capsys.readouterr().err


def test_valid_args_reach_the_runtime_stage(tmp_path, monkeypatch):
    """A valid call clears every guard and reaches runtime.enrich with the parsed options. Stubbed so
    the test stays token-free/infra-free."""
    import orion.runtime as runtime

    seen = []
    monkeypatch.setattr(runtime, "enrich", lambda *a, **k: seen.append((a, k)) or {})
    monkeypatch.chdir(tmp_path)          # the run log lands under ./.orion
    for lang in ("py", "js"):
        assert cli.main(["trace", str(tmp_path), "--language", lang, "--scan-id", "s1",
                         "--driver", "harness", "--budget", "7", "--quiet"]) == 0
    (a, k), _ = seen
    assert a[:2] == ("s1", str(tmp_path))
    assert (k["language"], k["driver"], k["budget"]) == ("py", "harness", 7)


def test_unknown_driver_is_rejected(tmp_path):
    import pytest
    with pytest.raises(SystemExit):
        cli.main(["trace", str(tmp_path), "--driver", "docker"])
