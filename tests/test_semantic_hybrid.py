"""Hybrid keyword + vector semantic search (orion/embed.py, ORION_SEMANTIC_MODE). Token-free.

Pure parts (the Lucene query builder, Reciprocal Rank Fusion, the merge) run anywhere. The search
routing runs on stubbed hit functions. One live test writes plain Chunk nodes (no embeddings, so no
model download) and searches them by keyword; it skips without Neo4j.

Pins: the default mode is "vector" and takes the original code path; the MCP tool description is
unchanged in that mode; hybrid falls back to keyword-only when the embedding model is unavailable.
"""
from __future__ import annotations

import asyncio

import pytest

from orion import config, embed, mcp_server


# ── query building: safe by construction ───────────────────────────────
def test_keyword_query_splits_identifiers_like_the_index():
    assert embed.keyword_query("eval(req.body.preTax)") == "eval req body pretax"
    assert embed.keyword_query("$where") == "where"
    assert embed.keyword_query("Where WHERE where") == "where"          # lowercased, deduped


def test_keyword_query_cannot_carry_lucene_syntax():
    q = embed.keyword_query('*:* OR title:"x"~2 AND -y^3 \\ (z) [a TO b] /re/')
    assert q == "or title x and y z a to b re"
    assert not any(ch in q for ch in '*:"~^\\()[]/+-')
    assert embed.keyword_query("123 +- !! ()") == ""                     # nothing to match


def test_keyword_query_caps_terms():
    words = " ".join(f"w{chr(97 + i % 26)}{chr(97 + i // 26)}" for i in range(100))
    assert len(embed.keyword_query(words).split()) == embed._MAX_QUERY_TERMS


# ── Reciprocal Rank Fusion ─────────────────────────────────────────────
def test_rrf_scores_and_order():
    fused = embed.rrf([["a", "b", "c"], ["c", "d"]], k=60)
    scores = dict(fused)
    assert scores["a"] == pytest.approx(1 / 61)
    assert scores["c"] == pytest.approx(1 / 63 + 1 / 61)                # found by both rankings
    assert [item for item, _ in fused][0] == "c"                         # agreement beats one top spot
    assert [item for item, _ in fused][1:] == ["a", "b", "d"]            # 1/61 > 1/62; b ties d, b seen first


def test_rrf_empty():
    assert embed.rrf([[], []]) == []


def _hit(file, span, text="t", score=1.0):
    return {"file": file, "span": span, "text": text, "score": score}


def test_fuse_labels_where_each_chunk_matched():
    vector = [_hit("a.js", "a.js:1-40"), _hit("b.js", "b.js:1-40")]
    keyword = [_hit("c.js", "c.js:1-40"), _hit("a.js", "a.js:1-40")]
    rows = embed.fuse(vector, keyword, k=2)
    assert [(r["file"], r["matched"]) for r in rows] == [("a.js", "both"), ("c.js", "keyword")]
    assert set(rows[0]) == {"file", "span", "text", "score", "matched"}
    assert rows[0]["score"] > rows[1]["score"]


# ── search routing on stubs ────────────────────────────────────────────
class _Session:
    def __init__(self, has_vector_index=True):
        self.has = has_vector_index

    def run(self, query, **kw):
        assert query.startswith("SHOW INDEXES")
        has = self.has

        class _R:
            def single(self):
                return {"c": 1 if has else 0}
        return _R()


@pytest.fixture
def stubs(monkeypatch):
    calls = {"vector": 0, "keyword": [], "fulltext": 0}

    def vec(session, query, scan_id, n):
        calls["vector"] += 1
        return [_hit("v.js", "v.js:1-40")]

    def kw(session, lucene, scan_id, n):
        calls["keyword"].append(lucene)
        return [_hit("k.js", "k.js:1-40")]

    monkeypatch.setattr(embed, "_vector_hits", vec)
    monkeypatch.setattr(embed, "_keyword_hits", kw)
    monkeypatch.setattr(embed, "_ensure_fulltext_index", lambda s, wait=True: calls.__setitem__("fulltext", 1))
    return calls


def test_hybrid_uses_both_rankings(stubs):
    rows = embed._search_hybrid(_Session(), "find $where", "s", 5, "hybrid")
    assert stubs["vector"] == 1 and stubs["keyword"] == ["find where"] and stubs["fulltext"]
    assert {r["matched"] for r in rows} == {"vector", "keyword"} and "note" not in rows[0]


def test_keyword_mode_never_touches_the_model(stubs, monkeypatch):
    monkeypatch.setattr(embed, "_get_model", lambda: pytest.fail("keyword mode loaded the model"))
    rows = embed._search_hybrid(_Session(), "eval", "s", 5, "keyword")
    assert stubs["vector"] == 0 and [r["matched"] for r in rows] == ["keyword"]


def test_hybrid_falls_back_to_keyword_when_model_unavailable(stubs, monkeypatch):
    def broken(*a):
        raise RuntimeError("failed to load embedding model")
    monkeypatch.setattr(embed, "_vector_hits", broken)
    rows = embed._search_hybrid(_Session(), "eval", "s", 5, "hybrid")
    assert [r["file"] for r in rows] == ["k.js"]
    assert "keyword-only" in rows[0]["note"] and "failed to load" in rows[0]["note"]


def test_hybrid_without_vector_index_is_keyword_only(stubs):
    rows = embed._search_hybrid(_Session(has_vector_index=False), "eval", "s", 5, "hybrid")
    assert stubs["vector"] == 0 and [r["matched"] for r in rows] == ["keyword"] and "note" not in rows[0]


def test_query_with_no_words_skips_keyword(stubs):
    assert embed._search_hybrid(_Session(), "++ 42", "s", 5, "keyword") == []
    assert stubs["keyword"] == []


def test_default_mode_is_vector_and_takes_the_original_path(monkeypatch):
    assert config.SEMANTIC_MODE == "vector"            # the default until bench/prove.py measures hybrid
    monkeypatch.setattr(embed, "_search_hybrid", lambda *a: pytest.fail("vector mode went hybrid"))

    class _Stop(Exception):
        pass

    def _driver():
        raise _Stop
    monkeypatch.setattr(embed, "_driver", _driver)
    with pytest.raises(_Stop):
        embed.search("q", "s")                          # reached the original vector code path


def test_unknown_mode_is_an_error():
    with pytest.raises(RuntimeError, match="unknown ORION_SEMANTIC_MODE"):
        embed.search("q", "s", mode="fuzzy")


def test_set_semantic_mode_reaches_the_env(monkeypatch):
    monkeypatch.setattr(config, "SEMANTIC_MODE", "vector")
    monkeypatch.delenv("ORION_SEMANTIC_MODE", raising=False)
    config.set_semantic_mode("hybrid")
    import os
    assert config.SEMANTIC_MODE == "hybrid" and os.environ["ORION_SEMANTIC_MODE"] == "hybrid"
    with pytest.raises(ValueError):
        config.set_semantic_mode("bm25")


# ── the MCP tool description ───────────────────────────────────────────
def test_tool_description_unchanged_in_vector_mode():
    tools = {t.name: t.description for t in asyncio.run(mcp_server.mcp.list_tools())}
    assert tools["semantic_search"] == "Nearest code chunks to `query` in the given scan, by meaning (not structure)."
    assert mcp_server.semantic_search_description("vector") == tools["semantic_search"]


def test_tool_description_mentions_exact_identifiers_when_hybrid():
    for mode in ("hybrid", "keyword"):
        d = mcp_server.semantic_search_description(mode)
        assert "`eval`" in d and "`$where`" in d


# ── live: keyword search over real Chunk nodes ─────────────────────────
def test_keyword_and_fallback_search_live(monkeypatch):
    from neo4j import GraphDatabase
    drv = GraphDatabase.driver(config.NEO4J_URI, auth=config.NEO4J_AUTH)
    try:
        drv.verify_connectivity()
    except Exception as exc:  # noqa: BLE001
        drv.close()
        pytest.skip(f"Neo4j not reachable: {exc}")
    sid = "test-semantic-hybrid-live"
    chunks = [("app/data/allocations-dao.js", "app/data/allocations-dao.js:70-110",
               "const searchCriteria = { $where: `this.userId == ${parsedUserId}` };\nreturn db.find(searchCriteria);"),
              ("app/routes/contributions.js", "app/routes/contributions.js:30-70",
               "const preTax = eval(req.body.preTax);"),
              ("app/views/layout.js", "app/views/layout.js:1-40", "function renderLayout(page) { return page; }")]
    try:
        with drv.session(database=config.NEO4J_DATABASE) as s:
            s.run("UNWIND $rows AS r CREATE (:Chunk {scan_id:$sid, file:r[0], span:r[1], text:r[2]})",
                  rows=[list(c) for c in chunks], sid=sid)
        where = embed.search("$where", sid, k=3, mode="keyword")
        assert where[0]["file"] == "app/data/allocations-dao.js" and where[0]["matched"] == "keyword"
        assert embed.search("eval(req.body.preTax)", sid, k=1, mode="keyword")[0]["file"] == \
            "app/routes/contributions.js"
        assert all(r["file"] for r in embed.search("render", sid, k=5, mode="keyword"))
        assert embed.search("$where", "some-other-scan", k=3, mode="keyword") == []   # scan-scoped

        def no_model():
            raise RuntimeError("failed to load embedding model (stubbed)")
        monkeypatch.setattr(embed, "_get_model", no_model)
        rows = embed.search("$where", sid, k=3, mode="hybrid")
        assert rows[0]["file"] == "app/data/allocations-dao.js"
        if "note" in rows[0]:                    # only when a vector index exists on this database
            assert "keyword-only" in rows[0]["note"]
    finally:
        with drv.session(database=config.NEO4J_DATABASE) as s:
            s.run("MATCH (c:Chunk {scan_id:$sid}) DETACH DELETE c", sid=sid)
        drv.close()
