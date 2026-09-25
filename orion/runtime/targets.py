"""Target selection: pick the (Driver, Tracer) for a repo, or None if nothing fits. Pure detection.

The framework seam for EXECUTION, parallel to graph/profiles.py (which answers static source/sink
questions). Precedence:

  1. `.orion/runtime.json` descriptor -- boot commands, base URLs and especially LOGIN are
     app-specific, so a repo can spell them out:
       {"kind":"http","boot":["npm","start"],"base_url":"http://localhost:4000",
        "login":{"path":"/login","fields":{"userName":"user1","password":"User1_123"}}}
       {"kind":"process","build":["go","build","./..."],"out":"target.bin"}
       {"kind":"harness","language":"py","file":"scripts/drive.py","image":"myapp-deps:latest"}
  2. an explicit `driver` ("harness" | "http" | "process") or a pinned `harness_file`;
  3. manifest sniff: npm `start` script -> http; go.mod -> process; Python markers -> py harness;
     any other package.json -> js harness.

None means the stage is skipped and the graph is untouched.
"""
from __future__ import annotations

import json
from pathlib import Path

from .go_tracer import GoCoverTracer
from .harness import HarnessDriver
from .http_driver import HttpDriver
from .process_driver import ProcessDriver
from .py_tracer import PyTracer
from .v8_tracer import V8Tracer

DESCRIPTOR = ".orion/runtime.json"
DRIVERS = ("auto", "harness", "http", "process")
SANDBOXES = ("auto", "docker", "host")   # isolation for HARNESS runs (see runtime/sandbox.py)
_PY_MARKERS = ("requirements.txt", "setup.py", "pyproject.toml", "setup.cfg", "Pipfile")


def load_descriptor(repo: str) -> dict | None:
    p = Path(repo) / DESCRIPTOR
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _package_json(repo: str) -> dict | None:
    try:
        data = json.loads((Path(repo) / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _script_language(repo: str, harness_file: str | None) -> str | None:
    """'py' / 'js' for a harness: the pinned file's extension, else the repo's markers."""
    if harness_file:
        ext = Path(harness_file).suffix.lower()
        if ext == ".py":
            return "py"
        if ext in (".js", ".cjs", ".mjs"):
            return "js"
    if any((Path(repo) / m).exists() for m in _PY_MARKERS):
        return "py"
    if (Path(repo) / "package.json").exists():
        return "js"
    return None


def _http(repo: str, desc: dict | None = None):
    desc = desc or {}
    start = (["npm", "start"] if "start" in ((_package_json(repo) or {}).get("scripts") or {})
             else None)
    boot = desc.get("boot") or start
    if not boot:
        return None
    v8 = V8Tracer()
    return HttpDriver(boot_cmd=boot, base_url=desc.get("base_url", "http://localhost:3000"),
                      tracer=v8, login=desc.get("login")), v8


def _process(repo: str, desc: dict | None = None):
    desc = desc or {}
    if not desc and not (Path(repo) / "go.mod").exists():
        return None
    tracer = GoCoverTracer(module_prefix=desc.get("module_prefix"))
    return ProcessDriver(build_cmd=desc.get("build", ["go", "build", "./..."]),
                         out_bin=desc.get("out", "orion-target.bin"), tracer=tracer), tracer


def _harness(repo: str, language: str | None, harness_file: str | None, timeout: float, on_event,
             sandbox: str = "auto", image: str | None = None):
    language = language or _script_language(repo, harness_file)
    if language not in ("py", "js"):
        return None
    tracer = PyTracer() if language == "py" else V8Tracer()
    return HarnessDriver(repo, language, tracer, harness_file=harness_file, timeout=timeout,
                         on_event=on_event, isolation=sandbox, image=image), tracer


def select(repo: str, *, driver: str = "auto", language: str | None = None,
           harness_file: str | None = None, timeout: float = 120.0, on_event=None,
           sandbox: str = "auto"):
    """Return (Driver, Tracer) or None. Descriptor wins; then the explicit choice; then a sniff."""
    desc = load_descriptor(repo)
    if desc:
        kind = desc.get("kind")
        if kind == "http":
            return _http(repo, desc)
        if kind == "process":
            return _process(repo, desc)
        if kind == "harness":
            f = desc.get("file")
            return _harness(repo, desc.get("language") or language,
                            str(Path(repo) / f) if f else harness_file, timeout, on_event,
                            sandbox, desc.get("image"))
        return None

    if driver == "http":
        return _http(repo)
    if driver == "process":
        return _process(repo)
    if driver == "harness" or harness_file:
        return _harness(repo, language, harness_file, timeout, on_event, sandbox)

    return (_http(repo) or _process(repo)
            or _harness(repo, language, harness_file, timeout, on_event, sandbox))
