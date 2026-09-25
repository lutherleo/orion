"""The bounded, coverage-guided drive loop. Language-agnostic, deterministic given a seed.

Orion's own loop, not AFL++/libFuzzer: the goal is OBSERVING linkages, not crash-hunting, so keeping
inputs that reach new coverage is enough and runs anywhere with zero install burden. The loop adapts
to what the driver can report:

  feedback=True  (process exits per input, harness script) -> collect + reset after EVERY input, fold
                  the disjoint step trace into the accumulator, and grow the corpus with inputs that
                  reached new lines. Collect cost is paid only where it buys steering.
  feedback=False (a server that flushes on exit)           -> drive blind: no per-input collect at all
                  (it would read nothing), mutate the seeds round the budget; the pipeline collects
                  once after stop().
  mutable=False  (whole scripts)                           -> run the seeds once each, no mutation.

All impurity lives in the injected driver/tracer; the control flow and mutator are pure.
"""
from __future__ import annotations

import random
import urllib.parse
from dataclasses import replace

from .base import Driver, Input, Tracer
from .trace import RuntimeTrace, TraceAccumulator

# Byte menu for mutation -- small, deterministic, security-flavoured (path traversal, template/JS
# injection, SQL/NoSQL metacharacters). Enough to reach error branches.
_INJECT = [
    b"'", b'"', b"<script>", b"../../../../etc/passwd", b"{{7*7}}", b"$where",
    b"; ls", b"| id", b"\x00", b"%00", b"-1", b"0", b"true", b"[]", b"{}",
]


def _mutate(rng: random.Random, inp: Input) -> Input:
    """Derive one variant of `inp`. Pure given `rng`."""
    payload = rng.choice(_INJECT)
    if inp.kind == "http":
        if rng.random() < 0.5 and "?" not in inp.path:
            # Percent-encode: raw control bytes are illegal in a URL (urllib raises ValueError).
            q = urllib.parse.quote(payload, safe="")
            return replace(inp, path=f"{inp.path}?q={q}", label=f"{inp.label}~q")
        return replace(inp, body=inp.body + payload, label=f"{inp.label}~b")
    if rng.random() < 0.5:
        # argv cannot carry NUL; the stdin branch covers binary payloads.
        arg = payload.replace(b"\x00", b"").decode("latin-1") or "0"
        return replace(inp, argv=(*inp.argv, arg), label=f"{inp.label}~a")
    return replace(inp, stdin=inp.stdin + payload, label=f"{inp.label}~s")


def run(driver: Driver, tracer: Tracer, target, seeds: list[Input], *,
        budget: int = 200, seed: int = 1337, on_step=None) -> RuntimeTrace:
    """Drive `target` with up to `budget` inputs and return the accumulated per-input traces
    (empty for a no-feedback driver -- its trace is collected after stop). `on_step(i, input,
    new_lines)` is an optional progress hook."""
    feedback = getattr(driver, "feedback", True)
    mutable = getattr(driver, "mutable", True)
    rng = random.Random(seed)
    acc = TraceAccumulator()
    corpus: list[Input] = list(seeds)
    sent = 0

    def drive(inp: Input) -> bool:
        nonlocal sent
        driver.send(target, inp)
        sent += 1
        new = 0
        if feedback:
            before = acc.line_count()
            acc.add(tracer.collect(target.work, target.repo))
            tracer.reset(target.work)
            new = acc.line_count() - before
        if on_step is not None:
            on_step(sent, inp, new)
        return new > 0

    for inp in seeds:
        if sent >= budget:
            break
        if drive(inp):
            corpus.append(inp)          # a fruitful seed gets extra weight as a parent

    while mutable and corpus and sent < budget:
        child = _mutate(rng, corpus[rng.randrange(len(corpus))])
        if drive(child):
            corpus.append(child)

    return acc.freeze()
