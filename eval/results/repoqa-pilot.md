RepoQA SNF retrieval pilot -- Orion `semantic_search` (k=10), 30 repos, dev evidence. Each cell: hit@1 / hit@5 / hit@10 / MRR (n scored, missing). A hit = a returned chunk overlaps the needle function's file and lines.

| mode | python | java | typescript | all |
|---|---|---|---|---|
| vector | 0.59 / 0.75 / 0.80 / 0.66 (n 100, missing 0) | 0.59 / 0.77 / 0.87 / 0.67 (n 100, missing 0) | 0.61 / 0.80 / 0.82 / 0.68 (n 100, missing 0) | 0.60 / 0.77 / 0.83 / 0.67 (n 300, missing 0) |
| keyword | 0.01 / 0.19 / 0.34 / 0.10 (n 100, missing 0) | 0.14 / 0.29 / 0.37 / 0.20 (n 100, missing 0) | 0.15 / 0.35 / 0.42 / 0.24 (n 100, missing 0) | 0.10 / 0.28 / 0.38 / 0.18 (n 300, missing 0) |
| hybrid | 0.15 / 0.45 / 0.68 / 0.28 (n 100, missing 0) | 0.41 / 0.62 / 0.73 / 0.50 (n 100, missing 0) | 0.34 / 0.63 / 0.73 / 0.46 (n 100, missing 0) | 0.30 / 0.57 / 0.71 / 0.41 (n 300, missing 0) |

Strict (the chunk must START inside the needle): hit@1 / hit@10 / MRR.

| mode | python | java | typescript | all |
|---|---|---|---|---|
| vector | 0.48 / 0.77 / 0.56 | 0.41 / 0.80 / 0.53 | 0.47 / 0.81 / 0.59 | 0.45 / 0.79 / 0.56 |
| keyword | 0.00 / 0.25 / 0.05 | 0.00 / 0.14 / 0.03 | 0.06 / 0.22 / 0.11 | 0.02 / 0.20 / 0.07 |
| hybrid | 0.06 / 0.56 / 0.17 | 0.15 / 0.59 / 0.26 | 0.19 / 0.60 / 0.30 | 0.13 / 0.58 / 0.24 |

Ceiling: needles some chunk of the index overlaps at all, of those scored: python 100/100, java 100/100, typescript 92/100, all 292/300.

Index time and size per language (sums over the repos that built; build = Joern + persist, index = embed.index).

| language | repos ok | source files | source MB | lines | chunks | build s | index s |
|---|---|---|---|---|---|---|---|
| python | 10/10 | 1391 | 10.3 | 263001 | 28377 | 1311 | 10883 |
| java | 10/10 | 1029 | 9.9 | 251436 | 16352 | 437 | 3656 |
| typescript | 10/10 | 3180 | 9.5 | 327196 | 14302 | 434 | 3761 |

Per repo: hit@10 out of 10 per mode (`--` = failed).

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

Re-run (the replaced attempt): ethereum/web3.py (was failed: OutOfMemoryError: CUDA out of memory. Tried to allocate 48.00 GiB. GPU 0 has a total capac); Ciphey/Ciphey (was ok, 2 unusable keys); microsoft/gctoolkit (was failed: OutOfMemoryError: CUDA out of memory. Tried to allocate 12.90 GiB. GPU 0 has a total capac); microsoft/mssql-jdbc (was ok, 2 unusable keys)

Embedding device: not recorded: 26 repos; cuda:0: 4 repos

Queries answered with fewer than k rows (global-index overfetch crowded out by other scans): vector 0, keyword 0, hybrid 0.

Notes:

- First pass (the 26 repos without `embed_device`): the embedding ran on the GPU, an NVIDIA T1200 Laptop GPU (4 GB), seen in nvidia-smi for the run's process; not recorded per repo by the harness.
- Ciphey/Ciphey and microsoft/mssql-jdbc were re-run (first `resumes` entry): the first pass's needle check compared CRLF files byte for byte and skipped 2 needles in each (fixed, then re-run on the same GPU; `previous_attempt` keeps it).
- ethereum/web3.py and microsoft/gctoolkit: in the first pass `embed.index` ran out of GPU memory (tried 48 GiB / 12.9 GiB on the 4 GB card; `previous_attempt` keeps it). Re-run 2026-11-01 (second `resumes` entry) with the out-of-memory fallback `embed._encode` from branch claude/happy-meninsky-ac2c8a, commit 24497c9: `orion` was imported from that worktree, not from this checkout (its embed.py was last changed before the run started and is the committed version). The plain encode ran out of memory again; the fallback re-embedded 1 of 197 (web3.py) and 1 of 61 (gctoolkit) batches one chunk at a time, all on the GPU, 0 chunks on the CPU.
- That worktree's orion is main e755f75 + the fix, 8 commits behind the orion the other 28 repos ran on (20b51b8). On this benchmark's path the only difference is `embed.keyword_query` (ASCII letters on main, Unicode letters at 20b51b8); none of the 20 re-run descriptions has a non-ASCII character, so their keyword queries are the same under both. The build, chunking, vector search and fusion code is the same; the other differing files (scans, deps, shortlists, runtime, ui, agent plumbing) do not touch chunks or search, and the re-run's scans were deleted (no repoqa- scan left in Neo4j afterwards).
- Index times move with GPU load: mssql-jdbc indexed in 3080 s in the first pass and 607 s in the re-run; web3.py (6532 s) and gctoolkit (2144 s) include the one-chunk-at-a-time fallback, with bench/prove.py sharing the GPU.
- Keyword mode: 295 of the 300 descriptions have more than the 32 unique terms `embed.keyword_query` keeps (median 64), so BM25 sees the first half of each description, which opens with RepoQA's template words (purpose, input, output) and stopwords.
- Other scans in the database at query time: up to 10,503 chunks in all (pytorch_geometric's own 9,632 included); no query came back with fewer than k rows, so the global-index overfetch did not bite here.
