"""Token/cost ledger for the PLAN2 research eval — aggregates the `usage` events run_agent now emits.

`claude_cli._emit_usage` fires one `{"event":"usage","detail":<json>}` progress event per agent call,
carrying input/output/cache tokens + the CLI's own `total_cost_usd`. A `TokenLedger` wraps the run's
`on_event`, records every usage event (grouped by phase and shape), and prices it. Two cost figures are
kept side by side:
  - `cli_cost_usd`  — summed from the CLI's `total_cost_usd` (null through a non-Anthropic proxy → 0).
  - `priced_cost_usd` — computed from tokens against `PRICES` (the source of truth for the Claude arms;
    for the LOCAL arm this is $0 by construction — local tokens carry no API price, so cost lives in
    wall-clock/compute, reported separately by the runner).

Pure and token-free: unit-tested by feeding it hand-built usage events. It never calls the API.
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field

# Per-1M-token USD rates (Claude API skill, cached 2026-06-24). input/output; cache write ~1.25x input,
# cache read ~0.1x input. Aliases map to the id the CLI resolves them to, so a usage.model of either
# "haiku" or "claude-haiku-4-5" prices identically. A local/proxied model matches nothing here -> $0.
PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-fable-5": (10.0, 50.0),
}
_ALIASES = {"opus": "claude-opus-5", "sonnet": "claude-sonnet-5", "haiku": "claude-haiku-4-5",
            "fable": "claude-fable-5"}
_CACHE_WRITE_MULT = 1.25
_CACHE_READ_MULT = 0.10


def _resolve_price(model: str) -> tuple[float, float] | None:
    """Rates for a model id/alias, or None for a local/unknown model (which prices at $0)."""
    m = (model or "").strip().lower()
    if not m:
        return None                            # unknown/empty model prices at $0 (local arm)
    if m in PRICES:
        return PRICES[m]
    if m in _ALIASES:
        return PRICES[_ALIASES[m]]
    for known in PRICES:                       # substring: "claude-opus-5-20xx" style ids
        if known in m or m in known:
            return PRICES[known]
    for alias, known in _ALIASES.items():
        if alias in m:
            return PRICES[known]
    return None


def price_usd(model: str, input_tokens: int, output_tokens: int,
              cache_creation_input_tokens: int = 0, cache_read_input_tokens: int = 0) -> float:
    """Token-priced USD for one call. Cache-write billed 1.25x input rate, cache-read 0.1x. A local/
    unknown model returns 0.0 (no API price). Pure."""
    rates = _resolve_price(model)
    if rates is None:
        return 0.0
    in_rate, out_rate = rates
    dollars = (
        input_tokens * in_rate
        + cache_creation_input_tokens * in_rate * _CACHE_WRITE_MULT
        + cache_read_input_tokens * in_rate * _CACHE_READ_MULT
        + output_tokens * out_rate
    ) / 1_000_000.0
    return dollars


@dataclass
class Bucket:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    cli_cost_usd: float = 0.0
    priced_cost_usd: float = 0.0

    def add(self, u: dict) -> None:
        self.calls += 1
        self.input_tokens += int(u.get("input_tokens") or 0)
        self.output_tokens += int(u.get("output_tokens") or 0)
        self.cache_creation_input_tokens += int(u.get("cache_creation_input_tokens") or 0)
        self.cache_read_input_tokens += int(u.get("cache_read_input_tokens") or 0)
        cost = u.get("total_cost_usd")
        if isinstance(cost, (int, float)):
            self.cli_cost_usd += float(cost)
        self.priced_cost_usd += price_usd(
            u.get("model", ""), int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0),
            int(u.get("cache_creation_input_tokens") or 0), int(u.get("cache_read_input_tokens") or 0))

    def as_dict(self) -> dict:
        return {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
            "total_tokens": self.input_tokens + self.output_tokens
            + self.cache_creation_input_tokens + self.cache_read_input_tokens,
            "cli_cost_usd": round(self.cli_cost_usd, 6),
            "priced_cost_usd": round(self.priced_cost_usd, 6),
        }


class TokenLedger:
    """Records every `usage` progress event, grouped by phase and by (phase, shape). Wrap the run's
    on_event with `.wrap(...)`; the wrapper forwards untouched and records usage events on the side.

    `default_model` backfills the model when a usage event carries an empty one — the `claude -p`
    result event reports no `model` (and no `total_cost_usd`) on a Pro/OAuth subscription, so without
    this the ledger can't price real token counts. The runner passes the arm's known model here."""

    def __init__(self, default_model: str = "") -> None:
        self.default_model = default_model
        self.total = Bucket()
        self.by_phase: dict[str, Bucket] = defaultdict(Bucket)
        self.by_phase_shape: dict[tuple[str, str], Bucket] = defaultdict(Bucket)
        self.calls: list[dict] = []            # every raw usage record, for audit/reconciliation

    def record(self, ev: dict) -> None:
        """Record one progress event if it is a usage event; ignore everything else."""
        if not isinstance(ev, dict) or ev.get("event") != "usage":
            return
        detail = ev.get("detail")
        try:
            u = json.loads(detail) if isinstance(detail, str) else (detail or {})
        except (json.JSONDecodeError, TypeError):
            return
        if not isinstance(u, dict):
            return
        if not (u.get("model") or "").strip() and self.default_model:
            u = {**u, "model": self.default_model}   # backfill for pricing (Pro/OAuth reports no model)
        phase = ev.get("phase") or "?"
        shape = ev.get("shape") or "-"
        self.total.add(u)
        self.by_phase[phase].add(u)
        self.by_phase_shape[(phase, shape)].add(u)
        self.calls.append({"phase": phase, "shape": shape, **u})

    def wrap(self, on_event):
        """Return an on_event that records usage then forwards to `on_event` (or None)."""
        def _wrapped(ev: dict) -> None:
            self.record(ev)
            if on_event is not None:
                on_event(ev)
        return _wrapped

    def summary(self) -> dict:
        return {
            "total": self.total.as_dict(),
            "by_phase": {p: b.as_dict() for p, b in sorted(self.by_phase.items())},
            "by_phase_shape": {f"{p}/{s}": b.as_dict()
                               for (p, s), b in sorted(self.by_phase_shape.items())},
            "n_calls": len(self.calls),
        }
