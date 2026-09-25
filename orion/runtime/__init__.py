"""Runtime observation: execute the target and fold what actually ran into the scan graph.

One opt-in stage with two surfaces -- `orion scan --runtime` (right after the static build) and
`orion trace` (against a graph an earlier scan built). A (Driver, Tracer) pair is picked per repo
(targets.py), driven by one coverage-guided loop (engine.py), correlated onto existing nodes
(correlate.py) and written additively (writeback.py):

  executed / hit_count      on CpgCall / CpgMethod   -- what ran, overriding reachability guesses
  :ObservedMethod                                     -- functions that ran with no static node
  OBSERVED_CALL / OBSERVED_DISPATCH {origin:'runtime'} -- links the static graph lies about

It never touches normalize, the taint seam, FLOWS_TO or a NODE_KEY label, so the static graph (and
its 217/1075 FLOWS_TO parity) stays byte-for-byte identical. Runs the target's code on this host
(timeout + temp dir only) -- trace repos you trust.
"""
from __future__ import annotations

from .enrich import enrich

__all__ = ["enrich"]
