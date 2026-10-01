"""Precomputed shortlists for shapes B/C/D (orion/graph/shortlists.py) and their opt-in use in
discovery. Token-free: literal fixtures on disk, a hand-built batch, a stubbed run_agent. The one
Neo4j test (persist -> discovery's fetch round trip) skips without a database.

Pins: each detector on literal code, ranking + caps, deterministic uids, that the flag OFF leaves
every discovery prompt and message byte-identical, and that ON inlines rows only for B/C/D.
"""
from __future__ import annotations

import pytest

from orion import claude_cli, config, discover, strategies
from orion.graph import schema, shortlists
from orion.graph.profiles import EXPRESS, GENERIC

SID = "scan-shortlists"

SERVER_JS = """\
var express = require("express");
var csrf = require("csurf");
var app = express();
app.use(csrf());
swig.setDefaults({
    // Autoescape disabled
    autoescape: false
    /*
    // Fix for A3 - XSS, enable auto escaping
    autoescape: true // default value
    */
});
app.use(session({
    secret: "s3cret",
    /*
    // Fix for A3 - XSS
    // TODO: Add "maxAge"
    cookie: {
        httpOnly: true
        // secure: true
    }
    */
}));
var url = "http://example.com/a"; // a URL in a string is code, not a comment
"""

PROFILE_JS = """\
function handle(req) {
    var regexPattern = /([0-9]+)+\\#/;
    var ok = /^\\d+$/.test(req.body.x);
    var dyn = new RegExp(req.query.pattern);
    return regexPattern.test(req.body.bankRouting);
}
"""


def _repo(tmp_path, files: dict[str, str], deps=()) -> tuple[str, schema.Batch]:
    batch = schema.Batch(SID)
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        batch.emit_node("CpgFile", {"uid": rel, "file_path": rel})
    for name, version in deps:
        batch.emit_node("Dependency", {"name": name, "version": version})
    return str(tmp_path), batch


def _split(text: str, rel: str = "a.js"):
    return [(rel, shortlists._split_file(rel, text.splitlines()))]


def _findings(batch, shape):
    return sorted((p for label, p in batch.nodes if label == "CandidateFinding" and p["shape"] == shape),
                  key=lambda p: p["rank"])


# ── comment splitting ──────────────────────────────────────────────────
def test_split_comment_is_quote_aware_and_tracks_blocks():
    code, com, blk = shortlists.split_comment('var u = "http://x"; // note', False, "c")
    assert "http://x" in code and com.strip() == "note" and not blk
    code, com, blk = shortlists.split_comment("a(); /* start", False, "c")
    assert code.strip() == "a();" and com.strip() == "start" and blk
    code, com, blk = shortlists.split_comment("  still inside */ b();", True, "c")
    assert com.strip() == "still inside" and code.strip() == "b();" and not blk
    code, com, _ = shortlists.split_comment("x = '#not' # real", False, "hash")
    assert "'#not'" in code and com.strip() == "real"


# ── shape D: ReDoS heuristic ───────────────────────────────────────────
@pytest.mark.parametrize("rx,expected", [
    (r"([0-9]+)+\#", True), (r"(a+)+", True), (r"(x*)*", True), (r"(\w+\s?)*$", True),
    (r"((a+))+", True), (r"(a{1,})+", True), (r"(?:ab+)*", True),
    (r"^\d+$", False), (r"(ab)+", False), (r"(?:a|b)+", False), (r"[a+]+", False),
    (r"\(a+\)+", False), (r"(a+)?", False), (r"(a+){2}", False),
])
def test_nested_quantifier(rx, expected):
    assert shortlists.nested_quantifier(rx) is expected


def test_pattern_rows_find_redos_dynamic_regex_and_deps(tmp_path):
    _, batch = _repo(tmp_path, {}, deps=[("marked", "0.3.5"), ("express", "4.13.4")])
    rows = shortlists.pattern_rows(_split(PROFILE_JS, "app/profile.js"), batch)
    kinds = [(r["kind"], r.get("line")) for r in rows]
    assert kinds[0] == ("redos_regex", 2)                       # ReDoS ranks first
    assert ("dynamic_regex", 4) in kinds
    assert not any(k == "redos_regex" and ln == 3 for k, ln in kinds)   # /^\d+$/ is safe
    assert [r["detail"] for r in rows if r["kind"] == "dependency"] == ["express@4.13.4", "marked@0.3.5"]
    assert all(r["cwe"] for r in rows)


def test_python_regex_calls():
    rows = shortlists.pattern_rows(_split('import re\nP = re.compile(r"(\\w+\\s?)*$")\nre.match(user, s)\n',
                                          "m.py"), schema.Batch(SID))
    assert [(r["kind"], r["line"]) for r in rows] == [("redos_regex", 2), ("dynamic_regex", 3)]
    assert rows[1]["detail"] == "regex built from `user`"          # the pattern argument only


# ── shape B: controls checklist ────────────────────────────────────────
def test_controls_rows_express(tmp_path):
    rows = shortlists.controls_rows(_split(SERVER_JS, "server.js"), EXPRESS)
    by = {}
    for r in rows:
        by.setdefault(r["detail"].split(":")[0], []).append(r)
    esc = by["output escaping in templates"]
    assert esc[0]["kind"] == "disabled" and esc[0]["line"] == 7           # autoescape: false (code)
    assert any(r["kind"] == "commented_out" for r in esc)                  # autoescape: true, in /* */
    assert by["CSRF protection"][0]["kind"] == "present"
    assert by["session cookie httpOnly"][0]["kind"] == "commented_out"
    assert by["security headers"][0]["kind"] == "absent"                   # no helmet anywhere
    assert by["security headers"][0].get("file_path") is None
    # ranking: every disabled row before every absent row before every present row
    order = [r["kind"] for r in rows]
    assert order.index("disabled") < order.index("absent") < order.index("present")


def test_generic_controls_flag_switched_off_protections():
    py = "DEBUG = True\nrequests.get(url, verify=False)\n@csrf_exempt\ndef view(request): pass\n"
    rows = shortlists.controls_rows(_split(py, "settings.py"), GENERIC)
    disabled = {r["detail"].split(":")[0] for r in rows if r["kind"] == "disabled"}
    assert {"debug mode", "TLS certificate verification", "CSRF protection"} <= disabled


# ── shape C: hedge comments ────────────────────────────────────────────
def test_hedge_rows_find_reverted_fix_once_per_block():
    rows = shortlists.hedge_rows(_split(SERVER_JS, "server.js"))
    assert rows, "the 'Fix for A3 - XSS' blocks must be found"
    top = rows[0]
    assert top["kind"] == "commented_out_fix" and "xss" in top["detail"]
    # The four-line comment block under the session() call yields ONE row, not one per line.
    assert len([r for r in rows if 15 <= r["line"] <= 18]) == 1


def test_hedge_needs_a_security_term_nearby():
    assert shortlists.hedge_rows(_split("// TODO: tidy up the layout\nrender();\n")) == []
    assert shortlists.hedge_rows(_split("x = 1  # insecure: password compared in plain text\n", "a.py"))


# ── the build step ─────────────────────────────────────────────────────
def test_shortlists_emit_ranked_unique_nodes(tmp_path):
    repo, batch = _repo(tmp_path, {"server.js": SERVER_JS, "app/profile.js": PROFILE_JS,
                                   "node_modules/lib/x.js": "var r = /(a+)+/;\n",
                                   "test/t.js": "// TODO insecure password\n"},
                        deps=[("marked", "0.3.5")])
    summary = shortlists.shortlists(batch, EXPRESS, repo)
    assert summary["files"] == 2                                           # node_modules + test skipped
    for shape in "BCD":
        rows = _findings(batch, shape)
        assert [r["rank"] for r in rows] == list(range(len(rows))) and rows
        assert all(r["scan_id"] == SID and r["kind"] for r in rows)
    assert not any(r.get("file_path", "").startswith(("node_modules", "test/")) for r in _findings(batch, "D"))
    uids = [p["uid"] for label, p in batch.nodes if label == "CandidateFinding"]
    assert len(uids) == len(set(uids))
    # Deterministic: a second build of the same repo yields the same uids in the same order.
    again = schema.Batch(SID)
    again.nodes = [n for n in batch.nodes if n[0] != "CandidateFinding"]
    shortlists.shortlists(again, EXPRESS, repo)
    assert [p["uid"] for label, p in again.nodes if label == "CandidateFinding"] == uids


def test_cap_per_shape(tmp_path):
    js = "".join(f"var r{i} = /(a{i}+)+/;\n" for i in range(shortlists.MAX_PER_SHAPE + 15))
    repo, batch = _repo(tmp_path, {"many.js": js})
    assert shortlists.shortlists(batch, EXPRESS, repo)["D"] == shortlists.MAX_PER_SHAPE
    assert len(_findings(batch, "D")) == shortlists.MAX_PER_SHAPE


def test_no_repo_still_lists_dependencies():
    batch = schema.Batch(SID)
    batch.emit_node("Dependency", {"name": "lodash", "version": "4.17.4"})
    assert shortlists.shortlists(batch, GENERIC, None) == {"files": 0, "B": 0, "C": 0, "D": 1}


def test_candidate_finding_is_a_static_label():
    assert schema.NODE_KEY["CandidateFinding"] == ("scan_id", "uid")


# ── prompts: off is byte-identical, on adds the block for B/C/D only ───
def test_prompt_off_is_baseline():
    for shape in "ABCD":
        base = strategies.system_for(shape, "s", profile=EXPRESS)
        assert base == strategies.system_for(shape, "s", profile=EXPRESS, shortlist_hint=False)
        assert "CandidateFinding" not in base


def test_prompt_on_adds_block_for_bcd_only():
    assert strategies.system_for("A", "s", shortlist_hint=True) == strategies.system_for("A", "s")
    for shape in "BCD":
        on = strategies.system_for(shape, "s", shortlist_hint=True)
        off = strategies.system_for(shape, "s")
        assert "START WITH THE PRECOMPUTED SHORTLIST" in on and f"shape:'{shape}'" in on
        assert off.split("\n\n")[0] in on and "GROUNDING RULE" in on


def test_shortlist_message_rendering():
    rows = [{"rank": 0, "kind": "redos_regex", "file_path": "app/p.js", "line": 2, "cwe": "CWE-1333",
             "detail": "nested quantifier", "code": "var r = /(a+)+/;"},
            {"rank": 1, "kind": "dependency", "file_path": None, "line": None, "cwe": "CWE-1104",
             "detail": "marked@0.3.5", "code": None}]
    msg = strategies.shortlist_message("D", rows, 7)
    assert "top 2 of 7" in msg
    assert "#0 [redos_regex] CWE-1333 app/p.js:2 -- nested quantifier  `var r = /(a+)+/;`" in msg
    assert "#1 [dependency] CWE-1104 (repo-wide) -- marked@0.3.5" in msg
    assert "no candidates" in strategies.shortlist_message("B", [], 0)


# ── discovery threading ────────────────────────────────────────────────
def _capture(monkeypatch, fetch=None):
    calls: dict[str, dict] = {}

    def run_agent(sid, system, message, **kw):
        shape = message.split("Shape ")[1][0]
        calls[shape] = {"system": system, "message": message,
                        "kw": {k: v for k, v in kw.items() if k != "on_event"}}
        return {"leads": []}

    monkeypatch.setattr(claude_cli, "run_agent", run_agent)
    fetched: list[str] = []

    def _fetch(scan_id, shape, limit):
        fetched.append(shape)
        if fetch is not None:
            return fetch(scan_id, shape, limit)
        return ([{"rank": 0, "kind": "absent", "file_path": None, "line": None, "cwe": "CWE-693",
                  "detail": f"row for {shape}", "code": ""}], 3)

    monkeypatch.setattr(discover, "_fetch_shortlist", _fetch)
    return calls, fetched


def test_discover_off_is_byte_identical(monkeypatch):
    calls, fetched = _capture(monkeypatch)
    discover.discover(SID, lambda ev: None, EXPRESS)
    base = dict(calls)
    calls.clear()
    discover.discover(SID, lambda ev: None, EXPRESS, shortlists=False)
    assert calls == base and fetched == []
    assert all("SHORTLIST" not in c["message"] and "CandidateFinding" not in c["system"] for c in calls.values())


def test_discover_on_inlines_rows_for_bcd(monkeypatch):
    calls, fetched = _capture(monkeypatch)
    discover.discover(SID, lambda ev: None, EXPRESS, shortlists=True)
    assert sorted(fetched) == ["B", "C", "D"]
    assert "SHORTLIST" not in calls["A"]["message"]
    for shape in "BCD":
        assert f"row for {shape}" in calls[shape]["message"]
        assert "top 1 of 3" in calls[shape]["message"]
        assert "START WITH THE PRECOMPUTED SHORTLIST" in calls[shape]["system"]
    # one model per scan: shortlists never pick a model; every call uses config.MODEL via run_agent
    assert all("model" not in c["kw"] for c in calls.values())


def test_discover_default_follows_config(monkeypatch):
    calls, fetched = _capture(monkeypatch)
    monkeypatch.setattr(config, "SHORTLISTS", True)
    discover.discover(SID, lambda ev: None)
    assert sorted(fetched) == ["B", "C", "D"]


def test_fetch_failure_degrades_to_the_hint_only(monkeypatch):
    def boom(*a):
        raise RuntimeError("neo4j down")
    calls, _ = _capture(monkeypatch, fetch=boom)
    events = []
    discover.discover(SID, events.append, shortlists=True)
    assert "SHORTLIST" not in calls["B"]["message"]
    assert "START WITH THE PRECOMPUTED SHORTLIST" in calls["B"]["system"]
    assert any(e["event"] == "warn" and "neo4j down" in e["detail"] for e in events)


# ── live: persist, then discovery's own fetch query reads it back ──────
def test_persist_and_fetch_round_trip(tmp_path):
    from neo4j import GraphDatabase

    from orion.graph import persist
    drv = GraphDatabase.driver(config.NEO4J_URI, auth=config.NEO4J_AUTH)
    try:
        drv.verify_connectivity()
    except Exception as exc:  # noqa: BLE001
        drv.close()
        pytest.skip(f"Neo4j not reachable: {exc}")
    sid = "test-shortlists-live"
    repo, _ = _repo(tmp_path, {"server.js": SERVER_JS, "app/profile.js": PROFILE_JS})
    batch = schema.Batch(sid)
    batch.nodes = [("CpgFile", {"scan_id": sid, "uid": rel, "file_path": rel})
                   for rel in ("server.js", "app/profile.js")]
    batch.emit_node("Dependency", {"name": "marked", "version": "0.3.5"})
    summary = shortlists.shortlists(batch, EXPRESS, repo)
    try:
        persist.persist(batch)
        for shape in "BCD":
            rows, total = discover._fetch_shortlist(sid, shape, 2)
            assert total == summary[shape] and len(rows) == min(2, total)
            assert [r["rank"] for r in rows] == list(range(len(rows)))
        rows, _ = discover._fetch_shortlist(sid, "D", 5)
        assert rows[0]["kind"] == "redos_regex" and rows[0]["file_path"] == "app/profile.js"
        assert rows[-1]["kind"] == "dependency" and rows[-1]["file_path"] is None
    finally:
        with drv.session(database=config.NEO4J_DATABASE) as s:
            s.run("MATCH (n {scan_id:$sid}) DETACH DELETE n", sid=sid)
        drv.close()
