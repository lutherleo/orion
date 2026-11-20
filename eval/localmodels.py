"""Local small models as Orion's engine: the arm registry and the run helpers shared by eval.extbench and
eval.localbench.

An arm `lm-<alias>` runs `orion scan <tree> --engine lm --model <path> --lm-verify` on the in-process Orion-LM
engine, so no Claude, no API and no tokens billed. `<alias>` comes from the registry
(eval/dataset/local_models.json, or the file named by ORION_LOCAL_MODELS): each entry names an MLX model
(Apple silicon) and/or a GGUF file (llama.cpp, any OS: Windows/WSL, Linux). The MLX one is used on macOS,
the GGUF one elsewhere; a relative path resolves against this checkout.

Scoring is the same as the Orion arms: `lm-<alias>` = verdicts after the same-model --lm-verify pass,
`lm-<alias>:leads` = the leads before it. The engine reports no token counts, so tokens and cost are MISSING
(None), never 0 (CLAUDE.md research-data rule). What it does report is the progress log, summarised per run as
`behaviour`: graph queries, tool steps, warnings (malformed actions, context overflow), errors, verdicts.
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = ROOT / "eval" / "dataset" / "local_models.json"
PREFIX = "lm-"
# ORION_* knobs that change what the lm engine does; dropped so every arm measures the defaults.
LM_OPTION_VARS = ("ORION_SHORTLISTS", "ORION_SHORTLIST_INLINE", "ORION_VERIFY_CONTEXT", "ORION_MODEL",
                  "ORION_EFFORT", "ORION_SEMANTIC_MODE")


def is_lm(arm: str) -> bool:
    return arm.startswith(PREFIX)


def load_registry(path: str | os.PathLike | None = None) -> dict[str, dict]:
    p = Path(path or os.environ.get("ORION_LOCAL_MODELS") or REGISTRY)
    data = json.loads(p.read_text(encoding="utf-8"))
    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, dict):
        raise ValueError(f"{p}: expected {{\"models\": {{alias: {{\"mlx\"|\"gguf\": path}}}}}}")
    return models


def arms(registry: dict[str, dict] | None = None) -> tuple[str, ...]:
    return tuple(PREFIX + a for a in (registry if registry is not None else load_registry()))


def model_path(arm: str, registry: dict[str, dict] | None = None, *, platform: str = sys.platform,
               check: bool = True) -> str:
    """The model path an lm arm loads on this platform: MLX on macOS, GGUF elsewhere (either if only one)."""
    reg = registry if registry is not None else load_registry()
    alias = arm[len(PREFIX):]
    if alias not in reg:
        raise ValueError(f"unknown local model {alias!r} (registry has: {', '.join(reg) or 'none'})")
    entry = reg[alias]
    order = ("mlx", "gguf") if platform == "darwin" else ("gguf",)
    raw = next((entry[k] for k in order if entry.get(k)), None)
    if raw is None:
        need = "an \"mlx\" or \"gguf\"" if platform == "darwin" else "a \"gguf\" (MLX runs only on Apple silicon)"
        raise ValueError(f"local model {alias!r} has no path for {platform}: add {need} entry")
    if raw.startswith("hf:"):            # an MLX Hugging Face repo id: mlx_lm downloads and caches it
        return raw[3:]
    p = Path(raw).expanduser()
    p = p if p.is_absolute() else ROOT / p
    if check and not p.exists():
        raise ValueError(f"local model {alias!r}: {p} does not exist (download or convert it first)")
    return str(p)


def scan_cmd(tree: str, model: str, verdicts_json: Path) -> list[str]:
    return [sys.executable, "-m", "orion", "scan", tree, "--engine", "lm", "--model", model, "--lm-verify",
            "--json", str(verdicts_json)]


def scan_env(base: dict) -> dict:
    env = {k: v for k, v in base.items() if k not in LM_OPTION_VARS}
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(ROOT), base.get("PYTHONPATH", "")) if p)
    return env


def behaviour(progress: Path) -> dict:
    """Counts from an lm run's progress.jsonl: events per phase, the warn kinds, ERROR verdicts. Missing log -> {}."""
    if not progress.is_file():
        return {}
    events, warns, decisions = Counter(), Counter(), Counter()
    for line in progress.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        events[f"{ev.get('phase')}:{ev.get('event')}"] += 1
        detail = str(ev.get("detail") or "")
        if ev.get("event") == "warn":
            warns[detail.split(":", 1)[0][:40]] += 1
        if ev.get("event") == "verdict":
            decisions[detail.split(":", 1)[0]] += 1
    return {"events": dict(events), "warn_kinds": dict(warns), "decisions": dict(decisions),
            "graph_queries": sum(n for k, n in events.items() if k.endswith(":query"))}


NO_USAGE = {"calls": None, "uncached_in": None, "cache_read": None, "cache_write": None, "out": None,
            "tokens": None, "cli_cost_usd": None}
