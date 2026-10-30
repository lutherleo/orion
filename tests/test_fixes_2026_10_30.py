"""Regressions for the 2026-10-30 bug hunt in orion/'s core modules: each test reproduces a defect that was
demonstrated against the real module before it was fixed. Token-free, offline, no Neo4j."""
import io
import json
import sys

from orion import cli, embed, monitor, sarif
from orion.contracts import Lead, Verdict, clean_location
from orion.dedup import dedup
from orion.graph import schema, shortlists
from orion.graph.deps import parse_dependencies
from orion.graph.profiles import EXPRESS


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
