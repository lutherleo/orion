"""Scan jobs for `orion ui`: the allowlisted `orion scan` command line, and the one child process the UI
runs at a time.

The UI holds no scanning logic. A scan started from the browser is the ordinary CLI in a child process
(`python -m orion scan <repo> --quiet ...`), so it writes the same run dir a terminal scan writes and
`--resume` finishes it the same way. This module is where a browser request becomes a process, so it is
deliberately narrow:

- `build_argv` maps a request onto a FIXED allowlist (`OPTIONS`: one existing CLI flag per option, type- and
  range-checked; anything unknown, mistyped, out of range or meant for the other engine is refused with a
  message the page shows). Nothing free-form reaches the command line: the repo and an Orion-LM model are
  resolved ABSOLUTE paths of existing dirs (so neither can start with "-" and be read as a flag), and a
  Claude model name travels as ORION_MODEL in the child's env, never in argv. Secrets never pass through
  here at all: argv is visible to every local user via `ps`, so the caller's env (`keys.Access.child_env`)
  is the only way a key reaches the child.
- `Jobs` runs at most ONE child (Neo4j heap + Joern + embeddings on a 16 GB machine), started in its own
  process group (a new session on POSIX, CREATE_NEW_PROCESS_GROUP on Windows). That lets Stop reach the
  CLI's own children (`claude -p`, Joern) and keeps a Ctrl-C in the UI's terminal from hitting the scan
  behind the UI's back (`shutdown` stops it explicitly). Stop is two-step: the first press interrupts the
  group (SIGINT, the CLI's KeyboardInterrupt path; on Windows CTRL_BREAK, which ends a Python child at once
  with exit 0xC000013A because Python installs no Ctrl-Break handler); any later press kills.
- A reader thread keeps the last OUTPUT_LINES output lines, each scrubbed BEFORE it is stored (the caller's
  scrubber, plus the key the child itself was handed, so a key replaced in Settings mid-scan is still
  masked), and learns the run dir from the CLI's own `run log: <dir>/progress.jsonl` line: the UI never
  guesses which dir a scan writes. A separate waiter thread records the exit, so a grandchild that keeps the
  pipe open cannot leave a finished scan "running" with every later Start refused as Busy.

Stdlib only and no Orion import, so the tests run token-free anywhere, with fake processes for the signal
paths.
"""
from __future__ import annotations

import collections
import datetime
import itertools
import os
import re
import signal
import subprocess
import sys
import threading
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path

SCAN_CMD = [sys.executable, "-m", "orion", "scan"]
MODELS = ("sonnet", "opus", "haiku")             # offered; any model matching MODEL_RE is accepted
MODEL_RE = re.compile(r"^[A-Za-z0-9._:/@\[\]-]{1,120}$")
ENGINES = ("claude", "lm")
KINDS = ("scan", "resume")
OUTPUT_LINES = 2000                              # the ring buffer of a job's output


class OptionError(ValueError):
    """A scan request the allowlist refuses. The message is written for the page."""


class Busy(RuntimeError):
    """A scan is already running (one at a time). The message names it."""


# The public option spec: the page renders it and build_argv enforces it. Each entry maps to exactly one
# `orion scan` flag (tests/test_ui_jobs.py checks every flag and choice against the CLI's own parser).
# "default" is the CLI's own default, for the page to preselect; sending it explicitly is still allowed.
OPTIONS: dict[str, dict] = {
    "verify_routing": {
        "kind": "bool", "flag": "--verify-routing", "engines": ["claude"], "default": False,
        "label": "Adaptive verification",
        "help": "Clear-cut single-location leads get a light verifier pass (same model, smaller budget); "
                "anything it cannot settle gets the full pass.",
    },
    "verify_context": {
        "kind": "bool", "flag": "--verify-context", "engines": ["claude"], "default": False,
        "label": "Code context for the verifier",
        "help": "Put the code around each lead (its function, callers and callees) in the verifier's first "
                "message. Needs the lm extra installed.",
    },
    "shortlists": {
        "kind": "bool", "flag": "--shortlists", "engines": ["claude", "lm"], "default": False,
        "label": "Shortlists",
        "help": "Give discovery the build's candidate lists: missing controls, hedge comments, risky regexes "
                "and dependencies.",
    },
    "semantic_mode": {
        "kind": "choice", "flag": "--semantic-mode", "engines": ["claude"],
        "choices": ["vector", "hybrid", "keyword"], "default": "vector",
        "label": "Semantic search",
        "help": "How the agents' semantic search ranks: vector (meaning), hybrid (meaning and exact "
                "keywords) or keyword.",
    },
    "fail_on": {
        "kind": "choice", "flag": "--fail-on", "engines": ["claude", "lm"],
        "choices": ["none", "confirm", "inconclusive"], "default": "none",
        "label": "Fail on",
        "help": "End the scan with exit code 1 when a verdict is at or above this level (for CI).",
    },
    "language": {
        "kind": "text", "flag": "--language", "engines": ["claude", "lm"], "pattern": r"^[a-z0-9]{1,20}$",
        "default": "",
        "label": "Language",
        "help": "Joern frontend id: jssrc, pythonsrc, golang or javasrc. Leave empty to detect it from the "
                "repo.",
    },
    "lm_temperature": {
        "kind": "float", "flag": "--lm-temperature", "engines": ["lm"], "min": 0.0, "max": 2.0,
        "default": 0.0,
        "label": "Temperature",
        "help": "Orion-LM sampling temperature. 0 is greedy: the same answer every run.",
    },
    "lm_verify": {
        "kind": "bool", "flag": "--lm-verify", "engines": ["lm"], "default": False,
        "label": "Second-pass verification",
        "help": "Re-judge every lead alone on the same model (unmeasured). Without it every lead is "
                "reported INCONCLUSIVE, unverified.",
    },
    "runtime": {
        "kind": "bool", "flag": "--runtime", "engines": ["claude", "lm"], "default": False, "danger": True,
        "label": "Runtime stage",
        "help": "Before discovery, exercise the target (boot it, build it or run a harness script) and add the "
                "calls it makes to the graph.",
    },
    "runtime_sandbox": {
        "kind": "choice", "flag": "--runtime-sandbox", "engines": ["claude", "lm"], "requires": "runtime",
        "choices": ["auto", "docker", "host"], "default": "auto",
        "label": "Sandbox",
        "help": "Where a harness script runs: docker (isolated), host, or auto (Docker when it answers). "
                "The http and process drivers always run on the host.",
    },
    "runtime_driver": {
        "kind": "choice", "flag": "--runtime-driver", "engines": ["claude", "lm"], "requires": "runtime",
        "choices": ["auto", "harness", "http", "process"], "default": "auto",
        "label": "Driver",
        "help": "How the target is exercised: a harness script, http (boot it and fuzz its routes) or "
                "process (build it and fuzz argv and stdin). Auto picks from the repo.",
    },
    "runtime_budget": {
        "kind": "int", "flag": "--runtime-budget", "engines": ["claude", "lm"], "requires": "runtime",
        "min": 1, "max": 10000, "default": 200,
        "label": "Input budget",
        "help": "The most inputs the runtime stage sends.",
    },
}

_TOP_KEYS = ("repo", "engine", "model", "options")
_PATH_MAX = 4096


def _short(value: object) -> str:
    """A request value quoted for an error message, cut so a huge one cannot flood the page."""
    text = repr(value)
    return text if len(text) <= 80 else text[:77] + "..."


def _existing_dir(raw: object, what: str, base: Path | None) -> Path:
    """`raw` as the resolved absolute path of an existing directory, else OptionError naming `what`."""
    if not isinstance(raw, str) or not raw.strip():
        raise OptionError(f"{what} is required")
    if "\x00" in raw or len(raw) > _PATH_MAX:
        raise OptionError(f"{what} is not a usable path")
    try:
        path = Path(raw).expanduser()          # RuntimeError for ~user when there is no such user
        if not path.is_absolute() and base is not None:
            path = Path(base) / path
        path = path.resolve()
        found = path.is_dir()
    except (OSError, ValueError, RuntimeError):
        found = False
    if not found:
        raise OptionError(f"{what} not found or not a directory: {raw}")
    return path


def _flag_args(name: str, spec: dict, value: object) -> list[str]:
    """The argv words for one option value, or OptionError. Types are strict on purpose: JSON true is
    not the number 1 and "true" is not a bool, so a page bug is refused rather than guessed at."""
    kind, flag = spec["kind"], spec["flag"]
    if kind == "bool":
        if not isinstance(value, bool):
            raise OptionError(f"{name} must be true or false")
        return [flag] if value else []
    if kind == "choice":
        if not isinstance(value, str) or value not in spec["choices"]:
            raise OptionError(f"{name} must be one of: {', '.join(spec['choices'])}")
        return [flag, value]
    if kind == "text":
        if not isinstance(value, str):
            raise OptionError(f"{name} must be text")
        if value == "":
            return []
        if not re.fullmatch(spec["pattern"], value):     # fullmatch: "$" alone would let a trailing \n in
            raise OptionError(f"{name} must match {spec['pattern']}")
        return [flag, value]
    lo, hi = spec["min"], spec["max"]
    if kind == "int":
        if not isinstance(value, int) or isinstance(value, bool) or not lo <= value <= hi:
            raise OptionError(f"{name} must be a whole number from {lo} to {hi}")
        return [flag, str(value)]
    # float. NaN and infinity fail the range comparison, so they are refused too.
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not lo <= value <= hi:
        raise OptionError(f"{name} must be a number from {lo:g} to {hi:g}")
    return [flag, repr(float(value) + 0.0)]                # + 0.0 turns -0.0 into 0.0


def _option_args(options: dict, engine: str) -> list[str]:
    for name in options:
        if name not in OPTIONS:
            raise OptionError(f"unknown scan option {_short(name)}")
    given = {name: value for name, value in options.items() if value is not None}   # JSON null = not set
    out: list[str] = []
    for name, spec in OPTIONS.items():        # OPTIONS order: the same request always gives the same argv
        if name not in given:
            continue
        if engine not in spec["engines"]:
            raise OptionError(f"{name} is not available with the {engine} engine")
        needs = spec.get("requires")
        if needs and given.get(needs) is not True:
            raise OptionError(f"{name} needs {needs} turned on")
        out += _flag_args(name, spec, given[name])
    return out


def build_argv(req: dict, *, scan_cmd: list[str] = SCAN_CMD, lm_available: bool = False,
               cwd: Path | None = None) -> tuple[list[str], dict[str, str]]:
    """(argv, extra_env) for a page request {"repo", "engine", "model", "options"}, or OptionError.

    argv = scan_cmd + [repo, "--quiet", *engine flags, *option flags in OPTIONS order]. The repo (and an
    Orion-LM model dir) must be an existing directory and is passed resolved and absolute; a relative path
    resolves against `cwd` (default: this process's cwd). engine "claude" (the default) takes a model
    name (default "sonnet") that goes into extra_env as ORION_MODEL and never into argv; engine "lm"
    needs `lm_available` and passes `--engine lm --model <abs dir>`. Secrets never pass through here."""
    if not isinstance(req, dict):
        raise OptionError("a scan request must be a JSON object")
    for key in req:
        if key not in _TOP_KEYS:
            raise OptionError(f"unknown field {_short(key)}")
    engine = req.get("engine")
    if engine is None:
        engine = "claude"
    if engine not in ENGINES:
        raise OptionError(f"engine must be one of: {', '.join(ENGINES)}")
    repo = _existing_dir(req.get("repo"), "repo path", cwd)
    model = req.get("model")
    extra_env: dict[str, str] = {}
    if engine == "lm":
        if not lm_available:
            raise OptionError("Orion-LM is not available on this machine (it needs mlx_lm or llama_cpp)")
        engine_args = ["--engine", "lm", "--model", str(_existing_dir(model, "Orion-LM model path", cwd))]
    else:
        if model is None or model == "":
            model = "sonnet"
        # A leading "-" would reach `claude --model <name>` (orion/claude_cli.py) looking like a flag.
        if not isinstance(model, str) or not MODEL_RE.fullmatch(model) or model.startswith("-"):
            raise OptionError("model must be a name like sonnet or claude-sonnet-5: up to 120 letters, digits "
                              "or . _ : / @ [ ] -, not starting with -")
        engine_args = []
        extra_env["ORION_MODEL"] = model
    options = req.get("options")
    if options is None:
        options = {}
    if not isinstance(options, dict):
        raise OptionError("options must be a JSON object")
    argv = [*scan_cmd, str(repo), "--quiet", *engine_args, *_option_args(options, engine)]
    return argv, extra_env


# What a resume repeats: the verify-time options the CLI records in leads.json "settings" (cli._verify_settings).
RESUME_OPTIONS = ("verify_routing", "verify_context", "semantic_mode", "fail_on")


def resume_argv(run_dir: Path, *, scan_cmd: list[str] = SCAN_CMD, options: dict | None = None) -> list[str]:
    """`orion scan --resume <run_dir> --quiet [option flags]`. The dir must be absolute: the UI only resumes dirs
    it found under its own runs root, and a relative one would start with "-" or mean a different dir to the
    child. `options` (RESUME_OPTIONS only, checked like a scan's) are the run's recorded verify settings, so the
    rest of its leads are verified the way the first ones were."""
    if not Path(run_dir).is_absolute():
        raise ValueError(f"resume needs an absolute run dir, got {run_dir}")
    options = options or {}
    for name in options:
        if name not in RESUME_OPTIONS:
            raise OptionError(f"a resume repeats only {', '.join(RESUME_OPTIONS)}, not {_short(name)}")
    return [*scan_cmd, "--resume", str(run_dir), "--quiet", *_option_args(options, "claude")]


# ── the one running scan ─────────────────────────────────────────────────────────────────────────────
_ACTIVE = ("running", "stopping")
_SCAN_ID_LINE = re.compile(r"scan_id: (\S+)")                       # orion/cli.py prints both lines
_RUN_LOG_LINE = re.compile(r"run log: (.+)[\\/]progress\.jsonl")     # before any work starts
_CHILD_SECRET_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")   # where keys.Access.child_env puts one
_MIN_SECRET_LEN = 8                    # == keys.MIN_SECRET_LEN: shorter dummy tokens ("ollama") stay
_REDACTED = "[redacted]"
_WITHHELD = "[output line withheld: the scrubber failed]"
_LINE_MAX = 8000                       # per stored line, cut AFTER scrubbing so no key is ever half-kept
_DRAIN_GRACE = 2.0                     # seconds the waiter lets the reader finish after the child exits
_KILL_WAIT = 5.0                       # seconds shutdown waits for a killed child to be reaped
_SIGKILL = getattr(signal, "SIGKILL", 9)                    # POSIX only; the fallback keeps Windows importing
_CTRL_BREAK = getattr(signal, "CTRL_BREAK_EVENT", 1)        # Windows only
_NEW_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)   # Windows only
# 128 + SIGINT (through a shell), and Windows' STATUS_CONTROL_C_EXIT (Ctrl-C / Ctrl-Break) unsigned and signed.
_INTERRUPTED = {130, 0xC000013A, 0xC000013A - (1 << 32)}


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _resolve(path: Path) -> Path | None:
    """`path` absolute with symlinks resolved; None for a path no OS can open (a NUL in it)."""
    if "\x00" in str(path):
        return None
    try:
        return path.resolve()
    except (OSError, RuntimeError, ValueError):
        try:
            return Path(os.path.abspath(path))
        except (OSError, ValueError):
            return None


def _fail_on_given(argv: list[str]) -> bool:
    """True when argv asks for a --fail-on level, so exit 1 means "a verdict hit it" rather than a crash
    (an uncaught Python exception also exits 1)."""
    for i, arg in enumerate(argv):
        if arg == "--fail-on" and i + 1 < len(argv):
            value = argv[i + 1]
        elif arg.startswith("--fail-on="):
            value = arg.split("=", 1)[1]
        else:
            continue
        return value in ("confirm", "inconclusive")
    return False


def _signal_name(num: int) -> str:
    try:
        return signal.Signals(num).name
    except ValueError:
        return {9: "SIGKILL", 15: "SIGTERM"}.get(num, str(num))


def exit_note(returncode: int | None, *, stop_count: int = 0, forced: bool = False,
              fail_on: bool = False) -> str | None:
    """Plain words for how a scan ended (None while it runs). `forced` = the UI killed it: on Windows that
    exits 1, which would otherwise read as a --fail-on hit or a crash."""
    if returncode is None:
        return None
    stopped = stop_count > 0
    if forced and returncode != 0:
        return "stopped (force-killed)"
    if returncode in _INTERRUPTED:              # before the signal branch: its signed Windows form is < 0
        return "stopped (interrupted)" if stopped else f"interrupted (exit {returncode})"
    if returncode < 0:
        if -returncode == signal.SIGINT:
            return "stopped (interrupted)" if stopped else "interrupted (SIGINT)"
        return f"ended by signal {_signal_name(-returncode)}"
    if returncode == 0:
        return "finished after the stop request (exit 0)" if stopped else "finished (exit 0)"
    if returncode == 1:
        return ("finished; a verdict hit the --fail-on level (exit 1)" if fail_on
                else "failed with an error, see the scan output (exit 1)")
    if returncode == 2:
        return "refused: usage error, see the scan output (exit 2)"
    if returncode == 3:
        return "incomplete: some Orion-LM batches were not judged (exit 3)"
    return f"exited with {returncode}"


class Job:
    """One `orion scan` child: what it was started with, where it writes, how it ended, and its last
    OUTPUT_LINES output lines (already scrubbed). Created by `Jobs.start`; its fields change under the
    Jobs lock."""

    def __init__(self, *, argv: list[str], kind: str, repo: str | None, engine: str, cwd: Path,
                 env: Mapping[str, str], lock: threading.RLock, scrub: Callable[[str], str],
                 model: str | None = None) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.argv = list(argv)
        self.repo = repo
        self.engine = engine
        self.model = model                      # for display: the claude model name or the lm model dir
        self.started = _now()
        self.state = "running"
        self.returncode: int | None = None
        self.stop_count = 0
        self.scan_id: str | None = None
        self.run_dir: Path | None = None
        self._cwd = cwd
        self._lock = lock
        self._scrub = scrub
        # The key this child holds, masked by the job itself: the caller's scrubber only knows the CURRENT
        # key, and the user may replace or forget it in Settings while this scan still runs with the old one.
        held = {str(env[k]) for k in _CHILD_SECRET_VARS if len(str(env.get(k) or "")) >= _MIN_SECRET_LEN}
        self._own_secrets = sorted(held, key=len, reverse=True)
        self._fail_on = _fail_on_given(self.argv)
        self._forced = False
        self._lines: collections.deque[str] = collections.deque(maxlen=OUTPUT_LINES)
        self._next = 0                          # absolute index of the next line
        self._proc = None
        self._drained = threading.Event()       # the reader hit EOF
        self._exited = threading.Event()        # state became "exited"

    def _redact(self, text: str) -> str:
        try:
            out = self._scrub(text)
        except Exception:  # noqa: BLE001 -- a scrubber bug must withhold the text, never pass it on raw
            return _WITHHELD
        if not isinstance(out, str):
            return _WITHHELD
        for secret in self._own_secrets:
            out = out.replace(secret, _REDACTED)
        return out

    def _feed(self, raw: str) -> None:
        """Reader thread: store one output line, scrubbed, and learn scan_id / run_dir from the CLI's own
        lines (the first of each wins; parsed from the scrubbed text, so nothing unscrubbed is kept)."""
        line = self._redact(raw.rstrip("\r\n"))
        if len(line) > _LINE_MAX:
            line = f"{line[:_LINE_MAX]} ... [{len(line) - _LINE_MAX} more characters]"
        scan_id = run_dir = None
        if m := _SCAN_ID_LINE.fullmatch(line):
            scan_id = m.group(1)
        elif m := _RUN_LOG_LINE.fullmatch(line):
            run_dir = _resolve(self._cwd / m.group(1))     # relative to the child's cwd; a resume's is absolute
        with self._lock:
            if scan_id is not None and self.scan_id is None:
                self.scan_id = scan_id
            if run_dir is not None and self.run_dir is None:
                self.run_dir = run_dir
            self._lines.append(line)
            self._next += 1

    def output(self, since: int = 0) -> tuple[list[str], int]:
        """The stored lines with absolute index >= `since` (lines older than the ring are gone; a stale or
        bogus `since` just gets what is there) and the index to ask from next."""
        try:
            since = int(since)
        except (TypeError, ValueError):
            since = 0
        with self._lock:
            first = self._next - len(self._lines)
            start = max(since, first)
            if start >= self._next:
                return [], self._next
            return list(itertools.islice(self._lines, start - first, None)), self._next

    def wait(self, timeout: float | None = None) -> bool:
        """Block until the job has exited (True) or `timeout` passes (False)."""
        return self._exited.wait(timeout)

    def public(self, rid_for: Callable[[Path], str | None] | None = None) -> dict:
        """The job as JSON for the page; `rid_for` (Runs.rid_for) turns the run dir into a run id. argv
        carries no secret by construction; it is scrubbed anyway, since this goes into HTTP responses."""
        with self._lock:
            state, rc, stops, forced = self.state, self.returncode, self.stop_count, self._forced
            scan_id, run_dir = self.scan_id, self.run_dir
        run_id = None
        if rid_for is not None and run_dir is not None:
            try:
                run_id = rid_for(run_dir)
            except (OSError, ValueError, KeyError):
                run_id = None
        return {
            "id": self.id, "kind": self.kind, "argv": [self._redact(a) for a in self.argv],
            "repo": self.repo, "engine": self.engine, "model": self.model, "started": self.started, "state": state,
            "returncode": rc, "stop_count": stops, "scan_id": scan_id,
            "run_dir": str(run_dir) if run_dir is not None else None, "run_id": run_id,
            "exit_note": exit_note(rc, stop_count=stops, forced=forced, fail_on=self._fail_on),
        }


def _group_kwargs() -> dict:
    """Popen kwargs that put the child in its own process group (read at call time, so tests can fake
    either platform by swapping this module's `os`)."""
    if os.name == "nt":
        return {"creationflags": _NEW_GROUP}
    return {"start_new_session": True}


def _own_pgrp() -> int | None:
    getpgrp = getattr(os, "getpgrp", None)
    return getpgrp() if getpgrp is not None else None


def _taskkill_tree(pid: int) -> bool:
    """Windows: force-end `pid` and every process it started (taskkill /T /F). True when taskkill did."""
    try:
        r = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


def _send_stop(proc, *, graceful: bool) -> str:
    """Interrupt (graceful) or kill the child's process group; returns "interrupt" or "kill" -- what was
    sent, or asked for when the child turned out to be gone already (the waiter records its exit). Polling
    first, as Popen.send_signal does, keeps a reaped child's recycled pid from being signalled."""
    if proc.poll() is not None:
        return "interrupt" if graceful else "kill"
    if os.name == "nt":
        if graceful:
            try:
                proc.send_signal(_CTRL_BREAK)
                return "interrupt"
            except OSError:
                pass    # no console shared with the child to carry Ctrl-Break: ending it is the only stop left
        # TerminateProcess (Popen.kill) ends the scan's python alone: its claude.exe, node and MCP server
        # children would run on. taskkill /T ends the whole tree; kill() is the fallback.
        if not (isinstance(proc, subprocess.Popen) and _taskkill_tree(proc.pid)):
            proc.kill()
        return "kill"
    sig = signal.SIGINT if graceful else _SIGKILL
    try:
        pgid = os.getpgid(proc.pid)
        if pgid == _own_pgrp():
            proc.send_signal(sig)   # never signal the UI's own group, should a child not be in its own session
        else:
            os.killpg(pgid, sig)
    except ProcessLookupError:
        pass                        # already gone
    return "interrupt" if graceful else "kill"


class Jobs:
    """At most one running `orion scan` child, plus the most recent one for display. Thread-safe: one lock
    guards every job's state and output."""

    def __init__(self, *, cwd: Path, scrub: Callable[[str], str] = lambda s: s,
                 popen: Callable[..., object] = subprocess.Popen) -> None:
        self.cwd = Path(cwd).resolve()
        self._scrub = scrub
        self._popen = popen
        self._lock = threading.RLock()
        self._current: Job | None = None

    def _active(self) -> Job | None:
        job = self._current
        return job if job is not None and job.state in _ACTIVE else None

    def start(self, argv: list[str], env: Mapping[str, str], *, kind: str, repo: str | None,
              engine: str, model: str | None = None) -> Job:
        """Spawn `argv` (from build_argv / resume_argv) in `cwd` with exactly `env` (plus PYTHONUNBUFFERED
        and PYTHONIOENCODING when the caller left them out: the output is read as UTF-8 lines while the scan
        runs, and a block-buffered child would only reveal its run dir when it exits). Busy if one runs."""
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}")
        if engine not in ENGINES:
            raise ValueError(f"engine must be one of {ENGINES}")
        if not argv or not all(isinstance(a, str) for a in argv):
            raise ValueError("argv must be a non-empty list of strings")
        child_env = dict(env)
        child_env.setdefault("PYTHONUNBUFFERED", "1")
        child_env.setdefault("PYTHONIOENCODING", "utf-8")
        with self._lock:
            busy = self._active()
            if busy is not None:
                raise Busy(f"a scan is already {busy.state} ({busy.repo or busy.scan_id or busy.id}); "
                           "stop it or wait for it to finish")
            job = Job(argv=argv, kind=kind, repo=repo, engine=engine, cwd=self.cwd, env=child_env,
                      lock=self._lock, scrub=self._scrub, model=model)
            job._proc = self._popen(
                list(argv), cwd=str(self.cwd), env=child_env, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                errors="replace", bufsize=1, **_group_kwargs())
            self._current = job
            threading.Thread(target=self._pump, args=(job,), name=f"orion-ui-job-{job.id}-out",
                             daemon=True).start()
            threading.Thread(target=self._reap, args=(job,), name=f"orion-ui-job-{job.id}-wait",
                             daemon=True).start()
        return job

    def _pump(self, job: Job) -> None:
        """Reader thread: every output line into the job, until EOF."""
        stream = job._proc.stdout
        try:
            if stream is not None:
                for raw in stream:
                    try:
                        job._feed(raw)
                    except Exception:  # noqa: BLE001 -- keep draining: a reader that stops leaves the
                        pass           # child blocked on a full pipe, i.e. a hung scan
        except (OSError, ValueError):
            pass            # the pipe was closed under the reader; the waiter still records the exit
        finally:
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
            job._drained.set()

    def _reap(self, job: Job) -> None:
        """Waiter thread: record the exit once the child is gone and its output is read (or, should a
        grandchild hold the pipe open, after _DRAIN_GRACE)."""
        proc = job._proc
        try:
            code = proc.wait()
        except OSError:
            code = getattr(proc, "returncode", None)
        job._drained.wait(_DRAIN_GRACE)
        with self._lock:
            job.returncode = code
            job.state = "exited"
        job._exited.set()

    def current(self) -> Job | None:
        """The running job, else the most recent one (for display)."""
        with self._lock:
            return self._current

    def running(self) -> bool:
        with self._lock:
            return self._active() is not None

    def stop(self, rid_for: Callable[[Path], str | None] | None = None) -> dict:
        """First call: interrupt the job's process group (POSIX SIGINT to the group, so the CLI's own
        `claude -p` / Joern children get it too; Windows CTRL_BREAK to the group, or a kill when no console
        can carry it); state "stopping". Every later call kills (POSIX SIGKILL to the group, Windows
        taskkill /T on the process tree). RuntimeError when nothing runs; a failed signal leaves the job
        untouched.
        -> {"signal": "interrupt"|"kill", "job": public}"""
        with self._lock:
            job = self._active()
            if job is None:
                raise RuntimeError("no scan is running")
            sent = _send_stop(job._proc, graceful=job.stop_count == 0)
            job.stop_count += 1
            job.state = "stopping"
            if sent == "kill":
                job._forced = True
        return {"signal": sent, "job": job.public(rid_for)}

    def active_dirs(self) -> dict[str, str]:
        """{str(resolved run dir): state} for a running/stopping job whose run dir is known (for Runs.list)."""
        with self._lock:
            job = self._active()
            if job is None or job.run_dir is None:
                return {}
            run_dir, state = job.run_dir, job.state
        return {str(_resolve(run_dir) or run_dir): state}

    def shutdown(self, timeout: float = 10.0) -> None:
        """UI exit: stop a running job gracefully (unless a stop was already sent), wait up to `timeout`,
        then kill it and wait briefly for the exit -- the child is in its own process group, so nothing
        else would ever stop it."""
        with self._lock:
            job = self._active()
        if job is None:
            return
        if job.stop_count == 0:
            self._stop_quietly()
        if job.wait(timeout):
            return
        self._stop_quietly()        # any stop after the first kills
        job.wait(min(timeout, _KILL_WAIT))

    def _stop_quietly(self) -> None:
        try:
            self.stop()
        except (RuntimeError, OSError):
            pass                    # it exited meanwhile, or the signal failed: shutdown goes on regardless
