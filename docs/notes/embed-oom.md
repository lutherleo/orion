# `embed.index` out of memory on long chunks (2026-11-01)

Box: WSL Ubuntu on the Windows dev laptop, NVIDIA T1200 Laptop GPU (4 GB, WDDM), 39 GB RAM in the VM.
Model: `jinaai/jina-embeddings-v2-base-code` (sentence-transformers 6.1.0, torch 2.14.0+cu130, transformers 4.57.6).

## What failed

The RepoQA retrieval pilot (`eval/repoqa.py`, results `eval/results/repoqa-pilot.json`) lost 2 of 30 repos
because `embed.index` crashed inside `model.encode(texts, batch_size=16, ...)`:

| Repo | Commit | Folder | Error |
|---|---|---|---|
| ethereum/web3.py | 257f4648 | `web3` | CUDA out of memory, tried to allocate 48.00 GiB |
| microsoft/gctoolkit | 1476fc12 | `parser/src/main/java` | CUDA out of memory, tried to allocate 12.90 GiB |

Their 20 needles were recorded as missing, not as misses.

## Why

- The model reads up to **8192 tokens** per chunk (`max_seq_length` 8192). Longer chunks are cut to 8192.
- sentence-transformers sorts the chunks longest first and pads each batch of 16 to its longest member.
- The model's own remote code (`modeling_bert.py`, `new_bias = attention_mask + bias` before
  `scaled_dot_product_attention`) builds a full attention bias of shape [batch, 12 heads, L, L] in fp32 in
  every layer. A float mask rules out the flash kernel.
- So one batch costs at least 16 x 12 x L² x 4 bytes:
  - L = 8192: 16 x 12 x 8192² x 4 B = **48.00 GiB**, exactly the web3.py error.
  - L ≈ 4247: 16 x 12 x 4247² x 4 B = **12.90 GiB**, the gctoolkit error.

Which chunks are that long (token counts with the model's own tokenizer; no Neo4j needed,
`embed-oom/toklen.py`):

- **web3.py:** `web3/_utils/contract_sources/contract_data/*.py` store compiled contract bytecode as hex
  strings. One line holds up to 17,212 characters (7,160 tokens). The first 40 lines of
  `offchain_resolver.py` (its `<module>` window) are 12,355 tokens, and `emitter_contract.py` 11,676.
  Both are cut to 8192, so the first batch of 16 is padded to 8192. About 10 chunks exceed 4096 tokens and
  about 15 exceed 2048. That's an estimate: method lines come from Python's `ast`, not the real graph.
- **gctoolkit:** files of regex constants (`CMSPatterns.java`, `G1GCPatterns.java`, ...). Interfaces with no
  methods fall back to 60-line blocks, and those reach about 4.2k tokens.

The driver on this box spills CUDA allocations past the 4 GB card into shared system memory (WDDM "sysmem
fallback"; the gctoolkit error says 27.98 GiB was allocated by PyTorch on the 4 GB card). So the real
ceiling here is about 32 GiB, and batches below it "fit", slowly.

## What one long chunk costs

`embed-oom/measure.py`, the 12,355-token chunk (cut to 8192), batch of 1:

| Device | Time | Peak memory |
|---|---|---|
| cuda (spills into system memory) | 78.0 s | 12.72 GiB (`torch.cuda.max_memory_allocated`) |
| cpu | 75.5 s | 14.10 GiB RSS |

The GPU and CPU vectors agree: cosine 1.0, largest difference 1.3e-5.

So a full batch of 16 at 8192 tokens can't run anywhere on this box: it needs more than the 39 GB VM even on
the CPU, and more than a 16 GB Mac under MPS. One chunk alone fits.

The whole repo, batch by batch (`embed-oom/batches.py`: the same approximate chunks, tokenizer only; memory
~203 B x n x L², time relative to one 8192-token chunk alone):

| web3.py | Value |
|---|---|
| Chunks / batches of 16 | 1,751 / 110 |
| Batch 0 (the 16 longest) | padded to 8192, est. 203 GiB; one chunk at a time = 6.75 x one 8192 chunk |
| Batch 1 | padded to 1426, est. 6.2 GiB, 0.48 x |
| Batches 2-109 | padded to ~530-770, est. ~1-1.8 GiB each, ~0.07-0.14 x each |
| Whole repo with batch 0 split | ~11.6 x one 8192 chunk, about 15 min at the measured 78 s, on a GPU no one else is using |

Only batch 0 runs out of memory (over the ~32 GiB ceiling here), so the fallback splits exactly one batch.

## The fix (approved by Love Kush 2026-11-01): retry after out-of-memory only

`orion/embed.py`:

- `index` still makes the ONE plain call `model.encode(texts, batch_size=16, show_progress_bar=False,
  convert_to_numpy=True)` (`_encode`). A run that fits is byte-identical to before.
- Only when that call raises an allocation error (`_is_oom`: "out of memory" from CUDA or MPS, the CPU
  allocator's "can't allocate memory" / "not enough memory", or `MemoryError`) does `_encode_split` redo
  the work:
  1. longest-first batches of 16 (the order sentence-transformers uses for str inputs), each tried whole;
  2. a batch that runs out of memory is redone one chunk at a time;
  3. a chunk that runs out of memory alone on the GPU/MPS goes to the CPU, then the model moves back;
  4. a chunk that fails even alone on the CPU raises as before (there is nowhere left to go).
- Same chunks and the same 8192-token cut, so the vectors are what a bigger machine computes, up to
  float rounding.
- Little work is thrown away: sentence-transformers runs the longest batch first, so the plain call
  usually fails on its first batch, before anything is embedded.
- The fallback reports itself: `index(on_warn=...)` gets one message (`orion scan` logs it as a
  `build`/`warn` event in the run log); without a listener it goes to stderr as `orion embed: ...`.
- Any error that isn't out-of-memory raises unchanged.
- Token-free test: `tests/test_embed_oom.py`. A fake model whose "memory" is a budget on
  len(batch) x longest² pins:
  - the single unchanged call when everything fits;
  - the split and CPU paths;
  - input order kept;
  - the model returned to its device;
  - non-OOM errors raising unchanged;
  - the OOM classifier;
  - `index()` writing every chunk after an OOM.

Not done (opt-in only if wanted later): an `ORION_EMBED_MAX_TOKENS` cap that cuts chunks short. It would be
much faster, but it changes the vector of every long chunk, so it needs a measured sign-off first.

## Re-run

`python -m eval.repoqa run --resume --repo ethereum/web3.py --repo microsoft/gctoolkit`, run in WSL with
`orion` imported from the fix's worktree (`eval/repoqa.py` is still uncommitted in the main checkout).
Log: WSL `~/bench-data/repoqa/rerun-oom-fix.log`; the runner is `embed-oom/rerun.py`.

- **web3.py: indexed, scored.** The plain call failed on the 48 GiB batch, as before. The fallback logged
  `re-embedded 1 of 197 batches one chunk at a time, 0 chunk(s) on the CPU`: the 16 longest chunks ran
  one at a time on the GPU (spilling 11-13 GB into shared memory), and the other 196 batches ran whole.
  - Build 71.3 s; **index 6,531.9 s** (1 h 49 min) for 3,146 chunks, 4,468 in the database.
  - Far slower than the ~15 min estimated from one chunk alone. The real graph has 3,146 chunks, not the
    1,751 estimated. Most batches also spill past the 4 GB card on this driver, and a `bench/prove.py`
    run (another session's) was using the same GPU the whole time. The time was not split by cause.
  - Needles, hit@1 / hit@10 (of 10): vector 5 / 8, keyword 0 / 6, hybrid 3 / 10.
- **gctoolkit: indexed, scored.** The plain call failed on the 12.90 GiB batch, as before. The fallback
  logged `re-embedded 1 of 61 batches one chunk at a time, 0 chunk(s) on the CPU`.
  - Build 38.3 s; **index 2,144.4 s** (36 min) for 968 chunks, 2,290 in the database.
  - Needles, hit@1 / hit@10 (of 10): vector 7 / 9, keyword 0 / 3, hybrid 4 / 8.
- The run exited 0, and all 30 repos in `eval/results/repoqa-pilot.json` are `ok`: 300 needles scored,
  0 missing. Each re-run record keeps the old OOM failure under `previous_attempt`, and `embed_device` is
  `cuda:0`: the model is back on the GPU after the fallback.
- Both scans were deleted after their queries; no `repoqa-` node is left in Neo4j (checked).
- The results file and the RepoQA report belong to the RepoQA session; the full table is there.

## What is left

- Speed: on this box a repo with bytecode-sized chunks now finishes, but slowly (web3.py 1 h 49 min).
  The opt-in token cap above is the speed lever; it needs a RepoQA measurement before it could become a
  default.
- `tests/test_embed_spans.py`'s two Neo4j tests fail instead of skipping when `fixtures/NodeGoat` is
  missing (a fresh worktree). Joern stops first ("Input path does not exist"), so no scan is written.
  This predates the fix.
