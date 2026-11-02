"""A real stop of a real scan with real `claude -p` sessions, real SIGINT to the scan's process group, and no tokens:
the CLI's API base URL is a local server that never answers (and fails the first session on demand), its key is a
dummy, and its config folder is a new empty one, so no login is read and nothing reaches a model. POSIX only.

    python reports/review-bbcd2ab/repros/repro_stop_real_claude.py [ORION_CHECKOUT] [--watch] [--after-transient]
                                                                   [--log-events=N] [RUNS]

One lead that adaptive verification routes to the light pass, through `orion scan --resume --verify-routing`.
Default: the stop kills the light pass's first attempt.
--after-transient: the first session fails on its own (HTTP 400), so the stop kills the light pass's LAST attempt, the
case where nothing but the stop flag can keep verify_lead from escalating to a fresh full session.
--log-events=N: the run's progress.jsonl starts with N events of a real scan's size. Under --watch the main thread
flags the stop only after tail() has re-read that whole log, so this widens the gap the flag has to win.

Prints, per run: each session's start and end relative to the signal, its exit code, whether its stream had a result
event, whether the stop was already flagged when its exit was seen, when the flag was set, and whether the scan left
on its own. A session started after the signal is one no signal reaches: the scan then cannot exit.
"""
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

if os.name == "nt":
    sys.exit("POSIX only: Windows cannot aim a Ctrl-C at one process group")


def child(checkout: str, run_dir: str, log_path: str, watch: bool) -> None:
    sys.path.insert(0, checkout)
    from orion import claude_cli, cli, embed
    from orion.graph import scans
    scans.check_usable = lambda *a, **k: None        # no Neo4j: the run's scan record is taken as usable
    embed.ensure_exploit_index = lambda: False       # no embedding model load
    log_file = open(log_path, "a", encoding="utf-8", buffering=1)
    log_lock = threading.Lock()

    def log(**fields) -> None:
        with log_lock:
            log_file.write(json.dumps({"t": time.time(), **fields}) + "\n")

    class LoggedStop(threading.Event):
        def set(self) -> None:
            if not self.is_set():
                log(what="stop flagged")
            super().set()

    claude_cli._STOP = LoggedStop()
    real_run = subprocess.run

    def logged_run(cmd, **kw):
        if "claude" not in Path(str(cmd[0])).name.lower():
            return real_run(cmd, **kw)
        log(what="session start")
        r = real_run(cmd, **kw)
        _, final = claude_cli.parse_stream_events(r.stdout)
        shown = None if final is None else {k: str(final.get(k))[:60] for k in ("subtype", "is_error", "result")}
        log(what="session end", rc=r.returncode, result=shown, flagged=claude_cli.interrupted())
        return r

    claude_cli.subprocess.run = logged_run
    argv = ["scan", "--resume", run_dir, "--quiet", "--verify-routing"] + (["--watch"] if watch else [])
    log(what="orion code", path=str(Path(sys.modules["orion"].__file__).parent))
    try:
        log(what="cli returned", code=cli.main(argv))
    except KeyboardInterrupt:
        log(what="cli raised KeyboardInterrupt")


class Api(BaseHTTPRequestHandler):
    """Anthropic's API as far as the CLI gets: the first session may fail with a 400, any other request hangs."""
    protocol_version = "HTTP/1.1"
    fail_first = False
    first_session: str | None = None
    lock = threading.Lock()
    hanging = threading.Event()
    release = threading.Event()

    def log_message(self, *args) -> None:
        pass

    def _session(self, body: bytes) -> str:
        session = self.headers.get("x-claude-code-session-id")
        if session:
            return session
        try:
            return str(json.loads(body).get("metadata", {}).get("user_id"))
        except ValueError:
            return "?"

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("content-length") or 0))
        session = self._session(body)
        if "/v1/messages" not in self.path or "count_tokens" in self.path:
            return self._send(404, b"{}")
        with Api.lock:
            if Api.first_session is None:
                Api.first_session = session
        if Api.fail_first and session == Api.first_session:
            return self._send(400, json.dumps({"type": "error", "error": {
                "type": "invalid_request_error", "message": "repro: this session fails on its own"}}).encode())
        Api.hanging.set()
        Api.release.wait(300)
        self.close_connection = True

    def do_GET(self) -> None:
        self._send(404, b"{}")

    do_HEAD = do_GET

    def _send(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def one_run(run: int, checkout: str, watch: bool, after_transient: bool, log_events: int = 0) -> None:
    Api.fail_first, Api.first_session = after_transient, None
    Api.hanging, Api.release = threading.Event(), threading.Event()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Api)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with tempfile.TemporaryDirectory(prefix="repro-real-stop-", ignore_cleanup_errors=True) as tmp:
        work = Path(tmp)
        (work / "repo").mkdir()
        run_dir = work / "run"
        run_dir.mkdir()
        lead = {"index": 0, "shape": "D", "text": "a regex built from user input", "evidence": "e", "confidence": "HIGH"}
        (run_dir / "leads.json").write_text(json.dumps({"scan_id": "s1", "repo": str(work / "repo"), "engine": "claude",
                                                        "model": "sonnet", "leads": [lead]}), encoding="utf-8")
        line = json.dumps({"ts": "2026-11-01T00:00:00+00:00", "phase": "verify", "shape": None, "lead": 3, "turn": None,
                           "event": "tool", "detail": "MATCH (c:CpgCall {scan_id:$scan_id}) RETURN c.code LIMIT 25 " * 3})
        (run_dir / "progress.jsonl").write_text((line + "\n") * log_events, encoding="utf-8")
        log_path = work / "log.jsonl"
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("ANTHROPIC", "CLAUDE"))}
        env.update({"ANTHROPIC_BASE_URL": f"http://127.0.0.1:{server.server_address[1]}",
                    "ANTHROPIC_API_KEY": "dummy-not-a-key", "CLAUDE_CONFIG_DIR": str(work / "config"),
                    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"})
        scan = subprocess.Popen([sys.executable, __file__, "--child", checkout, str(run_dir), str(log_path),
                                 "1" if watch else "0"], env=env, start_new_session=True, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=open(work / "stderr.txt", "wb"))
        if not Api.hanging.wait(120):
            os.killpg(scan.pid, signal.SIGKILL)
            scan.wait()
            print(f"run {run}: no session reached the local server in 120 s: not a valid sample")
            print((work / "stderr.txt").read_text(errors="replace")[-400:])
            server.shutdown()
            return
        time.sleep(1.0)                     # settled inside the request
        t0 = time.time()
        os.killpg(scan.pid, signal.SIGINT)
        try:
            scan.wait(30)
            left = f"left on its own after {time.time() - t0:.2f} s (exit {scan.returncode})"
        except subprocess.TimeoutExpired:
            left = "STILL RUNNING 30 s after the signal: SIGKILL to its group"
            os.killpg(scan.pid, signal.SIGKILL)
            scan.wait()
        Api.release.set()
        server.shutdown()
        events = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
        late = [e for e in events if e["what"] == "session start" and e["t"] > t0]
        print(f"run {run}: {left} | sessions started after the signal: {len(late)}")
        for e in events:
            if e["what"] == "orion code":
                if run == 1:
                    print("  orion code:", e["path"])
                continue
            detail = {k: v for k, v in e.items() if k not in ("t", "what")}
            print(f"  {(e['t'] - t0) * 1000:+9.1f} ms  {e['what']}" + (f"  {detail}" if detail else ""))


if __name__ == "__main__":
    if sys.argv[1:2] == ["--child"]:
        child(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5] == "1")
    else:
        flags = {"--watch", "--after-transient"}
        sized = [a for a in sys.argv[1:] if a.startswith("--log-events=")]
        log_events = int(sized[0].split("=")[1]) if sized else 0
        args = [a for a in sys.argv[1:] if a not in flags and a not in sized]
        runs = int(args[-1]) if args and args[-1].isdigit() else 1
        checkout = args[0] if args and not args[0].isdigit() else str(Path(__file__).resolve().parents[3])
        print("modes:", " ".join(a for a in sys.argv[1:] if a in flags or a in sized) or "(default)",
              "| claude:", subprocess.run(["claude", "--version"], capture_output=True, text=True).stdout.strip())
        for run in range(1, runs + 1):
            one_run(run, checkout, "--watch" in sys.argv[1:], "--after-transient" in sys.argv[1:], log_events)
