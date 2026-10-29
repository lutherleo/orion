"""The Docker sandbox for harness runs. Pure tests build the `docker run` command and stub the
subprocess; the one live test needs a Docker daemon (marked slow, skips without it)."""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest

from orion.runtime import sandbox as sb
from orion.runtime import targets
from orion.runtime.harness import HarnessDriver
from orion.runtime.py_tracer import BOOT_PY, PyTracer
from orion.runtime.sandbox import DockerSandbox, Mount


def _sandbox(tmp_path, script_dir=None):
    repo, work = tmp_path / "repo", tmp_path / "work"
    repo.mkdir(), work.mkdir()
    script = (script_dir or repo) / "drive.py"
    script.parent.mkdir(exist_ok=True)
    script.write_text("")
    return DockerSandbox.for_harness("python:3.12-slim", repo=str(repo), work=str(work),
                                     script=str(script)), repo, work, script


def _flag_values(argv, flag):
    return [argv[i + 1] for i, a in enumerate(argv) if a == flag]


def test_command_is_locked_down(tmp_path):
    box, repo, work, _ = _sandbox(tmp_path)
    argv = box.build_command(["echo"], cwd=str(repo), env=None, name="n1")
    assert argv[:4] == ["docker", "run", "--rm", "--name"]
    assert _flag_values(argv, "--network") == ["none"]
    assert "--read-only" in argv and _flag_values(argv, "--cap-drop") == ["ALL"]
    assert _flag_values(argv, "--security-opt") == ["no-new-privileges"]
    assert _flag_values(argv, "--memory") and _flag_values(argv, "--pids-limit")
    vols = _flag_values(argv, "-v")
    assert f"{os.path.abspath(repo)}:/src:ro" in vols                 # source is read-only
    assert f"{os.path.abspath(work)}:/work" in vols                   # only the work dir is writable
    assert any(v.endswith(":/orion:ro") for v in vols)
    assert _flag_values(argv, "-w") == ["/src"]


def test_host_paths_and_interpreter_are_translated(tmp_path):
    box, repo, work, script = _sandbox(tmp_path)
    cmd, env = PyTracer(python_exe=sys.executable).script_command(str(script), str(repo), work)
    argv = box.build_command(cmd, cwd=str(repo), env=env, name="n")
    tail = argv[argv.index("python:3.12-slim") + 1:]
    assert tail[0] == "python"                                         # not the host venv's path
    assert tail[1] == "/orion/_boot_py.py"
    assert tail[2:4] == ["/src/drive.py", "/src"]
    assert tail[4].startswith("/work/py-trace/") and tail[4].endswith(".json")
    assert "PYTHONDONTWRITEBYTECODE=1" in _flag_values(argv, "-e")


def test_script_outside_repo_gets_its_own_read_only_mount(tmp_path):
    box, _, _, script = _sandbox(tmp_path, script_dir=tmp_path / "tmpdir")
    assert box.to_container(str(script)) == "/harness/drive.py"
    assert Mount(str(script.parent), "/harness") in box.mounts


def test_unmounted_paths_and_non_paths_pass_through(tmp_path):
    box, *_ = _sandbox(tmp_path)
    assert box.to_container("--flag") == "--flag"
    assert box.to_container("500") == "500"
    elsewhere = os.path.abspath(str(tmp_path.parent / "elsewhere.txt"))
    assert box.to_container(elsewhere) == elsewhere


def test_timeout_kills_the_container(tmp_path, monkeypatch):
    box, repo, *_ = _sandbox(tmp_path)
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        if argv[1] == "run":
            raise subprocess.TimeoutExpired(argv, kw.get("timeout"))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(sb, "_ensure_image", lambda *a: None)
    monkeypatch.setattr(sb.subprocess, "run", fake_run)
    r = box.run(["echo"], cwd=str(repo), timeout=1)
    assert r.timed_out and r.exit_code is None
    name = calls[0][calls[0].index("--name") + 1]
    assert calls[1] == ["docker", "kill", name]


def test_missing_docker_is_exit_127_not_a_crash(tmp_path, monkeypatch):
    box, repo, *_ = _sandbox(tmp_path)
    box._docker = str(tmp_path / "no-such-docker")
    monkeypatch.setattr(sb, "_ensure_image", lambda *a: None)
    r = box.run(["echo"], cwd=str(repo), timeout=5)
    assert r.exit_code == 127 and not r.timed_out


# ── choosing the isolation ─────────────────────────────────────────────
def _driver(tmp_path, isolation, available, monkeypatch, events=None):
    monkeypatch.setattr("orion.runtime.harness.docker_available", lambda: available)
    d = HarnessDriver(str(tmp_path), "py", PyTracer(), harness_file=str(tmp_path / "d.py"),
                      isolation=isolation, on_event=(events.append if events is not None else None))
    d.start(str(tmp_path), tmp_path, [])
    return d


def test_auto_uses_docker_when_available_and_warns_when_not(tmp_path, monkeypatch):
    assert _driver(tmp_path, "auto", True, monkeypatch).use_docker
    events: list = []
    assert not _driver(tmp_path, "auto", False, monkeypatch, events).use_docker
    assert any(e["event"] == "warn" and "ON THIS HOST" in e["detail"] for e in events)
    assert not _driver(tmp_path, "host", True, monkeypatch).use_docker
    assert _driver(tmp_path, "docker", False, monkeypatch).use_docker     # explicit wins


def test_docker_send_goes_through_the_container(tmp_path, monkeypatch):
    d = _driver(tmp_path, "docker", True, monkeypatch)
    seen = []
    monkeypatch.setattr(DockerSandbox, "run",
                        lambda self, cmd, **kw: seen.append((self.image, cmd)) or sb.RunResult("", "", 0, False))
    from orion.runtime.base import Input, RunningTarget
    d.send(RunningTarget(kind="script", repo=str(tmp_path), work=tmp_path),
           Input(kind="script", argv=(str(tmp_path / "d.py"),)))
    (image, cmd), = seen
    assert image == "python:3.12-slim" and cmd[1] == BOOT_PY


def test_select_threads_sandbox_and_descriptor_image(tmp_path, monkeypatch):
    (tmp_path / "requirements.txt").write_text("")
    drv, _ = targets.select(str(tmp_path), sandbox="host")
    assert drv._isolation == "host"
    (tmp_path / ".orion").mkdir()
    (tmp_path / ".orion" / "runtime.json").write_text(
        '{"kind":"harness","language":"py","file":"d.py","image":"app-deps:1"}')
    drv, _ = targets.select(str(tmp_path), sandbox="docker")
    assert (drv._isolation, drv._image) == ("docker", "app-deps:1")


def test_boot_py_prefers_the_targets_module_over_orions(tmp_path):
    """Run by file path, the bootstrap's own dir (orion/runtime, which has report.py) must not shadow
    a target module of the same name."""
    (tmp_path / "report.py").write_text("def target_report():\n    return 1\n")
    (tmp_path / "d.py").write_text("import report\nreport.target_report()\n")
    work = tmp_path / "w"
    work.mkdir()
    tracer = PyTracer()
    cmd, env = tracer.script_command(str(tmp_path / "d.py"), str(tmp_path), work)
    sb.SubprocessSandbox().run(cmd, cwd=str(tmp_path), timeout=60, env=env)
    assert any(m.name == "target_report" for m in tracer.collect(work, str(tmp_path)).methods)


# ── live: the same harness inside a real container ─────────────────────
@pytest.mark.slow
def test_py_harness_in_docker_matches_host(tmp_path):
    if not sb.docker_available():
        pytest.skip("Docker daemon not available")
    (tmp_path / "target.py").write_text(textwrap.dedent("""\
        def sink(x):
            return x * 2

        def handle(v):
            return sink(v)
    """))
    (tmp_path / "drive.py").write_text("import target\nfor i in range(3):\n    target.handle(i)\n")

    def run(isolation):
        work = tmp_path / f"w-{isolation}"
        work.mkdir()
        tracer = PyTracer()
        d = HarnessDriver(str(tmp_path), "py", tracer, harness_file=str(tmp_path / "drive.py"),
                          isolation=isolation)
        t = d.start(str(tmp_path), work, [])
        from orion.runtime.base import Input
        d.send(t, Input(kind="script", argv=(str(tmp_path / "drive.py"),)))
        return tracer.collect(work, str(tmp_path))

    host, box = run("host"), run("docker")
    assert set(box.methods) == set(host.methods) and set(box.calls) == set(host.calls)
    assert any(m.name == "sink" and m.hit_count == 3 for m in box.methods)
