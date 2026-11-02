# Benchmark pilot: the same model with and without Orion (2026-11)

**Answer: on real code, at this size, Orion did not catch more and cost 4 to 6 times as much.** With Opus, both arms
caught the same 6 of 10 external bugs at L2 (right place and right bug type). With Sonnet, Orion caught 3 of 10 against
4 of 10 alone. The one large gain is Sonnet on NodeGoat (15/15 against 5/15), the app Orion was built and tuned against.

**Recommendation: don't scale this setup.** Fix what the pilot found first, then repeat it on Sonnet only ("Scale-up
decision" below).

Every number here is dev evidence: the benchmarks predate the models' cutoffs, the keys are proposed and unsigned, and
each cell is one run. Only large effects can show, and a tie on 10 bugs does not prove the arms are equal.

## The comparison

**L2 caught**

| Benchmark | Sonnet alone | Sonnet + Orion | Opus alone | Opus + Orion |
|---|---|---|---|---|
| VLB (5 tasks) | 3 | 2 | 3 | 3 |
| BountyBench (5 bounties) | 1 | 1 | 3 | 3 |
| NodeGoat (15 keys) | 5 | **15** | 11 | 13 |
| PyGoat (16 keys) | 12 | 12 | 14 | 12 (3 of 4 shapes, see "Problems") |
| Count, all 41 | 21 | 30 | 31 | 31 |

The count mixes four different benchmarks, so treat it as a rough tally. NodeGoat supplies all of Sonnet + Orion's gain.

**CLI-reported cost** (notional on a subscription login; the multiple is Orion against the same model alone)

| Benchmark | Sonnet alone | Sonnet + Orion | Opus alone | Opus + Orion |
|---|---|---|---|---|
| VLB (10 runs per arm) | $6.40 | $32.58 (5.1x) | $25.19 | $153.77 (6.1x) |
| BountyBench (5 runs per arm) | $4.89 | $20.04 (4.1x) | $21.75 | $93.09 (4.3x) |
| NodeGoat (1 run per arm) | $0.65 | $7.02 (10.8x) | $2.46 | $20.35 (8.3x) |
| PyGoat (1 run per arm) | $1.46 | $5.66 (3.9x) | $3.22 | $18.23 (5.7x) |
| Total | $13.40 | $65.30 (4.9x) | $52.62 | $285.44 (5.4x) |

**Tokens and wall time**

| Benchmark | Sonnet alone | Sonnet + Orion | Opus alone | Opus + Orion |
|---|---|---|---|---|
| VLB | 9,784,921; 2361 s | 39,889,589; 5524 s | 17,537,831; 4013 s | 68,414,280; 10263 s |
| BountyBench | 9,747,888; 1811 s | 26,146,001; 3860 s | 21,725,429; 2989 s | 42,265,533; 6149 s |
| NodeGoat | 941,833; 183 s | 10,260,718; 1010 s | 1,507,170; 442 s | 8,384,181; 1169 s |
| PyGoat | 2,906,456; 482 s | 7,940,703; 855 s | 2,105,304; 519 s | 8,525,111; 2129 s |

Costs and tokens count only the attempt that was scored; "Problems" lists what the plan limit wasted on top.

Mean cost per task:

| Per task | Sonnet alone | Sonnet + Orion | Opus alone | Opus + Orion |
|---|---|---|---|---|
| VLB (vulnerable + patched pair) | $1.28 | $6.52 | $5.04 | $30.75 |
| BountyBench (one snapshot) | $0.98 | $4.01 | $4.35 | $18.62 |

## Task by task

"L2" = right place and type. "L1" = right place only, which does not check the type. "-" = missed.

| Task (key CWE) | Sonnet alone | Sonnet + Orion | Opus alone | Opus + Orion |
|---|---|---|---|---|
| VLB express-xss-sanitizer (674) | L2 | - | L2 | L2 |
| VLB jquery.json-viewer (79) | L2 | L2 | L2 | L2 |
| VLB picklescan (502) | - | L1 | - | L1 |
| VLB python-jose (409) | L2 | L2 | L2 | L2 |
| VLB parsson (20, 834) | - | - | - | - |
| BountyBench parse-url | - | - | L2 | L2 |
| BountyBench zipp | - | - | - | - |
| BountyBench gunicorn | L2 | L2 | L2 | L2 |
| BountyBench lunary | L1 | L1 | L2 | L2 |
| BountyBench undici | - | L1 | L1 | L1 |

**VLB summary** (`eval/results/vlb-pilot.md`):

| | Sonnet alone | Sonnet + Orion | Opus alone | Opus + Orion |
|---|---|---|---|---|
| L1 / L2 | 3 / 3 | 3 / 2 | 3 / 3 | 4 / 3 |
| Mean File F1 (CWE family) | 0.533 | 0.333 | 0.533 | 0.533 |
| Silent on patched | 5/5 | 5/5 | 5/5 | 5/5 |
| Pair-correct | 3/5 | 3/5 | 3/5 | 4/5 |

**Orion + Opus's extra pair-correct on picklescan is not the real bug, so read that row as 3/5.**

- The fix adds `cloudpickle.cloudpickle` and `types.CodeType` to the deny list and changes parse-error handling.
- The credited finding says `builtins.__import__` is missing from the same list (CWE-184). That falls inside the key's
  ±10-line window, so L1 credits it.
- The same complaint comes back on the patched snapshot, because `__import__` is still absent after the fix. It still
  counts as silent, because silence only looks at the key's CWE family.
- Sonnet + Orion's L1 there is an unrelated path traversal in `scan_7z_bytes`, near a fix site.

**Gate 3** (`bench/prove.py --rescore`, eval's matcher; this gate has no pass/fail rule, it is a measurement):

| Fixture | Arm | L1 | L2 | Missed (L1) | Unmatched |
|---|---|---|---|---|---|
| NodeGoat | Sonnet alone | 5 | 5 | A1-3, A2-1, A3, A5, A6, A7, A8, A9, A10, ReDoS | 8 |
| NodeGoat | Sonnet + Orion | 15 | 15 | - | 4 |
| NodeGoat | Opus alone | 12 | 11 | A5, A6, A9 | 4 |
| NodeGoat | Opus + Orion | 15 | 13 | - | 4 |
| PyGoat | Sonnet alone | 14 | 12 | LOGGING, COMPONENTS | 4 |
| PyGoat | Sonnet + Orion | 12 | 12 | XXE, AUTHZ, DESIGN-OTP, LOGGING | 2 |
| PyGoat | Opus alone | 15 | 14 | COMPONENTS | 4 |
| PyGoat | Opus + Orion | 13 | 12 | SSTI, DESIGN-OTP, LOGGING | 5 |

I read the one-line text of each Gate 3 unmatched finding. They all describe plausible extra bugs in deliberately
vulnerable apps, but none was checked against the source, so no false-positive rate is claimed.

External arms also report bugs outside the key (Opus: 37 findings alone, 50 with Orion, on BountyBench). Only the key
bug is labelled, so nobody can yet say how many of those are real.

## What the verifier did

Orion's verifier is the check that separates its findings from its leads, and it is also most of Orion's cost on Opus.
`:leads` scores the same run before verification.

| Arm | Leads | CONFIRM | REJECT | INCONCLUSIVE | L2 before → after | Silent on patched before → after | Verifier's share of cost |
|---|---|---|---|---|---|---|---|
| VLB, Sonnet | 63 | 41 | 20 | 2 | 2 → 2 | 5 → 5 | 43% |
| VLB, Opus | 140 | 65 | 72 | 3 | 3 → 3 | 3 → 5 | 59% |
| BountyBench, Sonnet | 36 | 29 | 6 | 1 | 1 → 1 | n/a | 48% |
| BountyBench, Opus | 99 | 50 | 49 | 0 | 3 → 3 | n/a | 63% |

- **On Opus it did its job:** it rejected about half the leads and lost no L2 catch. It raised silence from 3/5 to
  5/5, which only brings Orion level with Opus alone (5/5, no verifier).
- **On Sonnet it removed two right-place leads:** express-xss-sanitizer and parse-url, both L1 only. Neither had
  reached L2 in discovery.
- **The share on Gate 3 is similar or higher:** 65% and 53% on Sonnet, 74% and 73% on Opus (NodeGoat, PyGoat).

## Problems in these runs

| Problem | Effect |
|---|---|
| **PyGoat, Opus + Orion: discovery shape A gave nothing** (0 verdicts and no usage event; the other three Gate 3 Orion runs got 9-10 from it) | The 12/16 is on 3 of 4 shapes, so it may understate Orion. The cause is missing: `bench/research_eval.py` keeps no progress log. The run reported success. All 30 external Orion runs finished all 4 shapes |
| **Semantic index ran out of GPU memory** on python-jose (both snapshots, both models) and gunicorn (both models) | 6 of 30 external Orion runs were graph-only. Both bugs were still caught at L2. The fix is on `claude/happy-meninsky-ac2c8a` (24497c9), not in the measured commit |
| **The 5-hour plan limit** cut 8 of 15 Opus + Orion runs; all 8 were re-run on 2026-11-02 | $46.86 and 22,837,335 tokens spent on 3 cut-off attempts are not in the tables: xss-sanitizer patched $7.21, python-jose vulnerable $16.28, lunary $23.37. The other aborted Opus attempts spent nothing; Sonnet's 5 aborted BountyBench attempts spent $0.05. Attempts reached number 5 |
| parse-url, Opus + Orion: all 4 shapes failed once on the limit and were retried | Its $14.69 includes those first calls |
| **`src/` layout:** a plain scan of a `src/`-layout Python repo misses imported calls (see the Graph Q&A report). Of the 10 external repos only picklescan is exposed | Orion's picklescan runs may be under-credited; not measured. Open as lutherleo/orion#7 |
| Plain Sonnet's first pass crashed 15 times: the sandbox tools were missing in WSL | 0 tokens; all re-run. `preflight` and `extbench` now refuse it up front |

Neo4j: none of the 72 benchmark scan ids are left. `orion gc --keep 1 --dry-run` lists 10 other scans; nothing was deleted.

## Caveats

- **Tuning:** Orion was built against NodeGoat and tested on PyGoat, so those rows are its best case. The external sets
  are the fairer test.
- **Different Claude homes:** Orion's agents ran in WSL's normal home (fp-check lives there); the plain arms ran in eval's
  clean home.
- **Selection:** the pilot takes the smallest repos first, so a larger set will cost more per task. Graph-based tools may
  also do better on larger code; this pilot cannot say.
- **Matcher:** L1 ignores type, silence checks only the key's CWE family, and pair-correct combines them.
- **Keys and data:** proposed and unsigned, N = 1 per cell, pre-cutoff data the models may have seen.
- **Wall times:** inflated by other jobs sharing WSL (RepoQA embedding, Graph Q&A) and spread over several limit windows
  for Opus + Orion.
- **Versions:** Orion commit 3e46ac2 (its `orion/` code is 20b51b8's). CLI 2.1.312 for the Sonnet round, 2.1.316 for Opus.
- **Gate 3 cost:** CLI-reported here, to match the other benchmarks. `bench/token_ledger` prices the Sonnet + Orion rows
  higher ($10.33 and $8.38).

## Memory and retrieval

The memory benchmarks can't test Orion directly, so these test its two memory layers on their own.

**`semantic_search` on RepoQA: keep `vector` as the default.** 300 needles over 30 repos, no model calls
(`reports/RepoQA semantic search pilot.md`).

| Mode | hit@1 | hit@10 | MRR |
|---|---|---|---|
| vector (default) | 0.60 | 0.83 | 0.67 |
| keyword | 0.10 | 0.38 | 0.18 |
| hybrid | 0.30 | 0.71 | 0.41 |

These queries are plain English. Agents' queries can contain identifiers, which is what hybrid was built for, and that
case is not tested.

**Graph as memory: no better than grep so far, at about 1.6x the tokens** (`reports/Graph-as-memory Q&A pilot.md`). On
20 questions every row was exact on every question, so that set only measures cost. The harder set stopped on the plan
limit at 18 of 24 questions (Sonnet, black + poetry): an unscored hand tally has the graph exact on 11 and grep on 13.
12 sessions remain, and no harder-set full run has been done on Opus.

## Scale-up decision

**Projection, not data:** 30 VLB tasks × 3 runs, from the measured cost per pair above. This is a lower bound, since the
pilot used the smallest repos.

| Arm | Projected CLI cost |
|---|---|
| Sonnet alone | $115 |
| Sonnet + Orion | $587 |
| Opus alone | $454 |
| Opus + Orion | $2,768 |

The re-run spent $141.80 while weekly usage rose from 66% to 80%, with side sessions also running, so roughly $10 per 1%
or $1,000 a week. On that rough rate the Sonnet pair needs about 0.7 of a week and the Opus pair about 3.2 weeks.

1. **Don't scale this setup.** Opus + Orion tied Opus alone on all 10 external tasks at L2, and more runs of the same
   setup would mainly resolve small differences at high cost.
2. **Fix what the pilot found first:** the lost shape, the embed fix and `src/` layout (PR #7).
3. **Cut the verifier's cost:** it is 43-74% of Orion's spend. `--verify-routing` exists, is off by default and is
   unmeasured.
4. **Then repeat the 5-task pilot on Sonnet only, with and without routing.** At the measured rate that is about $33 for
   Sonnet + Orion's 5 VLB pairs. Decide on a larger set after that.

## Orion bugs this round found

1. A discovery shape can fail and the run still looks complete (PyGoat above); earlier in this round, all shapes failing
   gave exit 0 with "0 leads".
2. `bench/research_eval.py` arm O keeps no progress log, so such a failure can't be diagnosed afterwards.
3. The verifier CONFIRMs "no X found" leads.
4. The verifier rejected the right-place leads on express-xss-sanitizer and parse-url on Sonnet.
5. PyGoat FLOWS_TO gaps at `views.py:150` and `mitre.py:243` (Graph Q&A report).
6. `embed.index` runs out of memory on a 4 GB GPU (fixed on `claude/happy-meninsky-ac2c8a`, not merged).
7. A plain scan of a `src/`-layout Python repo misses imported calls (PR #7, open).

## Files and how to re-score

- Scored tables: `eval/results/vlb-pilot.{md,json}`, `eval/results/bountybench-pilot.{md,json}`.
- Gate 3: `bench/research/{nodegoat,pygoat}/{O-sonnet,O-opus,P-plain-sonnet5,P-plain-opus5}.json`, plus the `P-*/` folders.
- Raw runs are not in git: `eval/runs-ext/` in the WSL worktree `~/orion-bench` only, so back them up before removing it.

Re-scoring is token-free, run from that worktree:

```bash
python -m eval.extbench score --benchmark vlb
```

```bash
python -m eval.extbench score --benchmark bountybench
```

```bash
python -u bench/prove.py --rescore --models sonnet,opus --plain-models sonnet,opus
```
