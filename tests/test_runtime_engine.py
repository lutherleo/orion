"""Token-free tests for the drive loop and HTTP route seeding. Fake driver/tracer -- no boot."""
from __future__ import annotations

from pathlib import Path

from orion.runtime import engine
from orion.runtime.base import Input, Response, RunningTarget
from orion.runtime.http_driver import routes_from_rows
from orion.runtime.trace import Hit, RuntimeTrace


class _FakeDriver:
    def __init__(self, feedback=True, mutable=True):
        self.feedback, self.mutable, self.sent = feedback, mutable, []

    def send(self, target, inp):
        self.sent.append(inp)
        return Response(ok=True)


class _FakeTracer:
    """The first three collects reach new lines; then the target plateaus."""
    def __init__(self):
        self.collects = self.resets = 0

    def collect(self, work, repo):
        self.collects += 1
        return RuntimeTrace(coverage=(Hit("a.js", min(self.collects, 3), 1),))

    def reset(self, work):
        self.resets += 1


def _target():
    return RunningTarget(kind="http", repo=".", work=Path("."))


def _seeds():
    return [Input(kind="http", label="s0", path="/"), Input(kind="http", label="s1", path="/x")]


def test_engine_deterministic_and_respects_budget():
    def run_once():
        d = _FakeDriver()
        engine.run(d, _FakeTracer(), _target(), _seeds(), budget=25, seed=42)
        return [(i.verb, i.path, i.label) for i in d.sent]
    a, b = run_once(), run_once()
    assert a == b
    assert len(a) == 25


def test_engine_accumulates_disjoint_step_traces():
    t = _FakeTracer()
    trace = engine.run(_FakeDriver(), t, _target(), _seeds(), budget=10, seed=1)
    assert {h.line for h in trace.coverage} == {1, 2, 3}
    assert t.collects == t.resets == 10          # collect + reset once per input


def test_no_feedback_driver_never_collects_per_input():
    """A server flushes coverage only on exit: per-input collects would read nothing, so the loop
    must not pay for them (the pipeline collects once after stop)."""
    d, t = _FakeDriver(feedback=False), _FakeTracer()
    trace = engine.run(d, t, _target(), _seeds(), budget=20, seed=1)
    assert len(d.sent) == 20
    assert t.collects == 0 and trace.is_empty()


def test_non_mutable_driver_runs_each_seed_once():
    d = _FakeDriver(mutable=False)
    engine.run(d, _FakeTracer(), _target(), _seeds(), budget=50, seed=1)
    assert [i.label for i in d.sent] == ["s0", "s1"]


def test_process_mutation_strips_nul_from_argv():
    import random
    rng = random.Random(0)
    for _ in range(200):
        child = engine._mutate(rng, Input(kind="process"))
        assert all("\x00" not in a for a in child.argv)


def test_http_seeds_from_graph():
    rows = [
        {"verb": "get", "code": 'app.get("/login", sessionHandler.displayLoginPage)'},
        {"verb": "post", "code": 'app.post("/login", sessionHandler.handleLoginRequest)'},
        {"verb": "get", "code": 'app.get("/login", sessionHandler.displayLoginPage)'},   # dup
        {"verb": "use", "code": "app.use(express.static(...))"},                          # not a verb
        {"verb": "get", "code": "app.get('env')"},                                        # a setting
        {"verb": "get", "code": "router.get('/users/:id/profile', h)"},                   # any router
    ]
    assert [(s.verb, s.path) for s in routes_from_rows(rows)] == [
        ("GET", "/login"), ("POST", "/login"), ("GET", "/users/1/profile")]
