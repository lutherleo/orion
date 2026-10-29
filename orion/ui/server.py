"""`orion ui`'s web server: a JSON API, one Server-Sent-Events stream per run, and the static page.

It holds no scanning logic. It wires three modules together: `runs` (read the run dirs the CLI writes), `jobs`
(the one `orion scan` child at a time, argv from a fixed allowlist) and `keys` (the model access the next scan's
child env gets, and the scrubber). Standard library only (`http.server.ThreadingHTTPServer`), no CDN, no build.

A local page that can start scans -- and, with --runtime, run a repo's own code -- must not be drivable by any
other site open in the same browser, and a key must never leave the scan child's environment. So:
- It binds 127.0.0.1 ONLY (there is no host option), and every request's Host header must be exactly
  127.0.0.1:<port>, localhost:<port> or [::1]:<port>: a DNS-rebinding page reaches the socket under its OWN
  name (evil.com:<port>) and is refused before anything runs. On Windows the socket takes SO_EXCLUSIVEADDRUSE
  instead of SO_REUSEADDR, which there lets a second server bind a port already in use (or fails with a
  misleading "access forbidden"); on POSIX SO_REUSEADDR only skips TIME_WAIT, so a quick restart still works.
- Every /api/* call needs the random launch token (in the URL `orion ui` prints) in X-Orion-Token, compared in
  constant time; only the event stream and the file downloads also take it as `?t=`, because EventSource and
  <a download> cannot set a header. Another origin cannot read the token, and cannot send a custom header
  without a CORS preflight -- which gets 405 and no Access-Control-* header, ever. POST/DELETE also need this
  page's own Origin (a second layer under the token) and a JSON body of at most 64 KB. Every response is
  nosniff / no-referrer / no-store, under a CSP that allows only this origin and refuses framing.
- Nothing secret is logged or answered. The request log is off by default; --verbose names only the method,
  the path (never the query: it holds the token) and the status, and no body is ever logged. The base class's
  own error texts quote the request line, so they are replaced by their standard phrase. Every JSON reply,
  event-stream data line, download and log line passes through `Access.scrub` as a last guard: a key that
  reached a file through some other door still leaves as "[redacted]".
- A scan is `jobs.build_argv` (a relative repo resolves against the server's cwd), run with environ +
  `Access.child_env` + its model + PYTHONUNBUFFERED / PYTHONIOENCODING. In endpoint mode the endpoint's model
  IS the scan's model and another one is refused: `child_env` already pins ORION_MODEL and every alias to it,
  and build_argv's default "sonnet" must not overwrite that. The rule is for the claude engine only; an
  Orion-LM scan names a local model dir and talks to no endpoint.
- One model per scan holds across Resume as well, and so does the rest of how the run began. The CLI records a
  claude run's model and verify settings in leads.json (cli._verify_settings: the verifier options, --fail-on, and
  whether the model was reached through an endpoint), and Resume repeats them: the same ORION_MODEL and the same
  flags. Model access must still reach that model the same way -- an endpoint run resumes only through an
  endpoint serving its model, an Anthropic run never through one -- or the resume is refused. A run another
  process is writing (its run-dir lock is held: a scan in a terminal, say) shows as running and is not resumed.
- The event stream polls the run dir every 0.25 s, because the CLI's files are the only truth (as in `runs`):
  progress events from a byte offset, each with `id: <the byte offset just after its own line>`, so ids rise
  with every event and a reconnect with Last-Event-ID (or ?offset=) resumes exactly after the last event
  received; then, while this run is the job's run, its scrubbed output lines (`i` = absolute index, for the
  page to drop repeats) and its state whenever that changes, the verdict counts whenever they change, a ping
  every 15 s, and `end` once the run is not active and a full poll found nothing new.
- A declared body of up to 1 MB is read before any check: replying while a request body is still unread and
  then closing resets the connection (at once on Windows), and the browser would see a network error instead
  of the refusal.
- `shutdown` ends every stream, stops serving and stops a running scan (`Jobs.shutdown`): the scan runs in its
  own process group, so nothing else would ever stop it.
"""
from __future__ import annotations

import hmac
import http
import http.server
import importlib.util
import json
import os
import platform
import re
import secrets
import shutil
import socket
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, TextIO

from . import jobs, keys, runs

DEFAULT_PORT = 8770
BODY_MAX = 64 * 1024                  # a POST/DELETE body; nothing the page sends comes near it
POLL_SECONDS = 0.25                   # event-stream poll, and serve_forever's shutdown check
PING_SECONDS = 15.0                   # an idle stream sends a comment, which also notices a gone client
STATIC_DIR = Path(__file__).resolve().parent / "static"
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "frame-ancestors 'none'; base-uri 'none'; form-action 'none'")

_HEADERS = (("X-Content-Type-Options", "nosniff"), ("Referrer-Policy", "no-referrer"),
            ("Cache-Control", "no-store"), ("Content-Security-Policy", CSP), ("X-Frame-Options", "DENY"))
_STATIC_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
                 ".ico": "image/x-icon", ".json": "application/json; charset=utf-8",
                 ".txt": "text/plain; charset=utf-8"}
_DOWNLOAD_TYPES = {".txt": "text/plain; charset=utf-8", ".json": "application/json; charset=utf-8",
                   ".sarif": "application/sarif+json; charset=utf-8",
                   ".jsonl": "application/x-ndjson; charset=utf-8"}
_JSON = "application/json; charset=utf-8"
_CLIENT_GONE = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError)
_ACCESS_FIELDS = ("mode", "key", "base_url", "token", "model", "auth")
_DRAIN_MAX = 1 << 20                  # a refused body this small is read first, so the client sees the reply
_NUMBER = re.compile(r"[0-9]{1,18}")
_LM_REASON = "Orion-LM needs mlx_lm (Apple silicon) or llama_cpp, and neither is installed here"
_CLAUDE_REASON = "the claude CLI is not on the PATH that scans run with"
_WINDOWS_DRIVE = re.compile(r"/mnt/[A-Za-z]/")      # WSL's view of a Windows drive
_TOKEN_REFUSED = "missing or wrong launch token: open the link `orion ui` printed in its terminal"

# /api routes: (path pattern, {method: handler}); "*" is one path segment, passed to the handler.
_ROUTES: tuple[tuple[tuple[str, ...], dict[str, str]], ...] = (
    (("state",), {"GET": "_get_state"}),
    (("runs",), {"GET": "_get_runs"}),
    (("runs", "*"), {"GET": "_get_run"}),
    (("runs", "*", "events"), {"GET": "_get_events"}),
    (("runs", "*", "stream"), {"GET": "_get_stream"}),
    (("runs", "*", "files", "*"), {"GET": "_get_file"}),
    (("runs", "*", "code"), {"GET": "_get_code"}),
    (("runs", "*", "resume"), {"POST": "_post_resume"}),
    (("compare",), {"GET": "_get_compare"}),
    (("job",), {"GET": "_get_job"}),
    (("job", "output"), {"GET": "_get_job_output"}),
    (("job", "stop"), {"POST": "_post_stop"}),
    (("scans",), {"POST": "_post_scans"}),
    (("access",), {"GET": "_get_access", "POST": "_post_access", "DELETE": "_delete_access"}),
)
# The two routes that also take the token as ?t= (EventSource and <a download> cannot set a header).
_QUERY_TOKEN = {("runs", "*", "stream"), ("runs", "*", "files", "*")}


def _route(segments: list[str]) -> tuple[tuple[str, ...], dict[str, str], list[str]] | None:
    for pattern, methods in _ROUTES:
        if len(pattern) == len(segments) and all(p in ("*", s) for p, s in zip(pattern, segments)):
            return pattern, methods, [s for p, s in zip(pattern, segments) if p == "*"]
    return None


def _one(query: Mapping[str, list[str]], name: str, default: str | None = None) -> str | None:
    values = query.get(name)
    return values[0] if values else default


def _offset(raw: str | None, name: str = "offset") -> int:
    if raw is None or raw == "":
        return 0
    if not _NUMBER.fullmatch(raw.strip()):
        raise ValueError(f"{name} must be a whole number >= 0")
    return int(raw)


def _printable(text: str) -> str:
    """Log-safe: a control character in a request path cannot rewrite the terminal it is printed to."""
    return "".join(ch if ch.isprintable() else "?" for ch in text)


def _env_get(environ: Mapping[str, str], name: str) -> str | None:
    if name in environ:
        return environ[name]
    if os.name == "nt":                    # a Windows env block ignores case
        for key, value in environ.items():
            if key.upper() == name:
                return value
    return None


def _in_wsl() -> bool:
    return sys.platform.startswith("linux") and "microsoft" in platform.uname().release.lower()


def _claude_status(environ: Mapping[str, str], *, wsl: bool | None = None) -> tuple[bool, str]:
    """(usable, why not) for the `claude` a scan will run: orion/claude_cli.py launches plain "claude" with no
    shell. On Windows that finds only claude.exe (CreateProcess adds .exe, never .cmd or .ps1, so npm's shims do
    not count). In WSL a claude that lives on a Windows drive, or is a .exe, is the Windows build: it cannot
    run the scan's Linux MCP server or read Linux paths, and it does not use WSL's own login."""
    path = _env_get(environ, "PATH")
    if path is None:
        return False, _CLAUDE_REASON
    if os.name == "nt":
        if shutil.which("claude.exe", path=path) is not None:
            return True, ""
        shim = shutil.which("claude", path=path)
        if shim is not None:
            return False, (f"only {shim} is on the PATH, and scans run claude.exe: install the native claude, "
                           "or run orion ui in WSL")
        return False, _CLAUDE_REASON
    found = shutil.which("claude", path=path)
    if found is None:
        return False, _CLAUDE_REASON
    if _in_wsl() if wsl is None else wsl:
        real = os.path.realpath(found)
        if _WINDOWS_DRIVE.match(real) or real.lower().endswith(".exe"):
            return False, (f"the claude on the PATH is the Windows one ({found}); scans in WSL need WSL's own "
                           "claude: install it in WSL, or start orion ui from a login shell (bash -l) so "
                           "~/.local/bin comes first")
    return True, ""


def _lm_importable() -> bool:
    for name in ("mlx_lm", "llama_cpp"):
        try:
            if importlib.util.find_spec(name) is not None:
                return True
        except (ImportError, ValueError):
            continue
    return False


def _static_files(folder: Path) -> dict[str, tuple[Path, str]]:
    """{name: (path, content type)}: the regular files directly in `folder` with a known extension, listed ONCE
    at start -- the allowlist. Their bytes are read per request, so an edited page shows on reload."""
    out: dict[str, tuple[Path, str]] = {}
    try:
        with os.scandir(folder) as it:
            entries = sorted(it, key=lambda e: e.name)
    except OSError:
        return out
    for entry in entries:
        ctype = _STATIC_TYPES.get(os.path.splitext(entry.name)[1].lower())
        try:
            regular = entry.is_file(follow_symlinks=False)
        except OSError:
            continue
        if ctype and regular and not entry.name.startswith("."):
            out[entry.name] = (Path(entry.path), ctype)
    return out


def _line_ends(path: Path, start: int, stop: int) -> list[int]:
    """The byte offset just after each line in [start, stop) that `Runs.events` turns into an event (not blank,
    a JSON object): each progress event's own resume point. [start, stop) is the complete lines events() just
    read, so it is at most its window and the append-only file still holds exactly those bytes."""
    with open(path, "rb") as f:
        f.seek(start)
        data = f.read(stop - start)
    ends, pos = [], start
    for raw in data.split(b"\n")[:-1]:     # the range ends with "\n", so the last piece is empty
        pos += len(raw) + 1
        if not raw.strip():
            continue
        try:
            evt = json.loads(raw)
        except (ValueError, RecursionError):
            continue
        if isinstance(evt, dict):
            ends.append(pos)
    return ends


def _sse(name: str, payload: Any, scrub: Callable[[str], str], event_id: int | None = None) -> str:
    """One event-stream event. json.dumps never emits a newline, so the data is always one line."""
    head = f"id: {event_id}\n" if event_id is not None else ""
    return f"{head}event: {name}\ndata: {scrub(json.dumps(payload, default=str))}\n\n"


def _why_not_resumable(rid: str, summary: dict) -> str:
    state, engine = summary["state"], summary["engine"]
    if state == "done":
        return f"run {rid} is finished: there is nothing to resume"
    if summary.get("elsewhere"):
        return (f"run {rid} is being written by a scan outside this UI (in a terminal?) right now; resume it "
                "once that scan has stopped")
    if state in runs.ACTIVE_STATES:
        return f"run {rid} is still {state}"
    if state == "stopped":
        return (f"run {rid} is an --engine {engine} run, which has no verify step to resume "
                "(its verdicts are already in verdicts.jsonl)")
    why = f"run {rid} stopped before discovery finished (it has no leads to verify), so start a new scan"
    return f"{why}; {summary['problem']}" if summary.get("problem") else why


# ── the server ───────────────────────────────────────────────────────────────────────────────────────────
class _HTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False                 # quitting never waits on a client that stopped reading
    request_queue_size = 64
    allow_reuse_address = os.name != "nt"  # see the module docstring
    ui: UIServer

    def server_bind(self) -> None:
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        # TCPServer's bind, not HTTPServer's: that one reverse-resolves the address (getfqdn), which can stall
        # for seconds on a machine with slow DNS.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = str(self.server_address[0]), int(self.server_address[1])

    def handle_error(self, request: Any, client_address: Any) -> None:
        """Never the default traceback dump (it can quote what the request carried)."""
        exc = sys.exc_info()[1]
        ui = getattr(self, "ui", None)
        if ui is not None and ui.verbose and not isinstance(exc, _CLIENT_GONE):
            ui.log(f"error handling a request: {type(exc).__name__}")


class UIServer:
    """The UI's state and its HTTP server (made by `make_server`). `runs`, `jobs` and `access` are the three
    modules' objects; the request handler only translates HTTP to calls on this object and back."""

    def __init__(self, httpd: _HTTPServer, *, cwd: Path | None, keychain: keys.Keychain | None,
                 environ: Mapping[str, str] | None, popen: Callable[..., Any], scan_cmd: list[str] | None,
                 token: str | None, lm_available: bool | None, verbose: bool, log_stream: TextIO | None,
                 static_dir: Path | None) -> None:
        self._httpd = httpd
        self.port: int = int(httpd.server_address[1])
        self.token: str = secrets.token_urlsafe(32) if token is None else token
        self.url = f"http://127.0.0.1:{self.port}/?t={urllib.parse.quote(self.token, safe='')}"
        self.cwd = Path(cwd if cwd is not None else Path.cwd()).resolve()
        self.environ: Mapping[str, str] = os.environ if environ is None else environ
        self.access = keys.Access(keychain if keychain is not None else keys.Keychain(), self.environ)
        self.runs = runs.Runs(self.cwd / ".orion" / "runs", base_dir=self.cwd)
        self.jobs = jobs.Jobs(cwd=self.cwd, scrub=self.access.scrub, popen=popen)
        self.scan_cmd = list(scan_cmd) if scan_cmd is not None else list(jobs.SCAN_CMD)
        self.lm_available = _lm_importable() if lm_available is None else bool(lm_available)
        self.verbose = bool(verbose)
        self.static = _static_files(Path(static_dir) if static_dir is not None else STATIC_DIR)
        hosts = (f"127.0.0.1:{self.port}", f"localhost:{self.port}", f"[::1]:{self.port}")
        if self.port == 80:                # http's default port: browsers send Host and Origin without it
            hosts += ("127.0.0.1", "localhost", "[::1]")
        self.hosts = frozenset(hosts)
        self.origins = frozenset(f"http://{h}" for h in hosts)
        self._log_stream = log_stream
        self._log_lock = threading.Lock()
        self._closing = threading.Event()
        self._serving = threading.Event()
        httpd.ui = self

    # -- lifecycle --
    def serve_forever(self) -> None:
        # Always enters the server's loop, even after shutdown() began: shutdown() waits for that loop to run
        # once and see its stop request, so returning early here would leave it waiting for ever.
        self._serving.set()
        try:
            self._httpd.serve_forever(poll_interval=POLL_SECONDS)
        finally:
            self._serving.clear()

    def start_background(self) -> threading.Thread:
        self._serving.set()                # before the thread runs, so an early shutdown() still stops it
        thread = threading.Thread(target=self.serve_forever, name=f"orion-ui-{self.port}", daemon=True)
        thread.start()
        return thread

    def shutdown(self, timeout: float = 10.0) -> None:
        """End every event stream, stop serving, then stop a running scan (graceful, then killed after
        `timeout`). Safe to call again, e.g. after a second Ctrl-C interrupted the first call."""
        self._closing.set()
        if self._serving.is_set():
            self._httpd.shutdown()
        try:
            self.jobs.shutdown(timeout)
        finally:
            self._httpd.server_close()

    @property
    def closing(self) -> bool:
        return self._closing.is_set()

    def wait_closing(self, seconds: float) -> bool:
        return self._closing.wait(seconds)

    # -- helpers for the handler --
    def scrub(self, text: str) -> str:
        return self.access.scrub(text)

    def log(self, line: str) -> None:
        stream = self._log_stream if self._log_stream is not None else sys.stderr
        if stream is None:
            return
        text = self.scrub(line)
        with self._log_lock:
            try:
                stream.write(text + "\n")
                stream.flush()
            except (OSError, ValueError):
                pass

    def job_public(self, job: jobs.Job) -> dict:
        return job.public(self.runs.rid_for)

    def active_state(self, run_dir: Path) -> str | None:
        """"running"/"stopping" when the UI's job writes `run_dir` (Path equality: case-folded on Windows)."""
        for path, state in self.jobs.active_dirs().items():
            if Path(path) == run_dir:
                return state
        return None

    def state(self) -> dict:
        job = self.jobs.current()
        claude, why = _claude_status(self.environ)
        return {
            "cwd": str(self.cwd),
            "runs_root": str(self.runs.root),
            "platform": sys.platform,
            "engines": {"claude": {"available": claude, "reason": why},
                        "lm": {"available": self.lm_available, "reason": "" if self.lm_available else _LM_REASON}},
            "access": self.access.public(),
            "job": self.job_public(job) if job is not None else None,
            "options": jobs.OPTIONS,
            "models": list(jobs.MODELS),
        }

    def child_env(self, extra: Mapping[str, str]) -> dict[str, str]:
        """environ + the access mode's variables + `extra` + unbuffered UTF-8 output (the job reads it live)."""
        env = self.access.child_env(self.environ)
        for name, value in {**extra, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}.items():
            if os.name == "nt":            # one Windows env block ignores case: drop an inherited spelling first
                for other in [k for k in env if k.upper() == name.upper() and k != name]:
                    del env[other]
            env[name] = value
        return env

    # -- actions --
    def start_scan(self, req: Any) -> jobs.Job:
        """POST /api/scans. OptionError (400) for a refused request, jobs.Busy (409) while a scan runs."""
        if not isinstance(req, dict):
            raise jobs.OptionError("a scan request must be a JSON object")
        req = dict(req)
        claude = req.get("engine") in (None, "claude")
        endpoint = self.access.endpoint_model
        if endpoint is not None and claude:
            if req.get("model") not in (None, "", endpoint):
                raise jobs.OptionError(f"model access is set to an endpoint, which serves {endpoint}: a scan "
                                       "through it uses that model (change it in Settings)")
            req["model"] = endpoint
        argv, extra = jobs.build_argv(req, scan_cmd=self.scan_cmd, lm_available=self.lm_available, cwd=self.cwd)
        model = extra.get("ORION_MODEL") if claude else argv[argv.index("--model") + 1]
        if endpoint is not None and claude:
            extra.pop("ORION_MODEL", None)     # child_env already pins it, and every alias, to the endpoint's
        return self.jobs.start(argv, self.child_env(extra), kind="scan", repo=argv[len(self.scan_cmd)],
                               engine="claude" if claude else "lm", model=model)

    def resume(self, rid: str) -> tuple[jobs.Job, str | None]:
        """POST /api/runs/<id>/resume: `orion scan --resume <run dir>` finishing the run as it began -- the model
        and verify settings its leads.json records, reached the same way (an endpoint or not). Returns the job
        and a note for the page when the run recorded none of that (runs from before 2026-10-29)."""
        run_dir = self.runs.run_dir(rid)
        summary = self.runs.summary(rid, active_state=self.active_state(run_dir))
        if not summary["resumable"]:
            raise ValueError(_why_not_resumable(rid, summary))
        model, settings = summary["model"], summary["settings"]
        via_endpoint = settings.get("endpoint") if settings else None
        endpoint = self.access.endpoint_model
        extra: dict[str, str] = {}
        if endpoint is not None:
            if via_endpoint is False:
                raise ValueError(f"run {rid} was scanned with Anthropic's {model or 'default model'}, but model "
                                 f"access is now an endpoint serving {endpoint}: a scan keeps one model, so switch "
                                 "Settings back to the claude login or an Anthropic key to resume it")
            if model is not None and model != endpoint:
                raise ValueError(f"run {rid} was scanned with {model}, but model access is now an endpoint "
                                 f"serving {endpoint}: a scan keeps one model, so switch Settings back to "
                                 "resume it")
            model = endpoint                   # child_env pins ORION_MODEL and every alias to it
        elif via_endpoint:
            raise ValueError(f"run {rid} was scanned through a compatible endpoint with {model}: a scan keeps one "
                             "model, so set Settings to that endpoint again to resume it")
        elif model is not None:
            extra["ORION_MODEL"] = model
        options = {k: v for k, v in (settings or {}).items() if k in jobs.RESUME_OPTIONS}
        argv = jobs.resume_argv(run_dir, scan_cmd=self.scan_cmd, options=options)
        job = self.jobs.start(argv, self.child_env(extra), kind="resume", repo=summary["repo"],
                              engine=summary["engine"], model=model)
        note = None
        if settings is None:
            note = (f"run {rid} recorded no model or verify settings (it predates 2026-10-29), so it is resumed "
                    f"with {model or 'the default model'} and the default verify settings")
        return job, note

    def set_access(self, body: Any) -> dict:
        """POST /api/access. AccessError (400) never echoes what was typed."""
        if not isinstance(body, dict):
            raise keys.AccessError('send a JSON object such as {"mode": "login"}')
        if any(name not in _ACCESS_FIELDS for name in body):
            raise keys.AccessError(f"unknown field: an access setting has only {', '.join(_ACCESS_FIELDS)}")
        mode = body.get("mode")
        if mode == "login":
            return self.access.set_login()
        if mode == "anthropic_key":
            return self.access.set_anthropic_key(body.get("key"))
        if mode == "endpoint":
            return self.access.set_endpoint(body.get("base_url"), body.get("token"), body.get("model"),
                                            body.get("auth"))
        raise keys.AccessError('mode must be "login", "anthropic_key" or "endpoint"')


def make_server(*, port: int = DEFAULT_PORT, cwd: Path | None = None, keychain: keys.Keychain | None = None,
                environ: Mapping[str, str] | None = None, popen: Callable[..., Any] = subprocess.Popen,
                scan_cmd: list[str] | None = None, token: str | None = None, lm_available: bool | None = None,
                verbose: bool = False, log_stream: TextIO | None = None,
                static_dir: Path | None = None) -> UIServer:
    """The UI server on 127.0.0.1:`port` (0 = any free port), not yet serving. The socket is bound FIRST, so a
    used port raises OSError before the keychain is read or anything else is set up. `keychain` None = the OS
    keychain (keys.Keychain()); `environ` None = os.environ (the base of every scan's env); `lm_available` None
    = detected (mlx_lm or llama_cpp importable); `log_stream` None = stderr (written only when `verbose`);
    `static_dir` None = the packaged page (orion/ui/static/)."""
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValueError(f"port must be a whole number from 0 to 65535 (0 = any free port), not {port!r}")
    if token is not None and (not isinstance(token, str) or not token):
        raise ValueError("token must be a non-empty string")
    httpd = _HTTPServer(("127.0.0.1", port), _Handler)
    try:
        return UIServer(httpd, cwd=cwd, keychain=keychain, environ=environ, popen=popen, scan_cmd=scan_cmd,
                        token=token, lm_available=lm_available, verbose=verbose, log_stream=log_stream,
                        static_dir=static_dir)
    except BaseException:
        httpd.server_close()
        raise


# ── the request handler ──────────────────────────────────────────────────────────────────────────────────
class _Handler(http.server.BaseHTTPRequestHandler):
    """One request. HTTP/1.0, so one request per connection and an event stream ends by closing it."""

    server: _HTTPServer
    server_version = "orion-ui"
    protocol_version = "HTTP/1.0"
    timeout = 60                          # a client that stops reading cannot hold a thread for ever
    _replied = False
    _body: bytes | None = None            # the request body, read up front by _take_body

    def version_string(self) -> str:
        return self.server_version

    # -- logging: never the request line (its query holds the token), never a body --
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 -- the base class's signature
        """Silenced: the base class formats the raw request line, token and all, into these."""

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        ui = self.server.ui
        if ui.verbose:
            status = int(code) if isinstance(code, int) else code
            ui.log(f"{_printable(self.command or '-')} {self._log_path()} {status}")

    def _log_path(self) -> str:
        raw = getattr(self, "path", "") or "-"
        return _printable(raw.split("?", 1)[0].split("#", 1)[0])[:300]

    # -- responses --
    def send_response(self, code: int, message: str | None = None) -> None:
        self._replied = True
        super().send_response(code, message)

    def end_headers(self) -> None:
        for name, value in _HEADERS:
            self.send_header(name, value)
        super().end_headers()

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        """The base class's own errors (bad request line, unknown method, headers too long...) as JSON with the
        standard phrase only: its messages quote the request line, token included."""
        self.close_connection = True
        try:
            phrase = http.HTTPStatus(code).phrase
        except ValueError:
            phrase = "error"
        self._json(code, {"error": phrase})

    def _send(self, code: int, body: bytes, ctype: str, headers: tuple[tuple[str, str], ...] = ()) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, payload: Any, headers: tuple[tuple[str, str], ...] = ()) -> None:
        text = self.server.ui.scrub(json.dumps(payload, default=str))
        self._send(code, text.encode("utf-8"), _JSON, headers)

    def _error(self, code: int, message: str, headers: tuple[tuple[str, str], ...] = ()) -> None:
        if not self._replied:              # a reply already under way (an event stream) cannot change its status
            self._json(code, {"error": message}, headers)

    # -- dispatch --
    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_DELETE(self) -> None:
        self._handle("DELETE")

    def do_OPTIONS(self) -> None:          # a CORS preflight: refused, with no Access-Control-* header
        self._handle("OPTIONS")

    def do_HEAD(self) -> None:
        self._handle("HEAD")

    def do_PUT(self) -> None:
        self._handle("PUT")

    def do_PATCH(self) -> None:
        self._handle("PATCH")

    def _handle(self, method: str) -> None:
        try:
            self._dispatch(method)
        except _CLIENT_GONE:
            self.close_connection = True   # the browser went away mid-reply: nothing left to answer
        except Exception as exc:  # noqa: BLE001 -- a bug answers 500 (scrubbed), never a traceback dump
            self.close_connection = True
            ui = self.server.ui
            if ui.verbose:
                ui.log(f"internal error on {self._log_path()}: {type(exc).__name__}")
            try:
                self._error(500, f"internal error: {type(exc).__name__}: {exc}")
            except _CLIENT_GONE:
                pass

    def _one_of(self, name: str, allowed: frozenset[str]) -> bool:
        values = self.headers.get_all(name) or []
        return len(values) == 1 and values[0] in allowed

    def _dispatch(self, method: str) -> None:
        ui = self.server.ui
        self._take_body()
        if not self._one_of("Host", ui.hosts):
            return self._error(403, "refused: the Host header must be 127.0.0.1:<port> or localhost:<port> "
                                    "(this guards against DNS rebinding)")
        if not self.path.startswith("/"):
            return self._error(400, "the request target must be a path")
        target = urllib.parse.urlsplit(self.path)
        segments = [urllib.parse.unquote(s) for s in target.path.split("/")[1:]]
        query = urllib.parse.parse_qs(target.query, keep_blank_values=True)
        if method not in ("GET", "POST", "DELETE"):
            return self._error(405, f"{method} is not supported", (("Allow", "GET, POST, DELETE"),))
        if segments[0] == "api":
            return self._api(method, segments[1:], query)
        if method != "GET":
            return self._error(405, f"{method} is not supported here", (("Allow", "GET"),))
        return self._static(segments)

    def _static(self, segments: list[str]) -> None:
        if segments == [""]:
            name = "index.html"
        elif len(segments) == 2 and segments[0] == "static":
            name = segments[1]
        else:
            return self._error(404, "not found")
        found = self.server.ui.static.get(name)
        if found is None:
            return self._error(404, "the page is missing: orion/ui/static/index.html was not found"
                               if name == "index.html" and segments == [""] else "not found")
        path, ctype = found
        try:
            body = path.read_bytes()
        except OSError:
            return self._error(404, "not found")
        self._send(200, body, ctype)

    def _token_ok(self, query: Mapping[str, list[str]] | None) -> bool:
        given = self.headers.get_all("X-Orion-Token") or []
        if len(given) > 1:
            return False
        if given:
            candidate = given[0]
        elif query is not None and len(query.get("t", [])) == 1:
            candidate = query["t"][0]
        else:
            return False
        return hmac.compare_digest(candidate.encode("utf-8", "replace"), self.server.ui.token.encode("utf-8"))

    def _api(self, method: str, segments: list[str], query: dict[str, list[str]]) -> None:
        ui = self.server.ui
        found = _route(segments)
        by_query = method == "GET" and found is not None and found[0] in _QUERY_TOKEN
        if not self._token_ok(query if by_query else None):
            return self._error(401, _TOKEN_REFUSED)
        if method in ("POST", "DELETE") and not self._one_of("Origin", ui.origins):
            return self._error(403, "refused: a request that changes something must come from this page "
                                    "(its Origin header)")
        if found is None:
            return self._error(404, "no such API endpoint")
        _, methods, args = found
        name = methods.get(method)
        if name is None:
            return self._error(405, f"{method} is not supported here", (("Allow", ", ".join(methods)),))
        body = None
        if method in ("POST", "DELETE"):
            ok, body = self._read_body()
            if not ok:
                return
        try:
            getattr(self, name)(args, query, body)
        except jobs.Busy as exc:
            self._error(409, str(exc))
        except runs.NotFound as exc:
            self._error(404, str(exc))
        except PermissionError as exc:
            self._error(403, str(exc))
        except ValueError as exc:          # OptionError and AccessError too: their messages are for the page
            self._error(400, str(exc))

    def _take_body(self) -> None:
        """Read a declared body of up to _DRAIN_MAX bytes FIRST, before any check. A reply sent while the
        request body is still unread, followed by the close, resets the connection (Windows does so at once),
        and the browser then sees a network error instead of the 403 / 401 / 405 it was refused with."""
        self._body = None
        lengths = self.headers.get_all("Content-Length") or []
        if self.headers.get("Transfer-Encoding") or len(lengths) != 1 or not _NUMBER.fullmatch(lengths[0].strip()):
            return
        size = int(lengths[0])
        if 0 < size <= _DRAIN_MAX:
            self._body = self.rfile.read(size)

    def _read_body(self) -> tuple[bool, Any]:
        """(True, the parsed JSON or None when there is no body), or (False, None) after refusing it."""
        if self.headers.get("Transfer-Encoding"):
            self._error(411, "send the body with a Content-Length")
            return False, None
        lengths = self.headers.get_all("Content-Length") or []
        if not lengths:
            return True, None
        if len(lengths) > 1 or not _NUMBER.fullmatch(lengths[0].strip()):
            self._error(400, "bad Content-Length")
            return False, None
        size = int(lengths[0])
        if size == 0:
            return True, None
        if size > BODY_MAX:
            self._error(413, f"the request body is larger than {BODY_MAX // 1024} KB")
            return False, None
        ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if ctype != "application/json":
            self._error(415, "the request body must be application/json")
            return False, None
        raw = self._body or b""
        if len(raw) != size:
            self._error(400, "the request body was cut short")
            return False, None
        try:
            return True, json.loads(raw.decode("utf-8"))
        except (ValueError, RecursionError):   # UnicodeDecodeError and JSONDecodeError are ValueErrors
            self._error(400, "the request body is not valid JSON")
            return False, None

    # -- API handlers: (path args, query, body) -> the reply; errors are mapped in _api --
    def _get_state(self, args: list[str], query: dict, body: Any) -> None:
        self._json(200, self.server.ui.state())

    def _get_runs(self, args: list[str], query: dict, body: Any) -> None:
        ui = self.server.ui
        self._json(200, {"runs": ui.runs.list(active=ui.jobs.active_dirs())})

    def _get_run(self, args: list[str], query: dict, body: Any) -> None:
        ui = self.server.ui
        rid = args[0]
        self._json(200, ui.runs.detail(rid, active_state=ui.active_state(ui.runs.run_dir(rid))))

    def _get_events(self, args: list[str], query: dict, body: Any) -> None:
        events, offset = self.server.ui.runs.events(args[0], _offset(_one(query, "offset")))
        self._json(200, {"events": events, "offset": offset})

    def _get_file(self, args: list[str], query: dict, body: Any) -> None:
        ui = self.server.ui
        rid, name = args
        path = ui.runs.file_path(rid, name)            # only runs.DOWNLOADS, only the run dir's own files
        ctype = _DOWNLOAD_TYPES.get(os.path.splitext(name)[1], "application/octet-stream")
        headers = (("Content-Disposition", f'attachment; filename="{name}"'),)
        with open(path, "rb") as f:
            size = os.fstat(f.fileno()).st_size        # a log still being written is sent as it was here
            if ui.access.secrets():
                # Scrubbed as text; surrogateescape round-trips any byte that is not UTF-8.
                text = ui.scrub(f.read(size).decode("utf-8", "surrogateescape"))
                return self._send(200, text.encode("utf-8", "surrogateescape"), ctype, headers)
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(size))
            for header, value in headers:
                self.send_header(header, value)
            self.end_headers()
            remaining = size
            while remaining > 0:
                chunk = f.read(min(65536, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def _get_code(self, args: list[str], query: dict, body: Any) -> None:
        file, line = _one(query, "file"), _one(query, "line")
        if file is None or line is None:
            raise ValueError("the code view needs ?file=<path>&line=<n> (and optionally &end=<n>)")
        end = _one(query, "end") or None
        self._json(200, self.server.ui.runs.code(args[0], file, line, end, _one(query, "context", "8")))

    def _get_compare(self, args: list[str], query: dict, body: Any) -> None:
        a, b = _one(query, "a"), _one(query, "b")
        if not a or not b:
            raise ValueError("compare needs two run ids: ?a=<id>&b=<id>")
        self._json(200, self.server.ui.runs.compare(a, b))

    def _get_job(self, args: list[str], query: dict, body: Any) -> None:
        ui = self.server.ui
        job = ui.jobs.current()
        if job is None:
            return self._json(200, {"job": None, "output": [], "next": 0})
        lines, nxt = job.output(0)
        self._json(200, {"job": ui.job_public(job), "output": lines, "next": nxt})

    def _get_job_output(self, args: list[str], query: dict, body: Any) -> None:
        job = self.server.ui.jobs.current()
        if job is None:
            return self._json(200, {"lines": [], "next": 0})
        lines, nxt = job.output(_one(query, "since", "0"))
        self._json(200, {"lines": lines, "next": nxt})

    def _post_scans(self, args: list[str], query: dict, body: Any) -> None:
        ui = self.server.ui
        try:
            job = ui.start_scan(body)
        except OSError as exc:             # the spawn failed (PermissionError included: not a 403 here)
            return self._error(500, f"could not start the scan: {exc}")
        self._json(202, {"job": ui.job_public(job)})

    def _post_stop(self, args: list[str], query: dict, body: Any) -> None:
        ui = self.server.ui
        try:
            out = ui.jobs.stop(rid_for=ui.runs.rid_for)
        except RuntimeError as exc:        # "no scan is running"
            return self._error(409, str(exc))
        except OSError as exc:
            return self._error(500, f"could not signal the scan: {exc}")
        self._json(200, out)

    def _post_resume(self, args: list[str], query: dict, body: Any) -> None:
        if body is not None and body != {}:
            raise ValueError("resume takes no options: it finishes the run as it was started")
        ui = self.server.ui
        try:
            job, note = ui.resume(args[0])
        except OSError as exc:
            return self._error(500, f"could not start the scan: {exc}")
        self._json(202, {"job": ui.job_public(job), "note": note})

    def _get_access(self, args: list[str], query: dict, body: Any) -> None:
        self._json(200, self.server.ui.access.public())

    def _post_access(self, args: list[str], query: dict, body: Any) -> None:
        self._json(200, self.server.ui.set_access(body))

    def _delete_access(self, args: list[str], query: dict, body: Any) -> None:
        self._json(200, self.server.ui.access.forget())

    # -- the event stream --
    def _get_stream(self, args: list[str], query: dict, body: Any) -> None:
        ui = self.server.ui
        rid = args[0]
        run_dir = ui.runs.run_dir(rid)                 # 404 / 400 before the stream starts
        last = (self.headers.get("Last-Event-ID") or "").strip()
        offset = int(last) if _NUMBER.fullmatch(last) else _offset(_one(query, "offset"))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True
        self._stream(rid, run_dir, offset)

    def _event_ends(self, rid: str, start: int, stop: int) -> list[int]:
        try:
            return _line_ends(self.server.ui.runs.file_path(rid, "progress.jsonl"), start, stop)
        except (KeyError, OSError):
            return []

    def _stream(self, rid: str, run_dir: Path, offset: int) -> None:
        ui = self.server.ui
        scrub = ui.scrub
        self.wfile.write(b"retry: 2000\n\n")
        out_next, last_job, last_counts = 0, None, None
        quiet_since = time.monotonic()
        while not ui.closing:
            active = ui.active_state(run_dir)          # read BEFORE the files: an `end` follows one full poll
            job = ui.jobs.current()
            mine = job is not None and job.run_dir is not None and job.run_dir == run_dir
            try:
                events, offset_next = ui.runs.events(rid, offset)
            except (KeyError, ValueError, OSError):
                self.wfile.write(_sse("end", {"state": "incomplete", "problem": "the run dir is gone"},
                                      scrub).encode("utf-8"))
                return
            ends = self._event_ends(rid, offset, offset_next) if events else []
            if len(ends) != len(events):       # cannot happen on an append-only log; never skip on a reconnect
                ends = [offset] * (len(events) - 1) + [offset_next]
            chunks = [_sse("progress", evt, scrub, end) for evt, end in zip(events, ends)]
            offset = offset_next
            if mine:
                lines, out_end = job.output(out_next)
                first = out_end - len(lines)
                chunks += [_sse("output", {"i": first + k, "line": line}, scrub) for k, line in enumerate(lines)]
                out_next = out_end
                public = ui.job_public(job)
                if public != last_job:
                    chunks.append(_sse("job", public, scrub))
                    last_job = public
            try:
                summary = ui.runs.summary(rid, active_state=active)
            except (KeyError, ValueError, OSError):
                summary = None
            if summary is not None:
                counts = (summary["counts"], summary["n_verdicts"])
                if counts != last_counts:
                    chunks.append(_sse("verdicts", {"counts": summary["counts"], "n": summary["n_verdicts"]},
                                       scrub))
                    last_counts = counts
            now = time.monotonic()
            if chunks:
                self.wfile.write("".join(chunks).encode("utf-8"))
                quiet_since = now
            elif active is None:
                state = summary["state"] if summary is not None else "incomplete"
                self.wfile.write(_sse("end", {"state": state}, scrub).encode("utf-8"))
                return
            elif now - quiet_since >= PING_SECONDS:
                self.wfile.write(b": ping\n\n")
                quiet_since = now
            if not events:
                ui.wait_closing(POLL_SECONDS)
