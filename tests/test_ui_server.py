"""`orion ui`'s server (orion/ui/server.py) and the `orion ui` command (orion/cli.py).

Token-free, and no network beyond 127.0.0.1. Each test runs a REAL server (make_server(port=0, ...), a memory-only
keychain, an explicit environ) in a background thread and talks to it with http.client, setting Host and Origin
by hand. A scan is a FAKE `orion scan`: a script in tmp_path that prints the CLI's `scan_id:` / `run log:` lines
and fills its run dir through the CLI's OWN writers (cli._save_leads / _append_verdict / _load_run,
monitor.run_logger, report, sarif), so the server reads exactly the files a real scan leaves. It runs as a real
child process, but nothing is ever signalled: the popen wrapper hands the server a proxy whose send_signal / kill
only append a word to a flag file the fake polls, and the jobs module's `os` is swapped for one whose killpg /
getpgid do the same -- so Stop runs its Windows path here and its POSIX path on Linux, without a real signal.
"""
from __future__ import annotations

import dataclasses
import hashlib
import http.client
import inspect
import io
import json
import os
import signal
import socket
import subprocess
import sys
import time
import tomllib
import types
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

import pytest

from orion import cli, monitor, report, runlock, sarif
from orion.contracts import Lead, Verdict
from orion.ui import jobs, keys, runs, server

REPO_ROOT = Path(__file__).resolve().parents[1]
KEY = "sk-ant-api03-" + "Qw7Er9Ty" * 6 + "-LeakCanaryKey1234"          # what the user types into Settings
TOKEN = "gw-" + "Zp4Xc8Vb" * 5 + "-LeakCanaryToken5678"                 # an endpoint token
ENV_KEY = "sk-ant-env-" + "Mn3Bv6Cx" * 5 + "-LeakCanaryEnv90"          # a key in the server's own environment
TS = "20261029T120000Z"

FAKE_SCAN = r'''
"""Stand-in for `orion scan` (tests/test_ui_server.py): the CLI's own `scan_id:` / `run log:` lines and run-dir
writers, a fake verifier, and a stop that arrives as a word in $FAKE_SIGNAL_FILE -- never as a real signal.
<repo>/fake.json says what to do: leads, delay, pause_after, ignore_interrupt, exit."""
import dataclasses
import hashlib
import json
import os
import sys
import time
from pathlib import Path

from orion import cli, config, report, runlock, sarif
from orion.contracts import Lead, Verdict
from orion.monitor import run_logger
from orion.ui import keys

HERE = Path(__file__).resolve().parent
DECISIONS = ("CONFIRM", "REJECT", "INCONCLUSIVE", "ERROR")
WATCHED = ("ANTHROPIC_BASE_URL", "ORION_MODEL", *keys.ALIASES, "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS",
           "PYTHONUNBUFFERED", "PYTHONIOENCODING", "ORION_VERIFY_ROUTING", "ORION_VERIFY_CONTEXT",
           "ORION_SHORTLISTS", "ORION_SEMANTIC_MODE")


def sha(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest() if value else None


def new_run_dir(scan_id):
    t = time.time()
    while True:
        path = Path(".orion", "runs", scan_id, time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(t)))
        try:
            path.mkdir(parents=True)
            return str(path)
        except FileExistsError:
            t += 1


def wait_for_stop(cfg, on_event):
    on_event(cli._event("verify", "warn", detail="paused until the UI stops this scan"))
    flag = Path(os.environ["FAKE_SIGNAL_FILE"])
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            got = flag.read_text(encoding="utf-8").split()
        except OSError:
            got = []
        if "kill" in got:
            os._exit(1)                     # what TerminateProcess leaves; nothing more is written
        if "interrupt" in got and not cfg.get("ignore_interrupt"):
            print("KeyboardInterrupt", flush=True)
            sys.exit(130)
        time.sleep(0.02)
    print("fake scan: nobody stopped it", flush=True)
    sys.exit(99)


def main():
    args = sys.argv[1:]
    with open(HERE / "child_env.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"argv": args, "cwd": os.getcwd(),
                            "key_sha256": sha(os.environ.get("ANTHROPIC_API_KEY")),
                            "token_sha256": sha(os.environ.get("ANTHROPIC_AUTH_TOKEN")),
                            "has_key": "ANTHROPIC_API_KEY" in os.environ,
                            "has_token": "ANTHROPIC_AUTH_TOKEN" in os.environ,
                            "env": {name: os.environ.get(name) for name in WATCHED}}) + "\n")
    ns = cli._build_parser().parse_args(["scan", *args])      # the real CLI's flags, as _run_scan reads them
    if ns.semantic_mode:
        config.set_semantic_mode(ns.semantic_mode)
    resume = args[0] == "--resume"
    if resume:
        run_dir = args[1]
        scan_id, repo, leads, finished = cli._load_run(run_dir)
    else:
        repo, scan_id, leads, finished = args[0], "s", None, {}
        run_dir = new_run_dir(scan_id)
    run_lock = runlock.acquire(run_dir)                      # as _run_scan: one scan per run dir
    if run_lock is None:
        print(f"orion scan: {run_dir} is being written by another scan right now", flush=True)
        sys.exit(2)
    cfg_file = Path(repo, "fake.json")
    cfg = json.loads(cfg_file.read_text(encoding="utf-8")) if cfg_file.exists() else {}
    on_event = run_logger(run_dir, quiet=True)
    print(f"scan_id: {scan_id}", flush=True)
    print(f"run log: {run_dir}/progress.jsonl", flush=True)
    print("env ANTHROPIC_API_KEY=" + os.environ.get("ANTHROPIC_API_KEY", ""), flush=True)
    print("env ANTHROPIC_AUTH_TOKEN=" + os.environ.get("ANTHROPIC_AUTH_TOKEN", ""), flush=True)
    print("naïve ✓ output", flush=True)
    delay = float(cfg.get("delay", 0))
    if resume:
        on_event(cli._event("build", "done", detail=f"resuming {run_dir}: {len(leads)} leads"))
    else:
        on_event(cli._event("build", "start", detail=f"building graph for {repo}"))
        on_event(cli._event("build", "done", detail="graph build complete"))
        on_event(cli._event("discover", "start", detail="discovery fleet starting"))
        on_event({"event": "usage", "detail": json.dumps({"model": "fake", "input_tokens": 100, "output_tokens": 20,
                                                          "cache_creation_input_tokens": 0,
                                                          "cache_read_input_tokens": 5, "total_cost_usd": 0.01})})
        leads = [Lead(index=i, shape="A", text=f"lead {i}: SQL injection in find{i}()", evidence=f"evidence {i}",
                      confidence="HIGH", file="app/db.js", line_start=3 + i, line_end=4 + i, function=f"find{i}",
                      cwe="CWE-89") for i in range(int(cfg.get("leads", 2)))]
        on_event(cli._event("discover", "done", detail=f"{len(leads)} candidate leads"))
        cli._save_leads(run_dir, scan_id, repo, leads, model=config.MODEL, settings=cli._verify_settings(ns, config))
    todo = [lead for lead in leads if lead.index not in finished]
    on_event(cli._event("verify", "start", detail=f"verifying {len(todo)} leads"))
    by_index = dict(finished)
    pause = None if resume else cfg.get("pause_after")
    for n, lead in enumerate(todo):
        if pause is not None and n == pause:
            wait_for_stop(cfg, on_event)
        time.sleep(delay)
        v = Verdict(lead, DECISIONS[lead.index % len(DECISIONS)], f"reason {lead.index}",
                    evidence=f"read app/db.js:{lead.line_start}", severity="HIGH")
        cli._append_verdict(run_dir, v)
        on_event(cli._event("verify", "verdict", lead=lead.index, detail=v.decision))
        by_index[lead.index] = v
    verdicts = [by_index[lead.index] for lead in leads if lead.index in by_index]
    on_event(cli._event("verify", "done", detail=f"{len(verdicts)} verdicts"))
    on_event(cli._event("report", "start", detail=f"rendering {len(verdicts)} verdicts"))
    text = report.render(verdicts)
    on_event(cli._event("report", "done", detail="report rendered"))
    print("\n" + "=" * 70, flush=True)
    print(text, flush=True)
    payload = json.dumps([dataclasses.asdict(v) for v in verdicts], indent=2, default=str)
    Path(run_dir, "verdicts.json").write_text(payload, encoding="utf-8")
    Path(run_dir, "report.txt").write_text(text, encoding="utf-8")
    Path(run_dir, "results.sarif").write_text(json.dumps(sarif.to_sarif(verdicts), indent=2), encoding="utf-8")
    print(f"\nreport, verdicts and SARIF saved under {run_dir}", flush=True)
    sys.exit(int(cfg.get("exit", 0)))


main()
'''


# ── the scan child: real process, fake signals ─────────────────────────────────────────────────────────────
class Proc:
    """What the server's Jobs holds as its child: a real process whose send_signal / kill / terminate only append
    a word to its flag file, which the fake scan polls. Nothing is ever really signalled."""

    def __init__(self, real: subprocess.Popen, flag: Path) -> None:
        self.real, self.flag, self.sent = real, flag, []

    @property
    def pid(self):
        return self.real.pid

    @property
    def stdout(self):
        return self.real.stdout

    @property
    def returncode(self):
        return self.real.returncode

    def poll(self):
        return self.real.poll()

    def wait(self, timeout=None):
        return self.real.wait(timeout)

    def note(self, what: str) -> None:
        self.sent.append(what)
        with open(self.flag, "a", encoding="utf-8") as f:
            f.write(what + "\n")

    def send_signal(self, sig) -> None:    # Windows' first stop: CTRL_BREAK
        self.note("interrupt")

    def kill(self) -> None:
        self.note("kill")

    def terminate(self) -> None:
        self.note("kill")


class Spawner:
    """The server's popen: spawns the real child (with its own flag file) and keeps what it was given."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.procs: list[Proc] = []
        self.calls: list[dict] = []        # argv + env handed over (in memory only: the env holds the key)

    def __call__(self, argv, **kwargs):
        flag = self.tmp / f"signal-{len(self.procs)}.txt"
        env = dict(kwargs.pop("env"))
        self.calls.append({"argv": list(argv), "env": env, "kwargs": dict(kwargs)})
        proc = Proc(subprocess.Popen(argv, env={**env, "FAKE_SIGNAL_FILE": str(flag)}, **kwargs), flag)
        self.procs.append(proc)
        return proc

    def by_pid(self, pid: int) -> Proc:
        return next(p for p in self.procs if p.pid == pid)

    def cleanup(self) -> None:
        for proc in self.procs:
            if proc.real.poll() is None:
                proc.real.kill()           # a safety net only: every test ends its scans through the server
            try:
                proc.real.wait(10)
            except subprocess.TimeoutExpired:
                pass


class NoSignalOS:
    """The jobs module's `os` in these tests: the POSIX process-group calls reach the spawner's proxies instead
    of the kernel. Everything else is the real os module (the global os.name is never patched)."""

    def __init__(self, spawner: Spawner) -> None:
        self._spawner = spawner

    def getpgid(self, pid):
        return pid

    def getpgrp(self):
        return -1

    def killpg(self, pgid, sig):
        self._spawner.by_pid(pgid).note("kill" if sig == jobs._SIGKILL else "interrupt")

    def __getattr__(self, name):
        return getattr(os, name)


@pytest.fixture
def spawner(tmp_path):
    made = Spawner(tmp_path)
    yield made
    made.cleanup()


@pytest.fixture(autouse=True)
def no_signals(monkeypatch, spawner):
    monkeypatch.setattr(jobs, "os", NoSignalOS(spawner))


# ── the HTTP client ──────────────────────────────────────────────────────────────────────────────────────────
@dataclasses.dataclass
class Reply:
    status: int
    headers: list[tuple[str, str]]
    body: bytes

    def json(self):
        return json.loads(self.body)

    def header(self, name: str) -> str | None:
        found = [v for k, v in self.headers if k.lower() == name.lower()]
        return found[0] if found else None


def _parse_sse(text: str) -> list[dict]:
    """[{"event", "data" (parsed JSON), "id" (that event's own id field, or None)}]; comments are kept as
    {"event": "comment"}; a block with neither event nor data (the retry hint) is not an event."""
    out, fields = [], {}
    for line in text.split("\n"):
        if line == "":
            if "event" in fields or "data" in fields:
                data = fields.get("data")
                out.append({"event": fields.get("event", "message"), "id": fields.get("id"),
                            "data": json.loads(data) if data is not None else None})
            fields = {}
        elif line.startswith(":"):
            out.append({"event": "comment", "id": None, "data": line[1:].strip()})
        else:
            name, _, value = line.partition(":")
            fields[name] = value[1:] if value.startswith(" ") else value
    return out


class Client:
    def __init__(self, ui: server.UIServer, log: io.StringIO) -> None:
        self.ui, self.log = ui, log
        self.port, self.token = ui.port, ui.token
        self.host = f"127.0.0.1:{self.port}"
        self.origin = f"http://127.0.0.1:{self.port}"
        self.seen: list[bytes] = []        # every status, header and body this client received
        self.replies: list[Reply] = []

    def _record(self, reply: Reply) -> None:
        head = f"{reply.status}\n" + "".join(f"{k}: {v}\n" for k, v in reply.headers)
        self.seen.append(head.encode("utf-8") + reply.body)
        self.replies.append(reply)

    def request(self, method: str, path: str, body=None, *, token=True, origin=True, host=None, headers=(),
                raw: bytes | None = None, ctype: str | None = "application/json") -> Reply:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            pairs = []
            if host is not False:
                pairs.append(("Host", self.host if host is None else host))
            if token:
                pairs.append(("X-Orion-Token", self.token if token is True else token))
            if origin and method in ("POST", "DELETE"):
                pairs.append(("Origin", self.origin if origin is True else origin))
            data = raw if raw is not None else (None if body is None else json.dumps(body).encode("utf-8"))
            if data is not None:
                if ctype:
                    pairs.append(("Content-Type", ctype))
                pairs.append(("Content-Length", str(len(data))))
            elif method in ("POST", "DELETE"):
                pairs.append(("Content-Length", "0"))
            for name, value in [*pairs, *headers]:
                conn.putheader(name, value)
            conn.endheaders(data)
            resp = conn.getresponse()
            reply = Reply(resp.status, resp.getheaders(), resp.read())
        finally:
            conn.close()
        self._record(reply)
        return reply

    def get(self, path: str, **kw) -> Reply:
        return self.request("GET", path, **kw)

    def post(self, path: str, body=None, **kw) -> Reply:
        return self.request("POST", path, body, **kw)

    def delete(self, path: str, **kw) -> Reply:
        return self.request("DELETE", path, **kw)

    def stream_raw(self, rid: str, query: str = "", headers: dict | None = None) -> Reply:
        """One event stream read to its end (the server closes it after `end`)."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            conn.request("GET", f"/api/runs/{rid}/stream?t={self.token}{query}",
                         headers={"Host": self.host, **(headers or {})})
            resp = conn.getresponse()
            reply = Reply(resp.status, resp.getheaders(), resp.read())
        finally:
            conn.close()
        self._record(reply)
        return reply

    def stream(self, rid: str, query: str = "", headers: dict | None = None) -> list[dict]:
        reply = self.stream_raw(rid, query, headers)
        assert reply.status == 200, reply.body
        return _parse_sse(reply.body.decode("utf-8"))


def _base_env(**extra: str) -> dict[str, str]:
    """This process's env without anything that decides a scan's model access, plus what the fake scan needs:
    orion importable from this checkout and no bytecode written into it."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("ANTHROPIC_", "CLAUDE_CODE_", "ORION_"))}
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.update(extra)
    return env


def _fake_scan(tmp_path: Path) -> Path:
    path = tmp_path / "fake_scan.py"
    if not path.exists():
        path.write_text(FAKE_SCAN, encoding="utf-8")
    return path


@pytest.fixture
def make(tmp_path, spawner):
    """make(**make_server overrides) -> a Client for a running server on a free port; all shut down after."""
    made: list[Client] = []

    def _make(*, environ=None, keychain=None, verbose=True, **kw) -> Client:
        cwd = tmp_path / "cwd"
        (cwd / "repo").mkdir(parents=True, exist_ok=True)
        log = io.StringIO()
        params = dict(port=0, cwd=cwd, keychain=keychain if keychain is not None else keys.Keychain(None),
                      environ=_base_env() if environ is None else environ, popen=spawner,
                      scan_cmd=[sys.executable, str(_fake_scan(tmp_path))], lm_available=False,
                      verbose=verbose, log_stream=log)
        params.update(kw)
        ui = server.make_server(**params)
        ui.start_background()
        client = Client(ui, log)
        made.append(client)
        return client

    yield _make
    for client in made:
        client.ui.shutdown(timeout=5.0)


# ── helpers ───────────────────────────────────────────────────────────────────────────────────────────────
def wait_until(fn, timeout: float = 30.0, what: str = "a condition"):
    deadline = time.monotonic() + timeout
    while True:
        value = fn()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.05)


def _repo(c: Client, cfg: dict | None = None) -> Path:
    repo = c.ui.cwd / "repo"
    (repo / "app").mkdir(parents=True, exist_ok=True)
    (repo / "app" / "db.js").write_text("".join(f"line {i}\n" for i in range(1, 31)), encoding="utf-8")
    (repo / "fake.json").write_text(json.dumps(cfg or {}), encoding="utf-8")
    return repo


def _scan(c: Client, cfg: dict | None = None, **req) -> dict:
    _repo(c, cfg)
    reply = c.post("/api/scans", {"repo": "repo", "engine": "claude", **req})
    assert reply.status == 202, reply.body
    return reply.json()["job"]


def _job(c: Client) -> dict | None:
    return c.get("/api/job").json()["job"]


def _wait_run_id(c: Client) -> str:
    return wait_until(lambda: (_job(c) or {}).get("run_id"), what="the job's run id")


def _wait_exit(c: Client) -> dict:
    return wait_until(lambda: (j := _job(c)) is not None and j["state"] == "exited" and j, what="the scan to exit")


def _records(tmp_path: Path) -> list[dict]:
    path = tmp_path / "child_env.jsonl"
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write_run(cwd: Path, scan_id: str = "t", ts: str = TS, *, repo: Path | None = None, decisions=("CONFIRM",
               "REJECT"), logged: int | None = None, final: bool = True, engine: str = "claude", leads: bool = True,
               secret: str = "") -> Path:
    """A run dir as a real scan leaves it, written with the CLI's own writers: no leads.json (`leads` False),
    stopped (`final` False, `logged` verdicts in verdicts.jsonl) or done."""
    run_dir = cwd / ".orion" / "runs" / scan_id / ts
    run_dir.mkdir(parents=True)
    extra = f" {secret}" if secret else ""
    on_event = monitor.run_logger(str(run_dir), quiet=True)
    on_event(cli._event("build", "start", detail=f"building graph{extra}"))
    on_event({"event": "usage", "detail": json.dumps({"input_tokens": 7, "note": secret})})
    if not leads:
        return run_dir
    made = [Lead(index=i, shape="A", text=f"lead {i}", evidence=f"evidence {i}{extra}", confidence="HIGH",
                 file="app/db.js", line_start=2 + i, cwe="CWE-89", function=f"f{i}") for i in range(len(decisions))]
    cli._save_leads(str(run_dir), scan_id, str(repo) if repo is not None else None, made, engine=engine,
                    model="/models/x" if engine == "lm" else None)
    verdicts = [Verdict(lead, d, f"reason {lead.index}{extra}", evidence=f"read{extra}")
                for lead, d in zip(made, decisions)]
    for v in verdicts[: len(verdicts) if logged is None else logged]:
        cli._append_verdict(str(run_dir), v)
    if final:
        (run_dir / "verdicts.json").write_text(json.dumps([dataclasses.asdict(v) for v in verdicts], indent=2,
                                                          default=str), encoding="utf-8")
        (run_dir / "report.txt").write_text(report.render(verdicts) + extra, encoding="utf-8")
        (run_dir / "results.sarif").write_text(json.dumps(sarif.to_sarif(verdicts)), encoding="utf-8")
    return run_dir


def _age(run_dir: Path, seconds: float) -> None:
    then = time.time() - seconds
    for path in run_dir.iterdir():
        os.utime(path, (then, then))


@pytest.fixture
def static_dir(tmp_path):
    folder = tmp_path / "static"
    folder.mkdir()
    (folder / "index.html").write_text('<!doctype html><title>Orion</title><script src="/static/app.js"></script>',
                                       encoding="utf-8")
    (folder / "app.js").write_text("console.log('orion');\n", encoding="utf-8")
    (folder / "app.css").write_text("body { margin: 0; }\n", encoding="utf-8")
    (folder / ".hidden.js").write_text("secret()\n", encoding="utf-8")
    (folder / "notes.md").write_text("not served\n", encoding="utf-8")
    (folder / "sub").mkdir()
    (folder / "sub" / "x.js").write_text("nested()\n", encoding="utf-8")
    return folder


# ── binding ───────────────────────────────────────────────────────────────────────────────────────────────
def test_it_binds_127_0_0_1_on_a_free_port_with_a_fresh_token(make):
    c = make()
    assert "host" not in inspect.signature(server.make_server).parameters
    assert c.ui._httpd.server_address[0] == "127.0.0.1" and c.port > 0
    assert len(c.token) >= 40 and c.ui.url == f"http://127.0.0.1:{c.port}/?t={c.token}"
    assert make().token != c.token


def test_a_used_port_raises_before_the_keychain_is_read(tmp_path):
    class Backend:                         # a usable keychain that records every read
        priority = 1
        reads: list = []

        def get_password(self, service, name):
            self.reads.append(name)

        def set_password(self, service, name, value):
            pass

        def delete_password(self, service, name):
            pass

    backend = Backend()
    keychain = keys.Keychain(backend)
    assert keychain.available
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        with pytest.raises(OSError):
            server.make_server(port=busy.getsockname()[1], cwd=tmp_path, keychain=keychain, environ={})
    assert backend.reads == []


@pytest.mark.parametrize("port", [-1, 65536, True, "8770", 1.5])
def test_a_port_out_of_range_is_refused(tmp_path, port):
    with pytest.raises(ValueError):
        server.make_server(port=port, cwd=tmp_path, keychain=keys.Keychain(None), environ={})


def test_an_empty_token_is_refused(tmp_path):
    with pytest.raises(ValueError):
        server.make_server(port=0, cwd=tmp_path, keychain=keys.Keychain(None), environ={}, token="")


def test_a_given_token_is_used(make):
    c = make(token="fixed-token-for-this-test-0123456789")
    assert c.ui.url.endswith("/?t=fixed-token-for-this-test-0123456789")
    assert c.get("/api/state").status == 200


# ── Host, token, Origin, CORS ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("host", ["evil.com:{port}", "127.0.0.1", "localhost", "127.0.0.1:{other}",
                                  "127.0.0.1.nip.io:{port}", "[::1]", "0.0.0.0:{port}", "", "LOCALHOST:{port}"])
def test_a_foreign_host_header_is_refused_dns_rebinding(make, static_dir, host):
    c = make(static_dir=static_dir)
    value = host.format(port=c.port, other=c.port + 1)
    for path in ("/", "/static/app.js", "/api/state", f"/api/runs/x~y/stream?t={c.token}"):
        reply = c.get(path, host=value)
        assert reply.status == 403, (value, path)
        assert b"DNS rebinding" in reply.body
    assert c.post("/api/access", {"mode": "login"}, host=value).status == 403


@pytest.mark.parametrize("host", ["127.0.0.1:{port}", "localhost:{port}", "[::1]:{port}"])
def test_the_loopback_names_are_accepted(make, host):
    c = make()
    assert c.get("/api/state", host=host.format(port=c.port)).status == 200


def test_a_missing_or_doubled_host_is_refused(make):
    c = make()
    assert c.get("/api/state", host=False).status == 403
    assert c.get("/api/state", headers=[("Host", c.host)]).status == 403      # two Host headers


def test_the_api_needs_the_launch_token(make):
    c = make()
    assert c.get("/api/state", token=False).status == 401
    assert c.get("/api/state", token="not-the-token").status == 401
    assert c.get(f"/api/state?t={c.token}", token=False).status == 401     # ?t= only for stream and files
    assert c.get("/api/state", headers=[("X-Orion-Token", c.token)]).status == 401   # sent twice
    assert c.get("/api/nope", token=False).status == 401                   # before the route is even looked up
    assert c.post("/api/access", {"mode": "login"}, token=False).status == 401
    assert c.get("/api/runs/x~y/stream?t=wrong", token=False).status == 401
    assert c.get("/api/runs/x~y/files/report.txt?t=wrong", token=False).status == 401
    reply = c.get("/api/state")
    assert reply.status == 200
    assert "launch token" in c.get("/api/state", token=False).json()["error"]


@pytest.mark.parametrize("origin", [False, "http://evil.com", "null", "https://127.0.0.1:{port}",
                                    "http://127.0.0.1:{other}", "http://127.0.0.1"])
def test_a_change_needs_this_pages_origin(make, origin):
    c = make()
    value = origin.format(port=c.port, other=c.port + 1) if isinstance(origin, str) else origin
    assert c.post("/api/job/stop", origin=value).status == 403
    assert c.delete("/api/access", origin=value).status == 403
    assert c.post("/api/access", {"mode": "login"}, origin=value).status == 403


@pytest.mark.parametrize("origin", ["http://127.0.0.1:{port}", "http://localhost:{port}", "http://[::1]:{port}"])
def test_the_pages_own_origins_are_accepted(make, origin):
    c = make()
    assert c.post("/api/job/stop", origin=origin.format(port=c.port)).status == 409     # nothing is running


def test_no_cors_header_and_the_safety_headers_on_every_reply(make, static_dir):
    c = make(static_dir=static_dir)
    preflight = [("Origin", "http://evil.com"), ("Access-Control-Request-Method", "POST"),
                 ("Access-Control-Request-Headers", "x-orion-token")]
    assert c.request("OPTIONS", "/api/state", token=False, headers=preflight).status == 405
    assert c.request("OPTIONS", "/", token=False).status == 405
    c.get("/")
    c.get("/static/app.js")
    c.get("/static/nope.js")
    c.get("/api/state")
    c.get("/api/state", token=False)
    c.get("/api/state", host="evil.com:1")
    c.get("/api/runs")
    c.get("/api/nope")
    c.post("/api/state")
    c.post("/api/access", {"mode": "nope"})
    c.post("/api/access", {"mode": "login"}, origin="http://evil.com")
    c.request("BREW", "/")
    _write_run(c.ui.cwd)
    c.stream(f"t~{TS}")
    c.get(f"/api/runs/t~{TS}/files/report.txt?t={c.token}", token=False)
    assert len(c.replies) > 15
    for reply in c.replies:
        names = [k.lower() for k, _ in reply.headers]
        assert not any(n.startswith("access-control-") for n in names), reply
        assert reply.header("X-Content-Type-Options") == "nosniff"
        assert reply.header("Referrer-Policy") == "no-referrer"
        assert reply.header("Cache-Control") == "no-store"
        assert reply.header("Content-Security-Policy") == server.CSP
        assert reply.header("X-Frame-Options") == "DENY"


# ── the static page ───────────────────────────────────────────────────────────────────────────────────────
def test_the_page_is_served_with_its_csp_and_no_token(make, static_dir):
    c = make(static_dir=static_dir)
    index = c.get(f"/?t={c.token}", token=False)
    assert index.status == 200 and index.body == (static_dir / "index.html").read_bytes()
    assert index.header("Content-Type") == "text/html; charset=utf-8"
    assert index.header("Content-Security-Policy") == (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
    assert index.header("X-Frame-Options") == "DENY" and index.header("X-Content-Type-Options") == "nosniff"
    js = c.get("/static/app.js", token=False)
    assert js.status == 200 and js.header("Content-Type") == "text/javascript; charset=utf-8"
    css = c.get("/static/app.css", token=False)
    assert css.status == 200 and css.header("Content-Type") == "text/css; charset=utf-8"
    (static_dir / "app.js").write_bytes(b"console.log('edited');\n")
    assert c.get("/static/app.js").body == b"console.log('edited');\n"       # read per request


@pytest.mark.parametrize("path", ["/static/../server.py", "/static/%2e%2e/server.py", "/static/..%2fserver.py",
                                  "/static/server.py", "/static/", "/static/sub/x.js", "/static/.hidden.js",
                                  "/static/notes.md", "/server.py", "/index.html", "/static/app.js/",
                                  "/static/%2e%2e%5cserver.py", "/static/APP.JS", "/static"])
def test_only_the_allowlisted_static_files_are_served(make, static_dir, path):
    c = make(static_dir=static_dir)
    assert c.get(path, token=False).status == 404


def test_the_static_allowlist_is_read_once_at_start(make, static_dir):
    c = make(static_dir=static_dir)
    (static_dir / "late.js").write_text("late()\n", encoding="utf-8")
    assert c.get("/static/late.js").status == 404
    assert c.post("/").status == 405


def test_the_packaged_page_is_served_when_it_is_there(make):
    names = sorted(name for name in server._static_files(server.STATIC_DIR))
    if not names:
        pytest.skip("orion/ui/static/ holds no page yet")
    c = make()
    for name in names:
        assert c.get(f"/static/{name}", token=False).status == 200, name
    if "index.html" in names:
        assert c.get("/", token=False).header("Content-Type") == "text/html; charset=utf-8"


def test_a_missing_page_is_a_plain_404(make, tmp_path):
    c = make(static_dir=tmp_path / "empty")
    reply = c.get("/")
    assert reply.status == 404 and "index.html" in reply.json()["error"]


# ── /api/state ────────────────────────────────────────────────────────────────────────────────────────────
def test_state_has_the_specified_shape_and_no_secret(make):
    c = make(environ=_base_env(ANTHROPIC_API_KEY=ENV_KEY))
    reply = c.get("/api/state")
    state = reply.json()
    assert set(state) == {"cwd", "runs_root", "platform", "engines", "access", "job", "options", "models"}
    assert state["cwd"] == str(c.ui.cwd) and state["runs_root"] == str(c.ui.cwd / ".orion" / "runs")
    assert state["platform"] == sys.platform and state["job"] is None
    assert set(state["engines"]) == {"claude", "lm"} and set(state["engines"]["claude"]) == {"available", "reason"}
    assert state["engines"]["lm"]["available"] is False and "mlx_lm" in state["engines"]["lm"]["reason"]
    assert state["options"] == json.loads(json.dumps(jobs.OPTIONS)) and state["models"] == list(jobs.MODELS)
    assert state["access"]["mode"] == "anthropic_key" and state["access"]["source"] == "env"
    assert state["access"]["key_hint"] == "..." + ENV_KEY[-4:]
    assert ENV_KEY.encode() not in reply.body


def test_lm_availability_override_and_detection(make):
    assert make(lm_available=True).get("/api/state").json()["engines"]["lm"] == {"available": True, "reason": ""}
    assert make(lm_available=None).ui.lm_available is server._lm_importable()


def test_claude_availability_follows_the_path_scans_get(make, tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    if os.name == "nt":       # orion/claude_cli.py runs plain "claude": CreateProcess finds claude.exe, never a .cmd
        (bin_dir / "claude.cmd").write_text("@echo off\n", encoding="utf-8")
        shim = make(environ=_base_env(PATH=str(bin_dir))).get("/api/state").json()["engines"]["claude"]
        assert shim["available"] is False and "claude.cmd" in shim["reason"].lower()
        assert "claude.exe" in shim["reason"]
    exe = bin_dir / ("claude.exe" if os.name == "nt" else "claude")
    exe.write_text("@echo off\n" if os.name == "nt" else "#!/bin/sh\n", encoding="utf-8")
    exe.chmod(0o755)
    found = make(environ=_base_env(PATH=str(bin_dir))).get("/api/state").json()["engines"]["claude"]
    assert found == {"available": True, "reason": ""}
    (tmp_path / "nothing").mkdir()
    missing = make(environ=_base_env(PATH=str(tmp_path / "nothing"))).get("/api/state").json()["engines"]["claude"]
    assert missing["available"] is False and "claude" in missing["reason"]
    no_path = {k: v for k, v in _base_env().items() if k.upper() != "PATH"}
    assert make(environ=no_path).get("/api/state").json()["engines"]["claude"]["available"] is False


def test_claude_status_in_wsl_refuses_the_windows_build(monkeypatch):
    """In WSL a claude on a Windows drive (or a .exe) is the Windows build: it cannot run the scan's Linux MCP
    server or read Linux paths, and it does not use WSL's own login."""
    fake_os = types.SimpleNamespace(name="posix", path=types.SimpleNamespace(realpath=lambda p: p))
    monkeypatch.setattr(server, "os", fake_os)
    for where in ("/mnt/c/Users/h/AppData/Roaming/npm/claude", "/opt/claude/claude.exe"):
        monkeypatch.setattr(server.shutil, "which", lambda name, path=None, w=where: w)
        ok, why = server._claude_status({"PATH": "/usr/bin"}, wsl=True)
        assert ok is False and where in why and "Windows" in why and "bash -l" in why
    monkeypatch.setattr(server.shutil, "which", lambda name, path=None: "/home/u/.local/bin/claude")
    assert server._claude_status({"PATH": "/usr/bin"}, wsl=True) == (True, "")
    monkeypatch.setattr(server.shutil, "which", lambda name, path=None: "/mnt/c/tools/claude")
    assert server._claude_status({"PATH": "/usr/bin"}, wsl=False) == (True, "")   # outside WSL it is just a path


# ── request rules ─────────────────────────────────────────────────────────────────────────────────────────
def test_body_rules(make):
    c = make()
    assert c.post("/api/access", raw=b'{"mode": "login"}', ctype="text/plain").status == 415
    assert c.post("/api/access", raw=b'{"mode": "login"}', ctype=None).status == 415
    assert c.post("/api/access", raw=b'{"mode": "login"}', ctype="application/json; charset=utf-8").status == 200
    big = c.post("/api/access", raw=b" " * (server.BODY_MAX + 1))
    assert big.status == 413
    assert c.post("/api/access", raw=b"{not json").status == 400
    assert c.post("/api/access", raw=b"\xff\xfe").status == 400
    assert c.post("/api/access", raw=b"[" * 30000 + b"]" * 30000).status == 400
    assert c.post("/api/access", [1, 2]).status == 400
    assert c.post("/api/access").status == 400                             # no body at all
    assert c.post("/api/scans").status == 400
    assert c.post("/api/access", headers=[("Transfer-Encoding", "chunked")]).status == 411
    assert c.post("/api/access", raw=b"{}", headers=[("Content-Length", "2")]).status == 400   # two lengths


def test_a_refused_request_with_a_body_still_gets_its_reply(make):
    """Refusals before the body is looked at (Host, token, Origin, route, method): the body is read first, so
    the close never resets the connection under the client (on Windows that used to lose the reply)."""
    c = make()
    body = {"mode": "anthropic_key", "key": "x" * 2000}
    for _ in range(25):
        assert c.post("/api/access", body, host="evil.com:1").status == 403
        assert c.post("/api/access", body, token="wrong").status == 401
        assert c.post("/api/access", body, origin="http://evil.com").status == 403
        assert c.post("/api/nope", body).status == 404
        assert c.post("/api/state", body).status == 405
        assert c.request("PUT", "/api/access", body).status == 405
    assert c.get("/api/access").json()["mode"] == "login"


def test_unknown_routes_and_methods(make):
    c = make()
    assert c.get("/api/nope").status == 404
    assert c.get("/api/runs/x~y/nope").status == 404
    scans = c.get("/api/scans")
    assert scans.status == 405 and scans.header("Allow") == "POST"
    assert c.post("/api/state").status == 405
    assert c.delete("/api/job").status == 405
    assert c.request("PUT", "/api/access", {"mode": "login"}).status == 405
    assert c.request("PATCH", "/api/access").status == 405
    assert c.request("HEAD", "/").status == 405
    brew = c.request("BREW", "/api/state")
    assert brew.status == 501 and brew.json() == {"error": "Not Implemented"}
    assert c.request("GET", "http://127.0.0.1/api/state").status == 400   # an absolute-form target


def test_run_ids_are_checked(make):
    c = make()
    assert c.get(f"/api/runs/nope~{TS}").status == 404
    for rid in ("a..b", "..~x", "a~b~c", "%2E%2E~x", "a%2Fb~c", "a%5Cb~c", "~x"):
        assert c.get(f"/api/runs/{rid}").status == 400, rid
    assert c.get(f"/api/runs/nope~{TS}/stream?t={c.token}", token=False).status == 404
    assert c.post(f"/api/runs/nope~{TS}/resume").status == 404


# ── a scan, end to end ────────────────────────────────────────────────────────────────────────────────────
def test_a_scan_from_start_to_report(make, tmp_path):
    c = make()
    job = _scan(c, {"leads": 3, "delay": 0.15}, model="opus", options={"shortlists": True, "fail_on": "none"})
    repo = (c.ui.cwd / "repo").resolve()
    assert job["kind"] == "scan" and job["engine"] == "claude" and job["repo"] == str(repo)
    assert job["argv"] == [sys.executable, str(tmp_path / "fake_scan.py"), str(repo), "--quiet", "--shortlists",
                           "--fail-on", "none"]                      # the relative repo, resolved against cwd
    rid = _wait_run_id(c)
    run_dir = c.ui.runs.run_dir(rid)
    assert rid.startswith("s~") and run_dir.parent.parent == (c.ui.cwd / ".orion" / "runs").resolve()

    events = c.stream(rid)
    kinds = [e["event"] for e in events]
    assert kinds[-1] == "end" and events[-1]["data"] == {"state": "done"}
    progress = [e for e in events if e["event"] == "progress"]
    phases = [e["data"].get("phase") for e in progress]
    assert phases[0] == "build" and "verify" in phases and phases[-1] == "report"
    assert any(e["data"].get("event") == "usage" for e in progress)
    raw_log = (run_dir / "progress.jsonl").read_bytes()
    line_ends = [i + 1 for i, byte in enumerate(raw_log) if byte == ord("\n")]
    assert [int(e["id"]) for e in progress] == line_ends                 # every event: the offset after its line
    output = [e["data"] for e in events if e["event"] == "output"]
    assert [o["i"] for o in output] == list(range(len(output)))
    lines = [o["line"] for o in output]
    assert "scan_id: s" in lines and any(x.startswith("run log: ") for x in lines) and "naïve ✓ output" in lines
    job_events = [e["data"] for e in events if e["event"] == "job"]
    assert job_events[-1]["state"] == "exited" and job_events[-1]["exit_note"] == "finished (exit 0)"
    assert job_events[-1]["run_id"] == rid
    verdict_events = [e["data"] for e in events if e["event"] == "verdicts"]
    assert verdict_events[-1] == {"counts": {"CONFIRM": 1, "INCONCLUSIVE": 1, "REJECT": 1, "ERROR": 0}, "n": 3}

    listed = c.get("/api/runs").json()["runs"]
    assert [r["id"] for r in listed] == [rid] and listed[0]["state"] == "done"
    detail = c.get(f"/api/runs/{rid}").json()
    assert detail["state"] == "done" and detail["n_leads"] == 3 and detail["n_verdicts"] == 3
    assert detail["repo"] == str(repo) and len(detail["leads"]) == 3 and len(detail["verdicts"]) == 3
    assert set(detail["files"]) == set(runs.DOWNLOADS) and "CONFIRM" in detail["report_text"]

    for name in runs.DOWNLOADS:
        got = c.get(f"/api/runs/{rid}/files/{name}?t={c.token}", token=False)
        assert got.status == 200 and got.body == (run_dir / name).read_bytes(), name
        assert got.header("Content-Disposition") == f'attachment; filename="{name}"'
    assert c.get(f"/api/runs/{rid}/files/report.txt", token=False).status == 401
    assert c.get(f"/api/runs/{rid}/files/fake.json?t={c.token}", token=False).status == 404
    assert c.get(f"/api/runs/{rid}/files/..%2F..%2Ffake_scan.py?t={c.token}", token=False).status == 404

    code = c.get(f"/api/runs/{rid}/code?file=app/db.js&line=3&end=4")
    assert code.status == 200
    view = code.json()
    assert view["file"] == "app/db.js" and view["highlight"] == [3, 4]
    assert {"n": 3, "text": "line 3"} in view["lines"] and view["start"] == 1 and view["end"] == 12
    assert c.get(f"/api/runs/{rid}/code?file=app/db.js&line=3&context=0").json()["lines"] == [{"n": 3, "text":
                                                                                                "line 3"}]
    (tmp_path / "outside.txt").write_text("outside\n", encoding="utf-8")
    assert c.get(f"/api/runs/{rid}/code?file=../outside.txt&line=1").status == 403
    outside = urllib.parse.quote(str(tmp_path / "outside.txt"))
    assert c.get(f"/api/runs/{rid}/code?file={outside}&line=1").status == 403
    assert c.get(f"/api/runs/{rid}/code?file=app/nope.js&line=1").status == 404
    assert c.get(f"/api/runs/{rid}/code?file=app/db.js&line=999").status == 400
    assert c.get(f"/api/runs/{rid}/code?file=app/db.js").status == 400
    assert c.get(f"/api/runs/{rid}/code?line=1").status == 400

    record = _records(tmp_path)[-1]
    assert record["argv"][0] == str(repo) and record["cwd"] == str(c.ui.cwd)
    assert record["env"]["ORION_MODEL"] == "opus" and record["has_key"] is False
    assert record["env"]["PYTHONUNBUFFERED"] == "1" and record["env"]["PYTHONIOENCODING"] == "utf-8"
    assert "ORION_MODEL" not in " ".join(job["argv"]) and "opus" not in job["argv"]   # the model is env, never argv


def test_a_stream_resumes_from_an_offset_or_last_event_id(make):
    c = make()
    _scan(c, {"leads": 1})
    rid = _wait_run_id(c)
    _wait_exit(c)
    log = c.ui.runs.run_dir(rid) / "progress.jsonl"
    every = c.get(f"/api/runs/{rid}/events?offset=0").json()
    assert every["events"] and every["offset"] == log.stat().st_size
    total = len(every["events"])
    first_line = log.read_bytes().index(b"\n") + 1
    rest = c.get(f"/api/runs/{rid}/events?offset={first_line}").json()
    assert len(rest["events"]) == total - 1
    assert c.get(f"/api/runs/{rid}/events?offset=x").status == 400

    def progress(events):
        return [e for e in events if e["event"] == "progress"]

    whole = progress(c.stream(rid))
    assert len(whole) == total and [e["data"] for e in whole] == every["events"]
    for k in (0, 1, total - 2):                # what the page does on a reconnect: resume after the last id seen
        after = progress(c.stream(rid, headers={"Last-Event-ID": whole[k]["id"]}))
        assert after == whole[k + 1:]
        assert progress(c.stream(rid, query=f"&offset={whole[k]['id']}")) == whole[k + 1:]
    assert progress(c.stream(rid, query=f"&offset={every['offset']}")) == []
    assert len(progress(c.stream(rid, query=f"&offset={first_line}"))) == total - 1
    by_header = c.stream(rid, headers={"Last-Event-ID": str(first_line)})
    assert len(progress(by_header)) == total - 1 and by_header[-1]["event"] == "end"
    assert len(progress(c.stream(rid, headers={"Last-Event-ID": "garbage"}))) == total
    assert c.get(f"/api/runs/{rid}/stream?t={c.token}&offset=abc", token=False).status == 400


def test_a_stream_of_a_run_on_disk_needs_no_job(make):
    c = make()
    _write_run(c.ui.cwd, decisions=("CONFIRM", "REJECT", "ERROR"))
    events = c.stream(f"t~{TS}")
    assert [e["event"] for e in events if e["event"] in ("output", "job")] == []
    assert events[-1] == {"event": "end", "id": None, "data": {"state": "done"}}
    counts = [e["data"] for e in events if e["event"] == "verdicts"]
    assert counts == [{"counts": {"CONFIRM": 1, "INCONCLUSIVE": 0, "REJECT": 1, "ERROR": 1}, "n": 3}]


def test_the_job_and_its_output(make):
    c = make()
    assert c.get("/api/job").json() == {"job": None, "output": [], "next": 0}
    assert c.get("/api/job/output?since=0").json() == {"lines": [], "next": 0}
    _scan(c, {"leads": 1})
    _wait_exit(c)
    full = c.get("/api/job").json()
    assert full["next"] == len(full["output"]) and "scan_id: s" in full["output"]
    part = c.get(f"/api/job/output?since={full['next'] - 2}").json()
    assert part == {"lines": full["output"][-2:], "next": full["next"]}
    assert c.get(f"/api/job/output?since={full['next']}").json()["lines"] == []
    assert c.get("/api/job/output?since=bogus").json()["lines"] == full["output"]
    assert c.get("/api/job/output").json()["lines"] == full["output"]


def test_a_bad_scan_request_is_a_400(make):
    c = make()
    _repo(c)
    for body, fragment in [({"repo": "nope"}, "not found"), ({"repo": "repo", "options": {"x": True}}, "unknown"),
                           ({"repo": "repo", "engine": "lm", "model": "repo"}, "Orion-LM"),
                           ({"repo": "repo", "model": "-x"}, "model"), ({"repo": "repo", "extra": 1}, "unknown"),
                           ({"repo": "repo", "options": {"runtime_budget": 5}}, "runtime")]:
        reply = c.post("/api/scans", body)
        assert reply.status == 400 and fragment in reply.json()["error"], body
    assert _records(c.ui.cwd.parent) == []                                 # nothing was started


def test_a_spawn_failure_is_a_500_and_no_job(make, tmp_path):
    def broken(argv, **kwargs):
        raise FileNotFoundError(2, "No such file or directory")

    c = make(popen=broken)
    _repo(c)
    reply = c.post("/api/scans", {"repo": "repo"})
    assert reply.status == 500 and "could not start the scan" in reply.json()["error"]
    assert c.get("/api/job").json()["job"] is None


# ── one at a time, stop, resume ───────────────────────────────────────────────────────────────────────────
def test_one_scan_at_a_time_and_stop_interrupts_it(make, spawner):
    c = make()
    _scan(c, {"leads": 3, "pause_after": 1})
    rid = _wait_run_id(c)
    wait_until(lambda: c.get(f"/api/runs/{rid}").json()["n_verdicts"] == 1, what="the first verdict")
    assert c.get(f"/api/runs/{rid}").json()["state"] == "running"
    assert c.get("/api/runs").json()["runs"][0]["state"] == "running"
    busy = c.post("/api/scans", {"repo": "repo"})
    assert busy.status == 409 and "already running" in busy.json()["error"] and "repo" in busy.json()["error"]
    resume_now = c.post(f"/api/runs/{rid}/resume")
    assert resume_now.status == 400 and "still running" in resume_now.json()["error"]

    stop = c.post("/api/job/stop")
    assert stop.status == 200
    assert stop.json()["signal"] == "interrupt" and stop.json()["job"]["state"] == "stopping"
    job = _wait_exit(c)
    assert job["exit_note"] == "stopped (interrupted)" and job["returncode"] == 130 and job["stop_count"] == 1
    assert spawner.procs[0].sent == ["interrupt"]
    summary = c.get(f"/api/runs/{rid}").json()
    assert summary["state"] == "stopped" and summary["resumable"] is True
    nothing = c.post("/api/job/stop")
    assert nothing.status == 409 and "no scan is running" in nothing.json()["error"]


def test_a_second_stop_kills_a_scan_that_ignores_the_first(make, spawner):
    c = make()
    _scan(c, {"leads": 2, "pause_after": 0, "ignore_interrupt": True})
    _wait_run_id(c)
    first = c.post("/api/job/stop").json()
    assert first["signal"] == "interrupt"
    time.sleep(0.3)
    assert _job(c)["state"] == "stopping"
    second = c.post("/api/job/stop").json()
    assert second["signal"] == "kill"
    job = _wait_exit(c)
    assert job["exit_note"] == "stopped (force-killed)" and spawner.procs[0].sent == ["interrupt", "kill"]


def test_resume_finishes_a_stopped_run_on_its_own_model(make, tmp_path):
    c = make()
    _scan(c, {"leads": 3, "pause_after": 1}, model="opus")
    rid = _wait_run_id(c)
    wait_until(lambda: c.get(f"/api/runs/{rid}").json()["n_verdicts"] == 1, what="the first verdict")
    c.post("/api/job/stop")
    _wait_exit(c)
    assert c.post(f"/api/runs/{rid}/resume", {"model": "haiku"}).status == 400     # no options

    reply = c.post(f"/api/runs/{rid}/resume")
    assert reply.status == 202 and reply.json()["note"] is None          # leads.json recorded how it began
    job = reply.json()["job"]
    run_dir = c.ui.runs.run_dir(rid)
    assert job["kind"] == "resume" and job["engine"] == "claude" and job["model"] == "opus"
    assert job["argv"] == [sys.executable, str(tmp_path / "fake_scan.py"), "--resume", str(run_dir), "--quiet",
                           "--semantic-mode", "vector", "--fail-on", "none"]   # its recorded verify settings
    done = _wait_exit(c)
    assert done["exit_note"] == "finished (exit 0)" and done["run_id"] == rid
    detail = c.get(f"/api/runs/{rid}").json()
    assert detail["state"] == "done" and detail["n_verdicts"] == 3 and detail["resumable"] is False
    first, second = _records(tmp_path)
    assert second["argv"][:2] == ["--resume", str(run_dir)]
    assert first["env"]["ORION_MODEL"] == second["env"]["ORION_MODEL"] == "opus"   # one model per scan
    again = c.post(f"/api/runs/{rid}/resume")
    assert again.status == 400 and "finished" in again.json()["error"]


def test_resume_refuses_runs_that_cannot_be_resumed(make):
    c = make()
    repo = _repo(c)
    _write_run(c.ui.cwd, "lm", repo=repo, final=False, logged=1, engine="lm")
    _write_run(c.ui.cwd, "early", leads=False)
    _write_run(c.ui.cwd, "done", repo=repo)
    for rid, fragment in [(f"lm~{TS}", "engine lm"), (f"early~{TS}", "discovery"), (f"done~{TS}", "finished")]:
        reply = c.post(f"/api/runs/{rid}/resume")
        assert reply.status == 400 and fragment in reply.json()["error"], rid


def test_resume_refuses_a_run_another_process_is_writing(make, tmp_path):
    """Not a guess from file times (a verifier session keeps a run quiet for minutes): the run dir's lock."""
    c = make(environ=_base_env(ORION_MODEL="gemma4:12b"))       # left over in the shell orion ui started from
    repo = _repo(c, {"leads": 2})
    run_dir = _write_run(c.ui.cwd, repo=repo, final=False, logged=1)
    rid = f"t~{TS}"
    _age(run_dir, 3600)                                 # quiet for an hour: no file time says it is running
    held = runlock.acquire(run_dir)                     # ...but a scan in a terminal holds it
    try:
        row = c.get(f"/api/runs/{rid}").json()
        assert (row["state"], row["elsewhere"], row["resumable"]) == ("running", True, False)
        busy = c.post(f"/api/runs/{rid}/resume")
        assert busy.status == 400 and "outside this UI" in busy.json()["error"]
    finally:
        held.close()
    row = c.get(f"/api/runs/{rid}").json()
    assert (row["state"], row["elsewhere"], row["resumable"]) == ("stopped", False, True)
    reply = c.post(f"/api/runs/{rid}/resume")
    assert reply.status == 202 and "recorded no model" in reply.json()["note"]    # a run from before 2026-10-29
    assert "with sonnet" in reply.json()["note"]
    _wait_exit(c)
    assert c.get(f"/api/runs/{rid}").json()["state"] == "done"
    record = _records(tmp_path)[-1]
    # The default the note names, as a new scan from the page gets -- not the inherited gemma4:12b.
    assert record["env"]["ORION_MODEL"] == "sonnet" and record["argv"][2:] == ["--quiet"]


def test_resume_repeats_the_runs_recorded_verify_settings(make, tmp_path):
    c = make()
    _scan(c, {"leads": 2, "pause_after": 1}, model="haiku",
          options={"verify_routing": True, "semantic_mode": "hybrid", "fail_on": "confirm"})
    rid = _wait_run_id(c)
    wait_until(lambda: c.get(f"/api/runs/{rid}").json()["n_verdicts"] == 1, what="the first verdict")
    c.post("/api/job/stop")
    _wait_exit(c)
    assert c.get(f"/api/runs/{rid}").json()["settings"] == {
        "verify_routing": True, "verify_context": False, "endpoint": False, "semantic_mode": "hybrid",
        "fail_on": "confirm"}
    job = c.post(f"/api/runs/{rid}/resume").json()["job"]
    tail = job["argv"][job["argv"].index("--quiet"):]
    assert tail == ["--quiet", "--verify-routing", "--semantic-mode", "hybrid", "--fail-on", "confirm"]
    _wait_exit(c)
    assert _records(tmp_path)[-1]["env"]["ORION_MODEL"] == "haiku"


def test_resume_keeps_the_route_an_endpoint_run_began_on(make, tmp_path):
    c = make()
    c.post("/api/access", {"mode": "endpoint", "base_url": "http://127.0.0.1:11434", "model": "qwen3:8b"})
    _scan(c, {"leads": 2, "pause_after": 1})
    rid = _wait_run_id(c)
    wait_until(lambda: c.get(f"/api/runs/{rid}").json()["n_verdicts"] == 1, what="the first verdict")
    c.post("/api/job/stop")
    _wait_exit(c)
    assert c.get(f"/api/runs/{rid}").json()["settings"]["endpoint"] is True
    c.post("/api/access", {"mode": "login"})
    refused = c.post(f"/api/runs/{rid}/resume")         # qwen3:8b would go to Anthropic
    assert refused.status == 400 and "endpoint" in refused.json()["error"] and "qwen3:8b" in refused.json()["error"]
    c.post("/api/access", {"mode": "endpoint", "base_url": "http://127.0.0.1:11434", "model": "qwen3:8b"})
    assert c.post(f"/api/runs/{rid}/resume").status == 202
    _wait_exit(c)
    assert _records(tmp_path)[-1]["env"]["ORION_MODEL"] == "qwen3:8b"


def test_shutting_the_ui_down_stops_a_running_scan(make, spawner):
    c = make()
    _scan(c, {"leads": 2, "pause_after": 0})
    _wait_run_id(c)
    c.ui.shutdown(timeout=10.0)
    job = c.ui.jobs.current()
    assert job.state == "exited" and job.returncode == 130 and spawner.procs[0].sent == ["interrupt"]
    assert c.ui._httpd.socket.fileno() == -1                               # no longer listening
    c.ui.shutdown(timeout=1.0)                                              # a second call is harmless


def test_a_stream_whose_client_goes_away_ends_quietly(make):
    c = make()
    _scan(c, {"leads": 2, "pause_after": 0})
    rid = _wait_run_id(c)
    conn = http.client.HTTPConnection("127.0.0.1", c.port, timeout=10)
    conn.request("GET", f"/api/runs/{rid}/stream?t={c.token}", headers={"Host": c.host})
    resp = conn.getresponse()
    assert resp.status == 200 and resp.readline()          # the stream is flowing
    conn.close()
    resp.close()
    c.post("/api/job/stop")
    _wait_exit(c)
    time.sleep(0.6)                                         # the handler's next writes meet the closed socket
    assert c.get("/api/state").status == 200
    assert "error" not in c.log.getvalue()


# ── model access ──────────────────────────────────────────────────────────────────────────────────────────
def test_access_settings_for_each_mode(make):
    c = make()
    start = c.get("/api/access").json()
    assert start["mode"] == "login" and start["source"] == "none" and start["keychain"]["available"] is False
    key = c.post("/api/access", {"mode": "anthropic_key", "key": KEY})
    body = key.json()
    assert key.status == 200 and body["mode"] == "anthropic_key" and body["source"] == "ui"
    assert body["key_set"] is True and body["key_hint"] == "..." + KEY[-4:] and body["persisted"] is False
    assert KEY.encode() not in key.body
    endpoint = c.post("/api/access", {"mode": "endpoint", "base_url": "https://gw.example.test/v1/", "token": "",
                                      "model": "m-1", "auth": "x-api-key"}).json()
    assert endpoint["mode"] == "endpoint" and endpoint["key_set"] is False
    assert endpoint["endpoint"] == {"base_url": "https://gw.example.test/v1", "model": "m-1", "auth": "x-api-key",
                                    "token_set": False, "token_hint": None}
    login = c.post("/api/access", {"mode": "login", "key": ""}).json()   # a mode's unused fields are ignored
    assert login["mode"] == "login" and login["source"] == "ui"
    forgot = c.delete("/api/access").json()
    assert forgot["mode"] == "login" and forgot["source"] == "none"


def test_access_refusals_never_echo_what_was_typed(make):
    c = make()
    for body in [{"mode": "endpoint", "base_url": "ftp://user:hunter2pass@h/", "model": "m"},
                 {"mode": "endpoint", "base_url": "http://user:hunter2pass@h/", "model": "m"},
                 {"mode": "anthropic_key", "key": "hunter2 pass"}, {"mode": "anthropic_key"},
                 {"mode": "endpoint", "base_url": "http://h", "model": "bad model hunter2pass"},
                 {"mode": "nope"}, {"mode": "login", "hunter2pass": 1}, {}]:
        reply = c.post("/api/access", body)
        assert reply.status == 400 and b"hunter2" not in reply.body, body
    assert c.get("/api/access").json()["mode"] == "login"                  # nothing changed


def test_forget_falls_back_to_the_environment_key(make):
    c = make(environ=_base_env(ANTHROPIC_API_KEY=ENV_KEY))
    c.post("/api/access", {"mode": "login"})
    assert c.get("/api/access").json()["mode"] == "login"
    back = c.delete("/api/access")
    assert back.json()["mode"] == "anthropic_key" and back.json()["source"] == "env"
    assert ENV_KEY.encode() not in back.body


def test_endpoint_mode_scans_on_the_endpoints_model(make, tmp_path):
    c = make()
    _repo(c, {"leads": 1})
    reply = c.post("/api/access", {"mode": "endpoint", "base_url": "http://127.0.0.1:11434/", "token": TOKEN,
                                   "model": "qwen3:8b", "auth": "bearer"})
    assert reply.status == 200 and TOKEN.encode() not in reply.body
    other = c.post("/api/scans", {"repo": "repo", "model": "opus"})
    assert other.status == 400 and "qwen3:8b" in other.json()["error"]
    lm = c.post("/api/scans", {"repo": "repo", "engine": "lm", "model": "repo"})
    assert lm.status == 400 and "Orion-LM" in lm.json()["error"]            # the endpoint rule is claude-only
    _scan(c, {"leads": 1})
    _wait_exit(c)
    _scan(c, {"leads": 1}, model="qwen3:8b")
    _wait_exit(c)
    for record in _records(tmp_path):
        env = record["env"]
        assert env["ORION_MODEL"] == "qwen3:8b" and all(env[a] == "qwen3:8b" for a in keys.ALIASES)
        assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:11434"
        assert env["CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS"] == "1"
        assert record["token_sha256"] == _sha(TOKEN) and record["has_key"] is False


def test_resume_in_endpoint_mode_keeps_the_scans_model(make):
    c = make()
    _scan(c, {"leads": 2, "pause_after": 1}, model="opus")
    rid = _wait_run_id(c)
    wait_until(lambda: c.get(f"/api/runs/{rid}").json()["n_verdicts"] == 1, what="the first verdict")
    c.post("/api/job/stop")
    _wait_exit(c)
    c.post("/api/access", {"mode": "endpoint", "base_url": "http://127.0.0.1:11434", "model": "qwen3:8b"})
    refused = c.post(f"/api/runs/{rid}/resume")
    assert refused.status == 400 and "opus" in refused.json()["error"] and "qwen3:8b" in refused.json()["error"]


# ── the key never leaks ───────────────────────────────────────────────────────────────────────────────────
def _touch_everything(c: Client, rid: str) -> None:
    for path in ("/api/state", "/api/runs", f"/api/runs/{rid}", f"/api/runs/{rid}/events", "/api/job",
                 "/api/job/output?since=0", "/api/access", f"/api/runs/{rid}/code?file=app/db.js&line=3",
                 f"/api/compare?a={rid}&b={rid}"):
        assert c.get(path).status == 200, path
    for name in runs.DOWNLOADS:
        assert c.get(f"/api/runs/{rid}/files/{name}?t={c.token}", token=False).status == 200
    c.stream(rid)


def test_the_key_and_the_token_never_leak(make, tmp_path, spawner):
    c = make()
    _repo(c, {"leads": 2})
    assert c.post("/api/access", {"mode": "anthropic_key", "key": KEY}).status == 200
    _scan(c, {"leads": 2})
    rid = _wait_run_id(c)
    c.stream(rid)
    _wait_exit(c)
    assert f"env ANTHROPIC_API_KEY={keys.REDACTED}" in c.get("/api/job").json()["output"]
    _touch_everything(c, rid)
    key_record = _records(tmp_path)[-1]
    assert key_record["key_sha256"] == _sha(KEY) and key_record["has_token"] is False    # the child got it

    assert c.post("/api/access", {"mode": "endpoint", "base_url": "http://127.0.0.1:11434", "token": TOKEN,
                                  "model": "qwen3:8b", "auth": "bearer"}).status == 200
    _scan(c, {"leads": 1})
    rid2 = _wait_run_id(c)
    c.stream(rid2)
    _wait_exit(c)
    assert f"env ANTHROPIC_AUTH_TOKEN={keys.REDACTED}" in c.get("/api/job").json()["output"]
    _touch_everything(c, rid2)
    token_record = _records(tmp_path)[-1]
    assert token_record["token_sha256"] == _sha(TOKEN) and token_record["has_key"] is False

    assert spawner.calls[0]["env"]["ANTHROPIC_API_KEY"] == KEY             # only ever through the child's env
    assert spawner.calls[1]["env"]["ANTHROPIC_AUTH_TOKEN"] == TOKEN
    for secret in (KEY, TOKEN):
        raw = secret.encode()
        assert all(raw not in blob for blob in c.seen), secret                         # no HTTP reply
        on_disk = [p for p in tmp_path.rglob("*") if p.is_file() and raw in p.read_bytes()]
        assert on_disk == [], secret                                                   # no file, run dirs included
        assert all(secret not in arg for call in spawner.calls for arg in call["argv"])  # never in argv
        assert secret not in json.dumps(c.ui.jobs.current().argv)
        assert secret not in c.log.getvalue()                                          # not in the request log
    assert len(c.seen) > 30 and "GET /api/state 200" in c.log.getvalue()


def test_a_secret_already_on_disk_leaves_every_reply_redacted(make):
    c = make(environ=_base_env(ANTHROPIC_API_KEY=ENV_KEY))
    repo = _repo(c)
    (repo / "app" / "db.js").write_text(f"line 1\nconst key = '{ENV_KEY}';\nline 3\n", encoding="utf-8")
    _write_run(c.ui.cwd, repo=repo, secret=ENV_KEY)
    rid = f"t~{TS}"
    replies = [c.get(f"/api/runs/{rid}"), c.get(f"/api/runs/{rid}/events"), c.get("/api/runs"),
               c.get(f"/api/runs/{rid}/code?file=app/db.js&line=2"), c.stream_raw(rid),
               *(c.get(f"/api/runs/{rid}/files/{n}?t={c.token}", token=False) for n in runs.DOWNLOADS)]
    for reply in replies:
        assert reply.status == 200 and ENV_KEY.encode() not in reply.body
    redacted = keys.REDACTED.encode()
    assert all(redacted in r.body for r in replies[:2] + replies[3:5])
    assert all(redacted in r.body for r in replies[5:])                     # downloads are scrubbed too
    plain = make()                                                          # no secret held: bytes as they are
    run_dir = plain.ui.runs.run_dir(rid)
    got = plain.get(f"/api/runs/{rid}/files/report.txt?t={plain.token}", token=False)
    assert got.body == (run_dir / "report.txt").read_bytes()


def test_the_request_log_names_method_path_and_status_only(make):
    c = make()
    c.get(f"/?t={c.token}", token=False)
    c.get("/api/state")
    c.get(f"/api/runs/x~y/files/report.txt?t={c.token}", token=False)
    c.post("/api/access", {"mode": "anthropic_key", "key": KEY})
    c.get("/api/state", host="evil.com:1")
    log = c.log.getvalue()
    assert "GET /api/state 200" in log and "POST /api/access 200" in log and "GET /api/state 403" in log
    assert "GET /api/runs/x~y/files/report.txt 404" in log
    assert c.token not in log and "?" not in log and KEY not in log and "hunter" not in log
    quiet = make(verbose=False)
    quiet.get("/api/state")
    quiet.get("/nope")
    quiet.request("BREW", "/")
    assert quiet.log.getvalue() == ""


# ── compare ───────────────────────────────────────────────────────────────────────────────────────────────
def test_compare_two_runs(make):
    c = make()
    repo = _repo(c)
    _write_run(c.ui.cwd, "a", repo=repo, decisions=("CONFIRM", "REJECT"))
    _write_run(c.ui.cwd, "b", repo=repo, decisions=("CONFIRM", "CONFIRM", "INCONCLUSIVE"))
    diff = c.get(f"/api/compare?a=a~{TS}&b=b~{TS}").json()
    assert diff["same_repo"] is True and diff["unchanged"] == 1
    assert [row["function"] for row in diff["appeared"]] == ["f2"] and diff["vanished"] == []
    assert [(x["a"]["decision"], x["b"]["decision"]) for x in diff["changed"]] == [("REJECT", "CONFIRM")]
    assert c.get(f"/api/compare?a=a~{TS}").status == 400
    assert c.get(f"/api/compare?a=a~{TS}&b=zz~{TS}").status == 404


# ── `orion ui` ────────────────────────────────────────────────────────────────────────────────────────────
def test_cli_ui_help_lists_its_options(capsys):
    with pytest.raises(SystemExit) as stopped:
        cli.main(["ui", "--help"])
    assert stopped.value.code == 0
    out = capsys.readouterr().out
    assert "--port" in out and "--open" in out and "--verbose" in out


def test_cli_ui_default_port_is_the_servers():
    args = cli._build_parser().parse_args(["ui"])
    assert args.port == server.DEFAULT_PORT and args.open is False and args.verbose is False


def test_cli_ui_on_a_used_port_says_so_and_exits_2(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        port = busy.getsockname()[1]
        assert cli.main(["ui", "--port", str(port)]) == 2
    out, err = capsys.readouterr()
    assert out == "" and len(err.strip().splitlines()) == 1
    assert "--port" in err and f"127.0.0.1:{port}" in err


def test_cli_ui_refuses_a_port_out_of_range(capsys):
    assert cli.main(["ui", "--port", "70000"]) == 2
    assert "--port" in capsys.readouterr().err


class FakeUI:
    url = "http://127.0.0.1:4321/?t=launch-token"

    def __init__(self, tmp_path: Path, *, running: bool, interrupt_shutdown: bool = False) -> None:
        self.cwd = tmp_path
        self.runs = types.SimpleNamespace(root=tmp_path / ".orion" / "runs")
        self.stops = []
        self.jobs = types.SimpleNamespace(running=lambda: running, stop=lambda: self.stops.append("stop"))
        self.shutdowns: list[float] = []
        self.interrupt_shutdown = interrupt_shutdown
        self.kwargs: dict = {}

    def serve_forever(self) -> None:
        raise KeyboardInterrupt            # Ctrl-C at once

    def shutdown(self, timeout: float = 10.0) -> None:
        self.shutdowns.append(timeout)
        if self.interrupt_shutdown and len(self.shutdowns) == 1:
            raise KeyboardInterrupt        # a second Ctrl-C while the scan winds down


def _fake_ui(monkeypatch, fake: FakeUI) -> FakeUI:
    def make_server(**kwargs):
        fake.kwargs = kwargs
        return fake

    monkeypatch.setattr(server, "make_server", make_server)
    return fake


def test_cli_ui_prints_the_url_then_stops_the_scan_on_ctrl_c(tmp_path, monkeypatch, capsys):
    fake = _fake_ui(monkeypatch, FakeUI(tmp_path, running=True))
    opened = []

    def open_url(url):
        page = Path(urllib.request.url2pathname(urllib.parse.urlsplit(url).path))
        opened.append((url, page, page.read_text(encoding="utf-8")))
        return True

    monkeypatch.setattr(webbrowser, "open", open_url)
    serve = fake.serve_forever
    fake.serve_forever = lambda: (wait_until(lambda: opened, what="the browser to open"), serve())
    before = signal.getsignal(signal.SIGTERM)
    assert cli.main(["ui", "--port", "0", "--open", "--verbose"]) == 0
    out, err = capsys.readouterr()
    assert out.splitlines() == [f"Orion UI: {fake.url}",
                                f"runs: {fake.runs.root} (scans run from {fake.cwd}); Ctrl-C quits"]
    (url, page, html), = opened
    # The browser is handed a private page, never the URL: a command line is readable by every local user.
    assert url.startswith("file:") and "launch-token" not in url and fake.url.replace("&", "&amp;") in html
    assert not page.parent.exists()                                         # removed when orion ui stops
    assert fake.kwargs == {"port": 0, "verbose": True}
    assert "stopping the running scan" in err and fake.shutdowns == [10.0] and fake.stops == []
    assert signal.getsignal(signal.SIGTERM) is before


def test_cli_ui_a_second_ctrl_c_kills_the_scan(tmp_path, monkeypatch, capsys):
    fake = _fake_ui(monkeypatch, FakeUI(tmp_path, running=True, interrupt_shutdown=True))
    assert cli.main(["ui"]) == 0
    assert fake.kwargs == {"port": 8770, "verbose": False}
    assert fake.stops == ["stop"] and fake.shutdowns == [10.0, 5.0]
    assert "killing the scan" in capsys.readouterr().err


def test_cli_ui_without_a_scan_just_quits(tmp_path, monkeypatch, capsys):
    fake = _fake_ui(monkeypatch, FakeUI(tmp_path, running=False))
    monkeypatch.setattr(webbrowser, "open", lambda url: False)
    assert cli.main(["ui", "--open"]) == 0
    err = capsys.readouterr().err
    assert "stopping the running scan" not in err and "could not open a browser" in err
    assert fake.shutdowns == [10.0]


def test_pyproject_ships_the_ui_and_its_page():
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert "orion.ui" in project["tool"]["setuptools"]["packages"]
    assert project["tool"]["setuptools"]["package-data"]["orion.ui"] == ["static/*"]


def test_port_80_takes_the_host_a_browser_sends_for_it(tmp_path):
    """http's default port: a browser sends `Host: 127.0.0.1` and `Origin: http://127.0.0.1`, without ":80"."""
    def ui_on(port):
        httpd = types.SimpleNamespace(server_address=("127.0.0.1", port))
        return server.UIServer(httpd, cwd=tmp_path, keychain=keys.Keychain(None), environ={"PATH": ""},
                               popen=subprocess.Popen, scan_cmd=None, token="t", lm_available=False, verbose=False,
                               log_stream=None, static_dir=tmp_path)

    on_80 = ui_on(80)
    assert {"127.0.0.1", "localhost", "[::1]", "127.0.0.1:80"} <= on_80.hosts
    assert {"http://127.0.0.1", "http://localhost"} <= on_80.origins
    assert "127.0.0.1" not in ui_on(8770).hosts and "http://127.0.0.1" not in ui_on(8770).origins


@pytest.mark.skipif(os.name != "posix", reason="an ignored SIGINT is inherited across exec on POSIX only")
def test_cli_ui_started_with_sigint_ignored_still_lets_stop_reach_its_scans(tmp_path, monkeypatch):
    """A background job of a script starts with SIGINT ignored, and an ignored signal survives exec: every scan
    would ignore Stop. A handler (reset to the default on exec) keeps the UI ignoring Ctrl-C all the same."""
    fake = _fake_ui(monkeypatch, FakeUI(tmp_path, running=False))
    seen = []
    fake.serve_forever = lambda: (seen.append(signal.getsignal(signal.SIGINT)), (_ for _ in ()).throw(KeyboardInterrupt))
    before = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        assert cli.main(["ui"]) == 0
        assert callable(seen[0]) and seen[0] not in (signal.SIG_IGN, signal.SIG_DFL)
        assert signal.getsignal(signal.SIGINT) == signal.SIG_IGN          # put back as it was started
    finally:
        signal.signal(signal.SIGINT, before)


def test_the_browser_page_is_private_and_carries_the_link(tmp_path):
    page = cli._browser_page("http://127.0.0.1:8770/?t=abc_DEF-123")
    try:
        html = page.read_text(encoding="utf-8")
        assert 'content="0; url=http://127.0.0.1:8770/?t=abc_DEF-123"' in html
        if os.name == "posix":
            assert page.stat().st_mode & 0o777 == 0o600 and page.parent.stat().st_mode & 0o777 == 0o700
    finally:
        import shutil
        shutil.rmtree(page.parent)


def test_a_recorded_off_stays_off_whatever_the_ui_inherited(make, tmp_path):
    """The page showed adaptive verification and code context off; the UI was started with both on in its
    environment. The scan and its resume both run with them off."""
    c = make(environ=_base_env(ORION_VERIFY_ROUTING="1", ORION_VERIFY_CONTEXT="1", ORION_SEMANTIC_MODE="hybrid"))
    _scan(c, {"leads": 2, "pause_after": 1})
    rid = _wait_run_id(c)
    wait_until(lambda: c.get(f"/api/runs/{rid}").json()["n_verdicts"] == 1, what="the first verdict")
    c.post("/api/job/stop")
    _wait_exit(c)
    settings = c.get(f"/api/runs/{rid}").json()["settings"]
    assert (settings["verify_routing"], settings["verify_context"], settings["semantic_mode"]) == (False, False, "vector")
    assert c.post(f"/api/runs/{rid}/resume").status == 202
    _wait_exit(c)
    for record in _records(tmp_path):
        env = record["env"]
        assert (env["ORION_VERIFY_ROUTING"], env["ORION_VERIFY_CONTEXT"], env["ORION_SEMANTIC_MODE"]) == (
            "0", "0", "vector")


def test_resume_refuses_a_recorded_model_that_reads_as_a_flag(make):
    """leads.json is just a file: its model reaches `claude --model` through ORION_MODEL."""
    c = make()
    run_dir = _write_run(c.ui.cwd, repo=_repo(c), final=False, logged=1)
    meta = json.loads((run_dir / "leads.json").read_text(encoding="utf-8"))
    meta.update(model="--dangerously-skip-permissions", settings={"endpoint": False})
    (run_dir / "leads.json").write_text(json.dumps(meta), encoding="utf-8")
    refused = c.post(f"/api/runs/t~{TS}/resume")
    assert refused.status == 400 and "will not pass on" in refused.json()["error"]
    assert c.ui.jobs.current() is None                                       # nothing was started
