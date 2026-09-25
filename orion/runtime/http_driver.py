"""HttpDriver: boot a web app, log in, seed requests from the graph's route table, drive them. Stdlib.

Seeds come from the graph being enriched: route registrations are `<router>.get/post/...('/path',
handler)` CpgCall nodes, so the verb and path literal lift straight out of `code`. Path parameters
(`/users/:id`, `/{id}`) get a benign placeholder so the route actually matches. Auth is a first-class
step -- NodeGoat gates nearly every route behind login -- so `login` (from a `.orion/runtime.json`
descriptor) establishes a session cookie before the loop starts.

A server only flushes V8 coverage on exit, so this driver is `feedback = False`: the engine drives the
budget without per-input collects, and the pipeline collects once after `stop()`.
"""
from __future__ import annotations

import http.cookiejar
import os
import re
import signal
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .base import Input, Response, RunningTarget

_VERBS = ("get", "post", "put", "delete", "patch")
_ROUTE_CYPHER = (
    "MATCH (c:CpgCall {scan_id:$scan_id}) "
    "WHERE c.name IN ['get','post','put','delete','patch'] "
    "RETURN c.name AS verb, c.code AS code ORDER BY c.file_path, c.line")
_ROUTE_PATH_RE = re.compile(r"""^[\w$.]+\.(?:get|post|put|delete|patch)\(\s*['"`](/[^'"`]*)['"`]""")
_PARAM_RE = re.compile(r":\w+\??|\{\w+\}")


def routes_from_rows(rows: list[dict]) -> list[Input]:
    """Pure: graph rows -> deduped seed Inputs, in graph order. `code` is the raw registration
    source; rows without a leading-slash path literal (router mounts, `app.get('env')`) are skipped."""
    out: list[Input] = []
    seen: set = set()
    for r in rows:
        verb = (r.get("verb") or "get").lower()
        m = _ROUTE_PATH_RE.match((r.get("code") or "").strip())
        if verb not in _VERBS or not m:
            continue
        path = _PARAM_RE.sub("1", m.group(1))
        key = (verb.upper(), path)
        if key not in seen:
            seen.add(key)
            out.append(Input(kind="http", label=f"{key[0]} {path}", verb=key[0], path=path))
    return out


class HttpDriver:
    """Drive a booted web app. `boot_cmd`/`base_url`/`login` come from the target descriptor."""

    feedback = False    # coverage flushes only when the server exits
    mutable = True

    def __init__(self, boot_cmd: list[str], base_url: str, tracer,
                 login: dict | None = None, boot_wait: float = 25.0) -> None:
        self._boot_cmd = boot_cmd
        self._base_url = base_url.rstrip("/")
        self._tracer = tracer        # launch_env(work) keyed to the ACTUAL work dir
        self._login = login          # {"path", "fields": {...}} or None
        self._boot_wait = boot_wait
        self._opener = None

    def start(self, repo: str, work: Path, build_flags: list[str]) -> RunningTarget:
        env = {**os.environ, **self._tracer.launch_env(work)}
        # Own process group (POSIX) / group (Windows) so stop() reaches the `node` child of
        # `npm start` -- signalling only npm would leave node running, never flushing coverage.
        kw = ({"start_new_session": True} if os.name == "posix"
              else {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP})
        proc = subprocess.Popen(self._boot_cmd, cwd=repo, env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        if not self._wait_ready(proc):
            self.stop(RunningTarget(kind="http", repo=repo, work=work, handle=proc))
            raise RuntimeError(f"target did not answer at {self._base_url} within {self._boot_wait:.0f}s")
        if self._login:
            self._do_login()
        return RunningTarget(kind="http", repo=repo, work=work, base_url=self._base_url, handle=proc)

    def _wait_ready(self, proc) -> bool:
        deadline = time.monotonic() + self._boot_wait
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                return False             # the app exited during boot -- no point waiting
            try:
                self._opener.open(self._base_url + "/", timeout=3)
                return True
            except urllib.error.HTTPError:
                return True              # any HTTP answer (even 404/500) means it is up
            except (urllib.error.URLError, OSError):
                time.sleep(0.5)
        return False

    def _do_login(self) -> None:
        data = urllib.parse.urlencode(self._login.get("fields", {})).encode()
        url = self._base_url + self._login.get("path", "/login")
        try:
            self._opener.open(urllib.request.Request(url, data=data), timeout=10)
        except (urllib.error.URLError, OSError):
            pass  # best-effort; the loop still runs (and observes the auth-wall paths)

    def seeds(self, db, scan_id: str) -> list[Input]:
        res = db.run_cypher(scan_id, _ROUTE_CYPHER, limit=2000)
        return routes_from_rows(res.get("rows", []))

    def send(self, target: RunningTarget, inp: Input) -> Response:
        req = urllib.request.Request(target.base_url + inp.path, method=inp.verb,
                                     data=inp.body or None, headers=inp.headers or {})
        try:
            resp = self._opener.open(req, timeout=10)
            return Response(ok=True, status=getattr(resp, "status", 0))
        except urllib.error.HTTPError as e:
            return Response(ok=True, status=e.code)           # a 500 is signal, not a driver failure
        except (urllib.error.URLError, OSError, ValueError) as e:
            # ValueError: a mutated input produced a URL urllib refuses. Skip it, never abort the run.
            return Response(ok=False, detail=str(e))

    def stop(self, target: RunningTarget) -> None:
        proc = target.handle
        if proc is None or proc.poll() is not None:
            return
        # SIGTERM the whole group so node's exit-flush preload writes coverage + the cpu profile.
        try:
            if os.name == "posix":
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            else:
                proc.send_signal(signal.CTRL_BREAK_EVENT)
        except (ProcessLookupError, PermissionError, OSError):
            proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
