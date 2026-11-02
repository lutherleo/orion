"""Do route and header variables in the UI's environment change where an endpoint-mode scan's `claude -p` really goes?
The real CLI, started with the environment keys.Access.child_env builds. No tokens: the endpoint is a local server that
answers 400, the key and headers are dummies, and HOME and the config folder are new empty ones, so no login or cloud
credential is there to find. POSIX only.

    python reports/review-bbcd2ab/repros/repro_route_vars_real_claude.py [ORION_CHECKOUT]

Per variable: whether child_env passed it on, how many requests reached the endpoint the page names, whether the
endpoint received the custom header, and what the CLI said.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

checkout = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[3]
sys.path.insert(0, str(checkout))
from orion.ui import keys   # noqa: E402

HEADER = "X-Gateway-Key"
SWITCHES = ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY", "CLAUDE_CODE_USE_MANTLE",
            "CLAUDE_CODE_USE_ANTHROPIC_AWS", "CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD", "CLAUDE_CODE_USE_GATEWAY")
CLAUDE = shutil.which("claude")


class Endpoint(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    requests: list[str] = []
    header_seen = False

    def log_message(self, *args) -> None:
        pass

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("content-length") or 0))
        if "/v1/messages" in self.path and "count_tokens" not in self.path:
            Endpoint.requests.append(self.path)
            Endpoint.header_seen = Endpoint.header_seen or self.headers.get(HEADER) is not None
        body = json.dumps({"type": "error", "error": {"type": "invalid_request_error", "message": "repro"}}).encode()
        self.send_response(400)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self.send_response(404)
        self.send_header("content-length", "0")
        self.end_headers()

    do_HEAD = do_GET


def said(out: str) -> str:
    """The CLI's own words: its result event if it printed one, else the tail of what it wrote."""
    for line in out.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "result":
            return str(ev.get("result"))[:110]
    return out.strip()[-110:]


def try_one(label: str, extra: dict[str, str]) -> None:
    Endpoint.requests, Endpoint.header_seen = [], False
    server = ThreadingHTTPServer(("127.0.0.1", 0), Endpoint)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with tempfile.TemporaryDirectory(prefix="repro-route-", ignore_cleanup_errors=True) as tmp:
        base = {k: v for k, v in os.environ.items() if not k.upper().startswith(("ANTHROPIC", "CLAUDE"))}
        base.update({"HOME": tmp, "USERPROFILE": tmp, "CLAUDE_CONFIG_DIR": str(Path(tmp) / "config"),
                     "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "AWS_EC2_METADATA_DISABLED": "true", **extra})
        access = keys.Access(keys.Keychain(backend=None), base)
        access.set_endpoint(f"http://127.0.0.1:{server.server_address[1]}", "", "sonnet")
        env = access.child_env(base)
        passed = "yes" if all(name in env for name in extra) and extra else "no" if extra else "-"
        try:
            r = subprocess.run([CLAUDE, "-p", "hi", "--output-format", "stream-json", "--verbose"], env=env, cwd=tmp,
                               stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace", timeout=45)
            words = f"exit {r.returncode}: {said(r.stdout + r.stderr)!r}"
        except subprocess.TimeoutExpired:
            words = "no answer in 45 s"
    server.shutdown()
    print(f"{label:40} passed on: {passed:3} | requests at the page's endpoint: {len(Endpoint.requests):2}"
          f" | header there: {'YES' if Endpoint.header_seen else 'no ':3} | {words}", flush=True)


print("orion code:", Path(sys.modules["orion"].__file__).parent)
print("claude:", subprocess.run([CLAUDE, "--version"], capture_output=True, text=True).stdout.strip())
try_one("(none: control)", {})
try_one("ANTHROPIC_CUSTOM_HEADERS", {"ANTHROPIC_CUSTOM_HEADERS": f"{HEADER}: repro-not-a-secret"})
for switch in SWITCHES:
    try_one(switch, {switch: "1"})
