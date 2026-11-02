"""eval.repoqa: RepoQA SNF as a retrieval benchmark of Orion's semantic_search. Token-free and offline: the
search is stubbed under the MCP tool's own code path, and no Neo4j, Joern or embedding model is touched."""
import json
import os

import pytest

from eval import repoqa


def _needle(path="src/pkg/mod.py", start=10, end=20, name="f"):
    return {"name": name, "path": path, "start": start, "end": end, "description": f"does {name}", "code": "x"}


# ─────────────────────────────── the tasks ───────────────────────────────
def test_needle_lines_turn_tree_sitter_rows_into_one_based_inclusive_lines():
    # tree-sitter: start row 0-based, end_line = end row + 1 (exclusive) -> lines 5..9 of the file
    assert repoqa.needle_lines({"start_line": 4, "end_line": 9}) == (5, 9)


def test_load_tasks_cuts_the_needle_code_by_byte_offsets(tmp_path):
    content = "# caf\u00e9\ndef f():\n    return 1\n"            # a 2-byte char before the needle
    start = content.encode().index(b"def f")
    data = {"python": [{"repo": "o/r", "commit_sha": "abc", "entrypoint_path": "src/", "needles": [
        {"name": "f", "path": "src/m.py", "start_line": 1, "end_line": 3, "start_byte": start,
         "end_byte": len(content.encode()) - 1, "description": "  returns one \n"}],
        "content": {"src/m.py": content}}], "java": []}
    p = tmp_path / "d.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    [task] = repoqa.load_tasks(p, ("python", "java"))
    assert task["entrypoint"] == "src"
    nd = task["needles"][0]
    assert (nd["start"], nd["end"]) == (2, 3)
    assert nd["code"] == "def f():\n    return 1"
    assert nd["description"] == "returns one"


def test_check_needle_accepts_the_code_on_its_lines_and_names_a_mismatch(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "m.py").write_text("a = 1\ndef f():\n    return 1\n", encoding="utf-8")
    nd = {"path": "src/m.py", "start": 2, "end": 3, "code": "def f():\n    return 1"}
    assert repoqa.check_needle(nd, tmp_path) is None
    assert "not on its lines" in repoqa.check_needle({**nd, "start": 1, "end": 1}, tmp_path)
    assert "unreadable" in repoqa.check_needle({**nd, "path": "src/gone.py"}, tmp_path)
    # line endings do not matter: a CRLF checkout still holds the needle on the same lines
    (tmp_path / "src" / "crlf.py").write_bytes(b"a = 1\r\ndef f():\r\n    return 1\r\n")
    assert repoqa.check_needle({**nd, "path": "src/crlf.py"}, tmp_path) is None


# ─────────────────────────────── matching ───────────────────────────────
def test_parse_span_reads_embeds_file_colon_range():
    assert repoqa.parse_span("lib/a.js:3-42") == ("lib/a.js", 3, 42)
    assert repoqa.parse_span("C:/x/a.py:1-40") == ("C:/x/a.py", 1, 40)   # only the last colon splits
    for bad in ("", "lib/a.js", "lib/a.js:3", "lib/a.js:x-4", None):
        assert repoqa.parse_span(bad) is None


def test_repo_path_prefixes_the_entrypoint_and_handles_absolute_paths(tmp_path):
    clone = tmp_path / "o__r"
    root = clone / "src" / "pkg"
    assert repoqa.repo_path("mod.py", root, clone) == "src/pkg/mod.py"
    assert repoqa.repo_path("sub/x.py", root, clone) == "src/pkg/sub/x.py"
    assert repoqa.repo_path(str(root / "mod.py"), root, clone) == "src/pkg/mod.py"


def test_overlap_needs_the_same_file_and_intersecting_lines():
    nd = _needle(start=10, end=20)
    assert repoqa.overlaps(("src/pkg/mod.py", 10, 49), nd)
    assert repoqa.overlaps(("src/pkg/mod.py", 1, 10), nd)        # touches the first line
    assert repoqa.overlaps(("src/pkg/mod.py", 20, 59), nd)       # touches the last line
    assert repoqa.overlaps(("src/pkg/mod.py", 12, 15), nd)       # inside
    assert not repoqa.overlaps(("src/pkg/mod.py", 21, 60), nd)
    assert not repoqa.overlaps(("src/pkg/mod.py", 1, 9), nd)
    assert not repoqa.overlaps(("src/pkg/other.py", 10, 20), nd)
    assert not repoqa.overlaps(("pkg/mod.py", 10, 20), nd)       # a path without the entrypoint is another file


def test_strict_overlap_only_credits_a_chunk_that_starts_inside_the_needle():
    nd = _needle(start=10, end=20)
    assert not repoqa.overlaps(("src/pkg/mod.py", 1, 40), nd, strict=True)   # a neighbour spilling in
    assert repoqa.overlaps(("src/pkg/mod.py", 10, 49), nd, strict=True)
    assert repoqa.overlaps(("src/pkg/mod.py", 20, 59), nd, strict=True)


def test_first_hit_is_the_one_based_rank_of_the_first_overlap():
    nd = _needle(start=10, end=20)
    chunks = [("src/pkg/a.py", 1, 40), None, ("src/pkg/mod.py", 1, 40), ("src/pkg/mod.py", 10, 49)]
    assert repoqa.first_hit(chunks, nd) == 3
    assert repoqa.first_hit(chunks, nd, strict=True) == 4
    assert repoqa.first_hit(chunks[:2], nd) is None
    assert repoqa.first_hit([], nd) is None


def test_hit_at_k_and_mrr():
    m = repoqa.metrics([1, 3, None, 7])
    assert m["n"] == 4
    assert m["hit@1"] == pytest.approx(1 / 4)
    assert m["hit@5"] == pytest.approx(2 / 4)
    assert m["hit@10"] == pytest.approx(3 / 4)
    assert m["mrr"] == pytest.approx((1 + 1 / 3 + 0 + 1 / 7) / 4)
    # a rank past the cut counts as no hit, in MRR too
    assert repoqa.metrics([12], ks=(1, 10))["mrr"] == 0
    # nothing scored: no rate at all, never a zero
    assert repoqa.metrics([]) == {"n": 0, "hit@1": None, "hit@5": None, "hit@10": None, "mrr": None}


# ─────────────────────────────── querying ───────────────────────────────
def test_classify_tells_an_empty_answer_from_a_failed_search():
    assert repoqa.classify([{"_note": repoqa.NO_RESULTS}]) == ("ok", None)
    status, note = repoqa.classify([{"_note": "semantic index unavailable: RuntimeError: cannot connect"}])
    assert status == "error" and "cannot connect" in note
    assert repoqa.classify([{"file": "a", "span": "a:1-2", "note": "keyword-only: vector search unavailable"},
                            {"file": "b", "span": "b:1-2"}])[0] == "degraded"
    assert repoqa.classify([{"file": "a", "span": "a:1-2", "text": "", "score": 0.5}]) == ("ok", None)


def test_modes_switch_through_the_mcp_tool_and_the_process_mode_is_restored(tmp_path, monkeypatch):
    from orion import config, embed
    monkeypatch.delenv("ORION_SEMANTIC_MODE", raising=False)
    original = config.SEMANTIC_MODE
    seen = []

    def fake_search(query, scan_id, k=5, mode=None):
        # what embed.search itself would rank by: the mode argument, else the process mode
        seen.append((mode or config.SEMANTIC_MODE, os.environ.get("ORION_SEMANTIC_MODE"), query, scan_id, k))
        if query == "does gone":
            return []                                                     # the tool turns this into a note
        if config.SEMANTIC_MODE == "keyword":
            return [{"file": "other.py", "span": "other.py:1-40", "text": ""}]
        return [{"file": "x.py", "span": "x.py:1-40", "text": ""},
                {"file": "mod.py", "span": "mod.py:12-51", "text": "", "matched": "both"}]

    monkeypatch.setattr(embed, "search", fake_search)
    clone = tmp_path / "o__r"
    needles = [_needle(name="f"), _needle(name="gone"), {**_needle(name="bad"), "key_problem": "moved"}]
    rows = repoqa.query_needles("repoqa-s1", needles, scan_root=clone / "src" / "pkg", clone_root=clone,
                                k=10, log=lambda *a, **k: None)

    # each usable needle queried once per mode, in mode order, through the real MCP impl (k passed through)
    assert [s[0] for s in seen] == ["vector", "vector", "keyword", "keyword", "hybrid", "hybrid"]
    assert all(s[1] == s[0] for s in seen)          # the env twin the agents' MCP subprocess reads follows
    assert {s[3] for s in seen} == {"repoqa-s1"} and {s[4] for s in seen} == {10}
    by = {(r["mode"], r["needle"]): r for r in rows}
    assert by[("vector", "f")]["rank"] == 2 and by[("vector", "f")]["rank_strict"] == 2
    assert by[("vector", "f")]["top"] == ["src/pkg/x.py:1-40", "src/pkg/mod.py:12-51"]
    assert by[("keyword", "f")]["rank"] is None and by[("keyword", "f")]["status"] == "ok"
    assert by[("hybrid", "f")]["matched"] == [None, "both"]
    assert by[("vector", "gone")]["status"] == "ok" and by[("vector", "gone")]["returned"] == 0
    assert by[("hybrid", "bad")]["status"] == "bad_key"
    # put back exactly as found
    assert config.SEMANTIC_MODE == original
    assert "ORION_SEMANTIC_MODE" not in os.environ


def test_a_search_that_raises_is_missing_not_a_miss(tmp_path):
    def boom(query, scan_id, k):
        raise RuntimeError("neo4j down")
    rows = repoqa.query_needles("s", [_needle()], scan_root=tmp_path, clone_root=tmp_path, modes=("vector",),
                                search=boom, log=lambda *a, **k: None)
    assert rows[0]["status"] == "error" and "neo4j down" in rows[0]["note"]
    assert "rank" not in rows[0]


def test_resume_keeps_ok_repos_reruns_failed_and_redo_ones_and_keeps_the_old_attempt(tmp_path, monkeypatch):
    tasks = [{"language": "python", "repo": r, "commit": "c", "entrypoint": "src", "needles": [_needle()]}
             for r in ("o/a", "o/b", "o/c")]
    out = tmp_path / "r.json"
    first = {"settings": {"k": 10, "modes": ["vector"], "languages": ["python"], "n_repos": 3, "started": "t0"},
             "repos": [{"language": "python", "repo": "o/a", "status": "ok", "needles": 1, "bad_keys": 1,
                        "size": {"files": 1, "bytes": 1, "lines": 1}, "chunks": 1, "build_s": 0, "index_s": 0,
                        "db_chunks_total": 1, "queries": [_q("vector", 1)]},
                       {"language": "python", "repo": "o/b", "status": "failed", "error": "CUDA out of memory",
                        "needles": 1, "queries": []},
                       {"language": "python", "repo": "o/c", "status": "ok", "needles": 1,
                        "size": {"files": 1, "bytes": 1, "lines": 1}, "chunks": 1, "build_s": 0, "index_s": 0,
                        "db_chunks_total": 1, "queries": [_q("vector", 2)], "marker": "kept"}]}
    out.write_text(json.dumps(first), encoding="utf-8")
    ran = []

    def fake_run_repo(task, repos_dir, **kw):
        ran.append(task["repo"])
        return {"language": "python", "repo": task["repo"], "status": "ok", "embed_device": "cpu",
                "size": {"files": 1, "bytes": 1, "lines": 1}, "chunks": 1, "build_s": 0, "index_s": 0,
                "db_chunks_total": 1, "queries": [_q("vector", 1)]}

    monkeypatch.setattr(repoqa, "load_tasks", lambda data, languages: tasks)
    monkeypatch.setattr(repoqa, "run_repo", fake_run_repo)
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "")
    monkeypatch.setattr("builtins.print", lambda *a, **k: None)
    repoqa.main(["run", "--resume", "--redo", "o/a", "--out", str(out), "--modes", "vector",
                 "--languages", "python"])

    assert ran == ["o/a", "o/b"]                                   # c was ok and not named: kept as it was
    res = json.loads(out.read_text(encoding="utf-8"))
    by = {r["repo"]: r for r in res["repos"]}
    assert by["o/c"]["marker"] == "kept" and "previous_attempt" not in by["o/c"]
    assert by["o/a"]["previous_attempt"]["bad_keys"] == 1
    assert by["o/b"]["previous_attempt"] == {"status": "failed", "error": "CUDA out of memory", "bad_keys": None,
                                             "embed_device": None, "previous_attempt": None}
    assert res["settings"]["started"] == "t0"                      # the first pass's settings stay
    assert res["settings"]["resumes"][0]["redo"] == ["o/a"]
    assert res["settings"]["resumes"][0]["CUDA_VISIBLE_DEVICES"] == ""
    md = (tmp_path / "r.md").read_text(encoding="utf-8")
    assert "o/b (was failed: CUDA out of memory)" in md and "o/a (was ok, 1 unusable keys)" in md

    # --repo limits what runs; every other repo keeps its record (failed ones too) and the order stays
    out.write_text(json.dumps(first), encoding="utf-8")
    ran.clear()
    repoqa.main(["run", "--resume", "--repo", "o/c", "--redo", "o/c", "--out", str(out), "--modes", "vector",
                 "--languages", "python"])
    assert ran == ["o/c"]
    res = json.loads(out.read_text(encoding="utf-8"))
    assert [(r["repo"], r["status"]) for r in res["repos"]] == [("o/a", "ok"), ("o/b", "failed"), ("o/c", "ok")]
    assert res["repos"][1].get("error") == "CUDA out of memory"


# ─────────────────────────────── summary + table ───────────────────────────────
def _q(mode, rank, status="ok", returned=10, strict=None):
    return {"mode": mode, "needle": "n", "status": status, "rank": rank, "returned": returned,
            "rank_strict": rank if strict is None else strict, "indexed": True}


def test_summary_counts_failed_repos_and_queries_as_missing():
    repos = [
        {"language": "python", "repo": "a", "status": "ok", "needles": 3,
         "queries": [_q("vector", 1), _q("vector", None), _q("vector", None, status="error")]},
        {"language": "python", "repo": "b", "status": "failed", "needles": 2, "queries": []},
        {"language": "java", "repo": "c", "status": "ok", "needles": 1, "queries": [_q("vector", 4, returned=3)]},
    ]
    s = repoqa.summarize(repos, modes=("vector",), languages=("python", "java"), k=10)["vector"]
    assert s["python"]["n"] == 2 and s["python"]["missing"] == 3          # 1 errored query + 2 needles of b
    assert s["python"]["hit@1"] == pytest.approx(0.5)
    assert s["all"]["n"] == 3 and s["all"]["missing"] == 3
    assert s["java"]["hit@5"] == 1.0 and s["java"]["hit@1"] == 0.0
    assert s["all"]["short_answers"] == 1


def test_table_renders_modes_as_rows_and_marks_failures():
    repos = [{"language": "python", "repo": "o/a", "status": "ok", "needles": 1, "size": {"files": 2, "bytes": 3000,
              "lines": 10}, "chunks": 5, "build_s": 1.0, "index_s": 2.0, "db_chunks_total": 9,
              "queries": [_q("vector", 1), _q("hybrid", None)]},
             {"language": "python", "repo": "o/b", "status": "failed", "needles": 1, "error": "joern died",
              "queries": []}]
    result = {"settings": {"k": 10, "modes": ["vector", "hybrid"], "languages": ["python"], "n_repos": 2},
              "repos": repos}
    result["summary"] = repoqa.summarize(repos, modes=("vector", "hybrid"), languages=("python",))
    md = repoqa.table(result)
    assert "| vector | 1.00 / 1.00 / 1.00 / 1.00 (n 1, missing 1) |" in md
    assert "| hybrid | 0.00 / 0.00 / 0.00 / 0.00 (n 1, missing 1) |" in md
    assert "| o/b | python | | | | | | | -- | -- |" in md
    assert "o/b: joern died" in md
