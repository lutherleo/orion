"""Sink rules 2026-10-28 (merge plan Phase 2, decision D4): the graph's flow ranking no longer
name-matches Joern operator calls, and EXPRESS's `Function` hint is the constructor, not the keyword.
Orion-LM's code-only sink lines (C1's training format) and the discovery prompt are unchanged. Pure.
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
    # (user-dao.js:57) drops to the lowest category: the plain `log` hint is a substring of
    # "validateLogin" -- the `log` hint's own looseness, unchanged here.
    (_call("<operator>.assignment",
           "this.validateLogin = function(userName, password, callback) {\n  // Helper function\n}"),
     profiles.EXPRESS, "log"),
    (_call("myFunction", "myFunction(x)"), profiles.EXPRESS, None),
    (_call("Function", "obj.Function(x)"), profiles.EXPRESS, None),
    (_call("handler", "app.get('/', function(req, res) {})"), profiles.EXPRESS, None),
])
def test_classify_sink(props, profile, want):
    assert pathfind._classify_sink(props, profile) == want


def test_the_prompt_still_shows_the_same_sink_vocabulary():
    # SinkPattern is its display text, so the default discovery prompt is byte-identical.
    line = strategies._profile_block(profiles.EXPRESS).splitlines()[-1]
    assert line == ("Dangerous sinks to watch: code_exec: eval, Function, exec, execSync; nosql: find, "
                    "findOne, $where; redirect: redirect; render: render, send; log: log.")


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
]


@pytest.mark.parametrize("code", TARGETS)
def test_c1_code_only_sink_lines_are_unchanged(code, monkeypatch):
    case = {"snippets": [{"role": "target", "file": "a.js", "function": "f", "start": 10, "code": code}]}
    new = sinks.annotate(case)
    monkeypatch.setattr(pathfind, "_classify_sink", _old_classify)
    old = sinks.annotate(case)
    assert new == old


def test_the_lm_code_only_profile_has_no_regex_hints():
    """The C1 pin above holds because Orion-LM's code-only profile (GENERIC + LM hints) has no
    SinkPattern and is matched with an empty name; adding one there would change C1's sink lines."""
    prof = sinks.default_profile()
    assert not any(hasattr(h, "rx") for hints in prof.sink_hints.values() for h in hints)
    assert re.search(r'"name": ""', open(sinks.__file__, encoding="utf-8").read())
