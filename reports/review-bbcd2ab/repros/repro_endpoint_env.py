"""Which of the server's own route and header variables reach a scan's environment in each model-access mode?
Token-free: only keys.Access.child_env runs (no keychain, nothing is launched).

    python reports/review-bbcd2ab/repros/repro_endpoint_env.py [ORION_CHECKOUT]

The server's environment gets the route switches the claude CLI reads plus ANTHROPIC_CUSTOM_HEADERS; the printout
shows which of them each mode passes on to the scan.
"""
import sys
from pathlib import Path

checkout = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[3]
sys.path.insert(0, str(checkout))
from orion.ui import keys   # noqa: E402

WATCHED = ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY", "CLAUDE_CODE_USE_MANTLE",
           "CLAUDE_CODE_USE_ANTHROPIC_AWS", "CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD", "CLAUDE_CODE_USE_GATEWAY",
           "ANTHROPIC_CUSTOM_HEADERS")
server_env = {name: "1" for name in WATCHED}
server_env["ANTHROPIC_CUSTOM_HEADERS"] = "X-Gateway-Key: corp-secret-123"
server_env["PATH"] = "/usr/bin"

print("orion code:", Path(sys.modules["orion"].__file__).parent)
for mode in ("login", "anthropic_key", "endpoint"):
    access = keys.Access(keys.Keychain(backend=None), server_env)
    if mode == "anthropic_key":
        access.set_anthropic_key("sk-ant-api03-" + "x" * 40)
    elif mode == "endpoint":
        access.set_endpoint("http://127.0.0.1:11434", "", "qwen3:8b")
    env = access.child_env(server_env)
    print(f"{mode:13} passes on:", [name for name in WATCHED if name in env] or "none of them")
