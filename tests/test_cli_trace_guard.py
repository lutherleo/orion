"""`orion trace` pre-flight guards. Token-free: every guard here runs before any heavy import
(no Neo4j, no joern, no `claude` subprocess) and before the dynamic phase starts.
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


def test_language_passes_guards_into_phase(tmp_path, monkeypatch):
    """A valid py OR js target clears every guard and reaches the dynamic phase. Stub trace_repo so
    the test stays token-free/infra-free — the point is only that the guards let a valid call
    through (and that JS is no longer rejected)."""
    import orion.dynamic.run as dyn_run

    def _boom(*a, **k):
        raise RuntimeError("reached-phase")

    monkeypatch.setattr(dyn_run, "trace_repo", _boom)
    for lang in ("py", "js"):
        try:
            cli.main(["trace", str(tmp_path), "--language", lang, "--scan-id", "s1"])
        except RuntimeError as exc:
            assert "reached-phase" in str(exc)
        else:
            raise AssertionError(f"guards should have passed a valid {lang} trace into the phase")
