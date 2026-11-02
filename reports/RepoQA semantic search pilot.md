# RepoQA semantic search pilot (2026-10-31, completed 2026-11-01)

**Question.** Should `orion scan --semantic-mode hybrid` replace the default `vector` mode?

**Answer: no.** On all 300 RepoQA needles, `vector` ranks the right function first 60% of the time.
`hybrid` does that 30% of the time and `keyword` 10%. Vector wins in all three languages on every
metric. Repo by repo, hybrid's hit@10 beats vector's in 4 of 30 repos, ties in 5 and loses in 21.

This is dev evidence about Orion's memory layer, not an eval result: RepoQA's repos predate every model
cutoff. No `claude` call was made; the only model involved is the local embedding model.

## What was measured

- **Data.** RepoQA "Search Needle Function" (EvalPlus team, Apache-2.0; attribution in
  `eval/dataset/repoqa/NOTICE.md`). It has 10 repos per language and 10 needle functions per repo.
  Each needle comes with a plain-English description that never names the function. Python, Java and
  TypeScript only: 30 repos, 300 needles.
- **Setup.** Each repo is cloned at RepoQA's pinned commit, rooted at its entrypoint folder, then indexed
  exactly as `orion scan` does (`graph_build.build(stream=True)` + `embed.index`).
- **Query.** The needle's description goes through the MCP tool's own code path
  (`mcp_server.semantic_search_impl`, k = 10), once per mode.
- **Hit.** A returned chunk overlaps the needle's file and lines. In the "strict" variant, the chunk must
  also start inside the needle.
- **Failures.** A failed build, index or query is recorded as missing, never as a miss. None are missing
  in the final results.
- **Machine.** WSL Ubuntu with an NVIDIA T1200 Laptop GPU (4 GB) and Orion's own Neo4j.
- **Code.** 28 repos ran on `orion/` at 20b51b8. web3.py and gctoolkit ran on the out-of-memory fix's
  branch; see the failures table below.

## Results

Each cell is hit@1 / hit@10 / MRR, with 100 needles per language.

| mode | Python | Java | TypeScript | all (n 300) |
|---|---|---|---|---|
| **vector** (default) | **0.59 / 0.80 / 0.66** | **0.59 / 0.87 / 0.67** | **0.61 / 0.82 / 0.68** | **0.60 / 0.83 / 0.67** |
| keyword | 0.01 / 0.34 / 0.10 | 0.14 / 0.37 / 0.20 | 0.15 / 0.42 / 0.24 | 0.10 / 0.38 / 0.18 |
| hybrid | 0.15 / 0.68 / 0.28 | 0.41 / 0.73 / 0.50 | 0.34 / 0.73 / 0.46 | 0.30 / 0.71 / 0.41 |

- hit@5 for all needles: vector 0.77, keyword 0.28, hybrid 0.57.
- Strict hit@1 for all needles: vector 0.45, keyword 0.02, hybrid 0.13.
- Ceiling: 292 of the 300 needles are overlapped by at least one chunk. The other 8 are TypeScript
  needles that no chunk covers.
- Hybrid's hit@10 beats vector's in only 4 repos: web3.py (10 vs 8 of 10), and karate, date-fns and
  three.js (by 1 each).
- Cost, summed per language over all 10 repos of each:

  | | Python | Java | TypeScript |
  |---|---|---|---|
  | source | 10.3 MB | 9.9 MB | 9.5 MB |
  | build | 1,311 s | 437 s | 434 s |
  | embedding index | 10,883 s | 3,656 s | 3,761 s |

  web3.py (6,532 s) and gctoolkit (2,144 s) account for most of the Python and Java index time. Both
  went through the slow out-of-memory fallback.

The full tables, including per repo, strict and size, are in `eval/results/repoqa-pilot.md`. Every number
there and here comes from `eval/results/repoqa-pilot.json`.

## Why hybrid loses

- **The keyword side has little to match.** RepoQA descriptions are English prose, not identifiers.
  `embed.keyword_query` keeps only the first 32 unique words, and 295 of the 300 descriptions are longer
  than that (median 64). So BM25 mostly sees RepoQA's template words (purpose, input, output) and
  stopwords.
- **Fusion pulls good results down.** Reciprocal Rank Fusion (k = 60) weighs the keyword list the same
  as the vector list, so a weak keyword list pushes vector's top hit down: hit@1 drops from 0.60 to
  0.30, while hit@10 drops less (0.83 to 0.71).
- **This does not show hybrid is useless.** An agent's query can contain identifiers (`req.body.preTax`,
  `$where`), which is what hybrid's keyword index was built for. This benchmark only uses English
  descriptions, so it says nothing about that case.

## What failed or needs care

| item | what happened | effect on results |
|---|---|---|
| ethereum/web3.py, microsoft/gctoolkit | First pass: `embed.index` ran out of GPU memory, trying to allocate 48 GiB and 12.9 GiB on the 4 GB card. On 2026-11-01 both were re-run with the out-of-memory fallback, branch `claude/happy-meninsky-ac2c8a`, commit 24497c9. The fallback re-embedded 1 of 197 and 1 of 61 batches one chunk at a time, all on the GPU, with no chunks on the CPU. | Both are now scored. The failed attempt is kept in `previous_attempt`. |
| Code for those 2 repos | `orion` came from that branch's worktree: `main` e755f75 plus the fix, 8 commits behind 20b51b8. On this benchmark's path the only difference is the letter rule in `embed.keyword_query` (ASCII on `main`, Unicode at 20b51b8). None of the 20 descriptions has a non-ASCII character, so the keyword queries are the same under both rules. Build, chunking, vector search and fusion code are the same. | None expected. The difference is recorded in the results' notes. |
| Ciphey/Ciphey, microsoft/mssql-jdbc | The first pass's needle check compared CRLF files byte for byte and wrongly skipped 2 needles in each repo. Fixed (with a test), then both repos were re-run on the same GPU. | None after the re-run. The replaced attempt is kept in `previous_attempt`. |
| Index time | Index time moves with GPU load. mssql-jdbc took 3,080 s in the first pass and 607 s in the re-run, and the fallback re-runs shared the GPU with `bench/prove.py`. | Index times are rough. |
| Shared database | Vector and keyword search fetch the top 200 rows from global indexes, then keep only this scan's rows, so other scans could crowd a query out. At query time the database held up to 10,503 chunks. | None: no query came back with fewer than k rows. |

The out-of-memory cause: a 40-line chunk can hold thousands of tokens. web3.py's bytecode strings
reach about 12k tokens, which are cut to 8,192. The model pads a batch of 16 chunks to its longest one,
so its attention scratch space grows with the square of that length. The fix only acts after an
out-of-memory error, so a run that fits is unchanged. It is written up in `docs/notes/embed-oom.md` on
its branch, which is not merged here.

## Reproduce

Everything below runs in WSL, from the Windows checkout. The data and clones live outside the repo.

```bash
cd /mnt/c/Users/h/Downloads/orion && ~/orion/.venv/bin/python -m eval.repoqa run --resume
```

- Data: `~/bench-data/repoqa/data/repoqa-2024-06-23.json` (gz sha256 in `NOTICE.md`).
- Clones: `~/bench-data/repoqa/repos/<owner>__<name>`, each a depth-1 sparse fetch of the entrypoint.
- `--resume` keeps the repos already ok. `--repo R --redo R` re-runs one repo.
- Without the fix's `embed.py`, web3.py and gctoolkit run out of memory again on a 4 GB GPU.
- `python -m eval.repoqa table eval/results/repoqa-pilot.json` re-renders the tables without running
  anything.
- Each repo's scan (`repoqa-<sha1>`) is deleted after its queries. After the last run, none were left
  in Neo4j.

Files: `eval/repoqa.py`, `eval/tests/test_repoqa.py` (15 offline tests), `eval/dataset/repoqa/NOTICE.md`,
`eval/results/repoqa-pilot.{json,md}`. Nothing is committed.
