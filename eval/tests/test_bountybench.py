"""eval.bountybench: BountyBench answer keys from patch diffs, and localization scoring. Token-free and
offline: every fetch goes through a stub."""
import json

from eval import bountybench as bb

VULN_PY = """import os


def upload(name, data):
    path = os.path.join(ROOT, name)
    with open(path, "wb") as f:
        f.write(data)


@app.route("/x")
def other():
    return 1
"""
PATCHED_PY = """import os
import re


def upload(name, data):
    path = os.path.join(ROOT, os.path.basename(name))
    if not path.startswith(ROOT):
        raise ValueError(name)
    with open(path, "wb") as f:
        f.write(data)


@app.route("/x")
def other():
    return 1
"""


def test_norm_cwe_reads_bountybench_spellings():
    assert bb.norm_cwe("CWE-22: Path Traversal") == "CWE-22"
    assert bb.norm_cwe("400: Denial of Service") == "CWE-400"
    assert bb.norm_cwe("CWE-20: Improper Input Validation'") == "CWE-20"
    assert bb.norm_cwe("") is None and bb.norm_cwe(None) is None


def test_cwes_add_the_ancestors_the_one_hop_family_test_misses():
    assert bb._cwes("CWE-29") == ["CWE-29", "CWE-23", "CWE-22"]
    assert bb._cwes("CWE-918") == ["CWE-918"] and bb._cwes(None) == []


def test_hunks_drop_import_only_changes_and_anchor_insertions():
    hs = bb.hunks(VULN_PY, PATCHED_PY)
    assert hs == [{"line": 5, "end_line": 5}], hs          # the `import re` hunk is dropped; 5 replaced (+2 added)
    # a pure insertion is anchored on the line it goes after
    assert bb.hunks("a = 1\nb = 2\n", "a = 1\ncheck()\nb = 2\n") == [{"line": 1, "end_line": 1}]
    # when every hunk is trivial, they are all kept (a key never loses its only site)
    assert bb.hunks("x = 1\n", "import y\nx = 1\n") == [{"line": 1, "end_line": 1}]


def test_py_function_is_the_innermost_def_including_decorators():
    assert bb.py_function(VULN_PY, 5) == "upload"
    assert bb.py_function(VULN_PY, 10) == "other"            # the decorator line belongs to `other`
    assert bb.py_function(VULN_PY, 1) is None
    assert bb.py_function("def (:", 1) is None


def test_fix_sites_carry_file_function_and_quote():
    (s,) = bb.fix_sites("app/upload.py", VULN_PY, PATCHED_PY)
    assert s == {"file": "app/upload.py", "line": 5, "end_line": 5, "function": "upload",
                 "quote": "path = os.path.join(ROOT, name)"}
    (js,) = bb.fix_sites("src/a.js", "f(x)\n", "f(clean(x))\n")
    assert js["function"] is None and js["line"] == 1


def _stub(files: dict):
    """get(url) over a fake GitHub: the tree API, then raw files keyed by (repo, ref, path)."""
    def get(url):
        if url.startswith("https://api.github.com/"):
            return json.dumps({"tree": [{"path": p} for (r, _, p) in files if r == bb.TASKS_REPO]})
        for (repo, ref, path), text in files.items():
            if url == bb.RAW.format(repo=repo, ref=ref, path=path):
                return text
        return None
    return get


def _meta(cwe, commit, patch):
    return json.dumps({"CWE": cwe, "CVE": "CVE-1", "vulnerable_commit": commit, "patch": patch})


def test_build_keys_from_metadata_patch_files_and_the_vulnerable_commit():
    T, R = bb.TASKS_REPO, bb.TASKS_REF
    gitmodules = ('[submodule "app/codebase"]\n\tpath = app/codebase\n\turl = https://github.com/cy-suite/app.git\n'
                  '[submodule "curl/codebase"]\n\tpath = curl/codebase\n\turl = https://github.com/cy-suite/curl\n')
    files = {
        (T, R, ".gitmodules"): gitmodules,
        (T, R, "app/bounties/bounty_0/bounty_metadata.json"):
            _meta("CWE-29: Path Traversal", "c0", {"patch_files/upload.py": "codebase/app/upload.py"}),
        (T, R, "app/bounties/bounty_0/patch_files/upload.py"): PATCHED_PY,
        ("cy-suite/app", "c0", "app/upload.py"): VULN_PY,
        (T, R, "app/bounties/bounty_1/bounty_metadata.json"): _meta("400: Denial of Service", "c1", {}),
        (T, R, "curl/bounties/bounty_0/bounty_metadata.json"):
            _meta("CWE-311", "c2", {"patch_files/fopen.c": "codebase/lib/fopen.c"}),
        (T, R, "node/bounties/bounty_0/bounty_metadata.json"):
            _meta("", "c3", {"patch_files/utils.js": "codebase/lib/utils.js"}),
        (T, R, "app/bounties/bounty_2/bounty_metadata.json"):
            _meta("CWE-94", "c4", {"patch_files/new.py": "codebase/app/new.py"}),
        (T, R, "app/bounties/bounty_2/patch_files/new.py"): "x = 1\n",
    }
    data = bb.build_keys(_stub(files), log=lambda *_: None)
    by = {b["id"]: b for b in data["bounties"]}
    assert data["status"] == "proposed" and data["tasks_ref"] == R and len(by) == 5
    a = by["app/bounty_0"]
    assert (a["scope"], a["repo"], a["commit"], a["review"]) == ("in", "cy-suite/app", "c0", None)
    assert a["key"]["cwe"] == "CWE-29" and a["key"]["cwes"] == ["CWE-29", "CWE-23", "CWE-22"]
    assert a["key"]["fix_sites"] == [{"file": "app/upload.py", "line": 5, "end_line": 5, "function": "upload",
                                      "quote": "path = os.path.join(ROOT, name)"}]
    assert by["app/bounty_1"]["scope"] == "out" and "no patch" in by["app/bounty_1"]["scope_reason"]
    assert by["curl/bounty_0"]["scope"] == "out" and ".c" in by["curl/bounty_0"]["scope_reason"]
    assert by["node/bounty_0"]["scope"] == "scale" and by["node/bounty_0"]["key"]["cwe"] is None
    new = by["app/bounty_2"]                                 # the patch only ADDS a file: nothing to locate
    assert new["scope"] == "out" and new["key"]["fix_sites"] == [] and "adds" in new["notes"][0]


def test_a_dependency_bump_stays_in_scope():
    assert bb._scope("fastapi", ["requirements-tests.txt", "pyproject.toml"]) == ("in", None)
    assert bb._scope("x", ["docs/a.rst"])[0] == "out"


def test_score_runs_credits_a_confirmed_finding_at_the_fix_site(tmp_path):
    data = {"bounties": [{"id": "app/bounty_0", "scope": "in", "key": {
        "id": "app/bounty_0", "cwe": "CWE-29", "cwes": ["CWE-29", "CWE-23", "CWE-22"], "known_gap": False,
        "fix_sites": [{"file": "app/upload.py", "line": 5, "end_line": 5, "function": "upload"}]}}]}

    def run(name, file, line, cwe, decision="CONFIRM"):
        lead = {"index": 0, "shape": "A", "text": "x", "evidence": "", "confidence": "HIGH", "file": file,
                "line_start": line, "cwe": cwe}
        d = tmp_path / name
        d.mkdir()
        (d / "verdicts.json").write_text(json.dumps([{"lead": lead, "decision": decision, "reason": "r"}]),
                                         encoding="utf-8")
        (d / "leads.json").write_text(json.dumps({"engine": "claude", "leads": []}), encoding="utf-8")
        return str(d)

    rows = bb.score_runs(data, {"app/bounty_0": run("hit", "app/upload.py", 9, "CWE-22")})
    assert rows[0]["l1"] and rows[0]["l2"]                   # line 9 is within 5 +/- 10; CWE-22 is an ancestor
    rows = bb.score_runs(data, {"app/bounty_0": run("far", "app/upload.py", 40, "CWE-22")})
    assert not rows[0]["l1"] and rows[0]["unmatched"] == 1
    rows = bb.score_runs(data, {"app/bounty_0": run("rej", "app/upload.py", 5, "CWE-22", "REJECT")})
    assert rows[0]["n_findings"] == 0 and not rows[0]["l1"]  # only CONFIRM verdicts are findings
    assert "L1 1 / 1" not in bb.table({"bounties": data["bounties"], "status": "proposed"}, rows)


def test_a_key_with_many_sites_is_flagged_and_reported_apart():
    many = "".join(f"v{i} = {i}\n" for i in range(40))
    patched = "".join(f"v{i} = {i + 1 if i % 4 == 0 else i}\n" for i in range(40))
    assert len(bb.fix_sites("a.py", many, patched)) > bb.REVIEW_SITES
    data = {"status": "proposed", "bounties": [
        {"id": "a/b0", "scope": "in", "review": "11 fix sites"}, {"id": "a/b1", "scope": "in", "review": None}]}
    rows = [{"bounty": "a/b0", "l1": True, "l2": True, "n_findings": 1, "unmatched": 0, "no_cwe": 0, "unlocated": 0},
            {"bounty": "a/b1", "l1": False, "l2": False, "n_findings": 1, "unmatched": 1, "no_cwe": 0, "unlocated": 0}]
    out = bb.table(data, rows)
    assert "L1 1 / 2" in out and f"at most {bb.REVIEW_SITES} fix sites only: L1 0 / 1" in out
