"""The agents' MCP tool server is launched portably: by default an INLINE config that runs the
current interpreter (no hardcoded `./.venv/bin/python`), with ORION_MCP_CONFIG still able to point at
a config file. Pure -- builds the command, never launches `claude`."""
from __future__ import annotations

import json
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
    assert server == {"command": sys.executable, "args": ["-m", "orion.mcp_server"]}
    assert "--strict-mcp-config" in cmd


def test_explicit_config_path_wins(monkeypatch):
    monkeypatch.setattr(config, "MCP_CONFIG", ".mcp/orion.json")
    assert _mcp_arg()[0] == ".mcp/orion.json"
