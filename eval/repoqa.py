"""RepoQA "Search Needle Function" (SNF) as a token-free benchmark of Orion's `semantic_search` memory layer.

RepoQA (github.com/evalplus/repoqa, Apache-2.0; attribution in eval/dataset/repoqa/NOTICE.md) pins 10 repos per
language and picks 10 "needle" functions in each; every needle has a natural-language description of what the
function does that never names it. RepoQA gives an LLM the description plus a long code context and asks for the
function. Here the description is the QUERY: Orion indexes the repo exactly as `orion scan` does
(`graph_build.build(stream=True)` + `embed.index`) and the description goes through the MCP tool's own code path
(`mcp_server.semantic_search_impl`) once per `semantic_search` mode -- `vector` (the default), `keyword` (BM25) and
`hybrid` (both, fused by RRF). No `claude` call is made; the only model is the local embedding model.

Scored per needle: the 1-based rank of the first returned chunk that overlaps the needle (same file, chunk lines
intersect the needle's lines) -> hit@1/5/10 and MRR@10. A `strict` variant only credits a chunk that STARTS
inside the needle (a neighbour's 40-line window spilling into it does not count). `indexed` says whether any
chunk of the scan overlaps the needle at all (the ceiling the ranking works under).

Scope: Python, Java and TypeScript (Orion's frontends pythonsrc / javasrc / jssrc). Each repo is scanned at
RepoQA's pinned commit, rooted at RepoQA's `entrypoint_path` -- the code RepoQA's own haystack is cut from -- so
Orion's file paths are prefixed with that path before matching. A failed build / index / query is recorded as
missing (excluded from every rate, counted in `missing`), never as a miss.

Caveat measured, not corrected: Orion's searches overfetch from GLOBAL indexes (at k = 10: top 200 in vector mode,
top 800 in keyword and hybrid) and then keep this scan's rows, so other scans in the same Neo4j can crowd a scan out. Each query records how many
rows came back (`returned`) and each repo the database's total Chunk count when it was queried.

    python -m eval.repoqa run [--data F] [--repos DIR] [--out eval/results/repoqa-pilot.json] [--languages ...]
    python -m eval.repoqa table eval/results/repoqa-pilot.json      # re-render the markdown, token-free

`run --resume` keeps the repos already ok in --out and runs the rest again (`--redo R` re-runs an ok one); a
re-run record keeps what the attempt it replaced said (`previous_attempt`), and each record says where the
embedding ran (`embed_device`), so a CPU retry of a GPU out-of-memory failure is visible as one.

Only the scans this run builds are deleted (by scan id, every scan label), after each repo's queries.
RepoQA's repos predate every model cutoff: this is dev evidence on the memory layer, never an eval.report row.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

DATA_VERSION = "2024-06-23"
DATA_URL = (f"https://github.com/evalplus/repoqa_release/releases/download/{DATA_VERSION}/"
            f"repoqa-{DATA_VERSION}.json.gz")
DATA_GZ_SHA256 = "c050a2ad90a7df89d9dc1f1c3b3b20683edd20a56293b35fcaae43dec115d681"
DEFAULT_DATA = Path("~/bench-data/repoqa/data") / f"repoqa-{DATA_VERSION}.json"
DEFAULT_REPOS = Path("~/bench-data/repoqa/repos")
DEFAULT_OUT = Path(__file__).resolve().parent / "results" / "repoqa-pilot.json"

FRONTENDS = {"python": "pythonsrc", "java": "javasrc", "typescript": "jssrc"}
SOURCE_EXTS = {"python": (".py",), "java": (".java",),
               "typescript": (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")}
MODES = ("vector", "keyword", "hybrid")
KS = (1, 5, 10)
SCAN_PREFIX = "repoqa-"
NO_RESULTS = "semantic index unavailable: no results"   # mcp_server's note for an empty (not failed) search


# ─────────────────────────────── the tasks ───────────────────────────────
def needle_lines(needle: dict) -> tuple[int, int]:
    """RepoQA stores tree-sitter rows: `start_line` 0-based, `end_line` exclusive -> 1-based inclusive."""
    return needle["start_line"] + 1, needle["end_line"]


def load_tasks(data_path, languages=tuple(FRONTENDS)) -> list[dict]:
    """One task per repo: language, repo, commit, entrypoint and its needles (with the needle's own code,
    cut from RepoQA's copy of the file by byte offsets, to check the clone against)."""
    data = json.loads(Path(os.path.expanduser(str(data_path))).read_text(encoding="utf-8"))
    tasks = []
    for lang in languages:
        for r in data[lang]:
            needles = []
            for n in r["needles"]:
                start, end = needle_lines(n)
                code = r["content"][n["path"]].encode("utf-8")[n["start_byte"]:n["end_byte"]]
                needles.append({"name": n["name"], "path": n["path"], "start": start, "end": end,
                                "description": n["description"].strip(),
                                "code": code.decode("utf-8", errors="replace")})
            tasks.append({"language": lang, "repo": r["repo"], "commit": r["commit_sha"],
                          "entrypoint": r["entrypoint_path"].strip("/"), "needles": needles})
    return tasks


def check_needle(needle: dict, clone_root) -> str | None:
    """None if the clone has the needle's code on the needle's lines, else why not (the key is then unusable)."""
    try:
        with open(Path(clone_root) / needle["path"], encoding="utf-8", errors="replace", newline="") as fh:
            lines = fh.read().splitlines(keepends=True)
    except OSError as exc:
        return f"file unreadable: {exc}"
    window = "".join(lines[needle["start"] - 1:needle["end"]]).replace("\r\n", "\n")
    code = needle["code"].replace("\r\n", "\n")
    return None if code and code in window else "needle code not on its lines in the clone"


# ─────────────────────────────── matching ───────────────────────────────
def parse_span(span: str) -> tuple[str, int, int] | None:
    """embed's chunk span `file:a-b` -> (file, a, b); None if it is not in that shape."""
    head, sep, rng = (span or "").rpartition(":")
    a, dash, b = rng.partition("-")
    if not sep or not dash or not a.isdigit() or not b.isdigit():
        return None
    return head, int(a), int(b)


def repo_path(chunk_file: str, scan_root, clone_root) -> str:
    """A chunk's file as a repo-root-relative POSIX path (Orion's paths are relative to the scanned folder,
    or absolute for some frontends)."""
    full = chunk_file if os.path.isabs(chunk_file) else os.path.join(str(scan_root), chunk_file)
    return Path(os.path.relpath(full, str(clone_root))).as_posix()


def overlaps(chunk: tuple[str, int, int], needle: dict, *, strict: bool = False) -> bool:
    """Same file and the line ranges intersect; `strict`: the chunk starts inside the needle."""
    file, a, b = chunk
    if file != needle["path"]:
        return False
    if strict:
        return needle["start"] <= a <= needle["end"]
    return a <= needle["end"] and b >= needle["start"]


def first_hit(chunks: list[tuple[str, int, int] | None], needle: dict, *, strict: bool = False) -> int | None:
    """1-based rank of the first overlapping chunk, None if none of them overlaps."""
    for rank, chunk in enumerate(chunks, start=1):
        if chunk is not None and overlaps(chunk, needle, strict=strict):
            return rank
    return None


def metrics(ranks: list[int | None], ks=KS) -> dict:
    """hit@k = share of queries whose first hit is at rank <= k; MRR@max(ks) = mean 1/rank (0 past the cut).
    `ranks` holds only scored queries: a failed one is missing, never passed in as a miss."""
    n = len(ranks)
    if not n:
        return {"n": 0, **{f"hit@{k}": None for k in ks}, "mrr": None}
    cut = max(ks)
    out = {"n": n}
    for k in ks:
        out[f"hit@{k}"] = sum(1 for r in ranks if r is not None and r <= k) / n
    out["mrr"] = sum(1.0 / r for r in ranks if r is not None and r <= cut) / n
    return out


# ─────────────────────────────── querying ───────────────────────────────
def classify(rows: list[dict]) -> tuple[str, str | None]:
    """The MCP tool folds 'nothing found' and 'search failed' into one `_note` row; tell them apart.
    -> (status, note): ok (rows or a genuine empty answer), error (missing), degraded (hybrid fell back to
    keyword-only, so it is not a hybrid answer: missing)."""
    if len(rows) == 1 and "_note" in rows[0]:
        note = rows[0]["_note"]
        return ("ok", None) if note == NO_RESULTS else ("error", note)
    note = next((r["note"] for r in rows if r.get("note")), None)
    return ("degraded", note) if note else ("ok", None)


def _mcp_search(query: str, scan_id: str, k: int) -> list[dict]:
    from orion.mcp_server import semantic_search_impl
    return semantic_search_impl(query, scan_id, k)


def query_needles(scan_id: str, needles: list[dict], *, scan_root, clone_root, modes=MODES, k: int = max(KS),
                  search=_mcp_search, log=print) -> list[dict]:
    """Every usable needle's description through `search` once per mode. The mode is switched the way the
    CLI's --semantic-mode does it (`config.set_semantic_mode`, which `embed.search` reads per call) and the
    process's own mode is put back afterwards."""
    from orion import config
    before_mode, before_env = config.SEMANTIC_MODE, os.environ.get("ORION_SEMANTIC_MODE")
    out = []
    try:
        for mode in modes:
            config.set_semantic_mode(mode)
            for nd in needles:
                row = {"needle": nd["name"], "path": nd["path"], "lines": [nd["start"], nd["end"]], "mode": mode}
                if nd.get("key_problem"):
                    out.append({**row, "status": "bad_key", "note": nd["key_problem"]})
                    continue
                t0 = time.monotonic()
                try:
                    rows = search(nd["description"], scan_id, k)
                except Exception as exc:  # noqa: BLE001 -- recorded as missing, never as a miss
                    out.append({**row, "status": "error", "note": f"{type(exc).__name__}: {exc}"})
                    continue
                status, note = classify(rows)
                hits = [] if "_note" in (rows[0] if rows else {}) else rows
                chunks = []
                for h in hits:
                    p = parse_span(h.get("span", ""))
                    chunks.append(None if p is None else (repo_path(p[0], scan_root, clone_root), p[1], p[2]))
                out.append({**row, "status": status, "note": note, "returned": len(hits),
                            "rank": first_hit(chunks, nd), "rank_strict": first_hit(chunks, nd, strict=True),
                            "top": [None if c is None else f"{c[0]}:{c[1]}-{c[2]}" for c in chunks],
                            "matched": [h.get("matched") for h in hits] if mode == "hybrid" else None,
                            "seconds": round(time.monotonic() - t0, 3)})
            done = [r for r in out if r["mode"] == mode and r["status"] == "ok"]
            log(f"    {mode:7s} hit@1 {sum(1 for r in done if r['rank'] == 1)}/{len(done)}  "
                f"hit@10 {sum(1 for r in done if r['rank'] is not None)}/{len(done)}", flush=True)
    finally:
        config.SEMANTIC_MODE = before_mode
        if before_env is None:
            os.environ.pop("ORION_SEMANTIC_MODE", None)
        else:
            os.environ["ORION_SEMANTIC_MODE"] = before_env
    return out


# ─────────────────────────────── one repo ───────────────────────────────
def source_size(root, language: str) -> dict:
    """Files / bytes / lines of the language's source files under the scanned folder (what Joern parses)."""
    files = size = lines = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for f in filenames:
            if f.endswith(SOURCE_EXTS[language]):
                p = os.path.join(dirpath, f)
                files += 1
                size += os.path.getsize(p)
                with open(p, "rb") as fh:
                    lines += fh.read().count(b"\n")
    return {"files": files, "bytes": size, "lines": lines}


def _graph_counts(scan_id: str) -> dict:
    from neo4j import GraphDatabase
    from orion import config
    driver = GraphDatabase.driver(config.NEO4J_URI, auth=config.NEO4J_AUTH)
    try:
        with driver.session(database=config.NEO4J_DATABASE) as s:
            one = lambda q: s.run(q, sid=scan_id).single()["n"]   # noqa: E731
            return {"cpg_files": one("MATCH (f:CpgFile {scan_id:$sid}) RETURN count(f) AS n"),
                    "cpg_methods_located": one("MATCH (m:CpgMethod {scan_id:$sid}) WHERE m.file_path IS NOT NULL "
                                               "AND m.line IS NOT NULL RETURN count(m) AS n"),
                    "chunks": one("MATCH (c:Chunk {scan_id:$sid}) RETURN count(c) AS n"),
                    "db_chunks_total": s.run("MATCH (c:Chunk) RETURN count(c) AS n").single()["n"],
                    "spans": [r["span"] for r in s.run("MATCH (c:Chunk {scan_id:$sid}) RETURN c.span AS span",
                                                       sid=scan_id)]}
    finally:
        driver.close()


def _delete_scan(scan_id: str) -> dict:
    from neo4j import GraphDatabase
    from orion import config
    from orion.graph import scans
    driver = GraphDatabase.driver(config.NEO4J_URI, auth=config.NEO4J_AUTH)
    try:
        return scans.delete_scan(driver, scan_id, scans.SCAN_LABELS)
    finally:
        driver.close()


def _head(clone_root) -> str | None:
    out = subprocess.run(["git", "-C", str(clone_root), "rev-parse", "HEAD"], capture_output=True, text=True)
    return out.stdout.strip() if out.returncode == 0 else None


def clone_dir(repos_dir, repo: str) -> Path:
    return Path(os.path.expanduser(str(repos_dir))) / repo.replace("/", "__")


def run_repo(task: dict, repos_dir, *, modes=MODES, k: int = max(KS), keep_scan: bool = False, log=print) -> dict:
    from orion import embed, graph_build
    clone_root = clone_dir(repos_dir, task["repo"])
    scan_root = clone_root / task["entrypoint"]
    rec = {k_: task[k_] for k_ in ("language", "repo", "commit", "entrypoint")}
    rec.update(status="failed", queries=[])
    head = _head(clone_root)
    if head != task["commit"]:
        rec["error"] = f"clone at {head!r}, RepoQA pins {task['commit']}"
        return rec
    rec["size"] = source_size(scan_root, task["language"])
    needles = []
    for nd in task["needles"]:
        problem = check_needle(nd, clone_root)
        needles.append({**nd, "key_problem": problem} if problem else nd)
    rec["bad_keys"] = sum(1 for nd in needles if nd.get("key_problem"))
    scan_id = SCAN_PREFIX + graph_build.scan_id_for(str(scan_root))
    rec["scan_id"] = scan_id
    try:
        t0 = time.monotonic()
        graph_build.build(str(scan_root), FRONTENDS[task["language"]], stream=True, scan_id=scan_id)
        rec["build_s"] = round(time.monotonic() - t0, 1)
        t0 = time.monotonic()
        embed.index(str(scan_root), scan_id)
        rec["index_s"] = round(time.monotonic() - t0, 1)
        counts = _graph_counts(scan_id)
        spans = [parse_span(s) for s in counts.pop("spans")]
        spans = [(repo_path(p[0], scan_root, clone_root), p[1], p[2]) for p in spans if p]
        rec.update(counts)
        log(f"  built {rec['build_s']}s, indexed {rec['index_s']}s: {rec['chunks']} chunks "
            f"({rec['db_chunks_total']} in the database)", flush=True)
        if not rec["chunks"]:
            rec["error"] = "embed.index wrote no chunks"
            return rec
        rec["queries"] = query_needles(scan_id, needles, scan_root=scan_root, clone_root=clone_root,
                                       modes=modes, k=k, log=log)
        indexed = {nd["name"]: any(overlaps(c, nd) for c in spans) for nd in needles}
        for q in rec["queries"]:
            q["indexed"] = indexed[q["needle"]]
        rec["status"] = "ok"
    except Exception as exc:  # noqa: BLE001 -- one repo's failure is recorded, the run goes on
        rec["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        model = getattr(embed, "_model", None)        # where the embedding ran (cuda:0 / cpu), as loaded
        rec["embed_device"] = str(model.device) if model is not None else None
        if not keep_scan:
            try:
                rec["deleted"] = _delete_scan(scan_id)
            except Exception as exc:  # noqa: BLE001
                rec["delete_error"] = f"{type(exc).__name__}: {exc}"
    return rec


# ─────────────────────────────── summary + table ───────────────────────────────
def summarize(repos: list[dict], modes=MODES, languages=tuple(FRONTENDS), k: int = max(KS)) -> dict:
    """{mode: {language|all: metrics + missing + strict}}. Missing = needles of a failed repo, unusable keys,
    and failed / degraded queries."""
    out = {}
    for mode in modes:
        out[mode] = {}
        for lang in (*languages, "all"):
            rs = [r for r in repos if lang in ("all", r["language"])]
            expected = sum(r.get("needles", 10) for r in rs)
            qs = [q for r in rs if r["status"] == "ok" for q in r["queries"] if q["mode"] == mode]
            ok = [q for q in qs if q["status"] == "ok"]
            m = metrics([q["rank"] for q in ok])
            strict = metrics([q["rank_strict"] for q in ok])
            out[mode][lang] = {**m, "missing": expected - len(ok),
                               "strict": {k_: strict[k_] for k_ in strict if k_ != "n"},
                               "indexed": sum(1 for q in ok if q.get("indexed")),
                               "short_answers": sum(1 for q in ok if q["returned"] < k)}
    return out


def _f(x, digits=2) -> str:
    return "--" if x is None else f"{x:.{digits}f}"


def table(result: dict) -> str:
    langs = result["settings"]["languages"]
    modes = result["settings"]["modes"]
    summ = result["summary"]
    cols = [*langs, "all"]
    lines = [f"RepoQA SNF retrieval pilot -- Orion `semantic_search` (k={result['settings']['k']}), "
             f"{result['settings']['n_repos']} repos, dev evidence. Each cell: hit@1 / hit@5 / hit@10 / MRR "
             "(n scored, missing). A hit = a returned chunk overlaps the needle function's file and lines.", "",
             "| mode | " + " | ".join(cols) + " |", "|---|" + "---|" * len(cols)]
    for mode in modes:
        cells = []
        for c in cols:
            s = summ[mode][c]
            cells.append(f"{_f(s['hit@1'])} / {_f(s['hit@5'])} / {_f(s['hit@10'])} / {_f(s['mrr'])} "
                         f"(n {s['n']}, missing {s['missing']})")
        lines.append(f"| {mode} | " + " | ".join(cells) + " |")
    lines += ["", "Strict (the chunk must START inside the needle): hit@1 / hit@10 / MRR.", "",
              "| mode | " + " | ".join(cols) + " |", "|---|" + "---|" * len(cols)]
    for mode in modes:
        cells = [f"{_f(summ[mode][c]['strict']['hit@1'])} / {_f(summ[mode][c]['strict']['hit@10'])} / "
                 f"{_f(summ[mode][c]['strict']['mrr'])}" for c in cols]
        lines.append(f"| {mode} | " + " | ".join(cells) + " |")
    first = modes[0]
    lines += ["", "Ceiling: needles some chunk of the index overlaps at all, of those scored: "
              + ", ".join(f"{c} {summ[first][c]['indexed']}/{summ[first][c]['n']}" for c in cols) + ".", "",
              "Index time and size per language (sums over the repos that built; build = Joern + persist, "
              "index = embed.index).", "",
              "| language | repos ok | source files | source MB | lines | chunks | build s | index s |",
              "|---|---|---|---|---|---|---|---|"]
    for lang in langs:
        rs = [r for r in result["repos"] if r["language"] == lang]
        ok = [r for r in rs if r["status"] == "ok"]
        tot = lambda key, sub=None: sum((r[sub][key] if sub else r.get(key, 0)) for r in ok)   # noqa: E731
        lines.append(f"| {lang} | {len(ok)}/{len(rs)} | {tot('files', 'size')} | {tot('bytes', 'size') / 1e6:.1f} | "
                     f"{tot('lines', 'size')} | {tot('chunks')} | {tot('build_s'):.0f} | {tot('index_s'):.0f} |")
    lines += ["", "Per repo: hit@10 out of 10 per mode (`--` = failed).", "",
              "| repo | language | files | KB | chunks | build s | index s | DB chunks | "
              + " | ".join(modes) + " |", "|---|---|---|---|---|---|---|---|" + "---|" * len(modes)]
    for r in result["repos"]:
        if r["status"] != "ok":
            lines.append(f"| {r['repo']} | {r['language']} | | | | | | | " + " | ".join("--" for _ in modes) + " |")
            continue
        per = []
        for mode in modes:
            qs = [q for q in r["queries"] if q["mode"] == mode and q["status"] == "ok"]
            per.append(f"{sum(1 for q in qs if q['rank'] is not None)}/{len(qs)}")
        lines.append(f"| {r['repo']} | {r['language']} | {r['size']['files']} | {r['size']['bytes'] // 1000} | "
                     f"{r['chunks']} | {r['build_s']:.0f} | {r['index_s']:.0f} | {r['db_chunks_total']} | "
                     + " | ".join(per) + " |")
    failed = [r for r in result["repos"] if r["status"] != "ok"]
    if failed:
        lines += ["", "Failed: " + "; ".join(f"{r['repo']}: {r.get('error')}" for r in failed)]
    redone = [r for r in result["repos"] if r.get("previous_attempt")]
    if redone:
        lines += ["", "Re-run (the replaced attempt): " + "; ".join(
            f"{r['repo']} (was {r['previous_attempt']['status']}"
            + (f": {r['previous_attempt']['error'][:90]}" if r["previous_attempt"].get("error") else "")
            + (f", {r['previous_attempt']['bad_keys']} unusable keys" if r["previous_attempt"].get("bad_keys") else "")
            + ")" for r in redone)]
    devices: dict = {}
    for r in result["repos"]:
        devices.setdefault(r.get("embed_device") or "not recorded", []).append(r["repo"])
    if len(devices) > 1 or "not recorded" not in devices:
        lines += ["", "Embedding device: " + "; ".join(
            f"{d}: {len(rs)} repos" + (f" ({', '.join(rs)})" if len(rs) <= 3 else "") for d, rs in devices.items())]
    short = {m: summ[m]["all"]["short_answers"] for m in modes}
    lines += ["", "Queries answered with fewer than k rows (global-index overfetch crowded out by other scans): "
              + ", ".join(f"{m} {n}" for m, n in short.items()) + "."]
    notes = result["settings"].get("notes") or []   # hand-written facts the records cannot carry
    if notes:
        lines += ["", "Notes:", ""] + [f"- {n}" for n in notes]
    return "\n".join(lines) + "\n"


# ─────────────────────────────── CLI ───────────────────────────────
def _write(result: dict, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, out)
    out.with_suffix(".md").write_text(table(result), encoding="utf-8")


def run(args) -> int:
    import orion
    from orion import config
    languages = tuple(args.languages.split(","))
    modes = tuple(args.modes.split(","))
    tasks = load_tasks(args.data, languages)
    chosen = lambda t: not args.repo or t["repo"] in args.repo   # noqa: E731 -- --repo limits what RUNS
    out = Path(args.out)
    prior, earlier, before = {}, {}, None
    if args.resume and out.exists():
        before = json.loads(out.read_text(encoding="utf-8"))
        earlier = {r["repo"]: r for r in before["repos"]}
        # kept as they are: repos already ok, unless named by --redo; everything else runs again
        prior = {k_: r for k_, r in earlier.items() if r["status"] == "ok" and k_ not in (args.redo or [])}
        # a repo --repo leaves out keeps its earlier record, failed or not, so the file stays whole
        prior.update({t["repo"]: earlier[t["repo"]] for t in tasks if not chosen(t) and t["repo"] in earlier})
    tasks = [t for t in tasks if t["repo"] in prior or chosen(t)]
    result = {"benchmark": "repoqa-snf", "status": "dev evidence (pre-cutoff repos)",
              "data": {"url": DATA_URL, "gz_sha256": DATA_GZ_SHA256, "version": DATA_VERSION},
              "settings": {"k": args.k, "modes": list(modes), "languages": list(languages), "n_repos": len(tasks),
                           "scope": "RepoQA entrypoint_path at the pinned commit", "stream": True,
                           "embed_model": config.EMBED_MODEL, "neo4j": config.NEO4J_URI,
                           "joern_heap_gb": config.JOERN_HEAP_GB or None, "orion": os.path.dirname(orion.__file__),
                           "started": time.strftime("%Y-%m-%dT%H:%M:%S%z")},
              "repos": []}
    if before is not None:   # the first pass's settings stay; each resume is logged with what it re-ran
        result["settings"] = {**before["settings"],
                              "resumes": [*before["settings"].get("resumes", []),
                                          {"at": result["settings"]["started"], "redo": args.redo or [],
                                           "CUDA_VISIBLE_DEVICES": os.environ.get("CUDA_VISIBLE_DEVICES")}]}
    for i, task in enumerate(tasks, 1):
        if task["repo"] in prior:
            result["repos"].append(prior[task["repo"]])
            continue
        print(f"[{i}/{len(tasks)}] {task['language']} {task['repo']} @ {task['commit'][:10]} ({task['entrypoint']})",
              flush=True)
        t0 = time.monotonic()
        rec = run_repo(task, args.repos, modes=modes, k=args.k, keep_scan=args.keep_scans)
        rec["needles"] = len(task["needles"])
        rec["wall_s"] = round(time.monotonic() - t0, 1)
        if task["repo"] in earlier:   # what the replaced attempt said, so a re-run never hides a failure
            old = earlier[task["repo"]]
            rec["previous_attempt"] = {k_: old.get(k_) for k_ in ("status", "error", "bad_keys", "embed_device",
                                                                  "previous_attempt")}
        if rec["status"] != "ok":
            print(f"  FAILED: {rec.get('error')}", flush=True)
        result["repos"].append(rec)
        result["summary"] = summarize(result["repos"], modes, languages, args.k)
        _write(result, out)
    result["summary"] = summarize(result["repos"], modes, languages, args.k)
    result["settings"]["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    _write(result, out)
    print(table(result))
    return 0 if all(r["status"] == "ok" for r in result["repos"]) else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="RepoQA SNF as a token-free benchmark of Orion's semantic_search")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="build + index each repo, query every needle in each mode, delete the scan")
    r.add_argument("--data", default=str(DEFAULT_DATA))
    r.add_argument("--repos", default=str(DEFAULT_REPOS), help="folder of <owner>__<name> clones")
    r.add_argument("--out", default=str(DEFAULT_OUT))
    r.add_argument("--languages", default=",".join(FRONTENDS))
    r.add_argument("--modes", default=",".join(MODES))
    r.add_argument("--k", type=int, default=max(KS))
    r.add_argument("--repo", action="append", help="only this owner/name (repeatable)")
    r.add_argument("--resume", action="store_true", help="keep repos already 'ok' in --out")
    r.add_argument("--redo", action="append", help="with --resume: run this owner/name again even if ok")
    r.add_argument("--keep-scans", dest="keep_scans", action="store_true", help="do not delete the scans built")
    t = sub.add_parser("table", help="print the markdown table of a results file")
    t.add_argument("results")
    args = ap.parse_args(argv)
    if args.cmd == "table":
        print(table(json.loads(Path(args.results).read_text(encoding="utf-8"))), end="")
        return 0
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
