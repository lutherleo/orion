# Embedding out-of-memory fix (2026-11-01, checks added 2026-11-02)

**Result.** `embed.index` no longer crashes on repos with very long chunks. ethereum/web3.py and
microsoft/gctoolkit, the two repos that lost their semantic index in the RepoQA pilot, now index and score. All
30 repos score, with 0 of 300 needles missing.

The fix is commit 24497c9 on branch `claude/happy-meninsky-ac2c8a`. When this was written it was **not merged into
`main` or `Kullervo`**.

What this means in practice:

| | Before | After |
|---|---|---|
| A repo that fits in memory | Works | **Byte-identical**: the same single `model.encode` call runs first |
| `embed.index` on a repo that doesn't fit | Raises out-of-memory | Finishes, using smaller batches |
| `orion scan` on such a repo | Warns "semantic index failed, continuing graph-only" | Gets a semantic index, so its output changes. Love Kush approved this on 2026-11-01 |
| Time on such a repo, on this box | Fails in about a minute | **web3.py 1 h 49 min, gctoolkit 36 min**. No time limit or off switch |

How much of that time the fallback itself costs is unknown; see [Why it was slow](#why-it-was-slow).

## What failed

The pilot (`eval/repoqa.py`) lost 2 of 30 repos inside `model.encode(texts, batch_size=16, ...)`. Their 20
needles were recorded as missing, not as misses.

| Repo | Commit | Folder | Error |
|---|---|---|---|
| ethereum/web3.py | 257f4648 | `web3` | CUDA out of memory, tried to allocate 48.00 GiB |
| microsoft/gctoolkit | 1476fc12 | `parser/src/main/java` | CUDA out of memory, tried to allocate 12.90 GiB |

Box: WSL Ubuntu on the Windows dev laptop, NVIDIA T1200 (4 GB), 39 GB RAM in the VM. Model:
`jinaai/jina-embeddings-v2-base-code`, with sentence-transformers 6.1.0, torch 2.14.0+cu130 and transformers 4.57.6.

## Why

- The model reads up to **8,192 tokens** per chunk and cuts anything longer.
- sentence-transformers sorts chunks longest first and pads each batch of 16 to its longest member.
- The model's own code (`modeling_bert.py`, `new_bias = attention_mask + bias`) builds a full attention table of
  shape [batch, 12 heads, L, L] in 4-byte floats, in every layer. So one batch needs at least
  16 x 12 x L² x 4 bytes, and memory grows with the square of the longest chunk.

The allocator's log gives the exact byte counts, and the formula explains both. The allocator rounds up to
2 MiB, and only one L fits each number:

| Repo | Failed allocation | L | Longest chunk |
|---|---|---|---|
| web3.py | 51,539,607,552 B (48.00 GiB) | 8,192 (the cap) | `offchain_resolver.py` lines 1-40: 12,355 tokens, cut to 8,192 |
| gctoolkit | 13,847,494,656 B (12.90 GiB) | 4,246 | `CMSPatterns.java` lines 61-120: exactly 4,246 tokens |

Why those chunks are so long:

- **web3.py:** `web3/_utils/contract_sources/contract_data/*.py` store compiled contract bytecode as hex strings.
  One line holds up to 17,212 characters (7,160 tokens). About 10 chunks exceed 4,096 tokens and 15 exceed 2,048.
  This count is an estimate, because method lines came from Python's `ast`, not the real graph.
- **gctoolkit:** files of regex constants (`CMSPatterns.java`, `G1GCPatterns.java`, ...) have no methods, so they
  fall back to 60-line blocks. Cutting every file in the folder into such blocks gives 230: 15 exceed 2,048 tokens,
  and only one exceeds 4,000 (the next longest is 3,997).

The driver on this box spills GPU allocations past the 4 GB card into shared system memory. The gctoolkit error
reports 27.98 GiB allocated by PyTorch on a 4 GB card. So the real ceiling here is about 32 GiB, and batches below
it "fit", slowly.

## What long chunks cost

One 12,355-token chunk (cut to 8,192), alone (`measure.py`):

| Device | Time | Peak memory |
|---|---|---|
| GPU, spilling | 78.0 s | 12.72 GiB |
| CPU | 75.5 s | 14.10 GiB RSS |

That is about 203 bytes x batch size x L². A full batch of 16 at 8,192 tokens would need about 203 GiB, which
doesn't fit on this box's GPU or CPU, or on a 16 GB Mac. One chunk alone fits.

web3.py batch by batch, estimated (`batches.py`, on approximate chunks: 1,751 chunks against the real 3,146):

| Batch | Padded to | Est. memory | Time, relative to one 8,192-token chunk |
|---|---|---|---|
| 0 (the 16 longest) | 8,192 | 203 GiB | 6.75 x, one chunk at a time |
| 1 | 1,426 | 6.2 GiB | 0.48 x |
| 2-109 | about 530-770 | about 1-1.8 GiB each | about 0.07-0.14 x each |

Only batch 0 is over the ceiling, which matches the real run: exactly one batch was split.

## The decision

| Option | Effect | Status |
|---|---|---|
| Retry only after out-of-memory | Runs that fit don't change. Same chunks and same 8,192-token cut | **Built**, approved by Love Kush 2026-11-01 |
| Token cap (`ORION_EMBED_MAX_TOKENS`) | Cuts long chunks shorter, which changes their vectors | Not built. Opt-in only, and would need a RepoQA measurement first |

## The fix

`orion/embed.py`:

1. `index` still makes the one plain call, `model.encode(texts, batch_size=16, show_progress_bar=False,
   convert_to_numpy=True)`, through `_encode`. The arguments are the same as before.
2. Only an allocation error goes further (`_is_oom`: "out of memory" from CUDA or MPS, the CPU's "can't allocate
   memory" or "not enough memory", or `MemoryError`). Any other error raises unchanged.
3. `_encode_split` then works through longest-first batches of 16:
   - each batch is tried whole;
   - a batch that fails is redone one chunk at a time;
   - a chunk that fails alone on the GPU or MPS goes to the CPU, and the model moves back afterwards;
   - a chunk that fails alone on the CPU raises, because there is nowhere left to go.
4. The fallback reports itself once. `index(on_warn=...)` receives the message, which `orion scan` logs as a
   `build`/`warn` event (`orion/cli.py`). Without a listener it goes to stderr as `orion embed: ...`.

Little work is thrown away: the plain call and the first whole-batch retry took about a minute or less in both
repos (allocator failures at 18:01:22-18:01:50 and 19:50:49-19:51:35).

## Evidence

### Tests

`tests/test_embed_oom.py` needs no tokens, GPU or Neo4j. A fake model has a memory "budget" on len(batch) x
longest². It pins:

- the single unchanged call when everything fits;
- the split and CPU paths;
- input order kept;
- the model returned to its device;
- the stderr message;
- other errors raising unchanged;
- a chunk too big for the CPU raising;
- a model already on the CPU;
- the out-of-memory classifier;
- `index()` writing every chunk after an out-of-memory.

| Run | Result |
|---|---|
| `tests/test_embed_oom.py`, WSL | 15 passed |
| `tests/test_embed_oom.py`, Windows (the real `torch` error test skips) | 14 passed, 1 skipped |
| Token-free suite, Windows, 2026-11-01 | 1534 passed, 43 skipped, 62 deselected |

### The re-run

`python -m eval.repoqa run --resume --repo ethereum/web3.py --repo microsoft/gctoolkit` ran in WSL from 17:59:47 to
20:26:25 on 2026-11-01 and exited 0. It imported `orion` from the fix's worktree (`main` e755f75 plus the fix),
through `docs/notes/embed-oom/rerun.py`, because `eval/repoqa.py` is still uncommitted in the main checkout. Log:
WSL `~/bench-data/repoqa/rerun-oom-fix.log`.

| | web3.py | gctoolkit |
|---|---|---|
| Plain call | Failed at 48.00 GiB, as before | Failed at 12.90 GiB, as before |
| Batches split | **1 of 197** | **1 of 61** |
| Chunks on the CPU | 0 | 0 |
| Build | 71.3 s | 38.3 s |
| Index | 6,531.9 s (1 h 49 min) | 2,144.4 s (36 min) |
| Chunks | 3,146 | 968 |
| Vector hit@1 / hit@10, of 10 | 5 / 8 | 7 / 9 |
| Keyword hit@1 / hit@10, of 10 | 0 / 6 | 0 / 3 |
| Hybrid hit@1 / hit@10, of 10 | 3 / 10 | 4 / 8 |

The whole pilot, over 300 needles (hit@1 / hit@5 / hit@10 / MRR):

| Mode | Result |
|---|---|
| vector | 0.60 / 0.77 / 0.83 / 0.67 |
| keyword | 0.10 / 0.28 / 0.38 / 0.18 |
| hybrid | 0.30 / 0.57 / 0.71 / 0.41 |

- Both repos' vector hit@10 (8 and 9 of 10) is in line with the pilot's 0.83. I did not check whether any needle
  sits in one of the 16 split chunks, so this shows the index works, not that those chunks rank well.
- Each re-run record keeps the old failure under `previous_attempt` in `eval/results/repoqa-pilot.json`.
- The pilot's own conclusions are in `reports/RepoQA semantic search pilot.md` (main checkout, uncommitted). That
  report and its results belong to the RepoQA session.
- The two scans used their own ids (`repoqa-ccd8e581...`, `repoqa-4e52e4b2...`) and were deleted after their
  queries. No `repoqa-` node was left in Neo4j when checked.

### Are the vectors the same?

The re-run couldn't answer this or exercise the CPU path, so `vectors.py` was added on 2026-11-02. It uses the real
model and takes 24 s.

| Check | Result |
|---|---|
| One 8,192-token chunk, GPU vs CPU | Cosine 1.0, largest difference 1.3e-5 |
| 16 chunks of 143-390 tokens, batch of 16 vs alone, GPU | Lowest cosine 0.99999994, largest difference 2.3e-6 |
| The real `_encode`, with an injected out-of-memory for the longest chunk | 1 of 1 batches split, 1 chunk on the CPU, model back on `cuda:0`, largest difference from the plain batch 2.3e-6 |

So the fallback's vectors differ from the plain call's only by float rounding, in everything measured.

## Why it was slow

I predicted "several extra minutes" before building, then about 15 minutes from `batches.py`. The real time was
1 h 49 min for web3.py. Per chunk, web3.py took 2.1 s and gctoolkit 2.2 s, against 0.05-0.94 s in the other 28 repos.

**The time was not split by cause**, because the log has no per-batch times. What is known:

- **More chunks than estimated:** web3.py has 3,146 real chunks, not 1,751.
- **A shared, spilling GPU:** the card read 2,730-3,712 MiB used and 100% busy at the checks during the web3.py
  index. Windows' counter showed about 11.5-13 GB of shared GPU memory, but that counter covers the whole WSL VM.
- **Another session's run:** a `bench/prove.py` started about 24 minutes into the re-run and was still running 1 h
  32 min in. Its scans use the same GPU, but its share wasn't measured.
- **GPU load alone can change index time about 5x:** mssql-jdbc took 3,080 s in the first pass and 607 s in its
  re-run.
- **The split batch looks like a small part, but this is only an estimate:** at 78 s per 8,192-token chunk, scaled
  by L², it comes to about 9 minutes for web3.py and 2.5 minutes for gctoolkit. Contention could stretch that too.

To split it, log each encode call's device, batch size, longest chunk and seconds, on a GPU nobody else is using.

## Limits

- **The CPU path has never hit a real out-of-memory.** It ran on the real model only with an injected error. MPS
  has only run against the fake model.
- **Linux can kill the process instead of raising an error.** If the CPU path runs out of RAM, the kernel's
  out-of-memory killer may act first, so "raises" isn't guaranteed. One 8,192-token chunk takes about 14 GiB, which
  is tight on a 16 GB Mac.
- **This box's spill hides part of the problem.** A card without it would split more batches and send more chunks
  to the CPU. That should still finish, but it is untested.
- **A later failure wastes earlier batches.** Sorting is by characters, not tokens, so the batch that fails may not
  be the first. The fallback then redoes everything.
- **`index_exploits` is unchanged.** The global exploit corpus still uses the plain call, with no fallback.
- **A scan can now take far longer on such repos.** There is no time limit or off switch.

## What's left

1. **Land the fix.** Until `claude/happy-meninsky-ac2c8a` merges, web3.py and gctoolkit run out of memory again on a
   4 GB GPU.
2. **Split the time by cause**, as above. Until then nobody knows how much a token cap would save.
3. **Token cap**, if the slow runs matter. It would need a RepoQA measurement before it could become a default.
4. **`tests/test_embed_spans.py`:** two Neo4j tests fail instead of skipping when `fixtures/NodeGoat` is missing, as
   in a fresh worktree. They stop at `joern-parse`, before writing anything. This predates the fix.

## Corrections to what I said earlier

During the work I said these things in messages or in the earlier note. They were wrong or unsupported:

| Earlier | Actual |
|---|---|
| gctoolkit's batch was padded to about 4,247 tokens | That was worked backward from "12.90 GiB". The exact byte count and the tokenizer both give 4,246 |
| A fifth 12.9 GiB allocator line meant a second batch that long | Wrong. Only one block is that long, and the run's own count is 1 of 61. The odd fifth line was not traced |
| `bench/prove.py` shared the GPU "the whole time" | It started about 24 minutes in, and its GPU share wasn't measured |
| `embed_device: cuda:0` shows the model went back to the GPU | The model never left, since 0 chunks went to the CPU. `vectors.py` now shows the move back |
| The 16 longest chunks spilled 11-13 GB | That counter covers the whole WSL VM and can't be tied to those chunks |
| web3.py's time was "mostly the spilled GPU" | Not established |
| The token cap is "the speed lever" | Not established until the time is split |
| The vectors match "up to float rounding" | That was reasoning at the time. It is now measured, though batch vs alone only on short chunks |

## Timeline (local time, UTC-5)

| When | What |
|---|---|
| 2026-11-01 15:16 | Task received from the RepoQA session |
| 15:25 | Crash reproduced without Neo4j, fix proposed |
| 17:52 | Love Kush: "Build the OOM fallback" |
| 17:59 | Fix and tests written, re-run started |
| 18:01 | web3.py's plain call fails, fallback takes over |
| 19:49 | web3.py index done |
| 19:50 | gctoolkit's plain call fails, fallback takes over |
| 20:26 | Re-run exits 0, all 30 repos ok |
| 20:27 | Commit 24497c9 |
| 2026-11-02 | This report, `vectors.py`, `blocks.py`, corrections |

## Files

| Path | What |
|---|---|
| `orion/embed.py` | `_encode`, `_encode_split`, `_encode_on_cpu`, `_encode_or_none`, `_is_oom`, `_free_device_memory` |
| `orion/cli.py` | Passes the fallback's message to the run log |
| `tests/test_embed_oom.py` | The token-free tests |
| `docs/notes/embed-oom/toklen.py` | Longest 40-line window per file |
| `docs/notes/embed-oom/blocks.py` | Longest 60-line blocks |
| `docs/notes/embed-oom/measure.py` | Cost of one 8,192-token chunk, GPU and CPU |
| `docs/notes/embed-oom/batches.py` | Estimated cost of every batch |
| `docs/notes/embed-oom/vectors.py` | Batch vs alone, and the CPU path with an injected error |
| `docs/notes/embed-oom/rerun.py` | The re-run, with `orion` from the worktree |
| `CLAUDE.md` | The gotcha, under "Embedding out-of-memory fallback" |

## Reproduce

Run these in a WSL login shell, from the worktree. The repos are RepoQA's clones under `~/bench-data/repoqa/repos`.

```bash
~/orion/.venv/bin/python -m pytest tests/test_embed_oom.py -q
```

```bash
~/orion/.venv/bin/python docs/notes/embed-oom/blocks.py ~/bench-data/repoqa/repos/microsoft__gctoolkit/parser/src/main/java .java
```

```bash
~/orion/.venv/bin/python docs/notes/embed-oom/vectors.py
```

`rerun.py` won't repeat the run as it stands: both repos are now `ok` in the results, and `--resume` keeps repos
that are `ok`. To repeat it, add `--redo <repo>` for each repo to its arguments. It has its paths hard-coded to this
box, needs Neo4j and Joern, took 2 h 27 min here, and overwrites those repos' records in the main checkout's
`eval/results/repoqa-pilot.json`.
