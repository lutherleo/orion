"""`orion ui`'s scan jobs (orion/ui/jobs.py): the argv allowlist and the one child process.

Token-free. build_argv is checked against the CLI's OWN `scan` parser, so the allowlist cannot drift from
`orion scan --help`, and every refusal is pinned. Jobs runs a REAL child (`python -c`) for output,
scrubbing, run-dir learning, exit codes and Busy. The stop paths (process groups, SIGINT/SIGKILL,
CTRL_BREAK) are POSIX- or Windows-only, so they run against a fake process with the jobs module's `os`
swapped for a recording fake (autouse below): nothing is ever really signalled, and both platforms are
covered on either one. The global `os.name` is never patched -- on Windows that breaks `pathlib.Path()`.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from orion import cli
from orion.ui import jobs
from orion.ui.jobs import OPTIONS, Busy, Jobs, OptionError, build_argv, exit_note, resume_argv

SCAN = jobs.SCAN_CMD


# ── fakes ─────────────────────────────────────────────────────────────────────────────────────────────
class FakeProc:
    """A Popen stand-in: `stdout` replays `lines`, wait() blocks until finish(). Signals are recorded
    and, per `exit_on` ({"interrupt": rc, "kill": rc}), end it the way a real child would."""

    def __init__(self, lines=(), *, pid=4242, exit_on=None, signal_error=None):
        self.pid = pid
        self.stdout = io.StringIO("".join(lines))
        self.returncode = None
        self.sent: list[tuple] = []
        self.exit_on = dict(exit_on or {})
        self.signal_error = signal_error
        self._done = threading.Event()

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self._done.wait(timeout)
        return self.returncode

    def finish(self, rc):
        if self.returncode is None:
            self.returncode = rc
        self._done.set()

    def deliver(self, sig):
        what = "kill" if sig == jobs._SIGKILL else "interrupt"
        if what in self.exit_on:
            self.finish(self.exit_on[what])

    def send_signal(self, sig):
        if self.signal_error is not None:
            raise self.signal_error
        self.sent.append(("signal", sig))
        self.deliver(sig)

    def kill(self):
        self.sent.append(("kill", None))
        self.deliver(jobs._SIGKILL)


class FakePopen:
    def __init__(self, *procs):
        self.queue = list(procs)
        self.calls: list[tuple] = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        return self.queue.pop(0)


class FakeOS:
    """The jobs module's `os` for one test: a chosen `name` and process-group calls that only record (and
    deliver to `group`). Everything else is the real os module."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.pgid = 4242
        self.own_pgrp = 1
        self.killpg_error: BaseException | None = None
        self.group: FakeProc | None = None
        self.calls: list[tuple] = []

    def getpgid(self, pid):
        self.calls.append(("getpgid", pid))
        return self.pgid

    def getpgrp(self):
        return self.own_pgrp

    def killpg(self, pgid, sig):
        if self.killpg_error is not None:
            raise self.killpg_error
        self.calls.append(("killpg", pgid, sig))
        if self.group is not None:
            self.group.deliver(sig)

    def __getattr__(self, attr):
        return getattr(os, attr)


@pytest.fixture(autouse=True)
def fake_os(monkeypatch):
    """Every test: no real process-group call can happen, whatever a test does by mistake."""
    fake = FakeOS(os.name)
    monkeypatch.setattr(jobs, "os", fake)
    return fake


@pytest.fixture
def procs():
    """FakeProc factory; every one is finished at teardown so no reader/waiter thread outlives the test."""
    made: list[FakeProc] = []

    def make(*args, **kwargs) -> FakeProc:
        proc = FakeProc(*args, **kwargs)
        made.append(proc)
        return proc

    yield make
    for proc in made:
        proc.finish(0)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    return path


@pytest.fixture
def model_dir(tmp_path: Path) -> Path:
    path = tmp_path / "orion-lm"
    path.mkdir()
    return path


def _until(pred, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return bool(pred())


def _cli(argv: list[str]) -> argparse.Namespace:
    """A built argv parsed by orion's real parser (argv minus `python -m orion`)."""
    assert argv[:len(SCAN)] == SCAN
    return cli._build_parser().parse_args(argv[len(SCAN) - 1:])


def _refused(req, fragment: str, **kwargs) -> None:
    with pytest.raises(OptionError) as err:
        build_argv(req, **kwargs)
    assert fragment in str(err.value)


def _start(js: Jobs, argv=("orion", "scan"), env=None, **kwargs):
    kw = {"kind": "scan", "repo": "/src/NodeGoat", "engine": "claude", **kwargs}
    return js.start(list(argv), env or {}, **kw)


# ── build_argv: happy paths ───────────────────────────────────────────────────────────────────────────
# Each option's ORION_* twin, pinned to the CLI default: what an untouched form sends.
PINS = {"ORION_VERIFY_ROUTING": "0", "ORION_VERIFY_CONTEXT": "0", "ORION_SHORTLISTS": "0",
        "ORION_SEMANTIC_MODE": "vector"}


def test_claude_defaults(repo):
    argv, env = build_argv({"repo": str(repo)})
    assert argv == [*SCAN, str(repo.resolve()), "--quiet"]
    assert env == {"ORION_MODEL": "sonnet", **PINS}
    args = _cli(argv)
    assert (args.repo, args.quiet, args.engine) == (str(repo.resolve()), True, "claude")


def test_claude_every_option_reaches_its_flag(repo):
    options = {"verify_routing": True, "verify_context": True, "shortlists": True, "semantic_mode": "hybrid",
               "fail_on": "inconclusive", "language": "pythonsrc", "runtime": True,
               "runtime_sandbox": "docker", "runtime_driver": "harness", "runtime_budget": 7}
    argv, env = build_argv({"repo": str(repo), "engine": "claude", "model": "opus", "options": options})
    assert argv[len(SCAN):] == [
        str(repo.resolve()), "--quiet", "--verify-routing", "--verify-context", "--shortlists",
        "--semantic-mode", "hybrid", "--fail-on", "inconclusive", "--language", "pythonsrc", "--runtime",
        "--runtime-sandbox", "docker", "--runtime-driver", "harness", "--runtime-budget", "7"]
    assert env == {"ORION_MODEL": "opus", "ORION_VERIFY_ROUTING": "1", "ORION_VERIFY_CONTEXT": "1",
                   "ORION_SHORTLISTS": "1", "ORION_SEMANTIC_MODE": "hybrid"}
    args = _cli(argv)
    assert (args.verify_routing, args.verify_context, args.shortlists) == (True, True, True)
    assert (args.semantic_mode, args.fail_on, args.language) == ("hybrid", "inconclusive", "pythonsrc")
    assert (args.runtime, args.runtime_sandbox, args.runtime_driver, args.runtime_budget) == \
        (True, "docker", "harness", 7)


def test_lm_engine_passes_the_model_dir_and_only_its_option_pins(repo, model_dir):
    options = {"shortlists": True, "fail_on": "confirm", "language": "jssrc", "lm_temperature": 0.7,
               "lm_verify": True, "runtime": True, "runtime_budget": 3}
    argv, env = build_argv({"repo": str(repo), "engine": "lm", "model": str(model_dir), "options": options},
                           lm_available=True)
    assert argv[len(SCAN):] == [
        str(repo.resolve()), "--quiet", "--engine", "lm", "--model", str(model_dir.resolve()), "--shortlists",
        "--fail-on", "confirm", "--language", "jssrc", "--lm-temperature", "0.7", "--lm-verify", "--runtime",
        "--runtime-budget", "3"]
    assert env == {"ORION_SHORTLISTS": "1"}            # no model env: the lm model is a dir in argv
    args = _cli(argv)
    assert (args.engine, args.lm_model, args.lm_temperature, args.lm_verify) == \
        ("lm", str(model_dir.resolve()), 0.7, True)


@pytest.mark.parametrize("value, word", [(0, "0.0"), (-0.0, "0.0"), (1, "1.0"), (2, "2.0"), (1.25, "1.25")])
def test_temperature_is_written_as_a_float(repo, model_dir, value, word):
    argv, _ = build_argv({"repo": str(repo), "engine": "lm", "model": str(model_dir),
                          "options": {"lm_temperature": value}}, lm_available=True)
    assert argv[-2:] == ["--lm-temperature", word]
    assert _cli(argv).lm_temperature == float(word)


def test_flags_follow_options_order_not_request_order(repo):
    opts = {"runtime_budget": 9, "runtime": True, "fail_on": "confirm", "verify_routing": True}
    forward, _ = build_argv({"repo": str(repo), "options": opts})
    backward, _ = build_argv({"repo": str(repo), "options": dict(reversed(list(opts.items())))})
    assert forward == backward
    assert forward.index("--verify-routing") < forward.index("--fail-on") < forward.index("--runtime") \
        < forward.index("--runtime-budget")


def test_false_empty_and_null_add_nothing(repo):
    opts = {"verify_routing": False, "shortlists": False, "language": "", "semantic_mode": None,
            "runtime": False, "runtime_budget": None}
    argv, _ = build_argv({"repo": str(repo), "engine": None, "model": None, "options": opts})
    assert argv == [*SCAN, str(repo.resolve()), "--quiet"]
    assert build_argv({"repo": str(repo), "options": None})[0] == argv


@pytest.mark.parametrize("model", [*jobs.MODELS, "claude-sonnet-5", "claude-opus-5[1m]",
                                   "us.anthropic.claude-5:0@v2/x"])
def test_claude_model_goes_to_env_never_argv(repo, model):
    argv, env = build_argv({"repo": str(repo), "model": model})
    assert env == {"ORION_MODEL": model, **PINS}
    assert model not in argv and "--model" not in argv


@pytest.mark.parametrize("model", [None, ""])
def test_claude_model_defaults_to_sonnet(repo, model):
    assert build_argv({"repo": str(repo), "model": model})[1] == {"ORION_MODEL": "sonnet", **PINS}


def test_scan_cmd_is_used_and_never_mutated(repo):
    argv, _ = build_argv({"repo": str(repo)}, scan_cmd=["fake-orion", "scan"])
    assert argv == ["fake-orion", "scan", str(repo.resolve()), "--quiet"]
    assert jobs.SCAN_CMD == [sys.executable, "-m", "orion", "scan"]


# ── build_argv: the repo and model paths ──────────────────────────────────────────────────────────────
def test_relative_repo_resolves_absolute(tmp_path, repo, monkeypatch):
    monkeypatch.chdir(tmp_path)
    argv, _ = build_argv({"repo": "repo"})
    assert argv[len(SCAN)] == str(repo.resolve())
    assert Path(argv[len(SCAN)]).is_absolute()


def test_relative_repo_resolves_against_the_cwd_argument(tmp_path, repo, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    argv, _ = build_argv({"repo": "repo"}, cwd=tmp_path)
    assert argv[len(SCAN)] == str(repo.resolve())
    _refused({"repo": "repo"}, "repo path not found or not a directory: repo")   # from elsewhere, no cwd


@pytest.mark.parametrize("name", ["-x", "--runtime", "--help"])
def test_a_repo_named_like_a_flag_stays_an_absolute_path(tmp_path, monkeypatch, name):
    (tmp_path / name).mkdir()
    monkeypatch.chdir(tmp_path)
    argv, _ = build_argv({"repo": name})
    word = argv[len(SCAN)]
    assert word == str((tmp_path / name).resolve()) and not word.startswith("-")
    args = _cli(argv)                                  # the CLI reads it as the repo, not as a flag
    assert args.repo == word and args.runtime is False


@pytest.mark.parametrize("value, fragment", [
    (None, "repo path is required"), ("", "repo path is required"), ("   ", "repo path is required"),
    (7, "repo path is required"), (["/src"], "repo path is required"),
    ("no/such/dir", "repo path not found or not a directory"),
    ("a\x00b", "repo path is not a usable path"), ("x" * 5000, "repo path is not a usable path"),
])
def test_repo_refusals(tmp_path, value, fragment):
    _refused({"repo": value}, fragment, cwd=tmp_path)


def test_a_missing_repo_or_a_file_is_refused(tmp_path):
    _refused({}, "repo path is required")
    (tmp_path / "f.txt").write_text("x")
    _refused({"repo": str(tmp_path / "f.txt")}, "not found or not a directory")


def test_lm_needs_lm_available(repo, model_dir):
    _refused({"repo": str(repo), "engine": "lm", "model": str(model_dir)}, "Orion-LM is not available")


@pytest.mark.parametrize("model, fragment", [
    (None, "Orion-LM model path is required"), ("", "Orion-LM model path is required"),
    ("missing-model", "Orion-LM model path not found or not a directory"), (3, "Orion-LM model path is required"),
])
def test_lm_model_must_be_an_existing_dir(tmp_path, repo, model, fragment):
    _refused({"repo": str(repo), "engine": "lm", "model": model}, fragment, lm_available=True, cwd=tmp_path)


def test_lm_relative_model_resolves_against_cwd(tmp_path, repo, model_dir):
    argv, _ = build_argv({"repo": "repo", "engine": "lm", "model": "orion-lm"}, lm_available=True, cwd=tmp_path)
    assert argv[argv.index("--model") + 1] == str(model_dir.resolve())
    (tmp_path / "model.bin").write_text("x")
    _refused({"repo": "repo", "engine": "lm", "model": "model.bin"}, "not a directory", lm_available=True,
             cwd=tmp_path)


@pytest.mark.parametrize("model", ["son net", "--dangerously-skip-permissions", "-x", "x" * 121, "sonnet\n",
                                   "sonnet;rm -rf", "$(id)", "sónnet", 5, ["sonnet"], True])
def test_bad_model_names_are_refused(repo, model):
    _refused({"repo": str(repo), "model": model}, "model must be")


# ── build_argv: every refusal ─────────────────────────────────────────────────────────────────────────
def test_request_level_refusals(repo):
    _refused([], "a scan request must be a JSON object")
    _refused("scan", "a scan request must be a JSON object")
    _refused({"repo": str(repo), "argv": ["--x"]}, "unknown field 'argv'")
    _refused({"repo": str(repo), "engine": "gpt"}, "engine must be one of: claude, lm")
    _refused({"repo": str(repo), "engine": ["claude"]}, "engine must be one of")
    _refused({"repo": str(repo), "options": ["--shortlists"]}, "options must be a JSON object")


BAD_CLAUDE_OPTIONS = [
    ({"harness_file": "/tmp/x.py"}, "unknown scan option 'harness_file'"),
    ({"use_dynamic": True}, "unknown scan option 'use_dynamic'"),
    ({"--runtime": True}, "unknown scan option"),
    ({"lm_temperature": 0.5}, "lm_temperature is not available with the claude engine"),
    ({"lm_verify": False}, "lm_verify is not available with the claude engine"),
    ({"verify_routing": "true"}, "verify_routing must be true or false"),
    ({"verify_routing": 1}, "verify_routing must be true or false"),
    ({"runtime": "yes"}, "runtime must be true or false"),
    ({"semantic_mode": "fuzzy"}, "semantic_mode must be one of: vector, hybrid, keyword"),
    ({"semantic_mode": 1}, "semantic_mode must be one of"),
    ({"fail_on": "CONFIRM"}, "fail_on must be one of: none, confirm, inconclusive"),
    ({"language": "JSSRC"}, "language must match"),
    ({"language": "js src"}, "language must match"),
    ({"language": "jssrc\n"}, "language must match"),
    ({"language": "a" * 21}, "language must match"),
    ({"language": "--x"}, "language must match"),
    ({"language": 5}, "language must be text"),
    ({"runtime_sandbox": "docker"}, "runtime_sandbox needs runtime turned on"),
    ({"runtime": False, "runtime_driver": "http"}, "runtime_driver needs runtime turned on"),
    ({"runtime": "true", "runtime_budget": 5}, "runtime must be true or false"),
    ({"runtime_budget": 5}, "runtime_budget needs runtime turned on"),
    ({"runtime": True, "runtime_sandbox": "vm"}, "runtime_sandbox must be one of: auto, docker, host"),
    ({"runtime": True, "runtime_driver": "ssh"}, "runtime_driver must be one of: auto, harness, http, process"),
    ({"runtime": True, "runtime_budget": True}, "runtime_budget must be a whole number from 1 to 10000"),
    ({"runtime": True, "runtime_budget": 5.0}, "runtime_budget must be a whole number"),
    ({"runtime": True, "runtime_budget": "5"}, "runtime_budget must be a whole number"),
    ({"runtime": True, "runtime_budget": 0}, "runtime_budget must be a whole number"),
    ({"runtime": True, "runtime_budget": 10001}, "runtime_budget must be a whole number"),
]


@pytest.mark.parametrize("options, fragment", BAD_CLAUDE_OPTIONS)
def test_claude_option_refusals(repo, options, fragment):
    _refused({"repo": str(repo), "engine": "claude", "options": options}, fragment)


BAD_LM_OPTIONS = [
    ({"verify_routing": True}, "verify_routing is not available with the lm engine"),
    ({"semantic_mode": "vector"}, "semantic_mode is not available with the lm engine"),
    ({"lm_temperature": -0.1}, "lm_temperature must be a number from 0 to 2"),
    ({"lm_temperature": 2.01}, "lm_temperature must be a number from 0 to 2"),
    ({"lm_temperature": True}, "lm_temperature must be a number from 0 to 2"),
    ({"lm_temperature": "0.5"}, "lm_temperature must be a number from 0 to 2"),
    ({"lm_temperature": float("nan")}, "lm_temperature must be a number from 0 to 2"),
    ({"lm_temperature": float("inf")}, "lm_temperature must be a number from 0 to 2"),
    ({"lm_temperature": 10 ** 400}, "lm_temperature must be a number from 0 to 2"),
    ({"lm_verify": "on"}, "lm_verify must be true or false"),
]


@pytest.mark.parametrize("options, fragment", BAD_LM_OPTIONS)
def test_lm_option_refusals(repo, model_dir, options, fragment):
    _refused({"repo": str(repo), "engine": "lm", "model": str(model_dir), "options": options}, fragment,
             lm_available=True)


def test_an_unknown_option_name_is_quoted_short(repo):
    with pytest.raises(OptionError) as err:
        build_argv({"repo": str(repo), "options": {"x" * 500: True}})
    assert len(str(err.value)) < 120


# ── OPTIONS is the CLI's own flags ────────────────────────────────────────────────────────────────────
def _scan_actions() -> dict[str, argparse.Action]:
    parser = cli._build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    return {flag: action for action in sub.choices["scan"]._actions for flag in action.option_strings}


def test_options_are_exactly_the_specified_set():
    assert list(OPTIONS) == ["verify_routing", "verify_context", "shortlists", "semantic_mode", "fail_on",
                             "language", "lm_temperature", "lm_verify", "runtime", "runtime_sandbox",
                             "runtime_driver", "runtime_budget"]
    assert [n for n, s in OPTIONS.items() if s.get("danger")] == ["runtime"]
    assert [n for n, s in OPTIONS.items() if s.get("requires")] == ["runtime_sandbox", "runtime_driver",
                                                                    "runtime_budget"]
    assert {n: s["engines"] for n, s in OPTIONS.items() if s["engines"] != ["claude", "lm"]} == {
        "verify_routing": ["claude"], "verify_context": ["claude"], "semantic_mode": ["claude"],
        "lm_temperature": ["lm"], "lm_verify": ["lm"]}


@pytest.mark.parametrize("name", list(OPTIONS))
def test_every_option_matches_the_cli_parser(name):
    spec = OPTIONS[name]
    action = _scan_actions()[spec["flag"]]
    kind = spec["kind"]
    assert kind in ("bool", "choice", "float", "int", "text")
    if kind == "bool":
        assert isinstance(action, argparse._StoreTrueAction) and spec["default"] is False
    elif kind == "choice":
        assert list(action.choices) == spec["choices"] and spec["default"] in spec["choices"]
    elif kind in ("int", "float"):
        assert action.type is {"int": int, "float": float}[kind]
        assert spec["min"] <= action.default <= spec["max"] and spec["default"] == action.default
    else:
        assert action.choices is None and action.nargs is None and spec["default"] == ""
    if action.default is not None and kind != "text":
        assert spec["default"] == action.default
    assert isinstance(spec["label"], str) and spec["label"] and isinstance(spec["help"], str) and spec["help"]
    assert spec["engines"] and set(spec["engines"]) <= set(jobs.ENGINES)
    if spec.get("requires"):
        assert OPTIONS[spec["requires"]]["kind"] == "bool"


@pytest.mark.parametrize("name", list(OPTIONS))
def test_every_option_default_is_accepted(repo, model_dir, name):
    spec = OPTIONS[name]
    engine = spec["engines"][0]
    options = {name: spec["default"]}
    if spec.get("requires"):
        options[spec["requires"]] = True
    argv, _ = build_argv({"repo": str(repo), "engine": engine,
                          "model": str(model_dir) if engine == "lm" else None, "options": options},
                         lm_available=True)
    _cli(argv)


def test_the_option_spec_is_json_for_the_page():
    data = json.loads(json.dumps({"options": OPTIONS, "models": jobs.MODELS}))
    assert data["models"] == ["sonnet", "opus", "haiku"]
    assert data["options"]["runtime"]["danger"] is True
    # The page shows its own red warning for a danger option (app.js RUNTIME_WARNING); the help must not repeat it.
    assert "exercise the target" in data["options"]["runtime"]["help"] and "warning" not in data["options"]["runtime"]
    assert "Runs the target repo's own code" not in data["options"]["runtime"]["help"]
    assert all(jobs.MODEL_RE.fullmatch(m) for m in jobs.MODELS)


# ── resume_argv ───────────────────────────────────────────────────────────────────────────────────────
def test_resume_argv(tmp_path):
    run = tmp_path / ".orion" / "runs" / "s" / "20261029T000000Z"
    argv = resume_argv(run)
    assert argv == [*SCAN, "--resume", str(run), "--quiet"]
    args = _cli(argv)
    assert (args.resume, args.quiet, args.repo) == (str(run), True, None)
    assert resume_argv(run, scan_cmd=["x"]) == ["x", "--resume", str(run), "--quiet"]


@pytest.mark.parametrize("run", ["-x", ".orion/runs/s/t"])
def test_resume_argv_needs_an_absolute_dir(run):
    with pytest.raises(ValueError):
        resume_argv(Path(run))


# ── exit notes ────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("rc, kw, note", [
    (None, {}, None),
    (0, {}, "finished (exit 0)"),
    (0, {"stop_count": 1}, "finished after the stop request (exit 0)"),
    (1, {"fail_on": True}, "finished; a verdict hit the --fail-on level (exit 1)"),
    (1, {}, "failed with an error, see the scan output (exit 1)"),
    (2, {}, "refused: usage error, see the scan output (exit 2)"),
    (3, {}, "incomplete: some Orion-LM batches were not judged (exit 3)"),
    (-2, {"stop_count": 1}, "stopped (interrupted)"),
    (-2, {}, "interrupted (SIGINT)"),
    (130, {"stop_count": 1}, "stopped (interrupted)"),
    (130, {}, "interrupted (exit 130)"),
    (0xC000013A, {"stop_count": 1}, "stopped (interrupted)"),               # Windows, after CTRL_BREAK
    (0xC000013A - (1 << 32), {"stop_count": 1}, "stopped (interrupted)"),   # the same, signed
    (1, {"stop_count": 2, "forced": True}, "stopped (force-killed)"),        # Windows TerminateProcess
    (-9, {"stop_count": 2, "forced": True}, "stopped (force-killed)"),       # POSIX SIGKILL
    (0, {"stop_count": 2, "forced": True}, "finished after the stop request (exit 0)"),
    (-9, {}, "ended by signal SIGKILL"),
    (-15, {}, "ended by signal SIGTERM"),
    (7, {}, "exited with 7"),
])
def test_exit_note(rc, kw, note):
    assert exit_note(rc, **kw) == note


@pytest.mark.parametrize("argv, expected", [
    (["--fail-on", "confirm"], True), (["--fail-on", "inconclusive"], True), (["--fail-on=confirm"], True),
    (["--fail-on", "none"], False), (["--fail-on"], False), (["--resume", "/r", "--quiet"], False),
])
def test_fail_on_is_read_from_argv(argv, expected):
    assert jobs._fail_on_given(argv) is expected


# ── Jobs with a fake process: spawn contract, output, run dir ─────────────────────────────────────────
@pytest.mark.parametrize("osname", ["posix", "nt"])
def test_start_spawns_in_its_own_group_with_a_copied_env(tmp_path, fake_os, procs, osname):
    fake_os.name = osname
    popen = FakePopen(procs())
    env = {"A": "1", "PYTHONIOENCODING": "latin-1"}
    js = Jobs(cwd=tmp_path / "x" / "..", popen=popen)
    assert js.cwd == tmp_path.resolve()
    _start(js, ["orion", "scan", "x"], env)
    (argv, kw), = popen.calls
    assert argv == ["orion", "scan", "x"]
    assert kw["cwd"] == str(tmp_path.resolve())
    assert kw["env"] == {"A": "1", "PYTHONIOENCODING": "latin-1", "PYTHONUNBUFFERED": "1"}   # caller's wins
    assert env == {"A": "1", "PYTHONIOENCODING": "latin-1"}                                  # not mutated
    assert (kw["stdin"], kw["stdout"], kw["stderr"]) == (subprocess.DEVNULL, subprocess.PIPE, subprocess.STDOUT)
    assert (kw["text"], kw["encoding"], kw["errors"], kw["bufsize"]) == (True, "utf-8", "replace", 1)
    if osname == "posix":
        assert kw["start_new_session"] is True and "creationflags" not in kw
    else:
        assert kw["creationflags"] == getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
        assert "start_new_session" not in kw


def test_start_refuses_a_bad_kind_engine_or_argv(tmp_path):
    js = Jobs(cwd=tmp_path, popen=FakePopen())
    for kw in ({"kind": "rescan"}, {"engine": "gpt"}):
        with pytest.raises(ValueError):
            _start(js, **kw)
    for argv in ([], [1, 2]):
        with pytest.raises(ValueError):
            js.start(argv, {}, kind="scan", repo=None, engine="claude")
    assert js.current() is None and not js.running()


def test_a_spawn_failure_registers_no_job(tmp_path, procs):
    def broken(argv, **kwargs):
        raise FileNotFoundError("no such interpreter")

    js = Jobs(cwd=tmp_path, popen=broken)
    with pytest.raises(FileNotFoundError):
        _start(js)
    assert js.current() is None and not js.running()
    js._popen = FakePopen(procs())
    assert _start(js).state == "running"


def test_one_scan_at_a_time(tmp_path, fake_os, procs):
    fake_os.name = "posix"
    first, second = procs(), procs()
    js = Jobs(cwd=tmp_path, popen=FakePopen(first, second))
    job = _start(js, repo="/src/NodeGoat")
    with pytest.raises(Busy, match="already running.*NodeGoat"):
        _start(js, repo="/src/other")
    assert js.running() and js.current() is job
    js.stop()
    with pytest.raises(Busy, match="already stopping"):
        _start(js)
    first.finish(-2)
    assert job.wait(5)
    assert not js.running() and js.current() is job          # the last job stays for display
    nxt = _start(js, kind="resume", repo=None)
    assert js.current() is nxt and nxt.id != job.id and nxt.kind == "resume"


def test_output_keeps_the_last_2000_lines(tmp_path, procs):
    proc = procs([f"line {i}\n" for i in range(2500)])
    js = Jobs(cwd=tmp_path, popen=FakePopen(proc))
    job = _start(js)
    proc.finish(0)
    assert job.wait(5)
    lines, nxt = job.output()
    assert nxt == 2500 and len(lines) == jobs.OUTPUT_LINES == 2000
    assert (lines[0], lines[-1]) == ("line 500", "line 2499")
    assert job.output(2490) == ([f"line {i}" for i in range(2490, 2500)], 2500)
    assert job.output(2500) == ([], 2500) and job.output(9999) == ([], 2500)
    assert job.output(-3)[0][0] == "line 500" and job.output("junk")[1] == 2500


def test_output_is_safe_to_read_while_lines_stream_in(tmp_path, procs):
    proc = procs([f"line {i}\n" for i in range(3000)])
    js = Jobs(cwd=tmp_path, popen=FakePopen(proc))
    seen: list[int] = []
    errors: list[BaseException] = []
    job = _start(js)

    def poll() -> None:
        try:
            since = 0
            while not job.wait(0):
                lines, since = job.output(since)
                seen.append(since)
        except BaseException as exc:  # noqa: BLE001 -- surfaced by the assert below
            errors.append(exc)

    reader = threading.Thread(target=poll)
    reader.start()
    proc.finish(0)
    reader.join(10)
    assert not errors and seen == sorted(seen)
    assert job.output(0)[1] == 3000


def test_lines_are_stripped_scrubbed_then_capped(tmp_path, procs):
    secret = "Zq9Zq9Zq9Zq9secret"
    edge = "e" * (jobs._LINE_MAX - 5) + secret + "tail"         # the secret straddles the cut
    big = "k" * (jobs._LINE_MAX + 50)
    proc = procs(["windows line\r\n", f"{secret} in a line\n", edge + "\n", big + "\n", "no newline at EOF"])
    js = Jobs(cwd=tmp_path, scrub=lambda s: s.replace(secret, "[redacted]"), popen=FakePopen(proc))
    job = _start(js)
    proc.finish(0)
    assert job.wait(5)
    lines, _ = job.output()
    assert lines[0] == "windows line"
    assert lines[1] == "[redacted] in a line"
    assert "Zq9" not in lines[2]
    assert lines[3] == "k" * jobs._LINE_MAX + " ... [50 more characters]"
    assert lines[4] == "no newline at EOF"


@pytest.mark.parametrize("scrub", [lambda s: 1 / 0, lambda s: None])
def test_a_broken_scrubber_withholds_rather_than_leaks(tmp_path, procs, scrub):
    proc = procs(["sk-live-secret-0123456789\n"])
    js = Jobs(cwd=tmp_path, scrub=scrub, popen=FakePopen(proc))
    job = _start(js, ["orion", "--x=sk-live-secret-0123456789"])
    proc.finish(0)
    assert job.wait(5)
    assert job.output() == ([jobs._WITHHELD], 1)
    assert "sk-live" not in json.dumps(job.public())


def test_the_childs_own_key_is_masked_even_when_the_scrubber_no_longer_knows_it(tmp_path, procs):
    key = "sk-ant-api03-OLDKEY-0123456789"
    proc = procs([f"api error for key {key}\n", "auth ollama\n"])
    js = Jobs(cwd=tmp_path, popen=FakePopen(proc))                     # identity scrubber
    job = _start(js, env={"ANTHROPIC_API_KEY": key, "ANTHROPIC_AUTH_TOKEN": "ollama"})
    proc.finish(0)
    assert job.wait(5)
    assert job.output()[0] == ["api error for key [redacted]", "auth ollama"]   # short dummy token stays


@pytest.mark.parametrize("line, rel", [
    ("run log: .orion/runs/s-1/20261029T000000Z/progress.jsonl", ".orion/runs/s-1/20261029T000000Z"),
    ("run log: .orion\\runs\\s-1\\20261029T000000Z/progress.jsonl", ".orion\\runs\\s-1\\20261029T000000Z"),
])
def test_scan_id_and_run_dir_come_from_the_clis_own_lines(tmp_path, procs, line, rel):
    proc = procs(["note: warming up\n", "  scan_id: not-this\n", "scan_id: s-1\n", line + "\n",
                  "scan_id: later\n", "run log: elsewhere/progress.jsonl\n"])
    js = Jobs(cwd=tmp_path, popen=FakePopen(proc))
    job = _start(js)
    assert _until(lambda: len(job.output()[0]) == 6)
    assert job.scan_id == "s-1"                                     # the first CLI line of each wins
    assert job.run_dir == (tmp_path.resolve() / rel).resolve()


def test_a_resumed_runs_absolute_dir_is_kept(tmp_path, procs):
    run = tmp_path / "abs" / "s" / "t"
    proc = procs([f"run log: {run}/progress.jsonl\n", "run log: progress.jsonl\n"])
    js = Jobs(cwd=tmp_path / "abs", popen=FakePopen(proc))
    job = _start(js, kind="resume")
    assert _until(lambda: job.run_dir is not None)
    assert job.run_dir == run.resolve()


def test_an_unrepresentable_run_dir_is_ignored_not_fatal(tmp_path, procs):
    proc = procs(["run log: a\x00b/progress.jsonl\n", "still read\n"])
    js = Jobs(cwd=tmp_path, popen=FakePopen(proc))
    job = _start(js)
    proc.finish(0)
    assert job.wait(5)
    assert job.run_dir is None and job.output()[0][-1] == "still read"


def test_public_is_json_and_names_the_run(tmp_path, procs):
    proc = procs(["scan_id: s\n", "run log: .orion/runs/s/t/progress.jsonl\n"])
    js = Jobs(cwd=tmp_path, scrub=lambda s: s.replace("hunter2hunter2", "[redacted]"), popen=FakePopen(proc))
    job = _start(js, ["orion", "scan", "--x=hunter2hunter2"], engine="lm")
    assert _until(lambda: job.run_dir is not None)
    asked: list[Path] = []
    pub = job.public(lambda p: asked.append(p) or "s~t")
    assert set(pub) == {"id", "kind", "argv", "repo", "engine", "model", "started", "state", "returncode",
                        "stop_count", "scan_id", "run_dir", "run_id", "exit_note"}
    json.dumps(pub)
    assert (pub["run_id"], pub["run_dir"], pub["scan_id"]) == ("s~t", str(job.run_dir), "s")
    assert asked == [job.run_dir]
    assert pub["argv"] == ["orion", "scan", "--x=[redacted]"] and job.argv[-1] == "--x=hunter2hunter2"
    assert (pub["state"], pub["returncode"], pub["exit_note"], pub["engine"]) == ("running", None, None, "lm")
    assert datetime.datetime.fromisoformat(pub["started"]).tzinfo is not None
    assert job.public()["run_id"] is None

    def outside(path):
        raise ValueError("not under the runs root")

    assert job.public(outside)["run_id"] is None


def test_active_dirs_names_only_a_live_job_with_a_known_dir(tmp_path, fake_os, procs):
    fake_os.name = "posix"
    quiet, proc = procs(), procs(["run log: .orion/runs/s/t/progress.jsonl\n"])
    js = Jobs(cwd=tmp_path, popen=FakePopen(quiet, proc))
    assert js.active_dirs() == {}
    first = _start(js)
    assert js.active_dirs() == {}                                   # running, but no run dir known yet
    quiet.finish(0)
    assert first.wait(5)
    job = _start(js)
    assert _until(lambda: job.run_dir is not None)
    key = str((tmp_path / ".orion" / "runs" / "s" / "t").resolve())
    assert js.active_dirs() == {key: "running"}
    js.stop()
    assert js.active_dirs() == {key: "stopping"}
    proc.finish(-2)
    assert job.wait(5)
    assert js.active_dirs() == {}


def test_exit_1_reads_as_a_fail_on_hit_only_when_fail_on_was_asked(tmp_path, repo, procs):
    p1, p2 = procs(), procs()
    js = Jobs(cwd=tmp_path, popen=FakePopen(p1, p2))
    argv, _ = build_argv({"repo": str(repo), "options": {"fail_on": "confirm"}})
    job = _start(js, argv)
    p1.finish(1)
    assert job.wait(5)
    assert job.public()["exit_note"] == "finished; a verdict hit the --fail-on level (exit 1)"
    job = _start(js, resume_argv(tmp_path), kind="resume")
    p2.finish(1)
    assert job.wait(5)
    assert job.public()["exit_note"] == "failed with an error, see the scan output (exit 1)"


# ── stop / shutdown: POSIX and Windows paths, faked ───────────────────────────────────────────────────
def test_stop_posix_interrupts_the_group_then_kills_it(tmp_path, fake_os, procs):
    fake_os.name = "posix"
    proc = procs(pid=4242)
    js = Jobs(cwd=tmp_path, popen=FakePopen(proc))
    job = _start(js)
    first = js.stop(rid_for=lambda p: None)
    assert first["signal"] == "interrupt"
    assert (first["job"]["state"], first["job"]["stop_count"]) == ("stopping", 1)
    assert fake_os.calls == [("getpgid", 4242), ("killpg", 4242, signal.SIGINT)]
    second = js.stop()
    assert (second["signal"], second["job"]["stop_count"]) == ("kill", 2)
    assert fake_os.calls[-1] == ("killpg", 4242, jobs._SIGKILL)
    assert proc.sent == []                              # the GROUP is signalled, never just the CLI
    proc.finish(-9)
    assert job.wait(5)
    pub = job.public()
    assert (pub["state"], pub["returncode"], pub["exit_note"]) == ("exited", -9, "stopped (force-killed)")
    with pytest.raises(RuntimeError, match="no scan is running"):
        js.stop()


def test_stop_posix_on_an_already_gone_group_is_not_an_error(tmp_path, fake_os, procs):
    fake_os.name = "posix"
    fake_os.killpg_error = ProcessLookupError()
    js = Jobs(cwd=tmp_path, popen=FakePopen(procs()))
    _start(js)
    out = js.stop()
    assert out["signal"] == "interrupt" and out["job"]["state"] == "stopping"


def test_a_failed_signal_leaves_the_job_untouched(tmp_path, fake_os, procs):
    fake_os.name = "posix"
    fake_os.killpg_error = PermissionError("EPERM")
    js = Jobs(cwd=tmp_path, popen=FakePopen(procs()))
    job = _start(js)
    with pytest.raises(PermissionError):
        js.stop()
    assert (job.state, job.stop_count) == ("running", 0)


def test_stop_posix_never_signals_the_uis_own_group(tmp_path, fake_os, procs):
    fake_os.name = "posix"
    fake_os.pgid = fake_os.own_pgrp = 77
    proc = procs()
    js = Jobs(cwd=tmp_path, popen=FakePopen(proc))
    _start(js)
    js.stop()
    assert not [c for c in fake_os.calls if c[0] == "killpg"]
    assert proc.sent == [("signal", signal.SIGINT)]


@pytest.mark.parametrize("osname", ["posix", "nt"])
def test_a_child_that_already_exited_is_not_signalled(fake_os, osname):
    fake_os.name = osname
    proc = FakeProc()
    proc.returncode = 0
    assert jobs._send_stop(proc, graceful=True) == "interrupt"
    assert jobs._send_stop(proc, graceful=False) == "kill"
    assert fake_os.calls == [] and proc.sent == []


def test_stop_windows_sends_ctrl_break_then_kills(tmp_path, fake_os, procs):
    fake_os.name = "nt"
    proc = procs()
    js = Jobs(cwd=tmp_path, popen=FakePopen(proc))
    job = _start(js)
    assert js.stop()["signal"] == "interrupt"
    assert proc.sent == [("signal", jobs._CTRL_BREAK)]
    assert js.stop()["signal"] == "kill"
    assert proc.sent[-1] == ("kill", None) and fake_os.calls == []
    proc.finish(1)                                      # TerminateProcess exits 1
    assert job.wait(5)
    assert job.public()["exit_note"] == "stopped (force-killed)"      # not a --fail-on hit, not a crash


def test_stop_windows_ctrl_break_ends_the_scan_as_interrupted(tmp_path, fake_os, procs):
    fake_os.name = "nt"
    proc = procs(exit_on={"interrupt": 0xC000013A})
    js = Jobs(cwd=tmp_path, popen=FakePopen(proc))
    job = _start(js)
    js.stop()
    assert job.wait(5)
    assert job.public()["exit_note"] == "stopped (interrupted)"


def test_stop_windows_without_a_console_kills_at_once(tmp_path, fake_os, procs):
    fake_os.name = "nt"
    proc = procs(signal_error=OSError(6, "The handle is invalid"))
    js = Jobs(cwd=tmp_path, popen=FakePopen(proc))
    job = _start(js)
    out = js.stop()
    assert out["signal"] == "kill" and proc.sent == [("kill", None)]
    proc.finish(1)
    assert job.wait(5)
    assert job.public()["exit_note"] == "stopped (force-killed)"


def test_stop_without_a_job_is_refused(tmp_path):
    with pytest.raises(RuntimeError, match="no scan is running"):
        Jobs(cwd=tmp_path).stop()


def _group_signals(fake_os: FakeOS) -> list:
    return [c[2] for c in fake_os.calls if c[0] == "killpg"]


def test_shutdown_interrupts_and_waits(tmp_path, fake_os, procs):
    fake_os.name = "posix"
    proc = procs(exit_on={"interrupt": -2})
    fake_os.group = proc
    js = Jobs(cwd=tmp_path, popen=FakePopen(proc))
    job = _start(js)
    js.shutdown(timeout=5)
    assert _group_signals(fake_os) == [signal.SIGINT]
    assert (job.state, job.public()["exit_note"]) == ("exited", "stopped (interrupted)")


def test_shutdown_kills_a_scan_that_ignores_the_interrupt(tmp_path, fake_os, procs):
    fake_os.name = "posix"
    proc = procs(exit_on={"kill": -9})
    fake_os.group = proc
    js = Jobs(cwd=tmp_path, popen=FakePopen(proc))
    job = _start(js)
    began = time.monotonic()
    js.shutdown(timeout=0.3)
    assert time.monotonic() - began < 5
    assert _group_signals(fake_os) == [signal.SIGINT, jobs._SIGKILL]
    assert job.wait(5) and job.returncode == -9


def test_shutdown_after_a_user_stop_only_kills(tmp_path, fake_os, procs):
    fake_os.name = "posix"
    proc = procs(exit_on={"kill": -9})
    fake_os.group = proc
    js = Jobs(cwd=tmp_path, popen=FakePopen(proc))
    job = _start(js)
    js.stop()                                           # the user's first press
    js.shutdown(timeout=0.2)
    assert _group_signals(fake_os) == [signal.SIGINT, jobs._SIGKILL]   # no second interrupt
    assert job.wait(5) and job.returncode == -9


def test_shutdown_with_nothing_running_does_nothing(tmp_path, fake_os, procs):
    fake_os.name = "posix"
    js = Jobs(cwd=tmp_path, popen=FakePopen(procs()))
    js.shutdown(timeout=0.1)
    job = _start(js)
    job._proc.finish(0)
    assert job.wait(5)
    js.shutdown(timeout=0.1)
    assert fake_os.calls == []


# ── Jobs with a REAL child ────────────────────────────────────────────────────────────────────────────
CHILD = r'''
import hashlib, os, sys, time
flag, code = sys.argv[1], int(sys.argv[2])
print("warming up", flush=True)
print("scan_id: s-real", flush=True)
run = os.path.join(".orion", "runs", "s-real", "20261029T000000Z")
os.makedirs(run, exist_ok=True)
print("run log: " + run + "/progress.jsonl", flush=True)
print("cwd " + os.getcwd(), flush=True)
print("fake secret " + os.environ.get("FAKE_SECRET", ""), flush=True)
key = os.environ.get("ANTHROPIC_API_KEY", "")
print("own key " + key, flush=True)
print("key sha " + hashlib.sha256(key.encode()).hexdigest(), flush=True)
print("unicode naïve ✓", flush=True)
sys.stdout.buffer.write(b"crlf line\r\n")
sys.stdout.flush()
deadline = time.monotonic() + 60
while flag != "-" and not os.path.exists(flag) and time.monotonic() < deadline:
    time.sleep(0.02)
sys.exit(code)
'''

UNFLUSHED = r'''
import os, sys, time
print("run log: .orion/runs/u/t/progress.jsonl")
deadline = time.monotonic() + 60
while not os.path.exists(sys.argv[1]) and time.monotonic() < deadline:
    time.sleep(0.02)
'''


def _child_env(**extra: str) -> dict[str, str]:
    """The test's own env minus what Jobs.start must supply itself (and any real key)."""
    drop = {"PYTHONUNBUFFERED", "PYTHONIOENCODING", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"}
    return {**{k: v for k, v in os.environ.items() if k.upper() not in drop}, **extra}


def test_a_real_child_end_to_end(tmp_path):
    flag = tmp_path / "release"
    secret = "FAKE-sk-ant-api03-SECRET-0123456789"
    key = "sk-ant-api03-OWNKEY-9876543210abcdef"
    js = Jobs(cwd=tmp_path, scrub=lambda s: s.replace(secret, "[redacted]"))
    argv = [sys.executable, "-c", CHILD, str(flag), "3"]
    job = js.start(argv, _child_env(FAKE_SECRET=secret, ANTHROPIC_API_KEY=key), kind="scan",
                   repo=str(tmp_path), engine="claude")
    run = (tmp_path / ".orion" / "runs" / "s-real" / "20261029T000000Z").resolve()
    try:
        assert _until(lambda: job.run_dir is not None)
        assert job.run_dir == run and run.is_dir() and run.is_relative_to(tmp_path.resolve())
        assert job.scan_id == "s-real"
        assert js.running() and js.active_dirs() == {str(run): "running"}
        with pytest.raises(Busy):
            js.start(argv, _child_env(), kind="scan", repo="again", engine="claude")
    finally:
        flag.touch()
    assert job.wait(30)
    pub = job.public(lambda p: "s-real~20261029T000000Z" if p == run else None)
    assert (pub["state"], pub["returncode"], pub["run_id"]) == ("exited", 3, "s-real~20261029T000000Z")
    assert pub["exit_note"] == "incomplete: some Orion-LM batches were not judged (exit 3)"
    lines, nxt = job.output()
    assert nxt == len(lines) == 9
    assert lines[:2] == ["warming up", "scan_id: s-real"]
    cwd_line = next(line for line in lines if line.startswith("cwd "))
    assert Path(cwd_line[4:]).resolve() == tmp_path.resolve()          # the child ran in Jobs.cwd
    assert "fake secret [redacted]" in lines and "own key [redacted]" in lines
    assert f"key sha {hashlib.sha256(key.encode()).hexdigest()}" in lines    # the child did get the key
    assert "unicode naïve ✓" in lines and lines[-1] == "crlf line"
    blob = json.dumps({"lines": lines, "job": pub, "dirs": js.active_dirs()})
    assert secret not in blob and key not in blob
    assert not js.running() and js.active_dirs() == {}

    again = js.start([sys.executable, "-c", CHILD, "-", "0"], _child_env(), kind="scan", repo=None,
                     engine="claude")
    assert again.wait(30)
    assert again.public()["exit_note"] == "finished (exit 0)"


def test_the_run_dir_arrives_while_the_child_runs_even_unflushed(tmp_path):
    flag = tmp_path / "release"
    js = Jobs(cwd=tmp_path)
    job = js.start([sys.executable, "-c", UNFLUSHED, str(flag)], _child_env(), kind="scan", repo=None,
                   engine="claude")
    try:
        assert _until(lambda: job.run_dir is not None)     # PYTHONUNBUFFERED, supplied by Jobs.start
        assert job.state == "running"
    finally:
        flag.touch()
    assert job.wait(30) and job.returncode == 0


@pytest.mark.skipif(os.name != "posix", reason="sessions are POSIX; the Windows group flag is pinned by the "
                                                "spawn-kwargs test")
def test_a_real_child_leads_its_own_process_group(tmp_path):
    """So Stop's killpg(getpgid(pid)) reaches the scan and its children, never the UI's own group."""
    js = Jobs(cwd=tmp_path)
    code = "import os; print('own group', os.getpgrp() == os.getpid(), os.getsid(0) == os.getpid())"
    job = js.start([sys.executable, "-c", code], _child_env(), kind="scan", repo=None, engine="claude")
    assert job.wait(30)
    assert job.output()[0] == ["own group True True"]


@pytest.mark.parametrize("code, note", [(0, "finished (exit 0)"), (2, "refused: usage error, see the scan "
                                                                       "output (exit 2)")])
def test_a_real_childs_exit_code_is_recorded(tmp_path, code, note):
    js = Jobs(cwd=tmp_path)
    job = js.start([sys.executable, "-c", f"import sys; print('bye'); sys.exit({code})"], _child_env(),
                   kind="scan", repo=None, engine="claude")
    assert job.wait(30)
    assert (job.returncode, job.output(), job.public()["exit_note"]) == (code, (["bye"], 1), note)


def test_an_unknown_home_is_a_refusal_not_a_crash(monkeypatch, tmp_path):
    """POSIX: `~nobody/repo` makes Path.expanduser raise RuntimeError; that is a 400 'not found', never a 500."""
    class NoHome(type(Path())):
        def expanduser(self):
            raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(jobs, "Path", NoHome)
    with pytest.raises(OptionError, match="not found"):
        build_argv({"repo": "~nobody/repo"}, cwd=tmp_path)


def test_resume_argv_repeats_only_the_recorded_verify_options(tmp_path):
    run = tmp_path / "run"
    argv = resume_argv(run, scan_cmd=["orion", "scan"], options={
        "verify_routing": True, "verify_context": False, "semantic_mode": "hybrid", "fail_on": "confirm"})
    assert argv == ["orion", "scan", "--resume", str(run), "--quiet", "--verify-routing", "--semantic-mode",
                    "hybrid", "--fail-on", "confirm"]
    for bad in ({"runtime": True}, {"shortlists": True}, {"semantic_mode": "--evil"}, {"fail_on": "always"}):
        with pytest.raises(OptionError):
            resume_argv(run, options=bad)


def test_a_windows_kill_ends_the_whole_process_tree(fake_os, monkeypatch):
    """TerminateProcess (Popen.kill) would end the scan's python alone and leave its claude.exe children running."""
    fake_os.name = "nt"
    trees: list[int] = []
    monkeypatch.setattr(jobs, "_taskkill_tree", lambda pid: trees.append(pid) or True)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        kills: list[str] = []
        monkeypatch.setattr(child, "kill", lambda: kills.append("kill"))
        assert jobs._send_stop(child, graceful=False) == "kill"
        assert trees == [child.pid] and kills == []
        monkeypatch.setattr(jobs, "_taskkill_tree", lambda pid: False)      # taskkill failed: TerminateProcess
        assert jobs._send_stop(child, graceful=False) == "kill" and kills == ["kill"]
    finally:
        subprocess.Popen.kill(child)
        child.wait(30)


def test_env_pins_follow_the_page_not_the_inherited_environment(repo):
    """An ORION_* default the UI inherited must not turn on what the page shows off, nor the reverse."""
    assert jobs.env_pins({}, "claude") == PINS
    assert jobs.env_pins({"verify_routing": True, "semantic_mode": "keyword", "verify_context": None}, "claude") == {
        **PINS, "ORION_VERIFY_ROUTING": "1", "ORION_SEMANTIC_MODE": "keyword"}
    assert jobs.env_pins({"shortlists": True}, "lm") == {"ORION_SHORTLISTS": "1"}   # only the lm engine's options
    _, env = build_argv({"repo": str(repo), "engine": "lm", "model": str(repo), "options": {}}, lm_available=True)
    assert env == {"ORION_SHORTLISTS": "0"}


def _gone_or_zombie(pid: int) -> bool:
    """POSIX, portably (Linux and macOS): the process has exited, killed but not yet reaped counting as exited."""
    out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return out == "" or out.startswith("Z")


@pytest.mark.skipif(os.name != "posix" or shutil.which("ps") is None, reason="POSIX process sessions, and ps")
def test_a_force_stop_also_ends_what_left_the_group(fake_os):
    """The runtime target runs in a session of its own (so its stop() reaches npm's node child): a kill to the
    scan's group misses it, so the tree is read first and every stray ends too."""
    script = ("import subprocess, sys, time\n"
              "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)\n"
              "print(g.pid, flush=True)\n"
              "time.sleep(60)\n")
    child = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True,
                             start_new_session=True)
    try:
        grandchild = int(child.stdout.readline())
        assert grandchild in jobs._descendants(child.pid)
        assert jobs._send_stop(child, graceful=False) == "kill"
        assert ("killpg", fake_os.pgid, jobs._SIGKILL) in fake_os.calls        # the group (faked here)
        deadline = time.monotonic() + 10
        while not _gone_or_zombie(grandchild):                                 # ...and the stray, for real
            if time.monotonic() > deadline:
                pytest.fail("the grandchild outside the group is still running")
            time.sleep(0.05)
    finally:
        child.kill()
        child.wait(30)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows job objects")
def test_a_windows_job_ends_a_grandchild_in_its_own_process_group():
    """The first Stop's Ctrl-Break ends the scan's python at once, and the runtime target sits in a process
    group of its own: only the job (every descendant joins it) still reaches the target."""
    import ctypes
    script = ("import subprocess, sys, time\n"
              "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],"
              " creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)\n"
              "print(g.pid, flush=True)\n"
              "time.sleep(60)\n")
    # As Jobs.start does: suspended until it is in the job. The tests' python is a venv launcher whose real
    # interpreter (its child) would otherwise often start outside the job.
    child = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True,
                             creationflags=jobs._SUSPENDED)
    tree = jobs._job_for(child, resume=True)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = ctypes.c_void_p
    try:
        assert tree is not None
        grandchild = int(child.stdout.readline())
        handle = k32.OpenProcess(0x00100000, False, grandchild)               # SYNCHRONIZE
        assert handle
        assert tree.end()
        assert k32.WaitForSingleObject(ctypes.c_void_p(handle), 10_000) == 0  # it ended
        k32.CloseHandle(ctypes.c_void_p(handle))
        child.wait(30)
    finally:
        if tree is not None:
            tree.close()
        if child.poll() is None:
            child.kill()
            child.wait(30)


def test_a_windows_force_stop_walks_the_live_tree_before_ending_the_job(fake_os, monkeypatch):
    """The job may have missed what started before it existed: taskkill /T (the live tree) always runs, and the
    job's end only adds to it -- it never replaces it."""
    fake_os.name = "nt"
    calls: list[str] = []

    class Tree:
        def end(self):
            calls.append("job")
            return True

    monkeypatch.setattr(jobs, "_taskkill_tree", lambda pid: calls.append("taskkill") or True)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        monkeypatch.setattr(child, "kill", lambda: calls.append("kill"))
        assert jobs._send_stop(child, graceful=False, tree=Tree()) == "kill"
        assert calls == ["taskkill", "job"]
    finally:
        subprocess.Popen.kill(child)
        child.wait(30)
