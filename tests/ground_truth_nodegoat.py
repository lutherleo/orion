"""NodeGoat ground truth: the 15 distinct exploitable vulnerabilities Orion is scored against.

Reconstructed from NodeGoat's own `/tutorial` (OWASP Top-10 2013 + SSRF + ReDoS bonus classes)
and reconciled with the 23 `// Fix for A...` source markers, matching the recall bar the prior
PoC hit (6/15 shape-A-only, 13/15 multi-shape, 0 false positives).

`run_nodegoat_eval.py` matches Orion's CONFIRMED findings to these by OWASP id + file overlap.
`shape` = the discovery lens most likely to surface it (A data-flow, B absent-control,
C disabled/reverted, D pattern/deps). `known_gap=True` marks items not derivable from this scan.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class GroundTruth:
    id: str          # OWASP-style id
    name: str
    files: tuple[str, ...]   # substrings that should appear in a correct finding's evidence
    shape: str               # A | B | C | D
    known_gap: bool = False  # true = not checkable from the CPG (e.g. dependency data absent)


GROUND_TRUTH: list[GroundTruth] = [
    GroundTruth("A1-1", "Server-Side JS Injection via eval()", ("contributions.js",), "A"),
    GroundTruth("A1-2", "NoSQL Injection via $where operator", ("allocations-dao.js",), "A"),
    GroundTruth("A1-3", "Log Injection (CRLF forging)", ("session.js",), "A"),
    GroundTruth("A2-1", "Broken Auth — insecure session management", ("server.js", "session.js"), "B"),
    GroundTruth("A2-2", "Broken Auth — weak password policy + user enumeration",
                ("session.js", "user-dao.js"), "C"),
    GroundTruth("A3", "Cross-Site Scripting (auto-escaping disabled)", ("server.js",), "C"),
    GroundTruth("A4", "Insecure Direct Object Reference", ("allocations.js",), "A"),
    GroundTruth("A5", "Security Misconfiguration (Helmet headers removed)", ("server.js",), "B"),
    GroundTruth("A6", "Sensitive Data Exposure (SSN/DOB encryption disabled)", ("profile-dao.js",), "C"),
    GroundTruth("A7", "Missing Function-Level Access Control", ("index.js",), "B"),
    GroundTruth("A8", "Cross-Site Request Forgery (missing CSRF middleware)",
                ("contributions.js", "app.js", "index.js"), "B"),
    # Dependency nodes ARE now populated (graph/deps.py parses package.json), but confirming a
    # component is *known-vulnerable* needs a CVE feed the graph doesn't carry — so still a gap.
    GroundTruth("A9", "Using Components with Known Vulnerabilities",
                ("package.json",), "D", known_gap=True),
    GroundTruth("A10", "Unvalidated Redirects and Forwards", ("index.js",), "A"),
    GroundTruth("SSRF", "Server-Side Request Forgery", ("research.js",), "A"),
    GroundTruth("ReDoS", "Regex Denial of Service (nested quantifier)", ("profile.js", "app"), "D"),
]

TOTAL = len(GROUND_TRUTH)          # 15
CHECKABLE = sum(1 for g in GROUND_TRUTH if not g.known_gap)  # 14 (A9 needs a CVE feed, not in scan)
RECALL_BAR = 13                    # the bar to match or beat

# DISTINCTIVE per-vuln tokens (moved here from run_nodegoat_eval.py so the matcher is benchmark-
# agnostic — PyGoat has its own map in ground_truth_pygoat.py). Two hard lessons drove this design:
#   1. Match the FINDING'S OWN CLAIM (text), not verbose evidence — the verifier cross-references other
#      files/vulns, which leaked matches across ground truths.
#   2. Use tokens UNIQUE to each vuln. "access control" collided A4/A7; "package" spuriously credited
#      A9. Each token identifies exactly its vuln class so vulns that share a file don't cross-match.
CLASS_KEYWORDS: dict[str, tuple[str, ...]] = {
    "A1-1": ("eval", "ssjs", "server-side js", "server side js"),
    "A1-2": ("$where", "nosql"),
    "A1-3": ("log injection", "log forging", "log/crlf", "crlf"),
    "A2-1": ("session cookie", "httponly", "session hardening", "session secret",
             "session management", "cookie name", "secure flag"),
    "A2-2": ("password policy", "password-policy", "enumeration", "weak password", "weak-password",
             "plaintext password", "password hashing", "password comparison", "comparepassword", "bcrypt"),
    "A3": ("autoescape", "auto-escap", "auto escap", "escaping disabled", "swig", "xss"),
    "A4": ("idor", "direct object", "req.params"),
    "A5": ("helmet", "x-frame", "clickjack", "hsts", "x-powered-by", "security header",
           "security response header", "security-misconfiguration"),
    "A6": ("encrypt", "ssn", "sensitive-data", "sensitive data", "pii"),
    "A7": ("isadmin", "function-level", "function level", "never attached",
           "admin-authorization", "admin authorization", "admin middleware"),
    "A8": ("csrf", "forgery"),
    "A9": ("cve-", "known vulnerabilit", "vulnerable version", "outdated version", "npm audit", "retire.js"),
    "A10": ("redirect", "forward", "unvalidated"),
    "SSRF": ("ssrf", "server-side request", "server side request", "needle.get"),
    "ReDoS": ("redos", "backtracking", "nested quantifier", "catastrophic", "([0-9]+)+"),
}
