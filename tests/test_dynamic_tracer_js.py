"""End-to-end JS tracer (orion/dynamic/tracer_js.py) against a REAL tiny Node driver+target.

Token-free (a local node subprocess, no Claude/Neo4j). Skips cleanly when Node is unavailable, so CI
without Node still passes. The JS trace is SAMPLED, so the target is exercised in a loop to be
reliably captured, and assertions are on presence (a lower bound), never exact counts.
"""
from __future__ import annotations

import shutil
import textwrap

import pytest

from orion.dynamic.tracer_js import trace

_NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(_NODE is None, reason="node not installed")


def _write(root):
    # `sink` does real work in a loop so V8 does not inline it away — otherwise the CPU sampler never
    # sees it as its own frame. Both functions are hot (driven in a 200k loop) so sampling is reliable.
    (root / "target.js").write_text(textwrap.dedent("""\
        function sink(x) {
          let s = 0;
          for (let i = 0; i < 200; i++) { s += (x * 3 + i) % 7; }
          return s;
        }
        function handle(req) { return sink(req.q); }
        module.exports = { sink, handle };
    """))
    (root / "driver.js").write_text(textwrap.dedent("""\
        const t = require('./target.js');
        let acc = 0;
        for (let i = 0; i < 200000; i++) { acc += t.handle({ q: i }); }  // loop so the sampler catches it
        if (acc < 0) console.log(acc);
    """))
    return str(root / "driver.js")


def test_missing_node_yields_empty_trace_not_crash(tmp_path):
    obs, result = trace(str(tmp_path / "nope.js"), str(tmp_path), node_exe="/no/such/node")
    # A bogus node path fails to launch; the sandbox reports it, the trace is empty, nothing raised.
    assert obs.is_empty()
    assert result.exit_code == 127


def test_trace_captures_methods_and_calls(tmp_path):
    driver = _write(tmp_path)
    obs, result = trace(driver, str(tmp_path), timeout=60)
    assert not result.timed_out
    assert not obs.is_empty()                       # a real Node run produced observations
    names = {m.name for m in obs.methods}
    assert "handle" in names                        # the hot entry function is reliably sampled
    # Everything recorded is scoped to the target (no node-internal frames).
    assert all(("target.js" in m.file) or ("driver.js" in m.file) for m in obs.methods)
    assert obs.calls                                # at least one caller->callee edge was observed
    assert all(c.callee_line >= 1 for c in obs.calls)   # 1-based lines, attribution intact
