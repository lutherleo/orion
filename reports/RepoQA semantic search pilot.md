# RepoQA semantic search pilot (2026-10-31 to 2026-11-02)

**Question.** Should `orion scan --semantic-mode hybrid` replace the default `vector` mode?

**Answer: no.** On all 300 RepoQA needles, `vector` ranks the right function first 60% of the time, `hybrid`
30% and `keyword` 10%. Vector wins in all three languages on every metric. At hit@1, vector finds 97 needles
that hybrid misses, and hybrid finds 8 that vector misses.

- **What kind of evidence this is.** Dev evidence about Orion's memory layer, not an eval result: RepoQA's
  repos predate every model cutoff. No `claude` call was made. The only model involved is the local embedding
  model.
- **What it does not cover.** The queries are English descriptions. Queries that contain identifiers were not
  tested, and those are what hybrid's keyword index was built for.
- **Two Orion bugs turned up.** `embed.index` ran out of memory on long chunks (fixed on a branch, not merged
  here). Vector search returns the same chunk more than once, mostly on Python (not fixed).
- **Status.** All 30 repos scored, 0 needles missing, no scans left in Neo4j. Committed on branch `Ifrit`, not pushed.

## Results

Each cell is hit@1 / hit@5 / hit@10 / MRR, with 100 needles per language.

| mode | Python | Java | TypeScript | all (n 300) |
|---|---|---|---|---|
| **vector** (default) | **0.59 / 0.75 / 0.80 / 0.66** | **0.59 / 0.77 / 0.87 / 0.67** | **0.61 / 0.80 / 0.82 / 0.68** | **0.60 / 0.77 / 0.83 / 0.67** |
| keyword | 0.01 / 0.19 / 0.34 / 0.10 | 0.14 / 0.29 / 0.37 / 0.20 | 0.15 / 0.35 / 0.42 / 0.24 | 0.10 / 0.28 / 0.38 / 0.18 |
| hybrid | 0.15 / 0.45 / 0.68 / 0.28 | 0.41 / 0.62 / 0.73 / 0.50 | 0.34 / 0.63 / 0.73 / 0.46 | 0.30 / 0.57 / 0.71 / 0.41 |

Strict scoring, where the chunk must also start inside the needle. Each cell is hit@1 / hit@10 / MRR.

| mode | Python | Java | TypeScript | all |
|---|---|---|---|---|
| vector | 0.48 / 0.77 / 0.56 | 0.41 / 0.80 / 0.53 | 0.47 / 0.81 / 0.59 | 0.45 / 0.79 / 0.56 |
| keyword | 0.00 / 0.25 / 0.05 | 0.00 / 0.14 / 0.03 | 0.06 / 0.22 / 0.11 | 0.02 / 0.20 / 0.07 |
| hybrid | 0.06 / 0.56 / 0.17 | 0.15 / 0.59 / 0.26 | 0.19 / 0.60 / 0.30 | 0.13 / 0.58 / 0.24 |

Vector against hybrid, needle by needle (300 needles):

| | only vector finds it | only hybrid finds it | both | neither |
|---|---|---|---|---|
| hit@1 | 97 | 8 | 82 | 113 |
| hit@5 | 73 | 11 | 159 | 57 |
| hit@10 | 43 | 8 | 206 | 43 |

- Hybrid ranks the needle lower than vector on 149 needles and higher on 22. The rank is the same on the other
  129, which include the 43 that neither finds in the top 10.
- Per repo, hybrid's hit@10 beats vector's in 4 of 30 repos (web3.py 10 vs 8, and karate, date-fns and three.js
  by 1 each), ties in 5 and loses in 21.
- This is one run with no confidence intervals or p-values. Needles in the same repo are not independent, and
  a difference of 1 needle in a repo means little.

**Ceiling.** 292 of the 300 needles are overlapped by at least one chunk. No mode can score the other 8, all
TypeScript. Why no chunk covers them was not investigated. Seven of the 8 are in test files:

| repo | needle | file | lines |
|---|---|---|---|
| date-fns/date-fns | `execScript` | `src/isDate/test.ts` | 49-64 |
| date-fns/date-fns | `fn` | `src/fp/_lib/convertToFP/test.ts` | 11-16 |
| langchain-ai/langchainjs | `isBinaryPath` | `langchain/src/document_loaders/web/github.ts` | 22-24 |
| cheeriojs/cheerio | `testAppleSelect` | `src/cheerio.spec.ts` | 16-24 |
| cheeriojs/cheerio | `createTestServer` | `src/batteries.spec.ts` | 109-129 |
| cheeriojs/cheerio | `withClass` | `src/api/attributes.spec.ts` | 13-15 |
| openai/openai-node | `mockFetch` | `src/lib/ChatCompletionRunFunctions.test.ts` | 28-81 |
| openai/openai-node | `mockStreamingChatCompletionFetch` | `src/lib/ChatCompletionRunFunctions.test.ts` | 105-133 |

### Per repo

The last three columns are hit@10 out of 10. "DB chunks" is every chunk in Neo4j when that repo was queried.

| repo | language | files | KB | chunks | build s | index s | DB chunks | vector | keyword | hybrid |
|---|---|---|---|---|---|---|---|---|---|---|
| psf/black | python | 25 | 419 | 663 | 28 | 58 | 1394 | 8/10 | 5/10 | 7/10 |
| python-poetry/poetry | python | 183 | 757 | 2694 | 95 | 170 | 3425 | 9/10 | 2/10 | 7/10 |
| locustio/locust | python | 64 | 997 | 4913 | 152 | 1179 | 7221 | 7/10 | 3/10 | 6/10 |
| pyg-team/pytorch_geometric | python | 598 | 3451 | 9632 | 620 | 819 | 10503 | 6/10 | 2/10 | 5/10 |
| openai/openai-python | python | 189 | 887 | 3027 | 54 | 192 | 3898 | 9/10 | 3/10 | 6/10 |
| mlc-ai/mlc-llm | python | 49 | 2028 | 1899 | 228 | 1776 | 3221 | 6/10 | 1/10 | 3/10 |
| reactive-python/reactpy | python | 47 | 223 | 704 | 23 | 45 | 2026 | 10/10 | 5/10 | 7/10 |
| marshmallow-code/marshmallow | python | 13 | 180 | 663 | 20 | 51 | 1985 | 9/10 | 3/10 | 9/10 |
| ethereum/web3.py | python | 149 | 1156 | 3146 | 71 | 6532 | 4468 | 8/10 | 6/10 | 10/10 |
| Ciphey/Ciphey | python | 74 | 191 | 1036 | 21 | 61 | 2358 | 8/10 | 4/10 | 8/10 |
| google/gson | java | 83 | 654 | 999 | 30 | 65 | 2321 | 9/10 | 5/10 | 8/10 |
| square/retrofit | java | 54 | 225 | 348 | 21 | 19 | 1670 | 10/10 | 2/10 | 7/10 |
| karatelabs/karate | java | 206 | 1345 | 3438 | 60 | 188 | 4760 | 7/10 | 4/10 | 8/10 |
| Password4j/password4j | java | 29 | 258 | 439 | 21 | 32 | 1761 | 8/10 | 3/10 | 5/10 |
| GoogleContainerTools/jib | java | 164 | 817 | 1365 | 28 | 74 | 2687 | 10/10 | 4/10 | 8/10 |
| microsoft/gctoolkit | java | 59 | 683 | 968 | 38 | 2144 | 2290 | 9/10 | 3/10 | 8/10 |
| palantir/palantir-java-format | java | 65 | 568 | 989 | 37 | 89 | 2311 | 9/10 | 3/10 | 6/10 |
| microsoft/mssql-jdbc | java | 170 | 3917 | 4784 | 133 | 607 | 6106 | 8/10 | 4/10 | 8/10 |
| google/conscrypt | java | 151 | 1188 | 2608 | 46 | 389 | 3930 | 7/10 | 3/10 | 6/10 |
| apache/flink-ml | java | 48 | 284 | 414 | 21 | 49 | 1736 | 10/10 | 6/10 | 9/10 |
| xenova/transformers.js | typescript | 16 | 855 | 1324 | 43 | 1132 | 2646 | 9/10 | 4/10 | 9/10 |
| expressjs/express | typescript | 11 | 91 | 166 | 16 | 9 | 1488 | 10/10 | 7/10 | 9/10 |
| axios/axios | typescript | 56 | 124 | 359 | 19 | 20 | 1681 | 9/10 | 7/10 | 9/10 |
| date-fns/date-fns | typescript | 1342 | 2447 | 2240 | 69 | 484 | 3562 | 5/10 | 6/10 | 6/10 |
| langchain-ai/langchainjs | typescript | 742 | 2165 | 2861 | 63 | 191 | 4183 | 7/10 | 2/10 | 4/10 |
| cheeriojs/cheerio | typescript | 33 | 391 | 264 | 16 | 20 | 1586 | 7/10 | 3/10 | 6/10 |
| openai/openai-node | typescript | 82 | 502 | 875 | 23 | 88 | 2197 | 7/10 | 3/10 | 6/10 |
| umami-software/umami | typescript | 418 | 596 | 1899 | 46 | 101 | 3221 | 10/10 | 4/10 | 7/10 |
| mrdoob/three.js | typescript | 374 | 1354 | 2641 | 99 | 1577 | 3963 | 8/10 | 2/10 | 9/10 |
| firebase/firebase-admin-node | typescript | 106 | 1014 | 1673 | 40 | 139 | 2995 | 10/10 | 4/10 | 8/10 |

### Cost

| | Python | Java | TypeScript |
|---|---|---|---|
| source files | 1,391 | 1,029 | 3,180 |
| source size | 10.3 MB | 9.9 MB | 9.5 MB |
| lines | 263,001 | 251,436 | 327,196 |
| chunks | 28,377 | 16,352 | 14,302 |
| build, all 10 repos | 1,311 s | 437 s | 434 s |
| embedding index, all 10 repos | 10,883 s | 3,656 s | 3,761 s |

- web3.py (6,532 s) and gctoolkit (2,144 s) make up about 60% of the Python and Java index time. Both went
  through the slow out-of-memory fallback.
- The final records add up to 20,640 s (5.7 hours) of wall time per repo. Failed and replaced attempts are not
  in that total.
- Query time is close enough that it does not decide anything:

| mode | median | 95th percentile | slowest |
|---|---|---|---|
| vector | 0.057 s | 0.218 s | 1.356 s |
| keyword | 0.020 s | 0.042 s | 0.097 s |
| hybrid | 0.061 s | 0.082 s | 1.673 s |

## Why hybrid loses

This is my reading of the results and the code. None of these causes was tested on its own.

- **The keyword side has little to match.** RepoQA descriptions are English prose, not identifiers.
  `embed.keyword_query` keeps only the first 32 unique words, and 295 of the 300 descriptions are longer than
  that (median 64). So BM25 mostly sees RepoQA's template words (purpose, input, output) and stopwords.
- **Fusion pulls good results down.** Reciprocal Rank Fusion (k = 60) weighs the keyword list the same as the
  vector list, so a weak keyword list pushes vector's top hit down. Hit@1 drops from 0.60 to 0.30, while hit@10
  drops less (0.83 to 0.71).
- **Repeated chunks may hurt further on Python.** See the next section.

**This does not show that hybrid is useless.** An agent's query can contain identifiers (`req.body.preTax`,
`$where`), and this benchmark says nothing about that case.

## Bug: vector search returns the same chunk more than once

I found this while writing this report. It does not change the answer, but it does affect the default mode.

What the recorded top-10 lists show:

| mode | Python | Java | TypeScript |
|---|---|---|---|
| vector: different spans in the top 10 (mean) | 6.86 | 10 | 9.91 |
| vector: queries with a repeat | 88 of 100 | 0 of 100 | 8 of 100 |
| keyword and hybrid: queries with a repeat | 0 of 100 | 0 of 100 | 0 of 100 |

All 323 repeated rows sit right next to their twin.

**Cause, from reading the code.** `embed._build_chunks` writes one chunk per `CpgMethod` and does not dedupe,
so two methods that start on the same line get the same span and text, and therefore the same vector. A vector
index returns each node once, so the repeats must be separate `Chunk` nodes. Which Python constructs produce
those methods was not checked, because the scans are deleted.

**Effects:**

- **The default mode loses result slots.** A search for 10 chunks on Python returns about 7 different ones.
- **Vector's scores above are slightly low.** Removing the repeats from the recorded lists gives Python hit@5
  0.77 (was 0.75) and MRR 0.669 (was 0.658), and all needles hit@5 0.78 (was 0.773) and MRR 0.670 (was 0.667).
  Hit@1 cannot change. Hit@10 cannot rise in this recount because no further rows were recorded, though a real
  fix would pull in new rows.
- **Fusion counts a repeat twice.** `fuse` shows a repeated span once, but `rrf` adds its score once per
  appearance. In a direct call, a chunk that appeared twice in the vector list alone (0.0325) outranked a chunk
  that both lists found (0.0323).
- **That may hurt hybrid and keyword on Python.** They have their lowest hit@1 there (0.15 and 0.01). Whether
  the double count is the reason was not measured.

**Why the answer stands.** Java's vector top-10 lists have no repeats and TypeScript's have few, and hybrid
still loses clearly in both (hit@1 0.41 vs 0.59 and 0.34 vs 0.61). The rows below the top 10 were not recorded,
so this does not rule out repeats deeper in the lists that fusion reads. A fix could narrow Python's gap, but it
would also help vector.

**Not fixed.** The default `vector` path is deliberately kept as the original code, byte for byte, so changing
it needs a decision. Measuring a fix means re-running the benchmark, because the scans are deleted.

## Bug: `embed.index` ran out of memory (fixed on a branch)

**What failed.** In the first pass, 2 of the 30 repos crashed inside `model.encode(texts, batch_size=16, ...)`:

| repo | commit | folder | tried to allocate |
|---|---|---|---|
| ethereum/web3.py | 257f4648 | `web3` | 48.00 GiB |
| microsoft/gctoolkit | 1476fc12 | `parser/src/main/java` | 12.90 GiB |

**Why.**

- The model reads up to 8,192 tokens per chunk and cuts anything longer.
- sentence-transformers sorts chunks longest first and pads each batch of 16 to its longest member.
- The model's own code builds a full attention bias of shape [batch, 12 heads, L, L] in fp32, so one batch costs
  at least 16 x 12 x L² x 4 bytes. That is 48.00 GiB at L = 8,192, and 12.90 GiB at L ≈ 4,247. Both match the
  errors exactly.
- web3.py stores compiled contract bytecode as hex strings in `web3/_utils/contract_sources/contract_data/*.py`.
  The first 40 lines of `offchain_resolver.py` are 12,355 tokens.
- gctoolkit has files of regex constants (`CMSPatterns.java`, `G1GCPatterns.java`). Interfaces with no methods
  fall back to 60-line blocks, which reach about 4.2k tokens.
- The driver on this machine spills past the 4 GB card into shared system memory, so the real ceiling is about
  32 GiB. Batches below that fit, slowly.

**What one long chunk costs** (the 12,355-token chunk, cut to 8,192, batch of 1):

| device | time | peak memory |
|---|---|---|
| GPU, spilling into system memory | 78.0 s | 12.72 GiB |
| CPU | 75.5 s | 14.10 GiB RSS |

The GPU and CPU vectors agree (cosine 1.0, largest difference 1.3e-5). A full batch of 16 at 8,192 tokens cannot
run anywhere on this machine, but one chunk alone fits.

**The fix** (approved by Love Kush 2026-11-01): commit 24497c9 on branch `claude/happy-meninsky-ac2c8a`, made in
a separate session. It is not pushed and not merged here. The change is in `orion/embed.py`, plus a small
call-site change in `orion/cli.py`.

1. `index` still makes the one plain `model.encode(texts, batch_size=16, ...)` call, so a run that fits is
   byte-identical to before.
2. Only if that call raises an allocation error does it redo the work in longest-first batches of 16, each tried
   whole.
3. A batch that runs out of memory is redone one chunk at a time.
4. A chunk that runs out alone on the GPU goes to the CPU, then the model moves back.
5. A chunk that fails even alone on the CPU raises, as before. Any other error raises unchanged.

The chunks and the 8,192-token cut stay the same, so the vectors match what a bigger machine computes, up to float
rounding. The fallback reports itself through `index(on_warn=...)` and `orion scan` logs it as a warning.

**Tests.** `tests/test_embed_oom.py` uses a fake model whose memory is a budget. I ran it from the fix's worktree
on 2026-11-02: 14 passed, 1 skipped (the skip needs `torch`, which the Windows venv does not have).

**The re-run on 2026-11-01:**

| repo | plain call | fallback | build | index | chunks | hit@1 of 10: vector / keyword / hybrid | hit@10 of 10: vector / keyword / hybrid |
|---|---|---|---|---|---|---|---|
| web3.py | out of memory at 48 GiB | 1 of 197 batches one chunk at a time, 0 on the CPU | 71.3 s | 6,531.9 s | 3,146 | 5 / 0 / 3 | 8 / 6 / 10 |
| gctoolkit | out of memory at 12.90 GiB | 1 of 61 batches one chunk at a time, 0 on the CPU | 38.3 s | 2,144.4 s | 968 | 7 / 0 / 4 | 9 / 3 / 8 |

web3.py took 1 h 49 min, far above the 15 minutes estimated from one chunk alone. The real graph has 3,146 chunks
rather than the 1,751 estimated, most batches spill past the 4 GB card, and a `bench/prove.py` run was using the
same GPU throughout. The time was not split by cause.

**Not built:** an opt-in `ORION_EMBED_MAX_TOKENS` cap. It would be much faster, but it changes the vector of every
long chunk, so it needs a measurement first.

## Method

- **Data.** RepoQA "Search Needle Function" (EvalPlus team, Apache-2.0; attribution in
  `eval/dataset/repoqa/NOTICE.md`), release `2024-06-23`, sha256 `c050a2ad...d681`. It has 10 repos per language
  and 10 needle functions per repo, each with a plain-English description that never names the function. Python,
  Java and TypeScript only: 30 repos, 300 needles.
- **Clones.** Each repo is fetched at RepoQA's pinned commit (depth 1, sparse to its entrypoint folder) and rooted
  at that folder, with Orion's file paths prefixed before matching. Downloads stay outside the repo.
- **Setup.** Each repo is indexed the way `orion scan` does it: `graph_build.build(stream=True)` + `embed.index`
  with `jinaai/jina-embeddings-v2-base-code`.
- **Query.** The needle's description goes through the MCP tool's own code path
  (`mcp_server.semantic_search_impl`, k = 10), once per mode, switched with `config.set_semantic_mode`.
- **Hit.** A returned chunk overlaps the needle's file and lines. RepoQA's lines are 0-based with an exclusive end,
  and are converted first. Ranks count rows as the tool returned them, repeats included. Metrics are hit@1/5/10 and
  MRR@10.
- **Failures.** A failed build, index or query is recorded as missing and left out of every rate, never counted as
  a miss. A needle whose code is not on its stated lines in the clone is set aside as an unusable key. There are
  none of either in the final results.
- **Isolation.** Each repo's scan is called `repoqa-<sha1>` and deleted after its queries, by scan id, across every
  scan label. The benchmark chain in `~/orion-bench` was not touched. After the last run, no `repoqa-` scan or node
  was left in Neo4j.
- **Machine.** WSL Ubuntu with an NVIDIA T1200 Laptop GPU (4 GB), 39 GB RAM in the VM, Orion's own Neo4j on
  port 7688, Joern heap 12 GB.
- **Code.** 28 repos ran from this checkout. web3.py and gctoolkit ran with `orion` imported from the fix's
  worktree; see the table below.

## Timeline

| when | what |
|---|---|
| 2026-10-31 18:19 -0400 | First pass starts |
| 2026-10-31 22:20 -0400 | First pass log ends. web3.py and gctoolkit failed out of memory; Ciphey and mssql-jdbc each lost 2 needles to the CRLF bug |
| 2026-10-31 22:21 -0400 | Ciphey and mssql-jdbc re-run after the CRLF fix, giving the 280-needle interim result |
| 2026-11-01 17:59 -0500 | web3.py and gctoolkit re-run with the out-of-memory fallback |
| 2026-11-01 20:26 -0500 | Re-run ends: 30 of 30 repos ok, 0 needles missing |
| 2026-11-01 20:27 -0500 | Fix committed as 24497c9 |
| 2026-11-02 17:42 -0500 | Benchmark and the earlier report committed as a93054b on `Ifrit` |
| 2026-11-02 | This full report. Repeated chunks found; code-version note and overfetch figure corrected |

The interim result on 280 needles was vector 0.60 / 0.83 / 0.67, keyword 0.11 / 0.37 / 0.18 and hybrid
0.30 / 0.70 / 0.41 (hit@1 / hit@10 / MRR). The last 20 needles changed these by at most 0.01.

## What failed or needs care

| item | what happened | effect on results |
|---|---|---|
| web3.py, gctoolkit | Out of memory in the first pass, re-run with the fallback (above). | Both are scored. The failed attempt is kept in `previous_attempt`. |
| Code for those 2 repos | `orion` came from the fix's worktree: `main` e755f75 plus the fix. `main` is 8 commits behind 3e46ac2 and 7 behind 20b51b8; its own 2 extra commits are merges that change nothing in `orion/`. On this benchmark's path the only difference is the letter rule in `embed.keyword_query` (ASCII on `main`, Unicode at 20b51b8). None of the 20 descriptions has a non-ASCII character, so their keyword queries are the same under both rules. `mcp_server.py`, `config.py`, `graph_build.py`, `stream_build.py` and `persist.py` are identical, and `embed.py` differs only by the letter rule and the fix. Under `orion/graph/`, `deps.py`, `shortlists.py` and `scans.py` do differ; they write or delete dependency, shortlist and scan data, not chunks or search. | None expected. Recorded in the results' notes. |
| Which commit the other 28 ran on | The harness records a path (`settings.orion`), not a commit. By commit times, HEAD was 3e46ac2, whose `orion/` is 20b51b8's. Uncommitted edits at the time cannot be ruled out. The only later commit to touch `orion/` (d472645) changes `cli.py`, `monitor.py` and `ui/keys.py`, none on this path. | None expected. |
| Ciphey, mssql-jdbc | The first pass's needle check compared CRLF files byte for byte and wrongly skipped 2 needles in each. Fixed with a test, then both were re-run on the same GPU. | None after the re-run. The replaced attempt is kept in `previous_attempt`. |
| Index time | It moves with GPU load: mssql-jdbc took 3,080 s in the first pass and 607 s in the re-run, and the fallback re-runs shared the GPU with `bench/prove.py`. | Index times are rough. |
| Shared database | Searches fetch top rows from global indexes, then keep only this scan's rows (at k = 10: 200 in vector mode, 800 in keyword and hybrid), so other scans could crowd a query out. The database held up to 10,503 chunks at query time. | None: no query came back with fewer than 10 rows. |
| Embedding device | 26 repos have no `embed_device` in their records, because the field was added later. The GPU was seen in `nvidia-smi` for that run's process. The other 4 say `cuda:0`. | None expected. |

## Where everything is

All of these are committed on branch `Ifrit`, which has no upstream, so it is not pushed.

| file | what it is |
|---|---|
| `eval/repoqa.py` | The benchmark (`run`, `table`) |
| `eval/tests/test_repoqa.py` | 15 offline tests, passing on 2026-11-02 |
| `eval/dataset/repoqa/NOTICE.md` | Source, license, file hash |
| `eval/results/repoqa-pilot.json` | Every number, down to each query's rank and top 10 |
| `eval/results/repoqa-pilot.md` | Tables and notes rendered from the JSON |
| `reports/RepoQA semantic search pilot.md` | This report |

On branch `claude/happy-meninsky-ac2c8a` (commit 24497c9): the fix, `tests/test_embed_oom.py`,
`docs/notes/embed-oom.md` and the measuring scripts in `docs/notes/embed-oom/`.

Outside the repo, in WSL: `~/bench-data/repoqa/data/` (the dataset), `~/bench-data/repoqa/repos/` (the clones),
`~/bench-data/repoqa/run.log` and `rerun-oom-fix.log`.

Most numbers here come straight from `eval/results/repoqa-pilot.json`. The needle-by-needle counts, query times,
repeat counts and repeat-free recount are computed from its per-query rows. The out-of-memory measurements come
from `docs/notes/embed-oom.md` and the run logs.

## Reproduce

Everything below runs in WSL, from the Windows checkout.

```bash
cd /mnt/c/Users/h/Downloads/orion && ~/orion/.venv/bin/python -m eval.repoqa run --resume
```

- `--resume` keeps the repos already ok. `--repo R --redo R` re-runs one repo.
- Without the fix's `embed.py`, web3.py and gctoolkit run out of memory again on a 4 GB GPU. The runner used for
  them is `docs/notes/embed-oom/rerun.py` on the fix's branch.

To re-render the tables without running anything:

```bash
python -m eval.repoqa table eval/results/repoqa-pilot.json
```

## Open items

1. **Merge the out-of-memory fix.** Until then, this checkout crashes on repos with very long chunks.
2. **Fix the repeated chunks and re-measure.** Dedupe chunks by span, and make `rrf` count a span once. This changes
   default-mode output, so it needs a decision, and it needs the fix from item 1 to re-run all 30 repos.
3. **Test identifier-style queries.** This is the one case where hybrid could still win.
4. **Find out why 8 TypeScript needles have no chunk.** Seven of them are in test files.
5. **Consider the token cap** (`ORION_EMBED_MAX_TOKENS`) if slow indexing of long chunks becomes a problem. It needs a
   RepoQA measurement before it could become a default.
6. **Record the commit in the harness.** `settings.orion` is only a path.
7. **`tests/test_embed_spans.py`.** The fix's session reported on 2026-11-01 that its two Neo4j tests fail instead of
   skipping when `fixtures/NodeGoat` is missing, as in a fresh worktree. That predates the fix. I did not re-check it.
