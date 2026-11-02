"""The harder graph-as-memory questions (eval/graphqa_hard.py): the source oracles on temp trees, the graph's plain call
graph from stubbed rows, the families, and the scoring split. Token-free; no Neo4j."""
import json
import random

from eval import graphqa as gq
from eval import graphqa_hard as gh


def tree(tmp_path, lang, files):
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return gq.SourceTree(tmp_path, lang)


def edges(cg):
    return {(f.name, cg.funcs[t].name) for f in cg.funcs.values() for t in f.calls}


PY = {
    "pkg/__init__.py": "from .helpers import shared\n",
    "pkg/helpers.py": """\
def shared():
    return leaf()


def leaf():
    return 1


def unused_helper():
    return 2
""",
    "pkg/base.py": """\
class Base:
    def inherited(self):
        return 1

    def overridden(self):
        return 2


class Child(Base):
    def overridden(self):
        return super().overridden()

    def go(self):
        self.inherited()
        self.overridden()
        self.missing()
        self.attr.leaf()
        Base.inherited(self)
        Base()
        return Child().go()

    @classmethod
    def build(cls):
        return cls.make()

    @classmethod
    def make(cls):
        return 1

    @staticmethod
    def tool(thing):
        return thing.inherited()
""",
    "pkg/app.py": """\
import os
import pkg.helpers
import pkg.helpers as h2
from pkg import helpers
from pkg import shared
from . import base


def by_name():
    return shared()


def by_module():
    helpers.leaf()
    h2.leaf()
    return pkg.helpers.leaf()


def lazy():
    from pkg.helpers import unused_helper
    return unused_helper()


def shadow(shared):
    leaf = shared
    leaf()
    return shared()


def nested():
    def inner():
        return base.Child.make()
    return [inner() for _ in os.listdir(".")]


def on_object(thing):
    thing.by_name()
    return thing.join("a")


def dunder():
    return shared.__call__()
""",
}


def test_python_oracle_follows_the_stated_rule(tmp_path):
    cg = gh.py_callgraph(tree(tmp_path, "py", PY))
    e = edges(cg)
    assert ("by_name", "shared") in e                   # through the re-export in pkg/__init__.py
    assert ("shared", "leaf") in e
    assert ("by_module", "leaf") in e                   # helpers.leaf, h2.leaf, pkg.helpers.leaf
    assert ("lazy", "unused_helper") in e               # an import inside the function
    assert not {x for x in e if x[0] == "shadow"}       # parameters and locals are not the repo's functions
    assert ("nested", "make") in e                      # a nested function's calls are its parent's
    assert ("go", "inherited") in e                     # self.f in a base class; Base.f
    assert ("overridden", "overridden") in e            # super().f goes to the base
    assert ("go", "overridden") in e
    assert ("go", "go") not in e                        # Child().go() is on an instance
    assert ("build", "make") in e                       # cls.f
    assert ("tool", "inherited") not in e               # a staticmethod's first parameter is not the receiver
    assert not {x for x in e if x[0] == "on_object"}
    cg_f = cg.by_name("on_object")[0]
    assert cg_f.maybe == {"by_name"} and {"by_name", "join"} <= cg_f.named   # kept only when the repo defines the name
    assert cg.by_name("go")[0].maybe >= {"leaf"}        # self.attr.leaf()
    assert not {x for x in e if x[0] == "dunder"}
    assert not cg.unparsed


def test_python_oracle_records_a_file_it_cannot_parse(tmp_path):
    cg = gh.py_callgraph(tree(tmp_path, "py", {"ok.py": "def f():\n    return 1\n", "py2.py": "print 'x'\n"}))
    assert cg.unparsed == ["py2.py"] and list(cg.funcs) == ["ok.py:1"]


def test_reach_is_transitive_and_leaves_out_the_start(tmp_path):
    t = tree(tmp_path, "py", {"m.py": "def a():\n    b()\n\n\ndef b():\n    c()\n    a()\n\n\ndef c():\n    return 1\n"})
    cg = gh.py_callgraph(t)
    assert {cg.funcs[k].name: v for k, v in cg.down("m.py:1").items()} == {"b": 1, "c": 2}
    assert {cg.funcs[k].name: v for k, v in cg.up(["m.py:10"]).items()} == {"b": 1, "a": 2}


JS = {
    "app/dao.js": """\
function UserDAO(db) {
    this.addUser = function (name, callback) {
        this.nextId((err, id) => {
            validate(name);
            callback(err, id);
        });
    };
    this.nextId = (callback) => {
        db.insert({}, callback);
    };
}
const validate = (name) => name.trim();
function next() {}
""",
    "app/routes.js": """\
function Handler(db) {
    const users = new UserDAO(db);
    this.handle = function (req, res, next) {
        users.addUser(req.body.name, (err) => next(err)); // validate( in a comment
        res.send("validate(");
    };
}
function next() {}
const insert = function () {};
""",
    "app/assets/vendor.js": "function vendored() { validate(); }\n",
}


def test_js_oracle_attributes_calls_to_the_innermost_named_function(tmp_path):
    t = tree(tmp_path, "js", JS)
    cg = gh.js_callgraph(t, files=["app/dao.js", "app/routes.js"])
    e = edges(cg)
    assert ("Handler", "UserDAO") in e                  # new X counts
    assert ("handle", "addUser") in e                   # obj.f on any object
    assert ("addUser", "nextId") in e and ("addUser", "validate") in e     # through an anonymous callback
    assert ("UserDAO", "nextId") not in e               # a this.f function is its own function
    assert ("handle", "validate") not in e              # comments and strings are blank
    assert not {x for x in e if x[1] == "next"}         # defined twice: not a name the rule follows
    assert "vendored" not in {f.name for f in cg.funcs.values()}
    assert ("nextId", "insert") in e
    assert cg.by_name("addUser")[0].cls == "UserDAO" and not cg.by_name("addUser")[0].module_level
    assert cg.by_name("UserDAO")[0].module_level
    spec = {"lang": "js", "skip": ("app/assets/",), "not_followed": ("insert",)}
    assert ("nextId", "insert") not in edges(gh.oracle_graph(t, spec))


def method(name, full, file, line):
    return {"name": name, "full_name": full, "file": file, "line": line}


def call(caller, name, callee=None, external=False):
    return {"caller": caller, "name": name, "callee": callee, "external": external}


def test_graph_callgraph_folds_nested_functions_and_ignores_adapters(tmp_path):
    t = tree(tmp_path, "py", {"m.py": "class C:\n    def a(self):\n        def inner():\n            return 1\n"
                                      "        return inner()\n\n\ndef b():\n    return 1\n\n\ndef c():\n    return 1\n"})
    oracle = gh.py_callgraph(t)
    methods = [method("<module>", "m.py:<module>", "m.py", 1),
               method("a", "m.py:<module>.C.a", "m.py", 2),
               method("a<metaClassAdapter>", "m.py:<module>.C.a<metaClassAdapter>", "m.py", 2),
               method("inner", "m.py:<module>.C.a.inner", "m.py", 3),
               method("b", "m.py:<module>.b", "m.py", 8),
               method("c", "m.py:<module>.c", "m.py", 12)]
    calls = [call("m.py:<module>.C.a.inner", "b", "m.py:<module>.b"),       # a nested function's call is its parent's
             call("m.py:<module>.C.a<metaClassAdapter>", "x", "m.py:<module>.c"),   # an adapter calls nothing
             call("m.py:<module>", "c", "m.py:<module>.c"),                 # module level: incoming only, no caller
             call("m.py:<module>.C.a", "append", "ext", external=True)]
    cg, incoming = gh.graph_callgraph(methods, calls, oracle)
    assert sorted(f.name for f in cg.funcs.values()) == ["a", "b", "c"]
    assert edges(cg) == {("a", "b")}
    assert incoming == {"m.py:8": 1, "m.py:12": 1}
    assert cg.by_name("a")[0].named == {"b", "append"} and cg.by_name("a")[0].cls == "C"
    assert cg.by_name("b")[0].module_level


def test_graph_callgraph_names_a_js_lambda_the_oracle_knows(tmp_path):
    t = tree(tmp_path, "js", {"d.js": "function D(db) {\n    this.get1 = function () {\n        go();\n    };\n}\n"
                                      "function go() {}\n"})
    oracle = gh.js_callgraph(t)
    methods = [method("D", "d.js::program:D", "d.js", 1), method("<lambda>0", "d.js::program:D:<lambda>0", "d.js", 2),
               method("go", "d.js::program:go", "d.js", 6)]
    cg, _ = gh.graph_callgraph(methods, [call("d.js::program:D:<lambda>0", "go", "d.js::program:go")], oracle)
    assert edges(cg) == {("get1", "go")}


def test_edge_stats_counts_a_call_on_an_object_apart(tmp_path):
    t = tree(tmp_path, "py", {"m.py": "def a(x):\n    b()\n    x.c()\n\n\ndef b():\n    return 1\n\n\ndef c():\n    return 1\n\n\n"
                                      "def d():\n    b()\n"})
    oracle = gh.py_callgraph(t)
    methods = [method(n, f"m.py:<module>.{n}", "m.py", ln) for n, ln in (("a", 1), ("b", 6), ("c", 10), ("d", 14))]
    calls = [call("m.py:<module>.a", "b", "m.py:<module>.b"), call("m.py:<module>.a", "c", "m.py:<module>.c"),
             call("m.py:<module>.b", "x", "m.py:<module>.c")]
    ref, _ = gh.graph_callgraph(methods, calls, oracle)
    st = gh.edge_stats(oracle, ref)
    assert (st["oracle_edges"], st["graph_edges"], st["common"]) == (2, 3, 1)
    assert (st["oracle_only"], st["graph_only"], st["graph_only_on_an_object"]) == (1, 2, 1)
    assert st["recall"] == 0.5 and st["precision"] == round(1 / 3, 4)


# ───────────────────────────── the families ─────────────────────────────

CHAIN = """\
def top():
    mid1()
    mid2()


def mid1():
    lowest()


def mid2():
    lowest()


def lowest():
    bottom()
    finish()


def bottom():
    open_thing()


def finish():
    return 1


def open_thing():
    import tarfile
    return tarfile.extractall()


def alone():
    return 1


def lonely():
    return 2


def forgotten():
    return 3
"""
USERS = """\
from m import lowest, bottom, finish, open_thing, top


def u1():
    lowest()
    finish()
    print(bottom)


def u2():
    lowest()
    finish()
    open_thing()


def u3():
    lowest()
    finish()
    return top()


def u4():
    return lowest()
"""
SPEC = {"lang": "py", "min_depth": 3}


def graph_of(tmp_path, files=None):
    t = tree(tmp_path, "py", files or {"m.py": CHAIN, "users.py": USERS})
    return t, gh.oracle_graph(t, SPEC)


def ctx(t, cg, ref=None, incoming=None, spec=SPEC):
    return gh.Ctx("x", t, cg, ref, incoming, spec, random.Random(1), gh.source_files(t, spec))


def by_candidate(gen, label):
    return next(b for lab, b in gen if lab == label)()


def fid(cg, name):
    return cg.by_name(name)[0].fid


def test_reach_down_question_has_the_rule_and_the_closure(tmp_path):
    t, cg = graph_of(tmp_path)
    q = by_candidate(gh.gen_reach_down(ctx(t, cg)), fid(cg, "top"))
    assert q["gold"] == sorted(["mid1", "mid2", "lowest", "bottom", "finish", "open_thing"])
    assert q["check"]["depth"]["open_thing"] == 4
    assert "`top` (defined at m.py:1)" in q["question"] and "What counts as a call" in q["question"]
    assert q["reference"] is None and q["graph_agrees"] is False    # no graph given: never counted as agreeing


def test_reach_up_covers_a_repo_function_and_a_library_call(tmp_path):
    t, cg = graph_of(tmp_path)
    q = by_candidate(gh.gen_reach_up(ctx(t, cg)), fid(cg, "bottom"))
    assert q["gold"] == sorted(["lowest", "mid1", "mid2", "top", "u1", "u2", "u3", "u4"])
    q = by_candidate(gh.gen_reach_up(ctx(t, cg, spec=dict(SPEC, min_depth=2))), "lib:extractall")
    assert q["gold"] == sorted(["open_thing", "bottom", "lowest", "mid1", "mid2", "top", "u1", "u2", "u3", "u4"])
    assert "No function named `extractall` is defined" in q["question"]


def test_chain_needs_more_than_one_route_and_lists_both_ends(tmp_path):
    t, cg = graph_of(tmp_path)
    c = ctx(t, cg)
    q = by_candidate(gh.gen_chain(c), f"{fid(cg, 'top')}->{fid(cg, 'open_thing')}")     # through mid1 or mid2
    assert q["gold"] == sorted(["top", "mid1", "mid2", "lowest", "bottom", "open_thing"])
    assert q["check"]["shortest_calls"] == 4


def test_hot_picks_the_highest_k_with_a_usable_answer(tmp_path):
    t, cg = graph_of(tmp_path)
    labels = dict(gh.gen_hot(ctx(t, cg)))
    q = labels["fan-in"]()
    # lowest: mid1, mid2, u1..u4 = 6; finish: lowest, u1, u2, u3 = 4; open_thing: bottom, u2 = 2. K = 2 first gives 3
    assert q["check"]["variant"] == "fan-in" and q["check"]["k"] == 2
    assert q["gold"] == ["finish", "lowest", "open_thing"]
    q = labels["fan-out"]()
    assert q["check"]["variant"] == "fan-out" and q["check"]["k"] == 2


DEAD = """def a():
    b()


def b():
    return 1


def c():
    return 1


def d():
    return 1


def e():
    return 1


def g():
    print(c, d, f"{k()}")
    return e()


def k():
    return 1


def h():
    return 1


def i():
    return 1


def j():
    # h(
    return 'i'
"""


def test_dead_is_about_mentions_and_ignores_comments_and_strings(tmp_path):
    t, cg = graph_of(tmp_path, {"m.py": DEAD})
    q = by_candidate(gh.gen_dead(ctx(t, cg)), "top:")
    assert q["gold"] == ["a", "g", "h", "i", "j"]      # c, d, k are mentioned though only g's f-string calls k
    assert q["check"]["kind"].startswith("module-level") and q["check"]["of"] == 10


def test_pair_both_and_not(tmp_path):
    t, cg = graph_of(tmp_path)
    c = ctx(t, cg)
    both = next(b for lab, b in gh.gen_pair(c) if lab.startswith("both:finish,lowest"))()
    assert both["gold"] == ["u1", "u2", "u3"]
    no = next(b for lab, b in gh.gen_pair(ctx(t, cg)) if lab == "not:lowest,finish")()
    assert no["gold"] == ["mid1", "mid2", "u4"]
    assert "call `lowest` but never call `finish`" in no["question"]


def test_a_graph_that_disagrees_keeps_the_question_and_only_labels_it(tmp_path):
    t, cg = graph_of(tmp_path)
    methods = [method(f.name, f"{f.file}:<module>.{f.name}", f.file, f.line) for f in cg.funcs.values()]
    # the graph has only one of the calls below top, so the plain query's answer is short
    calls = [call("m.py:<module>.top", "mid1", "m.py:<module>.mid1")]
    ref, incoming = gh.graph_callgraph(methods, calls, cg)
    q = by_candidate(gh.gen_reach_down(ctx(t, cg, ref, incoming)), fid(cg, "top"))
    assert q["gold"] == sorted(["mid1", "mid2", "lowest", "bottom", "finish", "open_thing"])
    assert q["reference"]["answer"] == ["mid1"] and q["reference"]["exact"] is False
    assert q["graph_agrees"] is False


def test_generate_takes_the_seeded_order_and_reports_skips(tmp_path):
    t, cg = graph_of(tmp_path)
    qs, skips = gh.generate("x", t, cg, None, None, dict(SPEC, min_depth=2), per_family=1, log=lambda *_: None)
    assert {q["family"] for q in qs} == set(gh.FAMILIES)
    assert [q["id"] for q in qs][0] == "x-reach_down-1"
    again, _ = gh.generate("x", t, cg, None, None, dict(SPEC, min_depth=2), per_family=1, log=lambda *_: None)
    assert [q["question"] for q in again] == [q["question"] for q in qs]
    assert all(isinstance(v, int) for v in skips.values())


def test_no_two_questions_share_an_answer(tmp_path):
    t, cg = graph_of(tmp_path)
    qs, _ = gh.generate("x", t, cg, None, None, dict(SPEC, min_depth=2), per_family=2, log=lambda *_: None)
    golds = [tuple(q["gold"]) for q in qs]
    assert len(golds) == len(set(golds))


# ───────────────────────────── running and scoring ─────────────────────────────

def test_check_scans_names_a_rebuilt_fixture():
    data = {"fixtures": {"a": {"scan_id": "A", "built_at": "t1"}, "b": {"scan_id": "B", "built_at": "t2"}}}
    assert gh.check_scans(data, stamp=lambda s: {"A": "t1", "B": "t9"}[s]) == ["b: built t2, now t9"]


def test_run_passes_the_turn_limit_and_records_it(tmp_path):
    seen = []

    def agent(session, **kw):
        seen.append(kw["max_turns"])
        kw["on_event"]({"event": "usage", "detail": json.dumps({"input_tokens": 1, "output_tokens": 1})})
        return {"answer": ["a"]}
    q = {"id": "q1", "rev": 1, "fixture": "x", "family": "hot", "answer_kind": "name", "question": "?", "gold": ["a"],
         "scan_id": "s"}
    data = {"fixtures": {"x": {"export": str(tmp_path), "repo": str(tmp_path)}}, "questions": [q]}
    log = tmp_path / "s.jsonl"
    gq.run_all(data, models=("m",), paths=("grep",), sessions_path=log, agent=agent, max_turns=40, log=lambda *_: None)
    assert seen == [40]
    assert json.loads(log.read_text().splitlines()[0])["max_turns"] == 40


def test_agreement_splits_by_whether_the_plain_query_is_right():
    data = {"questions": [{"id": "a", "graph_agrees": True, "reference": {"exact": True, "f1": 1.0}},
                          {"id": "b", "graph_agrees": False, "reference": {"exact": False, "f1": 0.5}},
                          {"id": "c", "graph_agrees": False, "reference": None}]}
    res = {"rows": [{"model": "m", "path": "graph"}],
           "sessions": [{"qid": "a", "model": "m", "path": "graph", "exact": True},
                        {"qid": "b", "model": "m", "path": "graph", "exact": True},
                        {"qid": "c", "model": "m", "path": "graph", "exact": False}]}
    a = gh.agreement(data, res)
    assert a["rows"][0]["plain_right"] == [1, 1] and a["rows"][0]["plain_wrong"] == [1, 2]
    assert a["plain_query"] == {"exact": 1, "n": 3, "f1": 0.5}


def test_score_and_table_use_the_files_own_families():
    q = {"id": "q1", "rev": 1, "fixture": "x", "family": "chain", "answer_kind": "name", "question": "?", "gold": ["a"],
         "scan_id": "s"}
    rec = {"qid": "q1", "rev": 1, "model": gq.MODELS[0], "path": "grep", "status": "ok", "answer": ["a"], "usage": [],
           "wall_s": 1.0}
    res = gq.score({"fixtures": {"x": {}}, "questions": [q]}, [rec])
    assert res["families"] == ["chain"]
    assert "| chain |" in gq.table(res)


def test_running_out_of_turns_is_an_outcome_not_a_retry(tmp_path):
    calls = []

    def agent(session, **kw):
        calls.append(session)
        return {"_error": "claude -p exited 1 | stderr: (empty) | stdout_tail: Reached maximum number of turns (15)"}
    q = {"id": "q1", "rev": 1, "fixture": "x", "family": "hot", "answer_kind": "name", "question": "?", "gold": ["a"],
         "scan_id": "s"}
    data = {"fixtures": {"x": {"export": str(tmp_path), "repo": str(tmp_path)}}, "questions": [q]}
    log = tmp_path / "s.jsonl"
    gq.run_all(data, models=("m",), paths=("grep",), sessions_path=log, agent=agent, sleep=lambda *_: None,
               log=lambda *_: None)
    assert len(calls) == 1
    assert json.loads(log.read_text().splitlines()[0])["status"] == "failed"


def test_the_rule_says_which_names_are_not_followed(tmp_path):
    t = tree(tmp_path, "js", {"a.js": "function f() {}\n"})
    c = gh.Ctx("x", t, gh.CallGraph(), None, None, {"lang": "js", "not_followed": ("insert", "test")},
               random.Random(1), ["a.js"])
    assert "`insert` and `test` are not followed" in c.rule


def test_a_stated_budget_reaches_both_paths_and_the_record(tmp_path):
    seen = {}

    def agent(session, **kw):
        seen[bool(kw.get("use_mcp"))] = kw["system"]
        return {"answer": ["a"]}
    q = {"id": "q1", "rev": 1, "fixture": "x", "family": "hot", "answer_kind": "name", "question": "?", "gold": ["a"],
         "scan_id": "s"}
    fx = {"export": str(tmp_path), "repo": str(tmp_path)}
    recs = [gq.run_session(q, fx, "m", p, agent=agent, max_turns=30, budget=25) for p in ("graph", "grep")]
    assert all("at most 25 rounds" in s for s in seen.values()) and len(seen) == 2
    assert [r["budget"] for r in recs] == [25, 25]


def test_no_budget_leaves_the_earlier_prompts_alone(tmp_path):
    seen = {}

    def agent(session, **kw):
        seen[bool(kw.get("use_mcp"))] = kw["system"]
        return {"answer": []}
    q = {"id": "q1", "rev": 1, "fixture": "x", "family": "hot", "answer_kind": "name", "question": "?", "gold": ["a"],
         "scan_id": "s"}
    fx = {"export": str(tmp_path), "repo": str(tmp_path)}
    gq.run_session(q, fx, "m", "graph", agent=agent)
    gq.run_session(q, fx, "m", "grep", agent=agent)
    assert seen[True] == gq.graph_system("s") and seen[False] == gq.grep_system(str(tmp_path))


def test_a_session_cut_off_by_the_plan_limit_waits_on_every_attempt(tmp_path):
    outcomes = iter(["cut", "cut", "ok"])
    waits = []

    def agent(session, on_event=None, **kw):
        if next(outcomes) == "ok":
            return {"answer": ["a"]}
        on_event({"event": "usage", "detail": json.dumps({"output_tokens": 500, "total_cost_usd": 0.1})})
        return {"_error": "claude -p exited 1 | stderr: (empty) | stdout_tail: r 5-hour limit · resets 12:10am (America/New_York)"}
    q = {"id": "q1", "rev": 1, "fixture": "x", "family": "hot", "answer_kind": "name", "question": "?", "gold": ["a"],
         "scan_id": "s"}
    data = {"fixtures": {"x": {"export": str(tmp_path), "repo": str(tmp_path)}}, "questions": [q]}
    log = tmp_path / "s.jsonl"
    gq.run_all(data, models=("m",), paths=("grep",), sessions_path=log, agent=agent, sleep=waits.append, limit_wait=0,
               log=lambda *_: None)
    rows = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]
    assert [r["status"] for r in rows] == ["aborted", "aborted", "ok"]


def test_running_out_of_turns_is_not_a_plan_limit():
    assert not gq._limit_hit("claude -p exited 1 | stdout_tail: Reached maximum number of turns (30)", [{"output_tokens": 9}])


def test_a_run_and_its_score_can_be_narrowed_to_some_fixtures():
    data = {"fixtures": {"a": {"x": 1}, "b": {"x": 2}}, "questions": [{"id": "a1", "fixture": "a"}, {"id": "b1", "fixture": "b"}]}
    assert gh.select_fixtures(data, "") is data
    one = gh.select_fixtures(data, "b")
    assert list(one["fixtures"]) == ["b"] and [q["id"] for q in one["questions"]] == ["b1"]
