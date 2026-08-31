"""Semgrep baseline (PLAN2 arm D) — the deterministic-scanner reference, scored through Orion's OWN
matcher (agenda item 1). No tokens.

Runs `semgrep --json --config <ruleset>` on a repo and maps each result into the `(text, evidence)`
finding shape `bench.scoring` consumes: the check_id + message carry the vulnerability CLASS (matched
by the per-benchmark CLASS_KEYWORDS), the file path carries the FILE match. Apples-to-apples: same
checkout, same matcher as every LLM arm; only the finder differs. Discloses the ruleset + Semgrep
version so the baseline is reproducible.
"""
from __future__ import annotations

import json
import shutil
import subprocess


def semgrep_bin() -> str | None:
    """Path to the semgrep CLI, or None if not installed. Checks PATH, then the running interpreter's
    own bin dir (semgrep installed into the same venv but not on PATH — common under WSL)."""
    found = shutil.which("semgrep")
    if found:
        return found
    import os
    import sys
    cand = os.path.join(os.path.dirname(sys.executable), "semgrep")
    return cand if os.path.exists(cand) else None


def run_semgrep(repo: str, *, ruleset: str = "auto", timeout: int = 900,
                semgrep: str | None = None) -> dict:
    """Run Semgrep on `repo` and return its parsed JSON (plus a `_meta` block with version/ruleset).

    `ruleset` is passed to `--config` (default "auto" = the registry's language-appropriate rules;
    needs network the first time). Never raises on a scan/parse failure — returns
    {"_error": ...} so the runner records an honest empty baseline instead of crashing."""
    binary = semgrep or semgrep_bin()
    if binary is None:
        return {"_error": "semgrep not installed (pip install semgrep)"}
    try:
        proc = subprocess.run(
            [binary, "--json", "--quiet", "--config", ruleset, repo],
            capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"_error": f"semgrep failed to run: {exc}"}
    if not proc.stdout.strip():
        return {"_error": f"semgrep produced no output (exit {proc.returncode}): "
                          f"{(proc.stderr or '').strip()[:300]}"}
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return {"_error": f"semgrep JSON parse failed: {exc}"}
    data["_meta"] = {"ruleset": ruleset, "version": _version(binary),
                     "exit_code": proc.returncode,
                     "n_results": len(data.get("results", []))}
    return data


def _version(binary: str) -> str:
    try:
        return subprocess.run([binary, "--version"], capture_output=True, text=True,
                              timeout=30).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def findings_from_semgrep(sg_json: dict) -> list[tuple[str, str]]:
    """Map Semgrep results into (text, evidence) pairs for bench.scoring. `text` = check_id + message
    (carries the vuln class); `evidence` = path:line + the matched source line (carries the file).
    Defensive: an `_error` or odd shape yields []."""
    if not isinstance(sg_json, dict) or "_error" in sg_json:
        return []
    out: list[tuple[str, str]] = []
    for r in sg_json.get("results", []):
        if not isinstance(r, dict):
            continue
        check_id = r.get("check_id") or ""
        extra = r.get("extra") or {}
        message = extra.get("message") or ""
        lines = extra.get("lines") or ""
        path = r.get("path") or ""
        start = (r.get("start") or {}).get("line", "")
        text = f"{check_id} {message}".strip()
        evidence = f"{path}:{start} {lines}".strip()
        if text or evidence:
            out.append((text, evidence))
    return out
