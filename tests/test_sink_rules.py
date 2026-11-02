"""Sink rules 2026-10-28 (merge plan Phase 2, decision D4): the graph's flow ranking no longer
name-matches Joern operator calls, and EXPRESS's `Function` hint is the constructor, not the keyword.
Sink rules 2026-11-02: a real call is judged by its own name and callee, not its arguments;
`Profile.flow_sinks` adds command execution on GENERIC and keeps `$where` on EXPRESS; EXPRESS's `log`
is a word. Orion-LM's code-only sink lines (C1's training format) and the discovery prompt are
unchanged. Pure.
"""
from __future__ import annotations

import re

import pytest

from orion import strategies
from orion.graph import pathfind, profiles
from orion.lm import sinks

LM_EXPRESS = profiles.with_lm_sinks(profiles.EXPRESS)
LM_GENERIC = profiles.with_lm_sinks(profiles.GENERIC)


def _call(name: str, code: str) -> dict:
    return {"name": name, "code": code}


@pytest.mark.parametrize("props,profile,want", [
    # the flood: every tainted `x = y` was prototype_pollution through its Joern name
    (_call("<operator>.assignment", "x = req.body.y"), LM_EXPRESS, None),
    (_call("<operator>.assignment", "data = request.POST['q']"), LM_GENERIC, None),
    # ... and every `a && b` a `log` sink through `<operator>.logicalAnd`
    (_call("<operator>.logicalAnd", "a && b"), profiles.EXPRESS, None),
    (_call("<operator>.logicalOr", "x || y"), profiles.EXPRESS, None),
    # code still matches on an operator call: the sink text is in the code, not in Joern's name
    (_call("<operator>.assignment", "x = eval(req.body.q)"), profiles.EXPRESS, "code_exec"),
    # real sinks keep their categories
    (_call("assign", "Object.assign(target, req.body)"), LM_EXPRESS, "prototype_pollution"),
    (_call("log", "console.log(x)"), profiles.EXPRESS, "log"),
    (_call("eval", "eval(x)"), profiles.EXPRESS, "code_exec"),
    (_call("find", "db.users.find({$where: q})"), profiles.EXPRESS, "nosql"),
    # `Function` is the constructor ...
    (_call("<operator>.new", 'new Function("return " + x)'), profiles.EXPRESS, "code_exec"),
    (_call("Function", "Function(x)()"), profiles.EXPRESS, "code_exec"),
    # ... not the keyword, a comment, or a name that merely contains it. NodeGoat's old #1 code_exec
    # (user-dao.js:57) is no sink at all: "validateLogin" holds no `log` word.
    (_call("<operator>.assignment",
           "this.validateLogin = function(userName, password, callback) {\n  // Helper function\n}"),
     profiles.EXPRESS, None),
    (_call("myFunction", "myFunction(x)"), profiles.EXPRESS, None),
    (_call("Function", "obj.Function(x)"), profiles.EXPRESS, None),
    (_call("handler", "app.get('/', function(req, res) {})"), profiles.EXPRESS, None),
])
def test_classify_sink(props, profile, want):
    assert pathfind._classify_sink(props, profile) == want


@pytest.mark.parametrize("text,want", [
    ("console.log(x)", "log"), ("logger.info(x)", "log"), ("logging.warn(x)", "log"), ("Logger.info(x)", "log"),
    ("LOG.warn(x)", "log"), ("LOGGER.debug(x)", "log"), ("logError(x)", "log"), ("auditLog(x)", "log"),
    ("/login", None), ("isLoggedIn", None), ("validateLogin", None), ("logIn(x)", None), ("logOut(x)", None),
    ("logout(x)", None), ("dialog.show(x)", None), ("catalog.list(x)", None), ("blog.post(x)", None),
    ("LOGIN", None), ("BLOG", None),
])
def test_the_log_hint_is_a_word(text, want):
    assert pathfind._classify_sink(_call("", text), profiles.EXPRESS) == want


@pytest.mark.parametrize("props,profile,want", [
    # the outer call is not the sink of a call in its arguments: each nested call has its own row
    (_call("updateUser", "profile.updateUser(res.render('x', y), z)"), profiles.EXPRESS, None),
    (_call("get", 'app.get("/login", isLoggedIn, handler)'), profiles.EXPRESS, None),
    (_call("create", "comments.objects.create(name='System')"), profiles.GENERIC, None),
    (_call("render", "render(request, 'page.html', {'q': sql_query})"), profiles.GENERIC, None),
    (_call("print", "print(sql_query)"), profiles.GENERIC, None),
    # the real one is matched by its name, its callee, or the head of a chain
    (_call("render", "res.render('x', y)"), profiles.EXPRESS, "render"),
    (_call("log", "console.log(x)"), profiles.EXPRESS, "log"),
    (_call("toArray", "db.users.find(q).toArray(cb)"), profiles.EXPRESS, "nosql"),
    (_call("raw", "User.objects.raw(q)"), profiles.GENERIC, "sql"),
    # a hint that holds the "(" still sees it
    (_call("open", "open(path)"), profiles.with_lm_sinks(profiles.GENERIC), "path"),
    # a call with no "(" in its code keeps its whole text
    (_call("x", "eval x"), profiles.GENERIC, "code_exec"),
    # operator calls keep their whole text: `$where` is only ever a field here
    (_call("<operator>.fieldAccess", "_tmp_5.$where"), profiles.EXPRESS, "nosql"),
    (_call("<operator>.assignment", "x = eval(req.body.q)"), profiles.EXPRESS, "code_exec"),
    (_call("<operator>.logicalAnd", "a && b"), profiles.EXPRESS, None),
    # flow_sinks read the whole text of a real call
    (_call("count", "db.users.count({$where: q})"), profiles.EXPRESS, "nosql"),
    (_call("Popen", "subprocess.Popen(cmd,shell = True,stdout = subprocess.PIPE)"), profiles.GENERIC, "code_exec"),
    (_call("Popen", 'subprocess.Popen(command.split(" "))'), profiles.GENERIC, "code_exec"),
    (_call("popen", "os.popen(cmd)"), profiles.GENERIC, "code_exec"),
    (_call("Popen", "Popen(cmd)"), profiles.GENERIC, "code_exec"),
    (_call("run", "subprocess.run(cmd,shell = True)"), profiles.GENERIC, "code_exec"),
    (_call("check_output", "subprocess.check_output(cmd,shell=True)"), profiles.GENERIC, "code_exec"),
    (_call("call", "sp.subprocess.call(cmd,\n    shell = True)"), profiles.GENERIC, "code_exec"),
    # ... anchored, so neither a safe call nor an outer one is filed
    (_call("run", "subprocess.run(cmd)"), profiles.GENERIC, None),
    (_call("run", "subprocess.run(cmd,shell = False)"), profiles.GENERIC, None),
    (_call("run", "pool.run(subprocess.Popen(cmd),shell = True)"), profiles.GENERIC, None),
    (_call("wrap", "wrap(subprocess.Popen(cmd))"), profiles.GENERIC, None),
    (_call("reopen", "reopen(cmd)"), profiles.GENERIC, None),
    # flow_sinks outrank a lower-severity match by name, and survive with_lm_sinks
    (_call("Popen", "subprocess.Popen(cmd)"), profiles.with_lm_sinks(profiles.GENERIC), "code_exec"),
    (_call("count", "db.users.count({$where: q})"), LM_EXPRESS, "nosql"),
])
def test_classify_flow_sink(props, profile, want):
    assert pathfind._classify_flow_sink(props, profile) == want


def test_classify_flow_sink_with_no_profile():
    assert pathfind._classify_flow_sink(_call("eval", "eval(x)"), None) is None


def test_pathfind_files_a_flow_by_the_new_rules():
    from orion.graph import schema
    b = schema.Batch("s")
    calls = {"src": ("handler", "handler(x)"), "cmd": ("Popen", "subprocess.Popen(x,shell = True)"),
             "outer": ("create", "comments.create(name='System')")}
    for uid, (name, code) in calls.items():
        b.emit_node("CpgCall", {"uid": uid, "name": name, "code": code})
        b.edges.append(("FLOWS_TO", "CpgCall", {"uid": "src"}, "CpgCall", {"uid": uid}, {}))
    pathfind.pathfind(b, profiles.GENERIC)
    flows = {p["sink_uid"]: p["sink_category"] for label, p in b.nodes if label == "CandidateFlow"}
    assert flows == {"cmd": "code_exec"}


def test_the_prompt_still_shows_the_same_sink_vocabulary():
    # SinkPattern is its display text, so the default discovery prompt is byte-identical.
    line = strategies._profile_block(profiles.EXPRESS).splitlines()[-1]
    assert line == ("Dangerous sinks to watch: code_exec: eval, Function, exec, execSync; nosql: find, "
                    "findOne, $where; redirect: redirect; render: render, send; log: log.")


def test_the_generic_prompt_shows_the_same_sink_vocabulary():
    # flow_sinks never reach the prompt: Popen and shell=True are flow ranking only.
    line = strategies._profile_block(profiles.GENERIC).splitlines()[-1]
    assert line == ("Dangerous sinks to watch: code_exec: eval, exec, system, spawn; sql: execute, query, "
                    "raw; redirect: redirect.")
    assert "Popen" not in strategies._profile_block(profiles.GENERIC)
    assert "shell" not in strategies._profile_block(profiles.GENERIC)


def test_the_lm_profiles_keep_their_flow_sinks():
    assert profiles.with_lm_sinks(profiles.GENERIC).flow_sinks == profiles.GENERIC.flow_sinks
    assert LM_EXPRESS.flow_sinks == profiles.EXPRESS.flow_sinks


def test_the_rules_version_is_in_every_pathfind_summary():
    from orion.graph import schema
    assert pathfind.pathfind(schema.Batch("s"), profiles.EXPRESS)["sink_rules"] == pathfind.SINK_RULES


# --- C1 pin: Orion-LM's code-only sink lines are exactly what the old rules gave -----------------

def _old_classify(props: dict, profile) -> str | None:
    """pathfind._classify_sink before 2026-10-28, verbatim."""
    if profile is None:
        return None
    name = str(props.get("name") or "").lower()
    code = str(props.get("code") or "").lower()
    best, best_sev = None, -1
    for category, hints in profile.sink_hints.items():
        for hint in hints:
            h = hint.lower()
            if h and (h in name or h in code):
                sev = pathfind._severity(category)
                if sev > best_sev:
                    best, best_sev = category, sev
                break
    return best


TARGETS = [
    "function run(x) {\n  const q = 'SELECT * FROM t WHERE id=' + x;\n  return db.query(q);\n}",
    "def view(request):\n    path = os.path.join(BASE, request.GET['f'])\n    return open(path).read()",
    "const merged = Object.assign({}, defaults, req.body);\nassignRole(user, merged.role);",
    "logger.log(input);\nconst ok = a && b || c;",
    "data = pickle.loads(request.body)\nyaml.load(stream)",
    "const fn = new Function('return ' + expr);\nreturn fn();",
    "public void doGet(HttpServletRequest r) {\n  Runtime.getRuntime().exec(r.getParameter(\"c\"));\n}",
    "res.redirect(req.query.next);\nres.render('page', {x});",
    "x = 1\ny = x + 2",
    "def run(cmd):\n    return subprocess.Popen(cmd, shell=True)",
    "def run(cmd):\n    return subprocess.check_output(cmd, shell=True)",
    "comments.objects.create(name='System')",
    "app.get('/login', isLoggedIn, handler);",
]


@pytest.mark.parametrize("code", TARGETS)
def test_c1_code_only_sink_lines_are_unchanged(code, monkeypatch):
    case = {"snippets": [{"role": "target", "file": "a.js", "function": "f", "start": 10, "code": code}]}
    new = sinks.annotate(case)
    monkeypatch.setattr(pathfind, "_classify_sink", _old_classify)
    old = sinks.annotate(case)
    assert new == old


@pytest.mark.parametrize("code,want", [
    # literal, not "same as the old rule": a Popen line is `path` through the `open(` hint, as C1 saw it
    ("subprocess.Popen(cmd, shell=True)", "path"),
    ("subprocess.check_output(cmd, shell=True)", None),
    ("comments.objects.create(name='System')", "code_exec"),
    ("print(sql_query)", "sql"),
])
def test_c1_sink_category_on_the_lines_the_flow_rules_changed(code, want):
    case = {"snippets": [{"role": "target", "file": "a.py", "function": "f", "start": 1, "code": code}]}
    assert sinks.annotate(case).get("sink_category") == want


def test_the_lm_code_only_hints_are_the_ones_c1_was_trained_on():
    assert profiles.GENERIC.sink_hints == {"code_exec": ("eval", "exec", "system", "spawn"),
                                           "sql": ("execute", "query", "raw"), "redirect": ("redirect",)}


def test_the_lm_code_only_profile_has_no_regex_hints():
    """The C1 pin above holds because Orion-LM's code-only profile (GENERIC + LM hints) has no
    SinkPattern and is matched with an empty name; adding one there would change C1's sink lines."""
    prof = sinks.default_profile()
    assert not any(hasattr(h, "rx") for hints in prof.sink_hints.values() for h in hints)
    assert re.search(r'"name": ""', open(sinks.__file__, encoding="utf-8").read())
