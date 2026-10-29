"""orion/ui/static (merge plan Phase 7): the page renders untrusted repo text as text only, loads nothing from
outside the server, keeps the server CSP's rules (no inline script, style or handler), and speaks the API of
spec section 5. Its pure helpers -- event formatting, usage sums, phase states, verify progress, locations,
the options payload, routes, the access wording -- run under node (skipped where node is absent); three are
pinned against the Python they mirror: monitor._format_event, contracts.Verdict.location() and the report's
verdict order. Token-free: no server, no network, no browser."""
from __future__ import annotations

import dataclasses
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from orion import monitor, report
from orion.contracts import Lead, Verdict

STATIC = Path(__file__).resolve().parents[1] / "orion" / "ui" / "static"
APP_JS = STATIC / "app.js"
FILES = ("index.html", "app.js", "app.css")
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not on PATH")


def _text(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def _node(tmp_path: Path, body: str):
    """Run `body` (a JS function body; `app` is app.js's exports) under node and return its JSON result."""
    probe = tmp_path / "probe.js"
    probe.write_text(f"const app = require({json.dumps(str(APP_JS))});\n"
                     f"const result = (function () {{\n{body}\n}})();\n"
                     "process.stdout.write(JSON.stringify(result));\n", encoding="utf-8")
    proc = subprocess.run([NODE, str(probe)], capture_output=True, text=True, encoding="utf-8", timeout=60,
                          cwd=tmp_path, env={**os.environ, "TZ": "UTC"})
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _ev(phase, event, **kw) -> dict:
    """A ProgressEvent as orion/cli.py writes it."""
    return {"ts": "2026-10-29T03:50:00+00:00", "phase": phase, "shape": kw.get("shape"), "lead": kw.get("lead"),
            "turn": kw.get("turn"), "event": event, "detail": kw.get("detail", "")}


def _usage(**fields) -> dict:
    """A usage event as claude_cli._emit_usage writes it: no phase, the detail a JSON string."""
    return {"event": "usage", "detail": json.dumps(fields)}


# ── The files and what is (not) in them ──────────────────────────────────────────────────────────────────

def test_the_three_files_exist():
    for name in FILES:
        assert (STATIC / name).is_file(), name
        assert (STATIC / name).stat().st_size > 0, name


@pytest.mark.parametrize("needle", ["innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(",
                                    "new Function", "DOMParser", "createContextualFragment", "setHTML",
                                    "parseHTML", "iframe", "import("])
def test_app_js_never_parses_html_or_evaluates_strings(needle):
    assert needle not in _text("app.js")


def test_timers_never_take_a_string():
    assert not re.search(r"\bset(?:Timeout|Interval)\s*\(\s*[\"'`]", _text("app.js"))


@pytest.mark.parametrize("name", FILES)
def test_no_external_url(name):
    text = _text(name)
    assert not re.search(r"https?://", text, re.IGNORECASE)
    assert not re.search(r"""(?:src|href|action)\s*=\s*["']?//""", text, re.IGNORECASE)   # protocol-relative


def test_css_loads_nothing():
    css = _text("app.css")
    assert "url(" not in css and "@import" not in css


def test_index_html_keeps_the_csp_rules():
    html = _text("index.html")
    scripts = re.findall(r"<script\b([^>]*)>(.*?)</script\s*>", html, re.IGNORECASE | re.DOTALL)
    assert len(scripts) == len(re.findall(r"<script\b", html, re.IGNORECASE)) == 1
    attrs, body = scripts[0]
    assert body.strip() == "", "an inline script body"
    assert re.search(r"""\bsrc\s*=\s*["']/static/app\.js["']""", attrs)
    assert "<style" not in html.lower()
    assert not re.search(r"\sstyle\s*=", html, re.IGNORECASE)
    assert not re.search(r"\son[a-z]+\s*=", html, re.IGNORECASE)
    assert "javascript:" not in html.lower()
    for url in re.findall(r"""\b(?:src|href)\s*=\s*["']([^"']*)["']""", html, re.IGNORECASE):
        assert url.startswith(("/static/", "#")), url


def test_index_html_has_the_page_frame():
    html = _text("index.html")
    assert re.search(r'<html[^>]*\blang="en"', html)
    assert 'name="viewport"' in html and 'charset="utf-8"' in html
    assert 'id="view"' in html
    assert re.search(r'id="status"[^>]*aria-live="polite"', html)
    for nav in ('href="#/"', 'href="#/new"', 'href="#/settings"'):
        assert nav in html


def test_app_js_never_writes_styles_or_handler_attributes():
    js = _text("app.js")
    assert not re.search(r"\.style\b", js)                                   # CSP: no inline style, even via CSSOM
    assert not re.search(r"""setAttribute\(\s*["'](?:on|style)""", js, re.IGNORECASE)
    assert not re.search(r"\.on[a-z]+\s*=", js)                              # handlers via addEventListener only


def test_the_token_stays_in_this_tab():
    js = _text("app.js")
    assert "document.cookie" not in js
    # localStorage holds one thing, Settings' default model (no secret); the token never goes there.
    for line in js.splitlines():
        if "localStorage." in line:
            assert "DEFAULT_MODEL_KEY" in line, line
    assert 'DEFAULT_MODEL_KEY = "orion-default-model"' in js
    assert "sessionStorage" in js and '"orion-token"' in js
    assert 'history.replaceState(null, "", location.pathname + location.hash)' in js
    assert '"X-Orion-Token"' in js
    lines = [line for line in js.splitlines() if '?t="' in line]   # "?t=" closing a JS string literal
    assert len(lines) == 2
    for line in lines:   # only the two routes that cannot send a header carry it in the URL
        assert "/stream?t=" in line or "/files/" in line, line
    assert 'new EventSource(url)' in js and '"/stream?t=" + encodeURIComponent(' in js


@pytest.mark.parametrize("path", ["/api/state", '"/api/runs"', '"/api/runs/"', '"/api/job"', "/api/job/stop",
                                  "/api/scans", "/api/access", "/api/compare?a=", "&b=", "/events?offset=",
                                  "/stream?t=", "/files/", "/code", "?file=", "&line=", "&end=", "/resume"])
def test_app_js_speaks_the_server_api(path):
    assert path in _text("app.js")


def test_app_js_listens_for_every_stream_event():
    js = _text("app.js")
    for name in ("progress", "output", "job", "verdicts", "end", "error"):
        assert f'addEventListener("{name}"' in js, name


def test_app_js_uses_every_api_method():
    js = _text("app.js")
    for call in ('api("GET", "/api/state")', 'api("GET", "/api/runs")', 'api("POST", "/api/scans"',
                 'api("POST", "/api/job/stop")', 'api("POST", "/api/access"', 'api("DELETE", "/api/access")',
                 'api("GET", "/api/access")', 'api("GET", "/api/job")'):
        assert call in js, call


@pytest.mark.parametrize("copy", [
    "printed in your terminal.",
    "Start your first scan",
    "Runs the target repo's own code. Only the harness driver with Sandbox docker keeps it in a ",
    "Force stop",
    "reported by the CLI",
    "not reported",
    "billed to your API account",
    "experimental; your code is sent there",
    "Saved in the OS keychain (",
    "Not saved: no keychain on this machine; held until orion ui stops",
    "From environment",
    "Forget key",
    "Compatible endpoint (experimental)",
    "Show all ",
    "Show code",
])
def test_the_page_says_what_the_spec_says(copy):
    assert copy in _text("app.js")


def test_the_copy_never_says_please():
    for name in FILES:
        assert "please" not in _text(name).lower(), name


def test_secret_inputs_are_never_filled_or_remembered():
    js = _text("app.js")
    for field in ("f-key", "f-token"):
        m = re.search(r'h\("input", \{\s*type: "password", id: "' + field + r'"[^}]*\}', js)
        assert m, field
        decl = m.group(0)
        assert 'autocomplete: "off"' in decl and 'spellcheck: "false"' in decl
        assert "value:" not in decl, "a secret input is filled from the server"
    assert 'keyInput.value = "";' in js and 'tokenInput.value = "";' in js


def test_css_has_both_themes_visible_focus_and_the_decision_colours():
    css = _text("app.css")
    assert "@media (prefers-color-scheme: dark)" in css
    assert ":focus-visible" in css and "outline:" in css
    assert "prefers-reduced-motion" in css
    for name in ("confirm", "inconclusive", "reject", "error"):
        assert f"--{name}:" in css and f".d-{name}" in css
    assert "--mono:" in css and "monospace" in css and "system-ui" in css


# ── node: syntax and the pure helpers ────────────────────────────────────────────────────────────────────

@needs_node
def test_node_check_passes():
    proc = subprocess.run([NODE, "--check", str(APP_JS)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr


@needs_node
def test_the_pure_helpers_are_exported(tmp_path):
    names = _node(tmp_path, "return Object.keys(app);")
    for name in ("formatEvent", "sumUsage", "phaseStates", "verifyProgress", "effectiveLocation", "sortVerdicts",
                 "collectOptions", "parseRoute", "accessLine", "keptText", "h", "safeHref"):
        assert name in names, name


FORMAT_CASES = [
    _ev("discover", "tool", shape="A", lead=3, turn=2, detail="MATCH (n {scan_id:$scan_id}) RETURN n"),
    {"event": "tool", "detail": "MATCH (c:CpgCall) RETURN c.code"},   # the verifier's raw tool call: no phase
    _ev("verify", "verdict", shape="A", lead=0, detail="CONFIRM"),     # lead 0 still shows
    _ev("build", "done"),                                              # an empty detail adds nothing
    {"phase": "report", "event": "start", "detail": "rendering 3 verdicts"},
    _ev("discover", "warn", shape="", turn=0, detail='<img src=x onerror="alert(1)"> stays text'),
    {"phase": "runtime", "event": "done", "detail": "line one\nline two"},
]


@needs_node
def test_format_event_matches_the_terminal_monitor(tmp_path):
    got = _node(tmp_path, f"return {json.dumps(FORMAT_CASES)}.map(app.formatEvent);")
    assert got == [monitor._format_event(e) for e in FORMAT_CASES]


@needs_node
def test_usage_events_are_summarised_as_tokens(tmp_path):
    events = [
        _usage(input_tokens=1200, output_tokens=300, cache_creation_input_tokens=None, cache_read_input_tokens=45000,
               cache_creation_5m_input_tokens=7, total_cost_usd=0.5),   # the 5m/1h split is part of cache_creation
        {**_usage(input_tokens=None, output_tokens=None, total_cost_usd=None), "phase": "discover", "shape": "A"},
    ]
    got = _node(tmp_path, f"return {json.dumps(events)}.map(app.formatEvent);")
    assert got == ["[usage] 46,500 tokens", "[usage] tokens not reported"]


@needs_node
def test_usage_sums_tokens_and_the_cli_reported_cost(tmp_path):
    events = [
        _usage(input_tokens=100, output_tokens=50, cache_creation_input_tokens=10, cache_read_input_tokens=1000,
               total_cost_usd=0.25),
        _usage(input_tokens=5, total_cost_usd=None),
        {"event": "usage", "detail": "{torn"},                                  # unreadable: not a call at all
        {"event": "usage", "detail": {"input_tokens": 7, "total_cost_usd": 0.004}},   # already parsed
        _usage(input_tokens=True, total_cost_usd="1.5"),                        # wrong types count for nothing
        _ev("verify", "tool", detail="MATCH (n) RETURN n"),
    ]
    got = _node(tmp_path, f"""
        const s = app.sumUsage({json.dumps(events)});
        const none = app.sumUsage([]);
        return {{ s, tokens: app.tokensText(s), cost: app.costText(s),
                  noneTokens: app.tokensText(none), noneCost: app.costText(none) }};""")
    assert got["s"]["calls"] == 4
    assert got["s"]["tokens"] == 1172 and got["s"]["tokenCalls"] == 3
    assert got["s"]["costCalls"] == 2 and abs(got["s"]["cost"] - 0.254) < 1e-9
    assert got["tokens"] == "1,172 tokens"
    assert got["cost"] == "$0.25 reported by the CLI"
    assert got["noneTokens"] == "not reported" and got["noneCost"] == "not reported"


PHASE_LOGS = {
    "empty": [],
    "building": [_ev("build", "start", detail="building graph for fixtures/NodeGoat")],
    "built": [_ev("build", "start"), _ev("build", "timing", detail="parse: 3.1s"), _ev("build", "done")],
    "runtime": [_ev("build", "start"), _ev("build", "done"), _ev("runtime", "start")],
    "shape_done_is_not_the_phase": [_ev("build", "done"), _ev("discover", "start"),
                                    _ev("discover", "start", shape="A"), _ev("discover", "done", shape="A")],
    "discovered": [_ev("build", "done"), _ev("discover", "start"), _ev("discover", "done", shape="A"),
                   _ev("discover", "done", detail="3 candidate leads")],
    "shape_error": [_ev("build", "done"), _ev("discover", "start"), _ev("discover", "error", shape="B", detail="timeout"),
                    _ev("discover", "done", detail="0 candidate leads")],
    "verifying": [_ev("build", "done"), _ev("discover", "done", detail="2 candidate leads"), _ev("verify", "start"),
                  _ev("verify", "start", shape="A", lead=0), _ev("verify", "verdict", shape="A", lead=0, detail="CONFIRM")],
    "finished": [_ev("build", "start"), _ev("build", "done"), _ev("discover", "start"),
                 _ev("discover", "done", detail="1 candidate leads"), _ev("verify", "start"),
                 _ev("verify", "verdict", lead=0, detail="REJECT"), _ev("verify", "done", detail="1 verdicts"),
                 _ev("report", "start"), _ev("report", "done")],
    "phaseless_ignored": [_ev("build", "start"), {"event": "tool", "detail": "q"}, _usage(input_tokens=3)],
    "resumed": [_ev("build", "done"), _ev("discover", "done", detail="4 candidate leads"), _ev("verify", "start"),
                _ev("verify", "verdict", lead=0, detail="CONFIRM"),
                _ev("build", "done", detail="resuming .orion/runs/s/t: 4 leads, 1 already verified"),
                _ev("verify", "start", detail="verifying 3 leads (1 already verified)")],
    "lm_engine": [_ev("build", "done"), _ev("discover", "start", shape="LM"),
                  _ev("discover", "done", shape="LM", detail="2 candidate leads from 9 flows")],
}


@needs_node
def test_phase_states_follow_the_event_log(tmp_path):
    got = _node(tmp_path, f"""
        const logs = {json.dumps(PHASE_LOGS)};
        const out = {{}};
        for (const k of Object.keys(logs)) out[k] = app.phaseStates(logs[k]).map(p => p.name + ":" + p.state);
        return out;""")
    assert got["empty"] == ["build:pending", "discover:pending", "verify:pending", "report:pending"]
    assert got["building"][0] == "build:active"
    assert got["built"][0] == "build:done" and got["built"][1] == "discover:pending"
    assert got["runtime"][:3] == ["build:done", "runtime:active", "discover:pending"]   # runtime only once seen
    assert got["shape_done_is_not_the_phase"][1] == "discover:active"
    assert got["discovered"][1] == "discover:done" and got["discovered"][2] == "verify:pending"
    assert got["shape_error"][1] == "discover:error"   # an error event marks its phase, even after its done
    assert got["verifying"] == ["build:done", "discover:done", "verify:active", "report:pending"]
    assert got["finished"] == ["build:done", "discover:done", "verify:done", "report:done"]
    assert got["phaseless_ignored"] == ["build:active", "discover:pending", "verify:pending", "report:pending"]
    assert got["resumed"] == ["build:done", "discover:done", "verify:active", "report:pending"]
    assert got["lm_engine"][1] == "discover:active"     # only the CLI's unscoped discover/done finishes it


@needs_node
def test_phase_state_wording(tmp_path):
    got = _node(tmp_path, """
        const p = (state, errors, current) => ({ state, errors, current });
        return [app.phaseStateText(p("pending", 0, false), true), app.phaseStateText(p("active", 0, true), true),
                app.phaseStateText(p("done", 0, false), true), app.phaseStateText(p("error", 1, false), true),
                app.phaseStateText(p("error", 2, true), true), app.phaseStateText(p("error", 2, true), false)];""")
    assert got == ["Waiting", "Running", "Done", "1 error", "Running, 2 errors", "2 errors"]


@needs_node
def test_verify_progress_counts_distinct_leads_against_the_discovered_total(tmp_path):
    cases = {
        "main": [_ev("discover", "done", shape="A", detail="3 lead(s)"), _ev("discover", "done", detail="12 candidate leads"),
                 _ev("verify", "verdict", lead=0, detail="CONFIRM"), _ev("verify", "verdict", lead=1, detail="ERROR"),
                 _ev("verify", "verdict", lead=1, detail="REJECT")],   # a retried ERROR is still one lead
        "lm": [_ev("discover", "done", shape="LM", detail="5 candidate leads from 9 flows"),
               _ev("discover", "done", detail="5 candidate leads, 1 batches not judged"),
               _ev("verify", "verdict", shape="LM", lead=2, detail="CONFIRM: re-judged")],
        "fallback": [_ev("verify", "verdict", lead=0, detail="CONFIRM")],
        "none": [],
    }
    got = _node(tmp_path, f"""
        const c = {json.dumps(cases)};
        return {{ main: app.verifyProgress(c.main), lm: app.verifyProgress(c.lm),
                  fallback: app.verifyProgress(c.fallback, 7), none: app.verifyProgress(c.none),
                  texts: [app.progressText({{ done: 2, total: 12 }}), app.progressText({{ done: 3, total: null }})] }};""")
    assert got["main"] == {"done": 2, "total": 12}
    assert got["lm"] == {"done": 1, "total": 5}
    assert got["fallback"] == {"done": 1, "total": 7}
    assert got["none"] == {"done": 0, "total": None}
    assert got["texts"] == ["2 of 12", "3 verified"]


LEAD = dict(index=0, shape="A", text="SQL injection", evidence="e", confidence="HIGH", file="app/a.js",
            line_start=4, line_end=6, function="handler", cwe="CWE-89")
BARE_LEAD = dict(index=1, shape="B", text="hedge comment", evidence="e", confidence="LOW")
VERDICT_OVERRIDES = [{}, {"line_start": 5}, {"file": "app/a.js", "line_start": 9, "line_end": 9},
                     {"file": "app/b.js"}, {"file": "app/b.js", "line_start": 12, "cwe": "CWE-79"}, {"cwe": "CWE-79"}]


@needs_node
def test_effective_location_matches_verdict_location(tmp_path):
    verdicts = [Verdict(lead=Lead(**lead), decision="CONFIRM", reason="r", **over)
                for lead in (LEAD, BARE_LEAD) for over in VERDICT_OVERRIDES]
    payload = [dataclasses.asdict(v) for v in verdicts]
    got = _node(tmp_path, f"return {json.dumps(payload)}.map(app.effectiveLocation);")
    assert got == [v.location() for v in verdicts]


@needs_node
def test_where_text(tmp_path):
    got = _node(tmp_path, """return [app.whereText({ file: "a.js", line_start: 4, line_end: 6 }),
        app.whereText({ file: "a.js", line_start: 4, line_end: 4 }), app.whereText({ file: "a.js" }),
        app.whereText({ line_start: 4 })];""")
    assert got == ["a.js:4-6", "a.js:4", "a.js", ""]


@needs_node
def test_sort_verdicts_matches_the_report_order(tmp_path):
    specs = [("REJECT", 0.0, 0), ("CONFIRM", 0.1, 1), ("ERROR", 0.0, 2), ("CONFIRM", 0.9, 3),
             ("INCONCLUSIVE", 0.0, 4), ("CONFIRM", 0.1, 5), ("REJECT", 0.5, 6)]
    verdicts = [Verdict(lead=Lead(index=i, shape="A", text="t", evidence="e", confidence="LOW"), decision=d,
                        reason="r", sink_centrality=c) for d, c, i in specs]
    expected = [v.lead.index for v in sorted(
        verdicts, key=lambda v: (report._ORDER.get(v.decision, 9), -v.sink_centrality, v.lead.index))]
    got = _node(tmp_path, f"""return app.sortVerdicts({json.dumps([dataclasses.asdict(v) for v in verdicts])})
        .map(v => v.lead.index);""")
    assert got == expected
    counts = _node(tmp_path, f"return app.countDecisions({json.dumps([dataclasses.asdict(v) for v in verdicts])});")
    assert counts == {"CONFIRM": 3, "INCONCLUSIVE": 1, "REJECT": 2, "ERROR": 1}


# The option spec as spec section 3 describes jobs.OPTIONS (labels and help left out).
SPEC = {
    "verify_routing": {"kind": "bool", "flag": "--verify-routing", "engines": ["claude"]},
    "verify_context": {"kind": "bool", "flag": "--verify-context", "engines": ["claude"]},
    "shortlists": {"kind": "bool", "flag": "--shortlists", "engines": ["claude", "lm"]},
    "semantic_mode": {"kind": "choice", "choices": ["vector", "hybrid", "keyword"], "flag": "--semantic-mode",
                      "engines": ["claude"]},
    "fail_on": {"kind": "choice", "choices": ["none", "confirm", "inconclusive"], "flag": "--fail-on",
                "engines": ["claude", "lm"]},
    "language": {"kind": "text", "pattern": "^[a-z0-9]{1,20}$", "flag": "--language", "engines": ["claude", "lm"]},
    "lm_temperature": {"kind": "float", "min": 0, "max": 2, "flag": "--lm-temperature", "engines": ["lm"]},
    "lm_verify": {"kind": "bool", "flag": "--lm-verify", "engines": ["lm"]},
    "runtime": {"kind": "bool", "flag": "--runtime", "engines": ["claude", "lm"], "danger": True},
    "runtime_sandbox": {"kind": "choice", "choices": ["auto", "docker", "host"], "flag": "--runtime-sandbox",
                        "requires": "runtime"},
    "runtime_driver": {"kind": "choice", "choices": ["auto", "harness", "http", "process"],
                       "flag": "--runtime-driver", "requires": "runtime"},
    "runtime_budget": {"kind": "int", "min": 1, "max": 10000, "flag": "--runtime-budget", "requires": "runtime"},
}


def _collect(tmp_path, cases: dict) -> dict:
    return _node(tmp_path, f"""
        const spec = {json.dumps(SPEC)}, cases = {json.dumps(cases)}, out = {{}};
        for (const k of Object.keys(cases)) out[k] = app.collectOptions(spec, cases[k][0], cases[k][1]);
        return out;""")


@needs_node
def test_collect_options_sends_only_what_differs_from_the_plain_scan(tmp_path):
    got = _collect(tmp_path, {
        "untouched": ["claude", {}],
        "claude": ["claude", {"verify_routing": True, "shortlists": False, "semantic_mode": "vector",
                              "fail_on": "confirm", "language": "  jssrc ", "lm_temperature": "1.5",
                              "lm_verify": True, "runtime": False, "runtime_budget": "50"}],
        "lm": ["lm", {"lm_temperature": "0.7", "lm_verify": True, "verify_routing": True, "runtime": True,
                      "runtime_sandbox": "docker", "runtime_driver": "auto", "runtime_budget": "250"}],
        "integral_float": ["lm", {"lm_temperature": "2"}],
    })
    assert got["untouched"] == {"options": {}, "errors": {}}
    # lm-only options, a sub-option without runtime, a default choice and an unticked box add nothing
    assert got["claude"] == {"options": {"verify_routing": True, "fail_on": "confirm", "language": "jssrc"},
                             "errors": {}}
    assert got["lm"] == {"options": {"lm_temperature": 0.7, "lm_verify": True, "runtime": True,
                                     "runtime_sandbox": "docker", "runtime_budget": 250}, "errors": {}}
    assert isinstance(got["lm"]["options"]["runtime_budget"], int)
    assert got["integral_float"]["options"] == {"lm_temperature": 2}


@needs_node
def test_collect_options_refuses_bad_values_without_sending_them(tmp_path):
    got = _collect(tmp_path, {
        "pattern": ["claude", {"language": "JS SRC"}],
        "fraction": ["claude", {"runtime": True, "runtime_budget": "2.5"}],
        "below": ["claude", {"runtime": True, "runtime_budget": "0"}],
        "above": ["lm", {"lm_temperature": "3"}],
        "not_a_number": ["claude", {"runtime": True, "runtime_budget": "not a number"}],
        "unknown_choice": ["claude", {"fail_on": "always"}],
    })
    for name, field in (("pattern", "language"), ("fraction", "runtime_budget"), ("below", "runtime_budget"),
                        ("above", "lm_temperature"), ("not_a_number", "runtime_budget"),
                        ("unknown_choice", "fail_on")):
        assert field in got[name]["errors"], name
        assert field not in got[name]["options"], name
    assert got["fraction"]["errors"]["runtime_budget"] == "Enter a whole number."
    assert got["below"]["errors"]["runtime_budget"] == "Enter a value from 1 to 10000."
    assert got["not_a_number"]["errors"]["runtime_budget"] == "Enter a number."


@needs_node
def test_option_engines(tmp_path):
    got = _node(tmp_path, f"""
        const spec = {json.dumps(SPEC)};
        const names = (engine) => Object.keys(spec).filter(n => app.appliesTo(spec, n, engine));
        return {{ claude: names("claude"), lm: names("lm"), def: app.choiceDefault(spec.fail_on),
                  def2: app.choiceDefault({{ choices: ["a", "b"], default: "b" }}) }};""")
    assert "lm_temperature" not in got["claude"] and "verify_routing" not in got["lm"]
    for sub in ("runtime_sandbox", "runtime_driver", "runtime_budget"):   # follow their parent's engines
        assert sub in got["claude"] and sub in got["lm"]
    assert got["def"] == "none" and got["def2"] == "b"


@needs_node
def test_routes(tmp_path):
    hashes = ["", "#", "#/", "#/runs", "#/new", "#/new/", "#/job", "#/settings", "#/run/s1~20261029T035014Z",
              "#/run/a%7Eb", "#/compare/x~1/y~2", "#/run/", "#/run", "#/run/a/b", "#/bogus", "#/run/%E0%A4%A",
              "#main"]
    got = _node(tmp_path, f"""
        const r = {json.dumps(hashes)}.map(app.parseRoute);
        const round = app.parseRoute(app.runHref("a~b c/d"));
        const cmp = app.parseRoute(app.compareHref("x~1", "y~2"));
        return {{ r, round, cmp }};""")
    names = [x["name"] for x in got["r"]]
    assert names == ["runs", "runs", "runs", "runs", "new", "new", "job", "settings", "run", "run", "compare",
                     "notfound", "notfound", "notfound", "notfound", "notfound", "notfound"]
    assert got["r"][8]["params"] == {"id": "s1~20261029T035014Z"}
    assert got["r"][9]["params"] == {"id": "a~b"}
    assert got["r"][10]["params"] == {"a": "x~1", "b": "y~2"}
    assert got["round"] == {"name": "run", "params": {"id": "a~b c/d"}}
    assert got["cmp"] == {"name": "compare", "params": {"a": "x~1", "b": "y~2"}}


KEYCHAIN = {"available": True, "backend": "keyring.backends.Windows.WinVaultKeyring", "reason": ""}
NO_KEYCHAIN = {"available": False, "backend": "keyring.backends.fail.Keyring",
               "reason": "no keychain backend on this machine (keyring.backends.fail.Keyring)"}
ACCESS = {
    "login": {"mode": "login", "source": "none", "key_set": False, "key_hint": None, "endpoint": None,
              "persisted": False, "keychain": KEYCHAIN, "notes": []},
    "key_saved": {"mode": "anthropic_key", "source": "ui", "key_set": True, "key_hint": "...abcd", "endpoint": None,
                  "persisted": True, "keychain": KEYCHAIN, "notes": []},
    "key_memory": {"mode": "anthropic_key", "source": "ui", "key_set": True, "key_hint": "...abcd", "endpoint": None,
                   "persisted": False, "keychain": NO_KEYCHAIN, "notes": []},
    "key_env": {"mode": "anthropic_key", "source": "env", "key_set": True, "key_hint": "...wxyz", "endpoint": None,
                "persisted": False, "keychain": KEYCHAIN, "notes": []},
    "endpoint": {"mode": "endpoint", "source": "ui", "key_set": False, "key_hint": None, "persisted": True,
                 "endpoint": {"base_url": "http://localhost:11434", "model": "qwen3:8b", "auth": "bearer",
                              "token_set": False, "token_hint": None},
                 "keychain": KEYCHAIN, "notes": []},
}


@needs_node
def test_access_wording(tmp_path):
    got = _node(tmp_path, f"""
        const a = {json.dumps(ACCESS)}, out = {{}};
        for (const k of Object.keys(a)) out[k] = [app.accessSummary(a[k]), app.accessLine(a[k]), app.keptText(a[k])];
        return out;""")
    assert got["login"] == ["Claude login", "Claude login: the claude CLI's own account", "Nothing to keep"]
    assert got["key_saved"] == ["API key ...abcd", "Anthropic key ...abcd, billed to your API account",
                                "Saved in the OS keychain (keyring.backends.Windows.WinVaultKeyring)"]
    assert got["key_memory"][2] == ("Not saved: no keychain on this machine; held until orion ui stops "
                                    "(no keychain backend on this machine (keyring.backends.fail.Keyring))")
    assert got["key_env"][0] == "API key ...wxyz (from environment)"
    assert got["key_env"][2] == "Read from the server's environment; not saved"
    assert got["endpoint"][0] == "Endpoint qwen3:8b (experimental)"
    assert got["endpoint"][1] == ("Endpoint http://localhost:11434, model qwen3:8b \u2014 experimental; "
                                  "your code is sent there")


@needs_node
def test_number_and_time_formatting(tmp_path):
    got = _node(tmp_path, f"""
        const local = new Date(2026, 9, 29, 3, 50, 14).toISOString();   // 03:50 on this machine's clock
        return {{ ints: [0, 7, 1234, 1234567, -9876, NaN].map(app.fmtInt),
                  elapsed: [0, 999, 59000, 61000, 3599000, 3720000, 90061000, -5].map(app.fmtElapsed),
                  time: app.fmtTime(local), bad: app.fmtTime("not a time"), missing: app.fmtTime(null),
                  base: {json.dumps(["C:\\x\\NodeGoat", "/a/b/", "repo", ""])}.map(app.basename) }};""")
    assert got["ints"] == ["0", "7", "1,234", "1,234,567", "-9,876", "0"]
    assert got["elapsed"] == ["0s", "0s", "59s", "1m 01s", "59m 59s", "1h 02m", "25h 01m", "0s"]
    assert got["time"] == "2026-10-29 03:50"
    assert got["bad"] == "not a time" and got["missing"] == ""
    assert got["base"] == ["NodeGoat", "b", "repo", ""]


@needs_node
def test_h_renders_server_text_as_text_and_refuses_handlers(tmp_path):
    """The one DOM-building path, against a fake document: strings become text nodes, never markup; an
    attribute named on* or style is refused; an href can only stay on this page or this server."""
    got = _node(tmp_path, r"""
        global.document = {
          createElement(tag) {
            return { tag, nodeType: 1, attrs: {}, children: [], listeners: {},
                     setAttribute(k, v) { this.attrs[k] = String(v); },
                     appendChild(c) { this.children.push(c); return c; },
                     addEventListener(t, f) { this.listeners[t] = typeof f; } };
          },
          createTextNode(text) { return { nodeType: 3, text }; },
        };
        const evil = '<img src=x onerror="alert(1)">';
        const p = app.h("p", { title: evil, "data-key": evil }, evil, 42, null, false, true, ["a", ["b"]]);
        const refused = [];
        for (const k of ["onclick", "onerror", "ONLOAD", "style", "srcdoc"]) {
          try { app.h("div", { [k]: "x" }); } catch (e) { refused.push(k); }
        }
        const hrefs = ["#/run/x", "/api/runs/x/files/report.txt?t=abc", "javascript:alert(1)", "JavaScript:x",
                       "//evil.example/x", "/\\evil.example", " /x", "data:text/html,x", "/\t/evil", "mailto:a@b"]
          .map(v => app.h("a", { href: v }).attrs.href);
        const button = app.h("button", { type: "button", on: { click: () => 1 }, "aria-pressed": true, disabled: false });
        return { children: p.children, title: p.attrs.title, key: p.attrs["data-key"], refused, hrefs,
                 listeners: button.listeners, attrs: button.attrs };""")
    evil = '<img src=x onerror="alert(1)">'
    assert got["children"] == [{"nodeType": 3, "text": evil}, {"nodeType": 3, "text": "42"},
                               {"nodeType": 3, "text": "a"}, {"nodeType": 3, "text": "b"}]
    assert got["title"] == evil and got["key"] == evil   # an attribute value is never parsed
    assert got["refused"] == ["onclick", "onerror", "ONLOAD", "style", "srcdoc"]
    assert got["hrefs"] == ["#/run/x", "/api/runs/x/files/report.txt?t=abc"] + ["#"] * 8
    assert got["listeners"] == {"click": "function"}
    assert got["attrs"] == {"type": "button", "aria-pressed": "true"}


@needs_node
def test_path_parts_keep_each_separator_without_a_lookbehind(tmp_path):
    """pathText's split: no lookbehind regex anywhere (Safari before 16.4 cannot parse one: a blank page)."""
    got = _node(tmp_path, r"""
        return ["a/b/c", "/abs/x.js", "C:\\dir\\f.py", "a//b", "a/", "", null].map(app.pathParts);
    """)
    assert got == [["a/", "b/", "c"], ["/", "abs/", "x.js"], ["C:\\", "dir\\", "f.py"], ["a/", "/", "b"], ["a/"],
                   [""], [""]]
    assert "(?<=" not in _text("app.js") and "(?<!" not in _text("app.js")


@needs_node
def test_valid_model_name_matches_the_servers_rule(tmp_path):
    names = ["sonnet", "claude-sonnet-5", "qwen3:8b", "a/b@c[1m]", "-flag", "", "two words", "x" * 121, None, 5]
    got = _node(tmp_path, f"return {json.dumps(names)}.map(app.validModelName);")
    from orion.ui import jobs
    want = [isinstance(n, str) and bool(jobs.MODEL_RE.fullmatch(n)) and not n.startswith("-") for n in names]
    assert got == want
