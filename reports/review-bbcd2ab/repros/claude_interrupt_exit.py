"""What exit code does the REAL `claude -p` give when it is interrupted mid-request? Token-free: its API base URL is
a local server that accepts the request and never answers, its key is a dummy, and its config folder is a new empty
one, so no login is read and nothing reaches a model.

    python reports/review-bbcd2ab/repros/claude_interrupt_exit.py [RUNS] [CLAUDE_BINARY]

POSIX: SIGINT to the session's own process group, as a Ctrl-C or the UI's Stop does.
Windows: Ctrl-Break to its own process group (Ctrl-C cannot be aimed at one group).
Prints the CLI version and, per run, the exit code and how long it took to exit after the signal.
"""
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

runs = int(sys.argv[1]) if len(sys.argv) > 1 else 3
# A .cmd shim's own exit code hides the real one (cmd.exe asks "Terminate batch job?"), so name the binary there.
claude = sys.argv[2] if len(sys.argv) > 2 else "claude.cmd" if os.name == "nt" else "claude"


def hanging_server() -> tuple[int, threading.Event]:
    """Accepts connections, reads, never answers; the event fires once a request's bytes have arrived."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(16)
    got_request = threading.Event()

    def serve_one(conn: socket.socket) -> None:
        try:
            while conn.recv(65536):
                got_request.set()
        except OSError:
            pass

    def serve() -> None:
        while True:
            conn, _ = srv.accept()
            threading.Thread(target=serve_one, args=(conn,), daemon=True).start()

    threading.Thread(target=serve, daemon=True).start()
    return srv.getsockname()[1], got_request


def end_tree(proc: subprocess.Popen) -> None:
    """The whole tree: on Windows `proc` is the npm shim's cmd.exe, and the real claude.exe is its child."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:          # the group is already gone
            pass


def describe(out: bytes | None) -> list[str]:
    """Each stream-json line as its type (+ subtype, is_error, and the fields a `result` event carries)."""
    shown = []
    for line in (out or b"").decode(errors="replace").splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            shown.append(f"not json: {line[:80]!r}")
            continue
        text = "/".join(str(ev[k]) for k in ("type", "subtype") if ev.get(k) is not None)
        if ev.get("type") == "result":
            text += f" is_error={ev.get('is_error')} result={str(ev.get('result'))[:80]!r} errors={ev.get('errors')!r}"
        shown.append(text)
    return shown


print("claude:", subprocess.run([claude, "--version"], capture_output=True, text=True).stdout.strip())
print("signal:", "CTRL_BREAK_EVENT to its group" if os.name == "nt" else "SIGINT to its group")
for run in range(1, runs + 1):
    port, got_request = hanging_server()
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:     # a straggler may still hold it (Windows)
        # Nothing of the caller's Claude setup: a session inside Claude Code hands its children its own switches.
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("ANTHROPIC", "CLAUDE"))}
        env.update({"ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}", "ANTHROPIC_API_KEY": "dummy-not-a-key",
                    "CLAUDE_CONFIG_DIR": str(Path(tmp) / "config"), "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"})
        kwargs = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
                  else {"start_new_session": True})
        proc = subprocess.Popen([claude, "-p", "hi", "--output-format", "stream-json", "--verbose"], env=env, cwd=tmp,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)
        reached = got_request.wait(60)
        if not reached:
            end_tree(proc)
            out, err = proc.communicate()
            print(f"run {run}: no request reached the local server within 60 s: not a valid sample"
                  f" | stderr {(err or b'').decode(errors='replace').strip()[:200]!r}")
            continue
        time.sleep(1.0)                     # settled inside the request
        t0 = time.monotonic()
        if os.name == "nt":
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(proc.pid, signal.SIGINT)
        try:
            out, err = proc.communicate(timeout=30)
            took = time.monotonic() - t0
            code = proc.returncode
            print(f"run {run}: exit {code} ({code & 0xFFFFFFFF:#x}) after {took:.2f} s"
                  f" | stdout {len(out or b'')} bytes | stderr {(err or b'').decode(errors='replace').strip()[:120]!r}")
            print("  stream events:", describe(out))
            end_tree(proc)                  # a straggler behind the shim, if any
        except subprocess.TimeoutExpired:
            print(f"run {run}: still running 30 s after the signal: killed")
            end_tree(proc)
            proc.communicate()
