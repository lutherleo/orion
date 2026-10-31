"""eval.vlb: Cisco's VLB keys (VLB's ground-truth files + fix sites from the compare diff) and scoring.
Token-free and offline: `gh api` goes through a stub."""
import csv
import io
import json

from eval import vlb

PATCH = """@@ -1,6 +1,8 @@
 import os
+import re

 def upload(name, data):
-    path = os.path.join(ROOT, name)
+    path = os.path.join(ROOT, os.path.basename(name))
+    check(path)
     with open(path, "wb") as f:
         f.write(data)
@@ -20,3 +22,4 @@ def other():
     a = 1
+    audit(a)
     return a
"""


def test_patch_sites_take_removed_lines_and_anchor_insertions():
    sites = vlb.patch_sites("app/upload.py", PATCH)
    assert sites == [{"file": "app/upload.py", "line": 4, "end_line": 4, "function": None},   # removed line 4
                     {"file": "app/upload.py", "line": 20, "end_line": 20, "function": None}]  # insert after 20
    # an import-only run is dropped; if every run is trivial they are all kept
    assert vlb.patch_sites("a.py", "@@ -1,1 +1,2 @@\n+import x\n y = 1\n") == [
        {"file": "a.py", "line": 1, "end_line": 1, "function": None}]


def test_ground_truth_follows_vlbs_filter():
    assert vlb.ground_truth(["src/a.js", "src/a.test.js", "README.md", "spec/x.py", "lib/b.go"]) == ["src/a.js", "lib/b.go"]


def test_file_f1_matches_vlbs_formula_with_the_same_file_rule():
    assert vlb.file_f1(["repo/src/a.js", "src/x.js"], ["src/a.js", "src/b.js"]) == \
        {"precision": 0.5, "recall": 0.5, "file_f1": 0.5}
    assert vlb.file_f1([], ["a.js"])["file_f1"] == 0.0


def _manifest(rows):
    cols = ["alpha_id", "repo_full_name", "ecosystem", "language", "cwes", "cwe_description", "ground_truth_files",
            "merge_commit_sha", "vulnerable_commit_sha", "ghsa_id", "cves", "year"]
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=cols)
    w.writeheader()
    for r in rows:
        w.writerow({k: r.get(k, "") for k in cols})
    return out.getvalue()


def test_build_keys_reads_the_manifest_and_the_compare_diff():
    manifest = _manifest([
        {"alpha_id": "T1", "repo_full_name": "o/app", "ecosystem": "pip", "language": "Python",
         "cwes": '["CWE-22"]', "cwe_description": '["CWE-22: Path Traversal"]',
         "ground_truth_files": '["app/upload.py", "tests/test_upload.py"]', "merge_commit_sha": "F1",
         "vulnerable_commit_sha": "V1", "ghsa_id": "GHSA-1", "cves": '["CVE-1"]', "year": "2025"},
        {"alpha_id": "T2", "repo_full_name": "o/gone", "ecosystem": "go", "cwes": '["CWE-918"]',
         "ground_truth_files": '["main.go"]', "merge_commit_sha": "F2", "vulnerable_commit_sha": "V2"},
        {"alpha_id": "T3", "repo_full_name": "o/rs", "ecosystem": "rust", "cwes": '["CWE-79"]',
         "ground_truth_files": '["src/lib.rs"]', "merge_commit_sha": "F3", "vulnerable_commit_sha": "V3"}])
    compare = {"files": [{"filename": "app/upload.py", "status": "modified", "patch": PATCH},
                         {"filename": "tests/test_upload.py", "status": "modified", "patch": "@@ -1 +1 @@\n-a\n+b\n"}]}
    calls = []

    def get(path, raw=False):
        calls.append(path)
        if path.startswith(f"repos/{vlb.VLB_REPO}/contents/data/manifest.csv"):
            return manifest
        if path == "repos/o/app/compare/V1...F1":
            return json.dumps(compare)
        return None                                            # o/gone: GitHub answers 404

    data = vlb.build_keys(get, log=lambda *_: None)
    by = {t["id"]: t for t in data["tasks"]}
    t1 = by["T1"]
    assert (t1["scope"], t1["ground_truth_files"], t1["cwes"]) == ("in", ["app/upload.py"], ["CWE-22"])
    assert [(s["line"], s["end_line"]) for s in t1["fix_sites"]] == [(4, 4), (20, 20)]
    assert by["T2"]["scope"] == "go" and by["T2"]["fix_sites"] == [] and "unavailable" in by["T2"]["notes"][0]
    assert by["T3"]["scope"] == "out" and not any("o/rs" in c for c in calls)   # no diff fetched for out-of-scope
    assert t1["added_files"] == []
    assert data["source"]["ref"] == vlb.VLB_REF and data["source"]["license"] == "Apache-2.0"


def _run(tmp_path, name, findings):
    d = tmp_path / name
    d.mkdir()
    verdicts = [{"lead": {"index": i, "shape": "A", "text": "x", "evidence": "", "confidence": "HIGH",
                          "file": f, "line_start": line, "cwe": cwe}, "decision": "CONFIRM", "reason": "r"}
                for i, (f, line, cwe) in enumerate(findings)]
    (d / "verdicts.json").write_text(json.dumps(verdicts), encoding="utf-8")
    (d / "leads.json").write_text(json.dumps({"engine": "claude", "leads": []}), encoding="utf-8")
    return str(d)


TASK = {"id": "T1", "scope": "in", "cwes": ["CWE-22"], "ground_truth_files": ["app/upload.py"],
        "fix_sites": [{"file": "app/upload.py", "line": 4, "end_line": 4, "function": None}]}


def test_a_located_finding_with_silence_on_the_patched_snapshot_is_pair_correct(tmp_path):
    data = {"tasks": [TASK]}
    runs = {"T1": {"vulnerable": _run(tmp_path, "v", [("app/upload.py", 6, "CWE-22"), ("app/other.py", 3, "CWE-79")]),
                   "patched": _run(tmp_path, "p", [("app/other.py", 3, "CWE-79")])}}      # an unrelated real bug
    (row,) = vlb.score_runs(data, runs)
    assert row["l1"] and row["l2"] and row["silent"] and row["pair_correct"]
    assert row["file_all"]["file_f1"] == round(2 * 0.5 * 1 / 1.5, 4) and row["file_cwe"]["file_f1"] == 1.0


def test_a_cwe_finding_left_on_the_patched_ground_truth_file_is_not_silent(tmp_path):
    runs = {"T1": {"vulnerable": _run(tmp_path, "v", [("app/upload.py", 4, "CWE-22")]),
                   "patched": _run(tmp_path, "p", [("app/upload.py", 5, "CWE-23")])}}       # CWE-23 is CWE-22's child
    (row,) = vlb.score_runs({"tasks": [TASK]}, runs)
    assert row["l1"] and not row["silent"] and not row["pair_correct"]
    assert "silent on patched 0 / 1" in vlb.table([row])


def test_without_a_diff_the_ground_truth_file_is_the_location_test(tmp_path):
    task = dict(TASK, fix_sites=[])
    (row,) = vlb.score_runs({"tasks": [task]}, {"T1": {"vulnerable": _run(tmp_path, "v", [("app/upload.py", 90, "CWE-22")])}})
    assert row["l1"] and row["l2"] and not row["site_level"] and "silent" not in row


def test_files_the_fix_added_are_scored_apart_and_broad_keys_are_flagged(tmp_path):
    task = dict(TASK, ground_truth_files=["app/upload.py", "app/guard.py"], added_files=["app/guard.py"],
                fix_sites=[dict(TASK["fix_sites"][0], line=i, end_line=i) for i in range(1, 13)])
    (row,) = vlb.score_runs({"tasks": [task]}, {"T1": {"vulnerable": _run(tmp_path, "v", [("app/upload.py", 4, "CWE-22")])}})
    assert row["file_cwe"]["recall"] == 0.5 and row["file_cwe_existing"]["file_f1"] == 1.0   # guard.py is new
    assert row["broad"] and "at most 10 fix sites: L1 0 / 0" in vlb.table([row])


def test_keys_dump_one_task_per_line_and_load_back(tmp_path):
    data = {"benchmark": "vlb", "source": {"ref": "x"}, "tasks": [dict(TASK, id="A"), dict(TASK, id="B")]}
    out = tmp_path / "k.json"
    vlb.dump_keys(data, out)
    assert vlb.load_keys(out) == data and len(out.read_text(encoding="utf-8").splitlines()) == 4
