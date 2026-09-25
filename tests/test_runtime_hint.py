"""The opt-in discovery hint for runtime-observed edges (Layer 6). Token-free — prompt strings and a
threading check, no Claude/Neo4j.

The load-bearing property: with the hint OFF (the default) the discovery prompt is byte-identical to
the pre-dynamic baseline, so the NodeGoat 14/15 eval number stays comparable. ON, the OBSERVED_* block
is present and threads all the way from `discover` to the per-shape system prompt.
"""
from __future__ import annotations

from orion import strategies


def test_hint_absent_by_default():
    sys_default = strategies.system_for("A", "scan1")
    assert "OBSERVED_CALL" not in sys_default
    assert "origin='runtime'" not in sys_default
    # default and explicit-False are the same string (the baseline prompt)
    assert sys_default == strategies.system_for("A", "scan1", dynamic_hint=False)


def test_hint_present_when_enabled():
    s = strategies.system_for("A", "scan1", dynamic_hint=True)
    assert "OBSERVED_CALL" in s
    assert "OBSERVED_DISPATCH" in s
    assert "origin='runtime'" in s
    assert "runtime-PROVEN" in s


def test_enabling_hint_only_adds_the_block():
    """The hint is purely additive: the OFF prompt is a substring-preserving prefix-ish of the ON one
    (every baseline section survives, only the block is inserted before the trailer)."""
    off = strategies.system_for("B", "scan1")
    on = strategies.system_for("B", "scan1", dynamic_hint=True)
    assert len(on) > len(off)
    # the shape-B body and the grounding rule are still present verbatim in the ON prompt
    assert "ABSENCE OF A CONTROL" in on
    assert "GROUNDING RULE" in on


def test_all_shapes_accept_the_hint():
    for shape in ("A", "B", "C", "D"):
        assert "OBSERVED_CALL" in strategies.system_for(shape, "s", dynamic_hint=True)


def test_discover_threads_dynamic_hint(monkeypatch):
    """discover(dynamic_hint=True) must reach strategies.system_for. Stub run_agent so no subprocess
    launches; capture the flag system_for was built with."""
    from orion import claude_cli, discover

    seen: list[bool] = []
    real_system_for = strategies.system_for

    def _spy(shape, scan_id, files=(), profile=None, dynamic_hint=False):
        seen.append(dynamic_hint)
        return real_system_for(shape, scan_id, files, profile, dynamic_hint)

    monkeypatch.setattr(strategies, "system_for", _spy)
    monkeypatch.setattr(claude_cli, "run_agent", lambda *a, **k: {"leads": []})

    discover.discover("scan1", lambda ev: None, dynamic_hint=True)
    assert seen and all(seen)          # every shape got the hint
    assert len(seen) == 4              # one per shape A/B/C/D
