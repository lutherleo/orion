"""PyGoat ground truth: the distinct exploitable vulnerabilities Orion is scored against (PLAN2).

Reconstructed from the PyGoat source (`fixtures/pygoat`, OWASP Top-10 2021 lab app — a Django project
whose vulnerable endpoints live almost entirely in `introduction/views.py`, plus `pygoat/settings.py`,
`requirements.txt`, and `introduction/apis.py`). Mirrors `tests/ground_truth_nodegoat.py`: same
`GroundTruth` dataclass, plus a per-vuln `CLASS_KEYWORDS` map. Because most vulns share
`introduction/views.py`, the FILE match barely discriminates here — the distinctive CLASS token does
the work, exactly as designed (`bench/scoring._matches`).

`shape` = the discovery lens most likely to surface it. `known_gap=True` marks items not derivable
from the CPG alone (A06 needs a CVE feed, like NodeGoat's A9). `RECALL_BAR` is PROVISIONAL — a real
scored run sets the honest number; it is a target, not a claim.
"""
from __future__ import annotations

from tests.ground_truth_nodegoat import GroundTruth

GROUND_TRUTH: list[GroundTruth] = [
    GroundTruth("SQLI", "SQL Injection via objects.raw()", ("views.py",), "A"),
    GroundTruth("CMDI", "OS Command Injection (subprocess shell=True)", ("views.py",), "A"),
    GroundTruth("XSS", "Cross-Site Scripting (reflected/stored)", ("xss",), "A"),
    GroundTruth("SSRF", "Server-Side Request Forgery (requests.get on user URL)", ("ssrf",), "A"),
    GroundTruth("SSTI", "Server-Side Template Injection", ("ssti",), "A"),
    GroundTruth("XXE", "XML External Entity injection", ("xxe",), "A"),
    GroundTruth("DESER-PICKLE", "Insecure Deserialization (pickle.loads of a cookie)", ("views.py",), "A"),
    GroundTruth("DESER-YAML", "Insecure Deserialization (yaml.load full Loader)", ("views.py",), "A"),
    GroundTruth("IDOR", "Broken Access Control / IDOR", ("views.py",), "A"),
    GroundTruth("CRYPTO", "Cryptographic Failure (MD5 password hashing)", ("views.py",), "C"),
    GroundTruth("MISCONFIG", "Security Misconfiguration (DEBUG=True, hardcoded SECRET_KEY)",
                ("settings.py",), "B"),
    GroundTruth("AUTHZ", "Authentication Failure (weak/broken auth flow)", ("views.py",), "B"),
    GroundTruth("DESIGN-OTP", "Insecure Design (predictable OTP / ticket)", ("views.py",), "B"),
    GroundTruth("LOGGING", "Security Logging & Monitoring Failure", ("apis.py", "views.py"), "B"),
    GroundTruth("CODEEXEC", "Code Injection via ImageMath.eval", ("views.py",), "A"),
    # A06 components-with-known-vulns: Dependency nodes ARE parsed, but confirming a version is
    # known-vulnerable needs a CVE feed the graph doesn't carry — a known gap (parallels NodeGoat A9).
    GroundTruth("COMPONENTS", "Vulnerable & Outdated Components", ("requirements.txt",), "D",
                known_gap=True),
]

CLASS_KEYWORDS: dict[str, tuple[str, ...]] = {
    "SQLI": ("sql injection", "sqli", "objects.raw", ".raw(", "raw sql", "raw query"),
    "CMDI": ("command injection", "os command", "shell=true", "subprocess", "popen", "os.system"),
    "XSS": ("xss", "cross-site scripting", "cross site scripting", "mark_safe", "reflected", "stored xss"),
    "SSRF": ("ssrf", "server-side request", "server side request", "requests.get", "fetch internal"),
    "SSTI": ("ssti", "template injection", "server-side template", "server side template", "render_template_string"),
    "XXE": ("xxe", "xml external entity", "external entity", "etree", "resolve_entities"),
    "DESER-PICKLE": ("pickle", "unpickle", "insecure deserial", "deserialization of untrusted"),
    "DESER-YAML": ("yaml.load", "yaml full loader", "yaml deserial", "unsafe yaml"),
    "IDOR": ("idor", "broken access control", "insecure direct object", "access control", "horizontal privilege"),
    "CRYPTO": ("md5", "weak hash", "cryptographic failure", "insecure hash", "unsalted"),
    "MISCONFIG": ("debug = true", "debug=true", "secret_key", "hardcoded secret", "security misconfiguration", "allowed_hosts"),
    "AUTHZ": ("authentication failure", "brute force", "session fixation", "weak password policy",
              "credential stuffing", "identification and authentication"),
    "DESIGN-OTP": ("insecure design", "predictable", "otp", "guessable", "business logic"),
    "LOGGING": ("logging", "insufficient logging", "security logging", "log monitoring", "audit log"),
    "CODEEXEC": ("imagemath", "imagemath.eval", "code injection", "code execution", "arbitrary code", "eval("),
    "COMPONENTS": ("cve-", "known vulnerabilit", "vulnerable version", "outdated version",
                   "vulnerable component", "outdated component"),
}

TOTAL = len(GROUND_TRUTH)                                          # 16
CHECKABLE = sum(1 for g in GROUND_TRUTH if not g.known_gap)       # 15 (COMPONENTS needs a CVE feed)
RECALL_BAR = 10                                                    # PROVISIONAL target; a real run sets truth
