"""The agents' MCP tool server is launched portably: by default an INLINE config that runs the
current interpreter (no hardcoded `./.venv/bin/python`), with ORION_MCP_CONFIG still able to point at
a config file. Pure -- builds the command, never launches `claude`."""
from __future__ import annotations

import json
import os
import sys

from orion import claude_cli, config


def _mcp_arg():
    cmd = claude_cli._build_cmd(session_id="s", system="sys", json_schema=None, add_dir=None,
                                extra_allowed=(), max_turns=None)
    return cmd[cmd.index("--mcp-config") + 1], cmd


def test_default_is_inline_config_with_this_interpreter(monkeypatch):
    monkeypatch.setattr(config, "MCP_CONFIG", "")
    arg, cmd = _mcp_arg()
    server = json.loads(arg)["mcpServers"]["orion"]
    assert (server["command"], server["args"]) == (sys.executable, ["-m", "orion.mcp_server"])
    # the agent no longer runs from the checkout, so the checkout goes on the server's import path
    assert server["env"]["PYTHONPATH"].split(os.pathsep)[0] == str(claude_cli._REPO_ROOT)
    assert "--strict-mcp-config" in cmd


def test_agents_run_outside_the_checkout_and_add_dir_is_absolute(monkeypatch):
    """2026-10-29: from Orion's checkout an agent could Read tests/ground_truth_*.py (answer keys)."""
    monkeypatch.setattr(config, "MCP_CONFIG", "")
    cwd = claude_cli._agent_cwd()
    assert cwd.is_dir() and claude_cli._REPO_ROOT not in (cwd, *cwd.parents)
    cmd = claude_cli._build_cmd(session_id="s", system="sys", json_schema=None, add_dir="fixtures/NodeGoat",
                                extra_allowed=(), max_turns=None)
    target = cmd[cmd.index("--add-dir") + 1]
    assert os.path.isabs(target) and target == str((claude_cli._REPO_ROOT / "fixtures/NodeGoat").resolve())
    monkeypatch.setattr(config, "MCP_CONFIG", ".mcp/orion.json")      # its relative paths keep the old cwd
    assert claude_cli._agent_cwd() == claude_cli._REPO_ROOT


def test_explicit_config_path_wins(monkeypatch):
    monkeypatch.setattr(config, "MCP_CONFIG", ".mcp/orion.json")
    assert _mcp_arg()[0] == ".mcp/orion.json"
