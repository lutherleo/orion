"""Token-free tests for the runtime stage's wiring and parity-safety. No Neo4j, no boot."""
from __future__ import annotations

from orion import cli
from orion.graph import persist
from orion.graph.schema import ALL_NODE_KEY, NODE_KEY, RUNTIME_NODE_KEY
from orion.runtime import enrich as enrich_fn
from orion.runtime import report, targets, writeback
from orion.runtime.correlate import WritePlan


# ── the flags ──────────────────────────────────────────────────────────
def test_runtime_flags_default_off(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "_run_scan", lambda a: seen.setdefault("args", a) and 0)
    cli.main(["scan", "repo"])
    a = seen["args"]
    assert (a.runtime, a.use_dynamic, a.runtime_driver, a.harness_file) == (False, False, "auto", None)
    seen.clear()
    cli.main(["scan", "repo", "--runtime", "--runtime-driver", "http", "--use-runtime"])
    assert (seen["args"].runtime, seen["args"].runtime_driver, seen["args"].use_dynamic) == (True, "http", True)


def test_cli_driver_choices_match_targets():
    assert cli._RUNTIME_DRIVERS == targets.DRIVERS


# ── the skip path leaves the graph alone ───────────────────────────────
def test_enrich_skips_when_no_driver_fits(tmp_path, monkeypatch):
    events, called = [], []
    monkeypatch.setattr(writeback, "apply_plan", lambda *a, **k: called.append(1))
    assert enrich_fn("scan-x", str(tmp_path), events.append) is None
    assert not called
    assert any(e["phase"] == "runtime" and e["event"] == "warn" for e in events)


# ── target selection ───────────────────────────────────────────────────
def _names(sel):
    return None if sel is None else tuple(type(x).__name__ for x in sel)


def test_select_none_for_empty_repo(tmp_path):
    assert targets.select(str(tmp_path)) is None


def test_select_http_for_npm_start(tmp_path):
    (tmp_path / "package.json").write_text('{"scripts": {"start": "node server.js"}}')
    assert _names(targets.select(str(tmp_path))) == ("HttpDriver", "V8Tracer")


def test_select_js_harness_for_library_package(tmp_path):
    (tmp_path / "package.json").write_text('{"name": "lib"}')
    assert _names(targets.select(str(tmp_path))) == ("HarnessDriver", "V8Tracer")


def test_select_process_for_go_mod(tmp_path):
    (tmp_path / "go.mod").write_text("module example.com/app\n")
    assert _names(targets.select(str(tmp_path))) == ("ProcessDriver", "GoCoverTracer")


def test_select_py_harness_for_python_repo(tmp_path):
    (tmp_path / "requirements.txt").write_text("flask\n")
    assert _names(targets.select(str(tmp_path))) == ("HarnessDriver", "PyTracer")


def test_pinned_harness_file_wins_and_sets_language(tmp_path):
    (tmp_path / "package.json").write_text('{"scripts": {"start": "node s.js"}}')
    drv, tracer = targets.select(str(tmp_path), harness_file=str(tmp_path / "drive.py"))
    assert (type(drv).__name__, type(tracer).__name__, drv.language) == ("HarnessDriver", "PyTracer", "py")


def test_explicit_driver_overrides_sniff(tmp_path):
    (tmp_path / "package.json").write_text('{"scripts": {"start": "node s.js"}}')
    assert _names(targets.select(str(tmp_path), driver="harness")) == ("HarnessDriver", "V8Tracer")
    assert targets.select(str(tmp_path), driver="process") is None      # no go.mod, no descriptor


def test_descriptor_overrides_sniff(tmp_path):
    (tmp_path / "package.json").write_text('{"scripts": {"start": "x"}}')
    (tmp_path / ".orion").mkdir()
    (tmp_path / ".orion" / "runtime.json").write_text(
        '{"kind":"http","boot":["npm","start"],"base_url":"http://localhost:4000",'
        '"login":{"path":"/login","fields":{"userName":"user1","password":"User1_123"}}}')
    driver, _ = targets.select(str(tmp_path))
    assert driver._base_url == "http://localhost:4000"
    assert driver._login["fields"]["userName"] == "user1"


# ── the writer can only touch runtime facts ────────────────────────────
def test_clears_are_label_scoped_and_runtime_only():
    assert "DETACH DELETE" not in writeback.CLEAR_EDGES and "DELETE r" in writeback.CLEAR_EDGES
    assert writeback.CLEAR_NODES.startswith("MATCH (n:ObservedMethod ")
    for q in writeback.CLEAR_PROPS:
        assert "REMOVE n.executed, n.hit_count" in q and "DELETE" not in q
    for label in NODE_KEY:                       # no static node is ever deleted
        assert f"(n:{label} " not in writeback.CLEAR_NODES
    for q in (writeback.CLEAR_EDGES, writeback.CLEAR_NODES, *writeback.CLEAR_PROPS):
        assert "MATCH (n {" not in q             # never an unlabeled full-partition scan
    for rel in ("FLOWS_TO", "CONTAINS_CALL", "RESOLVES_TO"):
        assert rel not in writeback.CLEAR_EDGES


def test_runtime_labels_are_outside_the_static_clear():
    assert set(NODE_KEY) & set(RUNTIME_NODE_KEY) == set()
    assert ALL_NODE_KEY == {**NODE_KEY, **RUNTIME_NODE_KEY}


def test_plan_to_batch_stamps_origin_and_hits():
    plan = WritePlan(
        new_methods=[{"uid": "u1", "name": "f", "file_path": "a.py", "line": 3, "origin": "runtime"}],
        edges={("OBSERVED_CALL", "CpgMethod", "a.caller", "ObservedMethod", "u1"): 4,
               ("OBSERVED_DISPATCH", "CpgCall", "c1", "CpgMethod", "a.impl"): 1})
    b = writeback.to_batch("s1", plan)
    (cypher, rows), = persist._node_jobs(b.nodes, RUNTIME_NODE_KEY)
    assert "CREATE (n:`ObservedMethod`)" in cypher and rows[0]["props"]["uid"] == "u1"
    by_type = {c.split("[r:`")[1].split("`")[0]: (c, r) for c, r in persist._edge_jobs(b.edges)}
    call_cypher, call_rows = by_type["OBSERVED_CALL"]
    assert "MATCH (a:`CpgMethod`" in call_cypher and "MATCH (b:`ObservedMethod`" in call_cypher
    assert call_rows[0]["props"] == {"origin": "runtime", "hits": 4, "scan_id": "s1"}
    assert call_rows[0]["fk"] == {"full_name": "a.caller", "scan_id": "s1"}      # B2 stamp
    assert by_type["OBSERVED_DISPATCH"][1][0]["fk"] == {"uid": "c1", "scan_id": "s1"}


def test_prop_jobs_carry_scan_id_per_row():
    plan = WritePlan(call_hits={"c1": 2}, method_hits={"m1": 1})
    jobs = writeback._prop_jobs("s1", plan)
    assert [r for _, rows in jobs for r in rows] == [
        {"sid": "s1", "k": "c1", "n": 2}, {"sid": "s1", "k": "m1", "n": 1}]


def test_static_node_rows_unaffected_by_runtime_key():
    b = [("CpgMethod", {"full_name": "a.f", "name": "f", "scan_id": "s"}),
         ("CpgMethod", {"full_name": "a.f", "name": "f", "line": 3, "scan_id": "s"})]
    rows = persist._node_rows(b)
    assert len(rows["CpgMethod"]) == 1 and rows["CpgMethod"][0]["props"]["line"] == 3


# ── report ─────────────────────────────────────────────────────────────
def test_report_names_counts_samples_and_drops():
    txt = report.report_text({"calls_marked": 7, "methods_marked": 3, "new_methods": 2,
                              "observed_calls": 5, "novel_edges": 4, "observed_dispatches": 3,
                              "unreachable_executed": 1, "dropped": {"call_unresolved": 6},
                              "method_samples": [{"name": "reflected", "file": "app/dyn.py", "line": 9}]})
    for s in ("LOWER BOUND", "7 calls + 3 methods", "2 runtime-only", "5 OBSERVED_CALL edges (4 with no",
              "3 OBSERVED_DISPATCH", "reflected  app/dyn.py:9", "call_unresolved=6"):
        assert s in txt


def test_empty_report_is_honest_and_events_are_runtime_phase():
    assert "empty delta" in report.report_text({})
    evs = report.to_events({"new_methods": 1})
    assert evs and all(e["phase"] == "runtime" for e in evs)
