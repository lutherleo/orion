"""bench.scoring generalized matcher + the PyGoat ground truth (PLAN2). Token-free, no infra."""
from __future__ import annotations

from bench import scoring
from tests import ground_truth_nodegoat as ng
from tests import ground_truth_pygoat as pg


def test_score_matches_a_correct_nodegoat_finding():
    findings = [("NoSQL injection via the $where operator in allocations-dao.js",
                 "MATCH (c:CpgCall) WHERE c.file_path CONTAINS 'allocations-dao.js' RETURN c.code")]
    s = scoring.score(findings, ng.GROUND_TRUTH, ng.CLASS_KEYWORDS)
    assert "A1-2" in s["found"]
    assert s["false_positive_candidates"] == 0


def test_file_without_class_token_does_not_match():
    # names the file but no distinctive class token -> not credited (prevents inflation)
    findings = [("something happens in allocations-dao.js", "evidence blob")]
    s = scoring.score(findings, ng.GROUND_TRUTH, ng.CLASS_KEYWORDS)
    assert s["recall"] == 0
    assert s["false_positive_candidates"] == 1


def test_pygoat_findings_score():
    findings = [
        ("SQL injection: user input flows into objects.raw() in introduction/views.py sql_lab",
         "MATCH ... file_path CONTAINS 'views.py'"),
        ("OS command injection via subprocess Popen shell=True in views.py",
         "c.code CONTAINS 'shell=True'"),
        ("Server-Side Request Forgery: requests.get on a user-supplied url in ssrf_lab2",
         "file ssrf/views"),
    ]
    s = scoring.score(findings, pg.GROUND_TRUTH, pg.CLASS_KEYWORDS)
    assert {"SQLI", "CMDI", "SSRF"} <= set(s["found"])
    assert s["false_positive_candidates"] == 0


def test_pygoat_ground_truth_is_well_formed():
    ids = [g.id for g in pg.GROUND_TRUTH]
    assert len(ids) == len(set(ids))                        # no duplicate ids
    assert pg.TOTAL == len(pg.GROUND_TRUTH)
    assert pg.CHECKABLE == sum(1 for g in pg.GROUND_TRUTH if not g.known_gap)
    # every checkable vuln has at least one distinctive class token
    for g in pg.GROUND_TRUTH:
        if not g.known_gap:
            assert pg.CLASS_KEYWORDS.get(g.id), f"{g.id} has no class keywords"


def test_score_reports_missed_and_known_gap():
    s = scoring.score([], pg.GROUND_TRUTH, pg.CLASS_KEYWORDS)   # found nothing
    assert s["recall"] == 0
    assert set(s["missed"]) == {g.id for g in pg.GROUND_TRUTH}
    assert s["checkable"] == pg.CHECKABLE < s["total"]          # COMPONENTS is a known gap


def test_structured_file_in_a_triple_satisfies_the_file_side_only():
    from tests.ground_truth_nodegoat import CLASS_KEYWORDS, GROUND_TRUTH
    # the prose names no file; the structured file supplies it -> credited
    found, _ = scoring.match([("NoSQL injection via $where", "query", "app/data/allocations-dao.js")],
                             GROUND_TRUTH, CLASS_KEYWORDS)
    assert "A1-2" in found
    # a structured file never supplies the CLASS token: no class word in the claim -> not credited
    found, _ = scoring.match([("something bad", "", "app/data/allocations-dao.js")],
                             GROUND_TRUTH, CLASS_KEYWORDS)
    assert "A1-2" not in found
