"""Graph-as-memory Q&A (eval/graphqa.py): question generation from stubbed graph rows, the gold checks against a
temp source tree, scoring, and the session runner with a fake agent. Token-free; no Neo4j."""
import json

import pytest

from eval import graphqa as gq


# ───────────────────────────── helpers ─────────────────────────────

class FakeGraph:
    """`rows(key, **params)` answered from a dict: a list, or a callable(params) -> list."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def rows(self, key, **params):
        self.calls.append((key, params))
        a = self.answers.get(key, [])
        return a(params) if callable(a) else list(a)


def tree(tmp_path, lang, files):
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return gq.SourceTree(tmp_path, lang)


def method(name, file, line, full=None):
    return {"name": name, "full_name": full or f"{file}::program:{name}", "file": file, "line": line}


PROFILE_JS = {"lang": "js", "sink_hints": {"code_exec": ("eval",), "redirect": ("redirect",)},
              "manifest": "package.json", "manifest_desc": "package.json", "dep_example": "express@^4.13.4",
              "routes": {"style": "express", "file": "app/routes/index.js", "variants": [
                  ("no-login", (), ("isLoggedIn",), "do NOT pass the `isLoggedIn` middleware")]}}
PROFILE_PY = {"lang": "py", "sink_hints": {"code_exec": ("eval",)}, "manifest": "requirements.txt",
              "manifest_desc": "requirements.txt", "dep_example": "Django@4.2",
              "routes": {"style": "django", "urls": "intro/urls.py", "views": "intro/views.py", "variants": [
                  ("no-auth", (), ("authentication_decorator",), "has no auth decorator")]}}


# ───────────────────────────── normalisation and scoring ─────────────────────────────

def test_norm_loc_strips_roots_and_accepts_line_spellings():
    roots = ("/home/u/.cache/orion/graphqa/NodeGoat-c5cb", "/home/u/orion/fixtures/NodeGoat")
    assert gq.norm_loc("/home/u/.cache/orion/graphqa/NodeGoat-c5cb/app/routes/index.js:34", roots) == \
        "app/routes/index.js:34"
    assert gq.norm_loc("`./app/routes/index.js#L34`") == "app/routes/index.js:34"
    assert gq.norm_loc("app\\routes\\index.js line 34") == "app/routes/index.js:34"
    assert gq.norm_loc("app/routes/index.js:34-40") == "app/routes/index.js:34"


def test_norm_name_dep_route():
    assert gq.norm_name("views.xss_lab()") == "xss_lab"
    assert gq.norm_name("`def generate_api_key`") == "generate_api_key"
    assert gq.norm_dep("express@^4.13.4") == gq.norm_dep("express@4.13.4") == "express@4.13.4"
    assert gq.norm_dep("Django==4.2") == gq.norm_dep("django@==4.2") == "django@4.2"
    assert gq.norm_dep("argon2_cffi@21.3.0") == "argon2-cffi@21.3.0"
    assert gq.norm_dep("@scope/pkg@~1.2.0") == "@scope/pkg@1.2.0"
    assert gq.norm_route("get /login/") == "GET /login"
    assert gq.norm_route("xssL") == gq.norm_route("/xssL") == "/xssL"
    assert gq.normalize([""], "route") == ["/"]          # Django's empty route is a real answer
    assert gq.normalize(["", "a.js:1"], "loc") == ["a.js:1"]


def test_score_answer_sets():
    s = gq.score_answer(["a", "b", "c"], ["a", "b", "d", "e"])
    assert s["precision"] == pytest.approx(2 / 3) and s["recall"] == pytest.approx(0.5)
    assert s["f1"] == pytest.approx(2 * (2 / 3) * 0.5 / (2 / 3 + 0.5)) and not s["exact"]
    assert gq.score_answer(["b", "a"], ["a", "b"])["exact"]
    empty = gq.score_answer([], ["a"])
    assert empty["precision"] == empty["recall"] == empty["f1"] == 0.0 and not empty["exact"]


# ───────────────────────────── source reading ─────────────────────────────

def test_code_only_blanks_comments_and_strings_and_keeps_line_count():
    js = 'a(); // b()\n/* c()\n d() */ e("f()")\nconst s = `g()`;\n'
    out = gq.code_only(js, "js", keep_strings=False)
    assert len(out) == js.count("\n") + 1
    assert "b(" not in out[0] and "c(" not in out[1] and "d(" not in out[2] and "e(" in out[2]
    assert "f()" not in out[2] and "g()" not in out[3]
    assert 'e("f()")' in gq.code_only(js, "js")[2]     # keep_strings (route paths are read from strings)
    py = 'x = 1  # y()\ndef f():\n    """doc z()\n    more w()"""\n    return v()\n'
    out = gq.code_only(py, "py", keep_strings=False)
    assert "y(" not in out[0] and "z(" not in out[2] and "w(" not in out[3] and "v(" in out[4]


def test_body_extent_js_and_python(tmp_path):
    t = tree(tmp_path, "js", {"a.js": "function A(db) {\n  if (x) {\n    y();\n  }\n}\nconst b = (x) => x + 1;\n"
                                      "this.c = function ({a}) {\n  z();\n};\n"})
    assert gq.body_extent(t, "a.js", 1) == (1, 5)
    assert gq.body_extent(t, "a.js", 6) == (6, 6)           # expression-bodied arrow
    assert gq.body_extent(t, "a.js", 7) == (7, 9)           # destructured params: the body brace, not the param one
    p = tree(tmp_path / "py", "py", {"v.py": "def f(a,\n      b):\n    x = 1\n\n    return g(x)\n\ndef h():\n    pass\n"})
    assert gq.body_extent(p, "v.py", 1) == (1, 5)


# ───────────────────────────── question generation and the gold checks ─────────────────────────────

def test_lookup_confirms_against_source_and_drops_a_wrong_line(tmp_path):
    t = tree(tmp_path, "js", {"app/a.js": "\nfunction ResearchHandler(db) {\n}\n",
                              "app/b.js": "const errorHandler = (err, req) => {\n};\n"})
    rows = {"ResearchHandler": [{"item": "app/a.js:2", "file": "app/a.js", "line": 2}],
            "errorHandler": [{"item": "app/b.js:9", "file": "app/b.js", "line": 9}]}   # graph line is wrong
    g = FakeGraph({"methods": [method("ResearchHandler", "app/a.js", 2), method("errorHandler", "app/b.js", 9)],
                   "lookup": lambda p: rows[p["name"]]})
    qs, drops, _ = gq.generate("nodegoat", g, t, families=("lookup",), per_family=2, profile=PROFILE_JS, log=lambda *_: 0)
    assert [q["gold"] for q in qs] == [["app/a.js:2"]]
    assert qs[0]["gold_cypher"] == gq.CYPHER["lookup"] and qs[0]["gold_params"] == {"name": "ResearchHandler"}
    assert len(drops) == 1 and drops[0]["candidate"] == "errorHandler" and "not a definition" in drops[0]["reason"]


def test_lookup_drops_when_the_source_defines_the_name_again(tmp_path):
    t = tree(tmp_path, "js", {"app/a.js": "function parseResponse(r) {\n}\n",
                              "test/b.js": "function parseResponse(x) {\n}\n"})     # a file the graph never parsed
    g = FakeGraph({"methods": [method("parseResponse", "app/a.js", 1)],
                   "lookup": [{"item": "app/a.js:1", "file": "app/a.js", "line": 1}]})
    qs, drops, _ = gq.generate("x", g, t, families=("lookup",), per_family=1, profile=PROFILE_JS, log=lambda *_: 0)
    assert not qs and "test/b.js:1" in drops[0]["reason"]


def test_callers_need_every_lexical_call_site(tmp_path):
    src = {"app/dao.js": "function AllocationsDAO(db) {\n  return new AllocationsDAO(db);\n}\n",
           "app/r.js": "const d = new AllocationsDAO(db);\n// AllocationsDAO(db) in a comment\n",
           "app/s.js": "const e = new AllocationsDAO(db);\n"}
    m = method("AllocationsDAO", "app/dao.js", 1)
    full = [{"item": f"{f}:{l}", "file": f, "line": l} for f, l in (("app/dao.js", 2), ("app/r.js", 1), ("app/s.js", 1))]
    ok = FakeGraph({"methods": [m], "resolved": [{"full_name": m["full_name"], "n": 3}], "callers": full})
    qs, drops, _ = gq.generate("x", ok, tree(tmp_path / "a", "js", src), families=("calls",), per_family=1,
                               profile=PROFILE_JS, log=lambda *_: 0)
    assert qs[0]["gold"] == ["app/dao.js:2", "app/r.js:1", "app/s.js:1"] and not drops
    missing = FakeGraph({"methods": [m], "resolved": [{"full_name": m["full_name"], "n": 2}], "callers": full[:2]})
    qs, drops, _ = gq.generate("x", missing, tree(tmp_path / "b", "js", src), families=("calls",), per_family=1,
                               profile=PROFILE_JS, log=lambda *_: 0)
    assert not qs and "app/s.js:1" in drops[0]["reason"]


def test_callees_drop_when_the_graph_misses_a_lambda_named_function(tmp_path):
    """`this.getUserById = function` is `<lambda>N` in the graph: a body calling it must not pass as complete."""
    src = {"app/u.js": ("function UserDAO(db) {\n  this.getUserById = function (id) {\n  };\n"
                        "  validate(1);\n  this.getUserById(2);\n}\nfunction validate(x) {\n}\n")}
    m = method("UserDAO", "app/u.js", 1)
    g = FakeGraph({"methods": [m, method("validate", "app/u.js", 7)], "resolved": [],
                   "callees": [{"item": "validate", "file": "app/u.js", "line": 4}]})
    qs, drops, _ = gq.generate("x", g, tree(tmp_path, "js", src), families=("calls",), per_family=1,
                               profile=PROFILE_JS, log=lambda *_: 0)
    assert not qs and any("getUserById" in d["reason"] for d in drops)


def test_callees_pass_when_complete(tmp_path):
    src = {"app/u.js": "function validateUserDoc(err, user) {\n  if (comparePassword(1, 2)) {\n  }\n}\n"
                       "function comparePassword(a, b) {\n}\n"}
    g = FakeGraph({"methods": [method("validateUserDoc", "app/u.js", 1), method("comparePassword", "app/u.js", 5)],
                   "resolved": [],
                   "callees": lambda p: ([{"item": "comparePassword", "file": "app/u.js", "line": 2}]
                                         if p["full_name"].endswith("validateUserDoc") else [])})
    qs, drops, skips = gq.generate("x", g, tree(tmp_path, "js", src), families=("calls",), per_family=1,
                                   profile=PROFILE_JS, log=lambda *_: 0)
    assert qs[0]["gold"] == ["comparePassword"] and qs[0]["check"]["direction"] == "callees"


def test_deps_against_an_independent_manifest_parse(tmp_path):
    pj = json.dumps({"dependencies": {"express": "^4.13.4", "marked": "0.3.5"}, "devDependencies": {"zaproxy": "^0.2.0"}})
    rows = [{"item": "express@^4.13.4", "name": "express", "version": "^4.13.4"},
            {"item": "marked@0.3.5", "name": "marked", "version": "0.3.5"},
            {"item": "zaproxy@^0.2.0", "name": "zaproxy", "version": "^0.2.0"}]
    import re

    def deps(p):
        return [r for r in rows if p["version_rx"] is None or re.fullmatch(p["version_rx"], r["version"])]
    qs, drops, _ = gq.generate("x", FakeGraph({"deps": deps}), tree(tmp_path / "a", "js", {"package.json": pj}),
                               families=("deps",), per_family=2, profile=PROFILE_JS, log=lambda *_: 0)
    assert [q["gold"] for q in qs] == [["express@4.13.4", "marked@0.3.5", "zaproxy@0.2.0"],
                                       ["marked@0.3.5", "zaproxy@0.2.0"]]
    short = FakeGraph({"deps": lambda p: [r for r in deps(p) if r["name"] != "zaproxy"]})
    qs, drops, _ = gq.generate("x", short, tree(tmp_path / "b", "js", {"package.json": pj}),
                               families=("deps",), per_family=2, profile=PROFILE_JS, log=lambda *_: 0)
    # "all" and "major0" lack zaproxy and drop; the third variant (exact versions) does not involve it
    assert [d["candidate"] for d in drops] == ["all", "major0"] and all("zaproxy" in d["reason"] for d in drops)
    assert [q["gold"] for q in qs] == [["marked@0.3.5"]] and qs[0]["check"]["variant"] == "exact"


def test_express_routes_ignore_commented_out_registrations(tmp_path):
    src = {"app/routes/index.js": ('app.get("/", h.welcome);\napp.post("/login", h.login);\n'
                                   'app.get("/dashboard", isLoggedIn, h.dash);\n'
                                   '/*\napp.get("/benefits", h.benefits);\n*/\n')}
    rows = [{"item": "GET /", "file": "app/routes/index.js", "line": 1},
            {"item": "POST /login", "file": "app/routes/index.js", "line": 2}]
    qs, drops, _ = gq.generate("x", FakeGraph({"routes_express": rows}), tree(tmp_path, "js", src),
                               families=("routes_noauth",), per_family=1, profile=PROFILE_JS, log=lambda *_: 0)
    assert qs[0]["gold"] == ["GET /", "POST /login"] and not drops


def test_django_routes_read_decorators_and_the_empty_route(tmp_path):
    src = {"intro/urls.py": "urlpatterns = [\n    path('', views.home),\n    path('xssL', views.xss_lab),\n]\n",
           "intro/views.py": "def home(request):\n    pass\n\n@authentication_decorator\ndef xss_lab(request):\n    pass\n"}
    rows = [{"item": "", "file": "intro/urls.py", "line": 2, "view": "home"}]
    qs, drops, _ = gq.generate("x", FakeGraph({"routes_django": rows}), tree(tmp_path, "py", src),
                               families=("routes_noauth",), per_family=1, profile=PROFILE_PY, log=lambda *_: 0)
    assert qs[0]["gold"] == ["/"] and not drops


PY_SQL = ("def sql_lab(request):\n"
          "    name=request.POST.get('name')\n"
          "    password=request.POST.get('pass')\n"
          "    sql_query = \"SELECT '\"+name+\"' AND '\"+password+\"'\"\n"
          "    print(sql_query)\n")


def test_flow_gaps_backward_and_forward(tmp_path):
    t = tree(tmp_path, "py", {"v.py": PY_SQL})
    assert gq.flow_gaps(t, "v.py", {3}, 5) == [2]            # `name` also reaches the sink through sql_query
    assert gq.flow_gaps(t, "v.py", {2, 3}, 5) == []
    t2 = tree(tmp_path / "m", "py", {"m.py": ("def lab(request):\n    ip = request.POST.get('ip')\n"
                                              "    res, err = run(ip)\n    res = res.decode()\n"
                                              "    return out(str(res), str(err))\n")})
    assert gq.flow_gaps(t2, "m.py", {2, 3}, 5) == [4]        # `res = res.decode()`, the mitre.py:243 case


JS_CALLBACK = ("const {\n    environmentalScripts\n} = require(\"../../config/config\");\n"   # 1-3
               "function H(db) {\n    const dao = new DAO(db);\n"                                       # 4-5
               "    this.show = (req, res, next) => {\n        const { userId } = req.session;\n"      # 6-7
               "        dao.getByUserId(userId, (error, contrib) => {\n"                                # 8
               "            if (error) return next(error);\n            contrib.userId = userId;\n"     # 9-10
               "            return res.render(\"contributions\", {\n                ...contrib,\n"      # 11-12
               "                environmentalScripts\n            });\n        });\n    };\n}\n")      # 13-17


def test_flow_gaps_follow_a_callback_parameter_past_an_attribute_assignment(tmp_path):
    t = tree(tmp_path, "js", {"c.js": JS_CALLBACK})
    # the NodeGoat contributions.js:21 case: `contrib` reaches the sink as the DAO callback's parameter (line 8),
    # which FLOWS_TO (return values only) never has; `contrib.userId = ...` (line 10) does not end the search
    assert gq.flow_gaps(t, "c.js", {3, 11}, 11) == [8]
    assert gq.flow_gaps(t, "c.js", {3, 8, 11}, 11) == []
    early = JS_CALLBACK.replace("(error, contrib) => {\n", "(error, contrib) => { done(contrib); }); {\n")
    assert gq.flow_gaps(tree(tmp_path / "e", "js", {"c.js": early}), "c.js", {3, 11}, 11) == []  # body ends first
    assert gq._reads("{...contrib, a.b, f(x)}") == {"contrib", "a", "x"}


def test_flow_path_question_and_drop(tmp_path):
    def chain(lines):
        return lambda p: [{"item": f"v.py:{l}", "file": "v.py", "line": l, "name": "get" if l < 5 else "print"}
                          for l in lines]
    flows = [{"rank": 0, "kind": "log", "sink_uid": "s", "file": "v.py", "line": 5, "name": "print", "code": ""}]
    prof = dict(PROFILE_PY, sink_hints={"code_exec": ("eval",), "log": ("print",)})
    ok = FakeGraph({"flows": flows, "flow_path": chain([2, 3, 5])})
    qs, drops, _ = gq.generate("x", ok, tree(tmp_path / "a", "py", {"v.py": PY_SQL}), families=("flow_path",),
                               per_family=1, profile=prof, log=lambda *_: 0)
    assert qs[0]["gold"] == ["v.py:2", "v.py:3", "v.py:5"]
    bad = FakeGraph({"flows": flows, "flow_path": chain([3, 5])})
    qs, drops, _ = gq.generate("x", bad, tree(tmp_path / "b", "py", {"v.py": PY_SQL}), families=("flow_path",),
                               per_family=1, profile=prof, log=lambda *_: 0)
    assert not qs and "[2]" in drops[0]["reason"]
    # a sink filed under a kind its line has no call of (the `log` hint on `app.get("/login", ...)`) is dropped
    junk = FakeGraph({"flows": [dict(flows[0], kind="code_exec")], "flow_path": chain([2, 3, 5])})
    qs, drops, _ = gq.generate("x", junk, tree(tmp_path / "c", "py", {"v.py": PY_SQL}), families=("flow_path",),
                               per_family=1, profile=prof, log=lambda *_: 0)
    assert not qs and drops[0]["reason"] == "sink v.py:5 has no code_exec call in the source"


def test_entry_sink_needs_every_listed_entry_point_with_a_sink(tmp_path):
    src = {"app/c.js": "function ContributionsHandler(db) {\n  this.u = (req) => {\n    const x = eval(req.body.a);\n"
                       "  };\n}\n",
           "app/p.js": "function ProfileHandler(db) {\n  const y = eval(\"1\");\n}\n"}
    eps = [method("ContributionsHandler", "app/c.js", 1), method("ProfileHandler", "app/p.js", 1)]
    rows = [{"item": "ContributionsHandler", "full_name": eps[0]["full_name"], "file": "app/c.js", "line": 3}]
    g = FakeGraph({"methods": eps, "entrypoints": eps, "sink_kinds": [{"kind": "code_exec", "n": 1}],
                   "entry_sink": rows})
    qs, drops, _ = gq.generate("x", g, tree(tmp_path, "js", src), families=("entry_sink",), per_family=1,
                               profile=PROFILE_JS, log=lambda *_: 0)
    # ProfileHandler's eval has no graph flow, so the source can't confirm the answer set: dropped, never asked
    assert not qs and "ProfileHandler" in drops[0]["reason"]
    assert g.calls[-1][1]["sink_names"] == ["eval"]
    # with both flows in the graph it is asked, and the names are the whole sink list, not examples
    rows.append({"item": "ProfileHandler", "full_name": eps[1]["full_name"], "file": "app/p.js", "line": 2})
    qs, _, _ = gq.generate("x", g, tree(tmp_path, "js", src), families=("entry_sink",), per_family=1,
                           profile=PROFILE_JS, log=lambda *_: 0)
    assert qs[0]["gold"] == ["ContributionsHandler", "ProfileHandler"]
    assert "a call to `eval` (Orion's code_exec sinks)" in qs[0]["question"]
    assert "Only those names count" in qs[0]["question"] and "`x.eval(...)`" in qs[0]["question"]
    assert gq._sink_names({"sink_hints": {"k": ("eval", "exec", "system", "spawn")}}, "k") == \
        "`eval`, `exec`, `system` or `spawn`"


# ───────────────────────────── the runner and scoring ─────────────────────────────

DATA = {"status": "proposed", "fixtures": {"f": {"export": "/x/f", "repo": "/r/f"}},
        "drops": {"total": 3, "by_family": {"calls": 3}},
        "questions": [{"id": "q1", "fixture": "f", "family": "lookup", "answer_kind": "loc", "question": "Where?",
                       "gold": ["a.js:1"], "scan_id": "sid"},
                      {"id": "q2", "fixture": "f", "family": "deps", "answer_kind": "dep", "question": "Deps?",
                       "gold": ["x@1.0"], "scan_id": "sid"}]}


def _usage(out=10, cost=0.01):
    return {"event": "usage", "detail": json.dumps({"input_tokens": 5, "output_tokens": out,
                                                    "cache_creation_input_tokens": 0,
                                                    "cache_read_input_tokens": 100, "total_cost_usd": cost,
                                                    "num_turns": 2})}


def test_run_session_paths_use_the_right_tools(monkeypatch):
    from orion import config
    monkeypatch.setattr(config, "MODEL", "unchanged")
    seen = []

    def agent(sid, **kw):
        seen.append(kw)
        kw["on_event"](_usage())
        kw["on_event"]({"event": "tool", "detail": "MATCH ..."})
        return {"answer": ["/x/f/a.js:1"]}
    g = gq.run_session(DATA["questions"][0], DATA["fixtures"]["f"], "claude-sonnet-5", "graph", agent=agent)
    r = gq.run_session(DATA["questions"][0], DATA["fixtures"]["f"], "claude-sonnet-5", "grep", agent=agent)
    assert seen[0]["use_mcp"] and seen[0]["extra_allowed"] == ("mcp__orion__get_schema",) and "add_dir" not in seen[0]
    assert 'scan_id="sid"' in seen[0]["system"]
    assert not seen[1]["use_mcp"] and seen[1]["add_dir"] == "/x/f" and seen[1]["extra_allowed"] == ("Read", "Grep", "Glob")
    assert all(k["max_turns"] == 15 and k["effort"] == "xhigh" and k["retries"] == 0 for k in seen)
    assert g["status"] == r["status"] == "ok" and g["cypher_calls"] == 1 and r["cypher_calls"] is None
    assert config.MODEL == "claude-sonnet-5"


def test_limit_detection():
    assert gq._limit_hit("claude -p exited 1 | stderr: (empty) | stdout_tail: Claude AI usage limit reached|1760000000",
                         [])
    assert gq._limit_hit("agent reported error: subtype='error_during_execution'", [{"output_tokens": 0}])
    assert not gq._limit_hit("agent reported error: subtype='error_max_turns'", [{"output_tokens": 0}])
    assert not gq._limit_hit("agent reported error: subtype='error_during_execution'", [{"output_tokens": 50}])


def test_run_all_waits_on_a_limit_retries_once_and_resumes(tmp_path):
    log = tmp_path / "s.jsonl"
    plan = {("q1", "graph"): [{"_error": "claude -p exited 1 | stdout_tail: You've hit your limit"},
                              {"answer": ["a.js:1"]}],
            ("q1", "grep"): [{"_error": "claude -p exited 1 | stderr: boom"}, {"answer": ["b.js:1"]}],
            ("q2", "graph"): [{"_error": "agent reported error: subtype='error_max_turns'"}],
            ("q2", "grep"): [{"answer": ["x@^1.0"]}]}
    sleeps = []

    def agent(sid, **kw):
        q = "q1" if "Where?" in kw["message"] else "q2"
        p = "graph" if kw["use_mcp"] else "grep"
        kw["on_event"](_usage(out=0 if p == "graph" and q == "q1" and len(plan[(q, p)]) == 2 else 10))
        return plan[(q, p)].pop(0)
    gq.run_all(DATA, models=("claude-sonnet-5",), sessions_path=log, agent=agent, sleep=sleeps.append,
               limit_wait=0, log=lambda *_: 0)
    recs = [json.loads(l) for l in log.read_text().splitlines()]
    assert [r["status"] for r in recs] == ["aborted", "ok", "retried", "ok", "failed", "ok"]
    assert 30 in sleeps                                   # the one transient retry waited
    # a second run finds everything finished
    n = gq.run_all(DATA, models=("claude-sonnet-5",), sessions_path=log, agent=agent, sleep=sleeps.append,
                   limit_wait=0, log=lambda *_: 0)
    assert n == {"ran": 0, "skipped": 4}

    res = gq.score(DATA, recs)
    graph, grep = res["rows"]
    assert graph["path"] == "graph" and graph["n"] == 2 and graph["failed"] == 1 and graph["exact"] == 1
    assert graph["f1"] == pytest.approx(0.5) and graph["f1_completed"] == pytest.approx(1.0)
    assert grep["exact"] == 1 and grep["f1"] == pytest.approx(0.5)       # b.js:1 is wrong, x@^1.0 = x@1.0
    # every attempt's usage is billed: graph = aborted + ok + failed, grep = retried + ok + ok
    assert graph["sessions_billed"] == 3 and grep["sessions_billed"] == 3
    assert graph["tokens"] == 2 * 115 + 105 and graph["tokens_per_correct"] == 2 * 115 + 105
    assert res["total_usage"]["sessions"] == 6 and res["total_usage"]["tokens_missing"] == 0
    md = gq.table(res)
    assert "| claude-sonnet-5 | graph | 2 | 1 |" in md and "Gold-check drops while generating: 3" in md


def test_a_reworded_question_gets_a_new_rev_and_only_it_runs_again(tmp_path):
    old = [dict(q) for q in DATA["questions"]]                     # written before revs existed: rev 1
    new = [dict(old[0]), dict(old[1], question="Deps, declared?"), dict(old[0], id="q3")]
    gq.carry_revs(new, old)
    assert [q["rev"] for q in new] == [1, 2, 1]
    gq.carry_revs(again := [dict(q) for q in new], new)
    assert [q["rev"] for q in again] == [1, 2, 1]                  # unchanged on a re-generate

    log = tmp_path / "s.jsonl"
    base = {"model": "claude-sonnet-5", "path": "grep", "status": "ok", "wall_s": 1.0}
    log.write_text("".join(json.dumps(dict(base, qid=q, answer=a, usage=[json.loads(_usage()["detail"])])) + "\n"
                           for q, a in (("q1", ["a.js:1"]), ("q2", ["y@2.0"]))))
    data = dict(DATA, questions=new[:2])
    asked = []

    def agent(sid, **kw):
        asked.append(kw["message"].split("\n")[0])
        kw["on_event"](_usage())
        return {"answer": ["x@1.0"]}
    n = gq.run_all(data, models=("claude-sonnet-5",), paths=("grep",), sessions_path=log, agent=agent,
                   log=lambda *_: 0)
    assert n == {"ran": 1, "skipped": 1} and asked == ["Deps, declared?"]
    recs = [json.loads(l) for l in log.read_text().splitlines()]
    assert recs[-1]["rev"] == 2
    res = gq.score(data, recs)
    row = res["rows"][0]
    assert row["n"] == 2 and row["exact"] == 2 and row["sessions_billed"] == 2   # the rev-1 q2 answer is not scored
    sup = res["total_usage"]["superseded"]
    assert res["total_usage"]["sessions"] == 3 and sup["sessions"] == 1 and sup["tokens"] == 115
    assert "answered an earlier rev" in gq.table(res)


def test_missing_usage_is_missing_not_zero():
    u = gq.session_usage({"usage": [{"input_tokens": 5, "output_tokens": None}]})
    assert u["tokens"] is None and u["input_tokens"] == 5 and u["total_cost_usd"] is None
    res = gq.score(DATA, [{"qid": "q1", "model": "claude-opus-5", "path": "grep", "status": "failed",
                           "usage": [], "answer": None, "error": "claude -p timed out after 900s"}])
    row = res["rows"][0]
    assert row["tokens"] is None and row["tokens_missing"] == 1 and row["cost_usd"] is None
    assert row["exact"] == 0 and row["tokens_per_correct"] is None
