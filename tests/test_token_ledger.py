"""Token ledger + claude_cli usage capture (PLAN2 instrumentation). Token-free — no subprocess/API."""
from __future__ import annotations

import json

from bench.token_ledger import PRICES, TokenLedger, price_usd
from orion import claude_cli


# --- claude_cli.extract_usage / _emit_usage ---------------------------------------------------

def _result_event(**usage):
    return {"type": "result", "model": usage.pop("model", "claude-opus-5"),
            "total_cost_usd": usage.pop("total_cost_usd", 0.123), "num_turns": 5,
            "usage": usage}


def test_extract_usage_reads_the_result_event():
    u = claude_cli.extract_usage(_result_event(
        input_tokens=100, output_tokens=50,
        cache_creation_input_tokens=10, cache_read_input_tokens=200))
    assert u["input_tokens"] == 100 and u["output_tokens"] == 50
    assert u["cache_creation_input_tokens"] == 10 and u["cache_read_input_tokens"] == 200
    assert u["total_cost_usd"] == 0.123 and u["model"] == "claude-opus-5"


def test_extract_usage_defensive_on_missing_fields():
    assert claude_cli.extract_usage(None) is None
    u = claude_cli.extract_usage({"type": "result"})     # no usage block
    # missing is None, not 0 (2026-10-29): a call with no usage is not a call that spent nothing
    assert u["input_tokens"] is None and u["total_cost_usd"] is None


def test_extract_usage_keeps_the_cache_write_breakdown():
    u = claude_cli.extract_usage(_result_event(
        input_tokens=1, output_tokens=1, cache_creation_input_tokens=30, cache_read_input_tokens=0,
        cache_creation={"ephemeral_5m_input_tokens": 10, "ephemeral_1h_input_tokens": 20}))
    assert (u["cache_creation_5m_input_tokens"], u["cache_creation_1h_input_tokens"]) == (10, 20)


def test_ledger_counts_missing_usage_instead_of_zero_tokens():
    led = TokenLedger()
    led.record({"phase": "verify", "shape": None, "event": "usage", "detail": json.dumps(
        claude_cli.extract_usage({"type": "result"}))})
    t = led.summary()["total"]
    assert t["calls"] == 1 and t["n_missing_usage"] == 1 and t["total_tokens"] is None


def test_emit_usage_fires_one_usage_event():
    events = []
    claude_cli._emit_usage(_result_event(input_tokens=7, output_tokens=3), events.append)
    assert len(events) == 1 and events[0]["event"] == "usage"
    u = json.loads(events[0]["detail"])
    assert u["input_tokens"] == 7 and u["output_tokens"] == 3


def test_emit_usage_noop_without_listener_or_usage():
    claude_cli._emit_usage(_result_event(), None)          # no listener -> no crash
    events = []
    claude_cli._emit_usage(None, events.append)            # no final -> nothing emitted
    assert events == []


# --- pricing ----------------------------------------------------------------------------------

def test_price_known_models():
    # 1M input @ $5 + 1M output @ $25 = $30 for opus-5
    assert price_usd("claude-opus-5", 1_000_000, 1_000_000) == 30.0
    assert price_usd("haiku", 1_000_000, 0) == 1.0            # alias resolves
    assert price_usd("claude-sonnet-5", 0, 1_000_000) == 15.0


def test_price_cache_multipliers():
    # cache write: 1.25x input for the 5-minute cache, 2x for the 1-hour one; read 0.1x (opus-5 $5/1M)
    assert price_usd("claude-opus-5", 0, 0, cache_creation_input_tokens=1_000_000,
                     cache_creation_5m_input_tokens=1_000_000) == 5.0 * 1.25
    assert price_usd("claude-opus-5", 0, 0, cache_creation_input_tokens=1_000_000,
                     cache_creation_1h_input_tokens=1_000_000) == 5.0 * 2.0
    assert round(price_usd("claude-opus-5", 0, 0, cache_read_input_tokens=1_000_000), 6) == 0.5
    # without the breakdown (or with one that does not add up) the price is missing, not assumed
    assert price_usd("claude-opus-5", 0, 0, cache_creation_input_tokens=1_000_000) is None
    assert price_usd("claude-opus-5", 0, 0, cache_creation_input_tokens=10,
                     cache_creation_5m_input_tokens=3, cache_creation_1h_input_tokens=3) is None


def test_committed_opus_baseline_cost_is_the_1h_cache_rate():
    """bench/research/*/C.json: re-pricing its tokens with 1-hour cache writes reproduces the CLI's own
    total_cost_usd within $0.001 -- the reported $0.61 / $0.84 (flat 1.25x) were ~17% low."""
    from pathlib import Path
    for repo in ("nodegoat", "pygoat"):
        t = json.loads(Path(f"bench/research/{repo}/C.json").read_text())["tokens"]["total"]
        cw = t["cache_creation_input_tokens"]
        p = price_usd("claude-opus-5", t["input_tokens"], t["output_tokens"], cw, t["cache_read_input_tokens"],
                      cache_creation_1h_input_tokens=cw)
        assert abs(p - t["cli_cost_usd"]) < 0.001, (repo, p, t["cli_cost_usd"])


def test_local_model_prices_at_zero():
    assert price_usd("qwen2.5-coder", 5_000_000, 5_000_000) == 0.0
    assert price_usd("", 1_000, 1_000) == 0.0


# --- TokenLedger ------------------------------------------------------------------------------

def _usage_ev(phase, shape, model="claude-opus-5", **u):
    return {"phase": phase, "shape": shape, "event": "usage",
            "detail": json.dumps({"model": model, "total_cost_usd": u.pop("cost", None), **u})}


def test_ledger_aggregates_by_phase_and_shape():
    led = TokenLedger()
    led.record(_usage_ev("discover", "A", input_tokens=100, output_tokens=10, cost=0.01))
    led.record(_usage_ev("discover", "B", input_tokens=200, output_tokens=20, cost=0.02))
    led.record(_usage_ev("verify", None, input_tokens=50, output_tokens=5, cost=0.005))
    s = led.summary()
    assert s["total"]["input_tokens"] == 350 and s["total"]["output_tokens"] == 35
    assert round(s["total"]["cli_cost_usd"], 3) == 0.035
    assert s["by_phase"]["discover"]["input_tokens"] == 300
    assert s["by_phase"]["verify"]["calls"] == 1
    assert s["by_phase_shape"]["discover/A"]["input_tokens"] == 100


def test_ledger_ignores_non_usage_events():
    led = TokenLedger()
    led.record({"phase": "discover", "event": "tool", "detail": "MATCH ..."})
    led.record({"phase": "discover", "event": "usage", "detail": "not json"})
    assert led.summary()["total"]["calls"] == 0


def test_ledger_wrap_forwards_and_records():
    led = TokenLedger()
    seen = []
    wrapped = led.wrap(seen.append)
    wrapped(_usage_ev("discover", "A", input_tokens=10, output_tokens=1))
    wrapped({"phase": "discover", "event": "tool", "detail": "q"})
    assert len(seen) == 2                                  # both forwarded downstream
    assert led.summary()["total"]["input_tokens"] == 10    # only the usage one recorded


def test_ledger_backfills_default_model_for_pricing():
    """A Pro/OAuth `claude -p` reports an empty model + no cost; the ledger must price it using the
    arm's known model passed as default_model."""
    led = TokenLedger(default_model="claude-opus-5")
    led.record(_usage_ev("verify", None, model="", input_tokens=1_000_000, output_tokens=0))
    s = led.summary()
    assert s["total"]["priced_cost_usd"] == 5.0        # priced as opus-5 despite empty model in event


def test_ledger_default_model_does_not_override_present_model():
    led = TokenLedger(default_model="claude-opus-5")
    led.record(_usage_ev("verify", None, model="claude-haiku-4-5", input_tokens=1_000_000, output_tokens=0))
    assert led.summary()["total"]["priced_cost_usd"] == 1.0   # haiku rate wins, not the default


def test_ledger_prices_from_tokens_when_cli_cost_absent():
    """The LOCAL arm gets total_cost_usd=null; priced_cost_usd stays $0 for a local model, but a Claude
    model with a null CLI cost still prices from tokens."""
    led = TokenLedger()
    led.record(_usage_ev("discover", "A", model="claude-opus-5",
                         input_tokens=1_000_000, output_tokens=0))   # no cost field -> None
    s = led.summary()
    assert s["total"]["cli_cost_usd"] == 0.0
    assert s["total"]["priced_cost_usd"] == 5.0            # priced from tokens regardless
