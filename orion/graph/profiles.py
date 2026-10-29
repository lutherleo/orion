"""Language/framework profiles — the ONE place framework-specific knowledge lives.

Orion is agnostic by construction: the graph builder and the discovery prompts read a `Profile`
instead of hardcoding any one framework. A profile answers three questions the rest of the builder
must not answer itself:

  - what is an attacker-controlled SOURCE?  (request-object roots, or — generically — the
    parameters of entry-point methods)
  - what does an ENTRY POINT look like?     (a structural fallback works for any language; a
    framework profile can refine it)
  - what SOURCE/SINK/entry vocabulary should the discovery prompts speak?

Two profiles ship today:
  - EXPRESS — formalizes the exact behavior Orion had hardcoded (`req`/`request` field-access
    taint sources), so a JS/Express repo like NodeGoat builds byte-for-byte the same.
  - GENERIC — the language-agnostic fallback: no request-object names, sources come structurally
    from entry-point parameters. This is what makes an UNKNOWN stack work without anyone writing a
    profile for it.

`select_profile(repo)` sniffs the repo (manifest) and returns EXPRESS when it clearly is one, else
GENERIC. Adding Flask/Spring/etc. later is just another `Profile` literal + a detect rule here —
no change anywhere else in the builder.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from pathlib import Path


class SinkPattern(str):
    """A sink hint matched by a regular expression (case-sensitive, on the raw text) instead of the
    default case-insensitive substring -- for a hint whose plain word is everywhere (`Function` is
    every JS `function`). It IS its display text, so the prompt's "Dangerous sinks to watch" line
    (strategies._profile_block) renders exactly as the plain string did."""

    rx: re.Pattern

    def __new__(cls, text: str, pattern: str) -> "SinkPattern":
        obj = super().__new__(cls, text)
        obj.rx = re.compile(pattern)
        return obj


@dataclass(frozen=True)
class Control:
    """One standard protection shape B checks for (graph/shortlists.py). Patterns are regexes run
    case-insensitively over first-party source lines:
      - `present`: the control is in use (matched in CODE). Matched only inside comments, it counts
        as commented out; matched nowhere, the control is reported absent. Empty = never "absent"
        (for controls that are only ever visible when someone turns them OFF).
      - `disabled`: the control is explicitly switched off (matched in CODE).
    Lexical on purpose: a row is a candidate the agent re-checks, never a finding."""
    name: str
    cwe: str
    present: tuple[str, ...] = ()
    disabled: tuple[str, ...] = ()


# Language-neutral "someone switched a protection off" patterns, shared by every profile.
_COMMON_CONTROLS: tuple[Control, ...] = (
    Control("TLS certificate verification", "CWE-295", disabled=(
        r"\bverify\s*=\s*False\b", r"rejectUnauthorized\s*:\s*false", r"InsecureSkipVerify\s*:\s*true",
        r"\bCERT_NONE\b", r"NODE_TLS_REJECT_UNAUTHORIZED\W+0")),
    Control("debug mode", "CWE-489", disabled=(
        r"^\s*DEBUG\s*=\s*True\b", r"\.run\(.*\bdebug\s*=\s*True")),
)


@dataclass(frozen=True)
class Profile:
    name: str

    # --- taint sources ---
    # Root identifier names that ARE the attacker-controlled request object (req.body.*, etc.).
    # Empty for GENERIC — a framework-free repo has no such global object.
    request_source_names: frozenset[str] = frozenset()
    # When true, the parameters of detected entry-point methods are treated as taint sources.
    # This is the structural, language-agnostic source model the GENERIC profile relies on.
    entrypoint_params_are_sources: bool = False

    # --- discovery-prompt vocabulary (Phase 5: injected into strategies.py prompts) ---
    # Human-readable examples of where untrusted input enters, shown to the discovery agent.
    source_examples: tuple[str, ...] = ()
    # Category -> example sink call names, to steer shape-A/B without hardcoding them in prompts.
    sink_hints: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # One line describing how request handlers / entry points are wired in this stack.
    entrypoint_hint: str = ""

    # --- shape-B checklist (graph/shortlists.py): the protections an app on this stack should have,
    # ordered most to least important (the order is the shortlist's tie-break rank).
    controls: tuple[Control, ...] = ()


EXPRESS = Profile(
    name="express",
    request_source_names=frozenset({"req", "request"}),
    source_examples=("req.body.*", "req.query.*", "req.params.*", "req.headers.*", "req.cookies.*"),
    sink_hints={
        # `Function` = the constructor (`new Function(...)` / `Function(...)`), case-sensitive, so the
        # `function` keyword and a "// Helper function" comment no longer rank a call as code_exec
        # (2026-10-28; before, user-dao.js:57 `this.validateLogin = function(...)` was NodeGoat's #1).
        "code_exec": ("eval", SinkPattern("Function", r"(?<![\w.$])(?:new\s+)?Function\s*\("), "exec",
                      "execSync"),
        "nosql": ("find", "findOne", "$where"),
        "redirect": ("redirect",),
        "render": ("render", "send"),
        "log": ("log",),
    },
    entrypoint_hint="Express route handlers registered via app.get/post/put/delete/use(path, handler).",
    controls=(
        Control("output escaping in templates", "CWE-79",
                present=(r"autoescape\s*:\s*true",),
                disabled=(r"autoescape\s*:\s*false", r"dangerouslySetInnerHTML", r"\.innerHTML\s*=")),
        Control("CSRF protection", "CWE-352", present=(r"\bcsurf\b", r"csrf", r"\blusca\b")),
        Control("password hashing", "CWE-916", present=(r"\bbcrypt", r"\bargon2", r"\bscrypt\b", r"pbkdf2")),
        Control("security headers", "CWE-693",
                present=(r"\bhelmet\b", r"X-Frame-Options", r"Content-Security-Policy", r"\bframeguard\b",
                         r"\bnoSniff\b", r"\bxssFilter\b", r"\bhsts\b")),
        Control("session cookie httpOnly", "CWE-1004",
                present=(r"httpOnly\s*:\s*true",), disabled=(r"httpOnly\s*:\s*false",)),
        Control("session cookie secure", "CWE-614",
                present=(r"\bsecure\s*:\s*true",), disabled=(r"\bsecure\s*:\s*false",)),
        Control("HTTPS transport", "CWE-319", present=(r"https\.createServer", r"\bspdy\b")),
        Control("login rate limiting", "CWE-307",
                present=(r"rate-?limit", r"express-brute", r"express-slow-down", r"\bratelimit")),
        *_COMMON_CONTROLS,
    ),
)

GENERIC = Profile(
    name="generic",
    request_source_names=frozenset(),
    entrypoint_params_are_sources=True,
    source_examples=(
        "the parameters of EntryPoint methods — query (:EntryPoint)-[:ENTERS_AT]->(:CpgMethod) and "
        "treat that method's parameters as untrusted input",
    ),
    sink_hints={
        "code_exec": ("eval", "exec", "system", "spawn"),
        "sql": ("execute", "query", "raw"),
        "redirect": ("redirect",),
    },
    entrypoint_hint=(
        "No framework assumed: entry points are first-party methods that nothing else in the code "
        "calls (call-graph roots) — request handlers, exported API, main. Their parameters are the "
        "untrusted inputs."
    ),
    controls=(
        Control("output escaping in templates", "CWE-79",
                disabled=(r"autoescape\s*[=:]\s*false", r"\bmark_safe\(", r"\|\s*safe\b",
                          r"dangerouslySetInnerHTML", r"\.innerHTML\s*=")),
        Control("CSRF protection", "CWE-352", present=(r"csrf",),
                disabled=(r"@csrf_exempt", r"WTF_CSRF_ENABLED\s*=\s*False", r"csrf\(\)\s*\.\s*disable\(")),
        Control("password hashing", "CWE-916",
                present=(r"\bbcrypt", r"\bargon2", r"\bscrypt\b", r"pbkdf2", r"\bmake_password\b",
                         r"PasswordEncoder", r"\bpasslib\b")),
        Control("security headers", "CWE-693",
                present=(r"SecurityMiddleware", r"X-Frame-Options", r"Content-Security-Policy",
                         r"\bhelmet\b", r"\bTalisman\b")),
        Control("session cookie flags", "CWE-614", disabled=(
            r"SESSION_COOKIE_SECURE\s*=\s*False", r"SESSION_COOKIE_HTTPONLY\s*=\s*False",
            r"httpOnly\s*:\s*false")),
        *_COMMON_CONTROLS,
    ),
)

# Extended sink vocabulary for the Orion-LM track, seeded from CodeQL models-as-data call names
# (engineering, not labels). Opt-in: layered over ANY profile by `with_lm_sinks`, never part of the
# default profiles, so the default scan and FLOWS_TO are untouched. Matching is case-insensitive
# substring over a call's name/code (pathfind._classify_sink), so these are a generous candidate
# generator that a later stage re-checks; hints are qualified where a bare word would match nearly
# everything. JSON.parse is deliberately NOT a deserialize sink.
LM_SINK_HINTS: dict[str, tuple[str, ...]] = {
    "path": ("readFile", "createReadStream", "sendFile", "path.join", "path.resolve",
             "os.path.join", "FileInputStream", "Paths.get", "open("),
    "template": ("render_template_string", "Template(", "Handlebars.compile", "ejs.render",
                 "pug.compile", "Velocity.evaluate", "nunjucks.renderString", "Jinja2"),
    "deserialize": ("pickle.loads", "yaml.load", "unserialize", "ObjectInputStream", "readObject",
                    "marshal.loads", "node-serialize", "jsyaml.load"),
    "xxe": ("DocumentBuilder", "SAXParser", "XMLReader", "etree.fromstring", "etree.parse",
            "libxmljs", "parseXml", "XMLInputFactory", "xml2js"),
    "ssrf": ("fetch(", "axios", "http.get", "http.request", "requests.get", "requests.post",
             "urlopen", "HttpClient", "openConnection"),
    "prototype_pollution": ("merge", "extend", "deepMerge", "assign", "defaultsDeep", "setWith"),
}


def with_lm_sinks(profile: Profile) -> Profile:
    """`profile` plus the LM_SINK_HINTS categories. Only `name` and `sink_hints` change; sources and
    entry-point rules are untouched, so the taint graph (FLOWS_TO) is identical to the base profile's.
    The base profile's own categories win on a key clash."""
    return replace(profile, name=f"{profile.name}+lm",
                   sink_hints={**LM_SINK_HINTS, **profile.sink_hints})


def _package_json_requires(repo: Path, pkg: str) -> bool:
    """True if `pkg` appears in package.json dependencies/devDependencies."""
    pj = repo / "package.json"
    if not pj.exists():
        return False
    try:
        data = json.loads(pj.read_text())
    except (ValueError, OSError):
        return False
    for key in ("dependencies", "devDependencies", "peerDependencies"):
        deps = data.get(key)
        if isinstance(deps, dict) and pkg in deps:
            return True
    return False


def select_profile(repo_path: str | Path, language: str | None = None) -> Profile:
    """Pick the best-fitting profile for a repo. EXPRESS when it clearly is a JS/Express app;
    otherwise the language-agnostic GENERIC fallback — never a hard failure on an unknown stack."""
    repo = Path(repo_path)
    if _package_json_requires(repo, "express"):
        return EXPRESS
    return GENERIC
