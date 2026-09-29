import os
import subprocess

_CHECKS = [
    ("ollama", ["ollama", "--version"]),
    ("ollama-serve", ["curl", "-sf", "http://localhost:11434/api/tags"]),
    ("codex", ["codex", "--version"]),
    ("claude", ["claude", "--version"]),
    ("docker", ["docker", "ps"]),
    # joern-parse (v4) has no --version flag -- it exits 1 on it -- so probe with --help. Honors
    # JOERN_HOME like orion/config.py does.
    ("joern", [os.path.join(os.path.expanduser(os.environ.get("JOERN_HOME", "~/joern/joern-cli")),
                            "joern-parse"), "--help"]),
    ("neo4j", ["curl", "-sf", "http://localhost:7475"]),
    ("cloc", ["cloc", "--version"]),
]


def _default_run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout


def check(*, run=_default_run) -> list[dict]:
    rows = []
    for tool, cmd in _CHECKS:
        try:
            detail = (run(cmd) or "").strip().splitlines()[:1]
            rows.append({"tool": tool, "ok": True, "detail": detail[0] if detail else "ok"})
        except Exception as e:
            rows.append({"tool": tool, "ok": False, "detail": str(e)})
    return rows


def render(rows) -> str:
    lines = [f"{'OK ' if r['ok'] else 'MISS'}  {r['tool']:<14} {r['detail']}" for r in rows]
    missing = [r["tool"] for r in rows if not r["ok"]]
    lines.append("MISSING: " + ", ".join(missing) if missing else "ALL PRESENT")
    return "\n".join(lines)


if __name__ == "__main__":
    print(render(check()))
