"""Regressions for the 2026-10-30 bug hunt in orion/'s core modules: each test reproduces a defect that was
demonstrated against the real module before it was fixed. Token-free, offline, no Neo4j."""
import io
import json
import os
import stat
import sys

import pytest

from orion import claude_cli, cli, discover, embed, monitor, sarif
from orion.contracts import Lead, Verdict, clean_location
from orion.dedup import dedup
from orion.graph import schema, shortlists
from orion.graph.deps import parse_dependencies
from orion.graph.profiles import EXPRESS
from orion.runtime.correlate import build_line_starts, correlate, index_from_rows
from orion.runtime.trace import ObservedDispatch, RuntimeTrace


def _lead(i=0, shape="A", conf="MEDIUM", **kw):
    return Lead(index=i, shape=shape, text=f"t{i}", evidence="e", confidence=conf, **kw)


# ── contracts ───────────────────────────────────────────────────────────
def test_location_takes_the_verifiers_lines_as_a_pair():
    lead = _lead(file="app/a.js", line_start=10, line_end=12, function="f", cwe="CWE-89")
    v = Verdict(lead=lead, decision="CONFIRM", reason="r", line_start=40)
    assert (v.location()["line_start"], v.location()["line_end"]) == (40, None)     # was 40-12
    region = sarif.to_sarif([v])["runs"][0]["results"][0]["locations"][0]["physicalLocation"]["region"]
    assert region == {"startLine": 40}
    wide = Verdict(lead=_lead(file="a.js", line_start=10, line_end=60), decision="CONFIRM", reason="r", line_start=5)
    assert (wide.location()["line_start"], wide.location()["line_end"]) == (5, None)  # was 5-60
    end_only = Verdict(lead=_lead(file="a.js", line_start=10), decision="CONFIRM", reason="r", line_end=14)
    assert (end_only.location()["line_start"], end_only.location()["line_end"]) == (10, 14)
    below = Verdict(lead=_lead(file="a.js", line_start=10), decision="CONFIRM", reason="r", line_end=3)
    assert (below.location()["line_start"], below.location()["line_end"]) == (10, None)


def test_clean_location_reads_a_cwe_followed_by_its_name():
    assert clean_location({"cwe": "CWE-89: SQL Injection"}) == {"cwe": "CWE-89"}
    assert clean_location({"cwe": "CWE-89 (SQL injection)"}) == {"cwe": "CWE-89"}
    assert clean_location({"cwe": "CWE-22 — Path Traversal"}) == {"cwe": "CWE-22"}
    for junk in ("CWE-79, CWE-80", "CWE-89 - see CWE-90", "sqli", "CWE-89x"):
        assert clean_location({"cwe": junk}) == {}, junk


# ── dedup ───────────────────────────────────────────────────────────────
def test_dedup_does_not_depend_on_the_order_of_an_anchored_and_an_unanchored_duplicate():
    def pair():
        return (_lead(0, conf="HIGH", file="a.js", cwe="CWE-89", function="q"),
                _lead(1, conf="LOW", file="a.js", cwe="CWE-89", function="q", source_uid="s", sink_uid="k"))
    un, an = pair()
    assert [(x.index, x.source_uid) for x in dedup([an, un])] == [(0, "s")]
    un, an = pair()
    assert [(x.index, x.source_uid) for x in dedup([un, an])] == [(0, "s")]          # was both kept
    a2 = _lead(2, file="a.js", cwe="CWE-89", function="q", source_uid="s2", sink_uid="k2")
    assert len(dedup([an, a2])) == 2                     # two anchored flows stay apart, as before


# ── sarif ───────────────────────────────────────────────────────────────
def test_sarif_uri_is_a_percent_encoded_reference_relative_to_srcroot():
    def uri(path):
        v = Verdict(lead=_lead(file=path, cwe="CWE-79"), decision="CONFIRM", reason="r")
        return sarif.to_sarif([v])["runs"][0]["results"][0]["locations"][0]["physicalLocation"]["artifactLocation"]
    assert uri("src/C#/Login.cs")["uri"] == "src/C%23/Login.cs"       # '#' made it point at "src/C"
    assert uri("app/my file.js")["uri"] == "app/my%20file.js"
    assert uri("pages/[id].tsx")["uri"] == "pages/%5Bid%5D.tsx"
    assert uri("/app/index.js") == {"uri": "app/index.js", "uriBaseId": "%SRCROOT%"}
    assert uri("app/routes/index.js")["uri"] == "app/routes/index.js"   # ordinary paths unchanged


# ── monitor / cli output ────────────────────────────────────────────────
def test_a_torn_progress_line_is_skipped_not_fatal(tmp_path):
    (tmp_path / "progress.jsonl").write_text(
        '{"phase": "discover", "event": "start", "detail": "a"}\n{"phase": "discover", "event": "le\n'
        '{"phase": "verify", "event": "verdict", "detail": "CONFIRM"}\n', encoding="utf-8")
    assert [e["event"] for e in monitor.read_events(str(tmp_path))] == ["start", "verdict"]


def test_a_console_that_cannot_encode_the_event_never_breaks_the_scan(tmp_path, monkeypatch):
    out = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")              # Windows stdout when redirected
    monkeypatch.setattr(sys, "stdout", out)
    on_event = monitor.run_logger(str(tmp_path))
    on_event({"phase": "verify", "event": "start", "lead": 3, "detail": "req.query.q → eval()"})
    assert "\\u2192" in (tmp_path / "progress.jsonl").read_text(encoding="utf-8")   # still logged


def test_cli_stdout_replaces_what_it_cannot_encode(monkeypatch):
    raw = io.BytesIO()
    out = io.TextIOWrapper(raw, encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", out)
    cli._safe_stdout()
    print("verified light→full")
    out.flush()
    assert raw.getvalue().decode("cp1252").strip() == "verified light?full"


# ── dependency manifests ────────────────────────────────────────────────
def _deps(tmp_path, files: dict[str, bytes]):
    for name, data in files.items():
        (tmp_path / name).write_bytes(data)
    return parse_dependencies(tmp_path)


def test_manifests_are_decoded_by_their_bom_and_never_crash_the_build(tmp_path):
    a = tmp_path / "a"
    a.mkdir()
    assert _deps(a, {"package.json": b"\xef\xbb\xbf" + json.dumps({"dependencies": {"express": "4.17.1"}}).encode()}) \
        == [("express", "4.17.1")]                                    # was [] (BOM)
    b = tmp_path / "b"
    b.mkdir()
    assert _deps(b, {"requirements.txt": b"\xef\xbb\xbfDjango==3.2.0\nrequests==2.19.0\n"}) == \
        [("Django", "==3.2.0"), ("requests", "==2.19.0")]             # was Django dropped
    c = tmp_path / "c"
    c.mkdir()
    assert _deps(c, {"requirements.txt": "Django==3.2.0\r\nrequests==2.19.0\r\n".encode("utf-16")}) == \
        [("Django", "==3.2.0"), ("requests", "==2.19.0")]             # was UnicodeDecodeError
    d = tmp_path / "d"
    d.mkdir()
    pom = ('<?xml version="1.0" encoding="ISO-8859-1"?><project><name>Caf\xe9</name><dependencies><dependency>'
           '<artifactId>log4j-core</artifactId><version>2.14.1</version></dependency></dependencies></project>')
    assert _deps(d, {"pom.xml": pom.encode("latin-1")}) == [("log4j-core", "2.14.1")]
    e = tmp_path / "e"
    e.mkdir()
    assert _deps(e, {"package.json": b"[]"}) == []                    # was AttributeError


def test_requirement_options_and_go_mod_comments_are_not_parsed_as_versions(tmp_path):
    a = tmp_path / "a"
    a.mkdir()
    assert _deps(a, {"requirements.txt": b"django==3.2.0 \\\n    --hash=sha256:aaaa\n"
                                         b"requests==2.19.0 --hash=sha256:cccc\n"}) == \
        [("django", "==3.2.0"), ("requests", "==2.19.0")]
    b = tmp_path / "b"
    b.mkdir()
    assert _deps(b, {"go.mod": b"module m\n\nrequire (\n\t// vendored fork, see docs\n"
                               b"\tgithub.com/gin-gonic/gin v1.6.0 // indirect\n)\n"}) == \
        [("github.com/gin-gonic/gin", "v1.6.0")]                      # was a phantom ('//', 'vendored')


# ── shortlists ──────────────────────────────────────────────────────────
def test_regexes_built_from_data_are_dynamic_and_every_call_on_a_line_is_seen():
    kinds = lambda line: [k for k, _ in shortlists._regexes_in(line)]
    assert kinds('var r = new RegExp("^" + req.query.q + "$");') == ["dynamic"]
    assert kinds("var r = new RegExp(`^${req.query.q}$`);") == ["dynamic"]
    assert kinds('rx = re.compile(f"^{request.GET["q"]}$")') == ["dynamic"]
    assert kinds('if re.match("^ok$", s) or re.match(user_rx, s): pass') == ["literal", "dynamic"]
    assert shortlists._regexes_in('re.compile("^(a+)+$")') == [("literal", "^(a+)+$")]   # unchanged


def test_separate_comments_with_code_between_are_separate_rows():
    js = ("// TODO: fix xss in the profile page\nrender(profile);\nsave(profile);\n"
          "// insecure: password stored with md5\ndb.save(md5(password));\n")
    rows = shortlists.hedge_rows([("a.js", shortlists._split_file("a.js", js.splitlines()))])
    assert sorted(r["line"] for r in rows) == [1, 4]                  # was only line 1
    block = "// TODO: fix xss here\n// FIXME: and the csrf token\nrender();\n"
    rows = shortlists.hedge_rows([("b.js", shortlists._split_file("b.js", block.splitlines()))])
    assert [r["line"] for r in rows] == [1]                           # one comment block, one row


def test_one_candidate_row_per_uid(tmp_path):
    (tmp_path / "a.js").write_text('s = s.replace(/(\\s+)+$/, "").replace(/(\\s+)+$/, "");\n', encoding="utf-8")
    b = schema.Batch("sid")
    b.emit_node("CpgFile", {"uid": "a.js", "file_path": "a.js"})
    assert shortlists.shortlists(b, EXPRESS, str(tmp_path))["D"] == 1
    d_rows = [p for label, p in b.nodes if label == "CandidateFinding" and p["shape"] == "D"]
    assert [p["rank"] for p in d_rows] == [0] and len({p["uid"] for p in d_rows}) == 1


# ── hybrid search ───────────────────────────────────────────────────────
def test_keyword_query_keeps_unicode_letters_like_the_simple_analyzer():
    assert embed.keyword_query("contraseña del usuario") == "contraseña del usuario"
    assert embed.keyword_query("eval(req.body.preTax) $where") == "eval req body pretax where"


# ── round 3: the agent plumbing (claude_cli, discover, runtime correlate) ─
def test_claude_output_is_decoded_as_utf8_not_the_locale(tmp_path):
    """Windows' cp1252 has no character for 0x8F (in the UTF-8 of the warning sign's U+FE0F): the reader
    died, stdout came back None and run_agent raised out of verify_all."""
    result = {"type": "result", "subtype": "success", "is_error": False,
              "structured_output": {"decision": "CONFIRM", "reason": "caf\u00e9 \u2192 \u26a0\ufe0f",
                                    "file": "src/caf\u00e9.js"}}
    script = tmp_path / "emit.py"
    script.write_text("import sys\nsys.stdout.buffer.write(" + repr((json.dumps(result, ensure_ascii=False) + "\n")
                                                                   .encode("utf-8")) + ")\n", encoding="utf-8")
    out = claude_cli._run_once([sys.executable, str(script)], timeout=60, on_event=None)
    assert out == result["structured_output"]


def test_stream_lines_that_are_not_objects_are_skipped():
    events, final = claude_cli.parse_stream_events('42\nnull\n[1]\n{"type": "result", "result": "{}"}\n')
    assert final == {"type": "result", "result": "{}"} and events == [final]
    assert claude_cli.parse_stream_events(None) == ([], None)


def test_a_launch_failure_is_not_retried(monkeypatch):
    calls, slept = [], []
    monkeypatch.setattr(claude_cli, "_run_once", lambda cmd, **kw: calls.append(cmd) or
                        {"_error": "failed to launch claude -p: [Errno 2] No such file"})
    monkeypatch.setattr(claude_cli.time, "sleep", slept.append)
    out = claude_cli.run_agent("s", "sys", "m", retries=2)
    assert out["_error"].startswith("failed to launch") and len(calls) == 1 and slept == []


def test_a_fenced_reply_whose_strings_quote_a_fence_is_unwrapped():
    obj = {"decision": "CONFIRM", "reason": "r", "evidence": "the code: ```js eval(x)```"}
    assert json.loads(claude_cli._unfence("```json\n" + json.dumps(obj) + "\n```")) == obj
    two = "```json\n{\"a\": 1}\n```\nprose\n```json\n{\"b\": 2}\n```"
    with pytest.raises(ValueError):
        json.loads(claude_cli._unfence(two))           # two fences are still refused


def test_leads_with_non_string_text_evidence_or_uids_are_dropped_or_cleaned():
    leads = discover._to_leads({"leads": [
        {"text": ["a"], "evidence": "e"}, {"text": 7, "evidence": "e"}, {"text": "t", "evidence": {"x": 1}},
        {"text": "ok", "evidence": "e", "source_uid": 5, "sink_uid": ["k"]}]}, "A")
    assert [(l.text, l.source_uid, l.sink_uid) for l in leads] == [("ok", None, None)]


def test_a_reply_without_a_leads_array_is_an_error_not_zero_leads(monkeypatch):
    monkeypatch.setattr(claude_cli, "run_agent", lambda *a, **k: {"findings": [{"text": "x"}]})
    events: list[dict] = []
    assert discover.discover("sid", events.append) == []
    errs = [e for e in events if e["event"] == "error"]
    assert errs and all("no leads array" in e["detail"] for e in errs)
    assert not [e for e in events if e["event"] == "done" and e.get("phase") == "discover" and e.get("shape")]


def test_javascript_line_terminators_end_a_line():
    assert build_line_starts("a\rb\rc") == [0, 2, 4]                  # classic-Mac CR-only file
    assert build_line_starts("a\u2028b\nc") == [0, 2, 4]               # U+2028 LINE SEPARATOR
    assert build_line_starts("a\r\nb\r\nc") == [0, 3, 6]               # CRLF is still one line end


def test_dispatch_goes_on_the_call_named_like_the_callee():
    idx = index_from_rows([{"f": "app/impl.py", "l": 4, "fn": "app.impl.Impl.handle"}],
                          [{"f": "app/a.py", "l": 6, "uid": "U_INFO", "name": "info"},
                           {"f": "app/a.py", "l": 6, "uid": "U_HANDLE", "name": "handle"}])
    trace = RuntimeTrace(dispatches=(ObservedDispatch("app/a.py", 6, "Impl.handle", "app/impl.py", 4),))
    assert correlate(trace, idx, "sid").edges_of("OBSERVED_DISPATCH") == {
        ("CpgCall", "U_HANDLE", "CpgMethod", "app.impl.Impl.handle"): 1}
    anon = RuntimeTrace(dispatches=(ObservedDispatch("app/a.py", 6, "", "app/impl.py", 4),))
    plan = correlate(anon, idx, "sid")                                  # two real calls, no name: not guessed
    assert plan.edges_of("OBSERVED_DISPATCH") == {} and plan.dropped["dispatch_unresolved"] == 1


@pytest.mark.skipif(os.name == "nt", reason="Windows' temp folder is per-user already")
def test_agents_run_from_a_private_folder_not_the_shared_tmp(monkeypatch, tmp_path):
    monkeypatch.setattr(claude_cli.config, "MCP_CONFIG", "")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    d = claude_cli._agent_cwd()
    assert d == tmp_path / "cache" / "orion" / "agent-cwd" and stat.S_IMODE(d.stat().st_mode) & 0o077 == 0


# ── round 4 ─────────────────────────────────────────────────────────────
def test_sarif_rules_carry_the_highest_security_severity_of_their_results():
    def v(cwe, severity, line):
        return Verdict(lead=_lead(file="a.js", cwe=cwe, line_start=line), decision="CONFIRM", reason="r",
                       severity=severity)
    run = sarif.to_sarif([v("CWE-89", "MEDIUM", 1), v("CWE-89", "HIGH", 9), v("CWE-79", None, 3)])["runs"][0]
    rules = {r["id"]: r["properties"] for r in run["tool"]["driver"]["rules"]}
    assert rules["CWE-89"]["security-severity"] == "8.0"            # GitHub reads it here, not on results
    assert "security-severity" not in rules["CWE-79"]                 # no severity given: none invented
    assert [r["properties"].get("security-severity") for r in run["results"]] == ["5.5", "8.0", None]


# scan records and gc (the data-deleting paths)
class _FakeResult:
    def __init__(self, n=0):
        self.n = n

    def consume(self):
        return None

    def single(self):
        return {"n": self.n}


class _FakeSession:
    def __init__(self, log):
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def run(self, query, **kw):
        self.log.append(("run", query))
        return _FakeResult()

    def execute_write(self, fn):
        class Tx:
            def run(tx, query, **kw):
                self.log.append(("write", query))
                return _FakeResult(0)
        return fn(Tx())


class _FakeDriver:
    def __init__(self):
        self.log = []

    def session(self, **kw):
        return _FakeSession(self.log)

    def close(self):
        pass


def test_a_scan_delete_marks_the_record_deleting_before_any_node_goes():
    from orion.graph import scans
    d = _FakeDriver()
    scans.delete_scan(d, "sid", scans.SCAN_LABELS)
    writes = [q for kind, q in d.log if kind == "write"]
    assert "SET r.status" in writes[0] and "DETACH DELETE" not in writes[0]
    assert all("DETACH DELETE" in q for q in writes[1:])
    d2 = _FakeDriver()
    scans.delete_scan(d2, "sid", ("CandidateFlow",))                 # the record is not being deleted
    assert all("DETACH DELETE" in q for kind, q in d2.log if kind == "write")


def test_check_usable_refuses_a_half_deleted_scan_and_an_lm_scan_without_flows(monkeypatch):
    from orion.graph import scans
    record = {"status": "complete", "profile": "", "built_at": "t",
              "counts": {"precompute": {"pathfind": {"error": "RuntimeError: boom"}}}}
    monkeypatch.setattr(scans, "get_record", lambda sid: dict(record, status="deleting"))
    assert "being deleted" in scans.check_usable("s", engine="claude", repo=None)
    monkeypatch.setattr(scans, "get_record", lambda sid: dict(record))
    monkeypatch.setattr(scans, "expected_profile", lambda engine, repo: "generic+lm" if engine == "lm" else None)
    record["profile"] = "generic+lm"
    problem = scans.check_usable("s", engine="lm", repo="r")
    assert "flow search failed" in problem and "orion reanalyze --scan-id s --engine lm" in problem
    record["counts"] = {"precompute": {"pathfind": {"flows": 3}}}
    assert scans.check_usable("s", engine="lm", repo="r") is None
    assert scans.pathfind_error(None) is None


def test_gc_keeps_a_record_that_is_still_building_unless_asked(monkeypatch):
    from orion.graph import scans
    recs = [{"scan_id": "new", "status": "complete", "built_at": "3"},
            {"scan_id": "busy", "status": "building", "built_at": "2"},
            {"scan_id": "old", "status": "complete", "built_at": "1"}]
    deleted = []
    monkeypatch.setattr(scans.GraphDatabase, "driver", lambda *a, **k: _FakeDriver())
    monkeypatch.setattr(scans, "_records", lambda s: [dict(r) for r in recs])
    monkeypatch.setattr(scans, "_unrecorded_ids", lambda s, recorded: set())
    monkeypatch.setattr(scans, "delete_scan", lambda driver, sid, labels: deleted.append(sid) or {})
    out = scans.gc(keep=1)
    assert out["kept"] == ["new"] and out["building"] == ["busy"] and deleted == ["old"]
    deleted.clear()
    out = scans.gc(keep=1, include_building=True)
    assert out["building"] == [] and deleted == ["busy", "old"]
