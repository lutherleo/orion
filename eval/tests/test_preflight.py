from eval import preflight


def test_check_marks_missing_tools_without_raising():
    def run(cmd):
        if cmd[0] == "codex":
            raise FileNotFoundError("codex")
        return "ok"
    rows = preflight.check(run=run)
    codex = [r for r in rows if r["tool"] == "codex"][0]
    assert codex["ok"] is False
    assert any(r["ok"] for r in rows)


def test_render_flags_missing():
    rows = [{"tool": "codex", "ok": False, "detail": "not found"},
            {"tool": "claude", "ok": True, "detail": "2.1.210"}]
    out = preflight.render(rows)
    assert "MISSING: codex" in out


def test_joern_is_probed_with_help_not_version():
    # joern-parse v4 exits 1 on --version, which made a working install look missing.
    cmds = {tool: cmd for tool, cmd in preflight._CHECKS}
    assert cmds["joern"][-1] == "--help" and cmds["joern"][0].endswith("joern-parse")
