"""Orion-LM hardening (merge plan Phase 3): streamed read_code is byte-identical to the old full read
on ordinary files and bounded on pathological ones; tool results are capped; path nodes name the
CALLING method. Pure; no Neo4j, no MLX, no tree-sitter.
"""
from __future__ import annotations

import json
import os

import pytest

from orion.lm import tools
from orion.lm.codeindex import _split_lines


def _old_read_code(repo_path, file, start, end):
    """tools.read_code before 2026-10-28 (full read), for the files these tests write."""
    full = os.path.join(repo_path, file)
    start, end = max(1, int(start)), int(end)
    end = max(start, end)
    end = min(end, start + tools.MAX_WINDOW - 1)
    with open(full, encoding="utf-8", errors="replace") as f:
        lines = _split_lines(f.read())
    end = min(end, len(lines))
    return {"file": file, "start": start, "end": end, "lines": "\n".join(lines[start - 1:end])}


FILES = {
    "lf.py": "def f():\n    return 1\n\n\nx = f()\n",
    "crlf.js": "function a() {\r\n  return 1;\r\n}\r\nconst b = a();\r\n",
    "no_trailing.py": "a = 1\nb = 2\nc = 3",
    "odd_breaks.js": "const s = 'a\x0cb';\nconst t = 'c d';\nlet u = 1;\n",
    "empty.py": "",
    "blank_lines.py": "\n\n\nx = 1\n\n",
}
WINDOWS = [(1, 1), (1, 3), (2, 4), (1, 500), (3, 2), (4, 10), (10, 20), (0, 2)]


@pytest.mark.parametrize("name", sorted(FILES))
@pytest.mark.parametrize("window", WINDOWS)
def test_streamed_read_code_matches_the_old_full_read(tmp_path, name, window):
    (tmp_path / name).write_bytes(FILES[name].encode("utf-8"))
    start, end = window
    new = tools.read_code(str(tmp_path), name, start, end)
    if start > end:
        assert "error" in new
        return
    assert new == _old_read_code(str(tmp_path), name, start, end)


def test_a_minified_line_and_a_huge_window_are_bounded(tmp_path):
    (tmp_path / "min.js").write_text("var a=1;" * 50_000 + "\n" + "ok\n", encoding="utf-8")
    out = tools.read_code(str(tmp_path), "min.js", 1, 2)
    assert out["truncated"] is True and out["lines"].endswith("ok")
    assert len(out["lines"]) < tools.LINE_CAP + 100
    (tmp_path / "wide.py").write_text("\n".join("x" * 400 for _ in range(200)) + "\n", encoding="utf-8")
    out = tools.read_code(str(tmp_path), "wide.py", 1, 200)
    assert out["truncated"] is True and len(out["lines"]) <= tools.READ_CAP
    assert out["end"] == out["lines"].count("\n") + 1          # end = the last line actually returned


def test_tool_rows_are_capped_per_field_and_in_total():
    big = {"code": "y" * 5000, "line": 3}
    rows, capped = tools._cap_rows([big] + [{"code": "z" * 900, "line": i} for i in range(20)])
    assert capped and rows[0]["code"].endswith("...[truncated]") and len(rows[0]["code"]) < 1100
    assert len(json.dumps(rows)) <= tools.RESULT_CAP or len(rows) == 1
    small = [{"code": "a", "line": 1}]
    assert tools._cap_rows(small) == (small, False)


class _FakeDB:
    def __init__(self, rows):
        self.rows, self.queries = rows, []

    def run_cypher(self, scan_id, query, limit=50, **kw):
        self.queries.append(query)
        if query.startswith("EXPLAIN"):
            return {"row_count": 0, "rows": [], "truncated": False, "query_type": "r"}
        return {"row_count": len(self.rows), "rows": self.rows, "truncated": False, "query_type": "r"}


def test_path_nodes_name_the_calling_method_not_the_callee():
    from orion.lm import slice as lm_slice
    db = _FakeDB([{"uid": "u1", "file": "a.js", "function": "routes.contributions.handle",
                   "line": 28, "code": "eval(x)"}])
    out = lm_slice.flow_slice(db, "s", {"rank": 0, "path_uids": ["u1"], "source_uid": "u1",
                                        "sink_uid": "u1", "sink_category": "code_exec"})
    assert out["nodes"][0]["function"] == "routes.contributions.handle"
    q = db.queries[-1]
    assert "CONTAINS_CALL" in q and "min(m.full_name) AS function" in q
    assert tools.check_query(q) is None            # the lm checker accepts its own query


def test_the_engines_own_slice_reads_are_never_capped():
    """The caps are for what the MODEL reads. slice.py's reads build the flows the model is given; a
    long path (path_uids, path code) must arrive whole."""
    from orion.lm import slice as lm_slice
    uids = [f"uid-{i:04d}-" + "x" * 40 for i in range(60)]           # ~3.3 KB of path_uids
    db = _FakeDB([{"rank": 0, "source_uid": uids[0], "sink_uid": uids[-1], "sink_category": "sql",
                   "path_uids": json.dumps(uids)}])
    [flow] = lm_slice.candidate_flows(db, "s", max_flows=5)
    assert flow["path_uids"] == uids
    rows = [{"uid": u, "file": "a.py", "function": "f", "line": i + 1, "code": "c" * 900}
            for i, u in enumerate(uids)]
    out = lm_slice.flow_slice(_FakeDB(rows), "s", flow)
    assert len(out["nodes"]) == 60 and all(len(n["code"]) == 900 for n in out["nodes"])
