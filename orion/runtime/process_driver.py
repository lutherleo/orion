"""ProcessDriver: build the target exe with coverage once, then exec it per input (argv/stdin). Stdlib.

`start()` runs the coverage build (the tracer's build_flags, e.g. `go build -cover`) into the work
dir; each `send()` execs the instrumented binary under the tracer's env. The process exits per input,
so its coverage is on disk immediately: `feedback = True`, the engine steers by new coverage.
It seeds one empty-argv run and lets the mutator derive argv/stdin variants from it.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .base import Input, Response, RunningTarget, target_env


class ProcessDriver:
    """Build + drive a Go (or other compiled-from-source) executable."""

    feedback = True
    mutable = True

    def __init__(self, build_cmd: list[str], out_bin: str, tracer,
                 build_flags: list[str] | None = None, run_timeout: float = 30.0) -> None:
        self._build_cmd = build_cmd        # e.g. ["go", "build", "./..."]
        self._out_bin = out_bin
        self._tracer = tracer
        self._extra_flags = build_flags or []
        self._run_timeout = run_timeout
        self._env: dict[str, str] = {}

    def start(self, repo: str, work: Path, build_flags: list[str]) -> RunningTarget:
        self._env = target_env(self._tracer.launch_env(work))     # no model credentials for the target
        # An `out` ending in "/" is a folder: `go build -o dir/ ./...` writes one binary per main package
        # there, where `-o file ./...` fails ("cannot write multiple packages to non-directory") for any
        # module with more than one package.
        to_dir = self._out_bin.endswith(("/", "\\"))
        out = str(Path(work) / self._out_bin.rstrip("/\\"))
        if to_dir:
            os.makedirs(out, exist_ok=True)
        cmd = self._inject_flags(self._build_cmd, build_flags + self._extra_flags, out + os.sep if to_dir else out)
        r = subprocess.run(cmd, cwd=repo, env=self._env, capture_output=True, text=True,
                           timeout=600, check=False)
        if r.returncode != 0:
            raise RuntimeError(f"coverage build failed: {(r.stderr or r.stdout).strip()[-300:]}")
        if to_dir:
            bins = sorted(p for p in Path(out).iterdir() if p.is_file())
            if len(bins) != 1:
                raise RuntimeError(
                    f"coverage build made {len(bins)} binaries ({', '.join(p.name for p in bins) or 'none'}): "
                    'name the main package in .orion/runtime.json, e.g. {"kind": "process", '
                    '"build": ["go", "build", "./cmd/app"], "out": "app.bin"}')
            out = str(bins[0])
        return RunningTarget(kind="process", repo=repo, work=Path(work), exe_path=out)

    @staticmethod
    def _inject_flags(build_cmd: list[str], flags: list[str], out: str) -> list[str]:
        """Insert coverage flags and `-o <out>` right after the `build` verb. Pure."""
        cmd = list(build_cmd)
        i = cmd.index("build") + 1 if "build" in cmd else 1
        cmd[i:i] = [*flags, "-o", out]
        return cmd

    def seeds(self, db, scan_id: str) -> list[Input]:
        # argv carries no per-entry information yet; one seed per entry point would only repeat the
        # same empty run, so seed once and let the mutator derive variants.
        return [Input(kind="process", label="argv:empty", argv=())]

    def send(self, target: RunningTarget, inp: Input) -> Response:
        try:
            out = subprocess.run([target.exe_path, *inp.argv], input=inp.stdin, capture_output=True,
                                 env=self._env, timeout=self._run_timeout, check=False)
            return Response(ok=True, status=out.returncode)
        except subprocess.TimeoutExpired:
            return Response(ok=True, detail="timeout", timed_out=True)
        except (OSError, subprocess.SubprocessError) as e:
            return Response(ok=False, detail=str(e))

    def stop(self, target: RunningTarget) -> None:
        pass
