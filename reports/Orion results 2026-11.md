# Orion results, November 2026

This report brings together everything measured between 2026-10-31 and 2026-11-03: the security benchmarks, Orion's two
memory layers, and what the runs showed about Orion itself. The details behind each part are in:

- `reports/benchmark-pilot-2026-11.md`: security benchmarks, costs, verifier, problems in the runs.
- `reports/RepoQA semantic search pilot.md`: semantic search.
- `reports/Graph-as-memory Q&A pilot.md`: the graph as memory.
- `todo.md`: the full task list.

All data is on branch `Ifrit`: scored tables in `eval/results/` and `bench/research/`, raw runs in `eval/runs-ext/`.

**What kind of evidence this is.** Every number is dev evidence. The benchmarks predate the models' cutoffs, so the models
may have seen them. The answer keys and Q&A questions are proposed and unsigned. Each cell is one run. Only large effects
can show, and a tie does not prove two arms are equal.

## The short answer

| Question | Answer from this round |
|---|---|
| Does Orion help a model find more vulnerabilities in real code? | **No, not at this size.** Opus caught the same 6 of 10 bugs with and without Orion; Sonnet caught 3 with Orion and 4 without |
| What does it cost? | **4 to 6 times as much** as the same model alone |
| Where did it help? | **NodeGoat with Sonnet:** 15/15 against 5/15. Orion was built and tuned on that app, so this is its best case |
| Does the verifier work? | **On Opus, yes:** it rejected about half the leads and lost no catch. But it is 43-74% of Orion's cost, and on Sonnet it rejected two right-place leads |
| Does the graph make a better memory than reading files? | **No sign of it.** Accuracy was the same or slightly lower. It was cheaper only for questions that count across a whole repo |
| Should semantic search switch to hybrid mode? | **No.** Vector search wins on every language and metric |

## 1. Finding vulnerabilities

Four benchmarks, four arms: each model alone (eval's plain arm) and each model with Orion (`orion scan`, default
options). Same model and effort in both arms.

- **VLB:** Cisco's localization benchmark, 5 tasks, each with a vulnerable and a patched snapshot.
- **BountyBench:** 5 bounties.
- **NodeGoat** and **PyGoat:** deliberately vulnerable apps.

**Bugs caught at L2** (right place and right bug type)

| Benchmark | Sonnet alone | Sonnet + Orion | Opus alone | Opus + Orion |
|---|---|---|---|---|
| VLB (5) | 3 | 2 | 3 | 3 |
| BountyBench (5) | 1 | 1 | 3 | 3 |
| NodeGoat (15) | 5 | **15** | 11 | 13 |
| PyGoat (16) | 12 | 12 | 14 | 12* |
| All 41 (rough tally) | 21 | 30 | 31 | 31 |

\* That run lost one of its four discovery shapes; see section 4.

**Cost and time** (cost as the CLI reports it, notional on a subscription login; the multiple compares Orion with the same
model alone)

| Benchmark | Sonnet alone | Sonnet + Orion | Opus alone | Opus + Orion |
|---|---|---|---|---|
| VLB | $6.40 | $32.58 (5.1x) | $25.19 | $153.77 (6.1x) |
| BountyBench | $4.89 | $20.04 (4.1x) | $21.75 | $93.09 (4.3x) |
| NodeGoat | $0.65 | $7.02 (10.8x) | $2.46 | $20.35 (8.3x) |
| PyGoat | $1.46 | $5.66 (3.9x) | $3.22 | $18.23 (5.7x) |
| **Total** | **$13.40** | **$65.30 (4.9x)** | **$52.62** | **$285.44 (5.4x)** |
| Wall time, VLB + BountyBench | 4,172 s | 9,384 s | 7,002 s | 16,412 s |

**Task by task on real code** (L2 = place and type; L1 = place only; "-" = missed)

| Task | Sonnet alone | Sonnet + Orion | Opus alone | Opus + Orion |
|---|---|---|---|---|
| VLB express-xss-sanitizer | L2 | - | L2 | L2 |
| VLB jquery.json-viewer | L2 | L2 | L2 | L2 |
| VLB picklescan | - | L1 | - | L1 |
| VLB python-jose | L2 | L2 | L2 | L2 |
| VLB parsson | - | - | - | - |
| BountyBench parse-url | - | - | L2 | L2 |
| BountyBench zipp | - | - | - | - |
| BountyBench gunicorn | L2 | L2 | L2 | L2 |
| BountyBench lunary | L1 | L1 | L2 | L2 |
| BountyBench undici | - | L1 | L1 | L1 |

Both picklescan L1 credits are other bugs that happen to sit near the fix, not the fixed bug. All four arms stayed silent
on all 5 patched VLB snapshots (no finding of the key's type in the fixed files).

**What Orion's verifier did**

| Arm | Leads | Kept | Rejected | Catches lost | Patched tasks with a false alarm, before → after | Share of Orion's cost |
|---|---|---|---|---|---|---|
| VLB, Sonnet | 63 | 41 | 20 | 0 | 0 → 0 | 43% |
| VLB, Opus | 140 | 65 | 72 | 0 | 2 → 0 | 59% |
| BountyBench, Sonnet | 36 | 29 | 6 | 0 | n/a | 48% |
| BountyBench, Opus | 99 | 50 | 49 | 0 | n/a | 63% |

"Kept" counts CONFIRM verdicts. The few INCONCLUSIVE leads are left out of both "Kept" and "Rejected". "Catches lost"
means L2 catches.

On Gate 3 the verifier's share was 53-74%. Opus alone was already silent on all 5 patched snapshots without a verifier,
so the verifier only brings Orion + Opus level with it.

## 2. Semantic search (RepoQA)

300 needle functions in 30 repos (Python, Java and TypeScript), found from an English description. No model calls are
involved, only Orion's local embedder.

| Mode | hit@1 | hit@5 | hit@10 | MRR |
|---|---|---|---|---|
| **vector** (default) | **0.60** | **0.77** | **0.83** | **0.67** |
| keyword | 0.10 | 0.28 | 0.38 | 0.18 |
| hybrid | 0.30 | 0.57 | 0.71 | 0.41 |

- **Vector wins in every language.** At hit@1 it finds 97 needles that hybrid misses; hybrid finds 8 that vector misses.
  Hybrid beats vector in only 4 of the 30 repos.
- **Bugs found:** the indexer ran out of GPU memory on long chunks (fix in PR #8). Vector search also returns the same
  chunk more than once, mostly on Python, where 88 of 100 queries had a repeat (left open).
- **Not tested:** queries containing code identifiers, the one case hybrid was built for.

## 3. The graph as memory (Graph Q&A)

The agent answers code-structure questions through Orion's graph tools only, or by reading the source only. Gold answers
are checked against the source.

| Set | Questions | Graph path | Grep path |
|---|---|---|---|
| Easier set, both models | 20 | all 20 exact, 1.56-1.57x grep's tokens | all 20 exact |
| Harder set, Sonnet, black + poetry | 24 | 16 exact, F1 0.86 | 17 exact, F1 0.81 |
| Harder set: tokens / cost / time | | 20.4M / $17.91 / 8,568 s | 26.2M / $18.78 / 6,212 s |

- **No accuracy gain.** Two of grep's misses are sessions that searched an empty folder; without those two questions,
  grep leads 17 of 22 to 14.
- **Cheaper only on whole-repo counts.** On the 4 "highest fan-in / fan-out" questions the graph used 0.39x grep's
  tokens. On the other 20 it used 1.13x.
- **The graph itself is incomplete:**
  - A plain scan of a `src/`-layout Python repo sees only 0.51 (black) and 0.75 (poetry) of the calls. Scanning from
    `src/` raises that to 0.92 and 0.89 (fix in PR #7).
  - `FLOWS_TO` also misses data passed through callbacks in NodeGoat, and two plain assignments in PyGoat.
- **The graph's plain query alone** answered 10 of 24 harder questions exactly. Agents using the graph got 7 of the 14
  it got wrong; grep agents got 8.

## 4. Problems the runs exposed

| Problem | Effect |
|---|---|
| A discovery shape can fail while the scan still reports success | PyGoat Opus + Orion ran on 3 of 4 shapes. Shape A gave nothing, and no log says why |
| The semantic index ran out of GPU memory (4 GB card) | 6 of 30 external Orion runs were graph-only. Both affected bugs were still caught |
| The 5-hour plan limit | It cut 8 of 15 Opus + Orion runs, all re-run. $46.86 was spent on attempts that are not scored |
| Plain-arm sandbox tools were missing in WSL | 15 crashed attempts at 0 tokens. The harness now refuses to start without them |

## 5. Conclusions

1. **Orion does not yet improve detection on real code.** On the 10 external tasks it never caught a bug that the same
   model missed alone, and it cost 4-6x as much. This is a small sample, but the most likely effect is small or none,
   not a large hidden gain.
2. **Its gains are on the code it was tuned on.** NodeGoat with Sonnet is the only large improvement. Across all 41 keys,
   Sonnet + Orion (30, $65) roughly matches Opus alone (31, $53), so on this data a stronger model alone is the cheaper
   way to get that recall.
3. **The verifier filters well but costs most.** With Opus it halved the leads without losing a catch. The biggest saving
   available is to make verification cheaper (the unmeasured `--verify-routing`), not to drop it.
4. **The graph is not yet a better memory than the source.** It answers as well as reading files, at about the same cost,
   and wins only on counting across a whole repo. Gaps in the graph (imports in `src/` layouts, callback data flow) are
   a likely reason, and they are fixable.
5. **Keep vector search as the default.**
6. **Reliability matters as much as accuracy.** A silently lost shape, an out-of-memory index and limit-cut runs each
   changed results. Several fixes are already in open PRs.
7. **Not tested yet:** large repos, real post-cutoff vulnerabilities, and repeated runs. Orion's graph could matter more
   in large code than in these small repos, but this pilot can't say.

## 6. What's left to do

The full list with paths is in `todo.md`.

**Decisions for you**
- [ ] Review and merge the open PRs: #5 Kullervo, #6 test gaps, #7 `src/` layout, #8 index memory, #9 sink rules.
- [ ] Decide whether `Ifrit` gets its own PR.
- [ ] Decide whether the side-session fix branches merge into `Ifrit`.

**Fixes before any larger run**
- [ ] Make a failed discovery shape fail the scan, and give arm O a progress log.
- [ ] Commit the plan-limit message fix. It is still uncommitted, in worktree `nervous-elbakyan-2e5acf`.
- [ ] Land the two side branches that have no PR: the `exec`/`system` sink hints and the stream vs legacy `FLOWS_TO` gap.
- [ ] Verifier: stop it confirming "no X found" leads, and look at the right-place leads it rejected on Sonnet.
- [ ] Graph: fix the PyGoat `FLOWS_TO` gaps at `views.py:150` and `mitre.py:243`.

**Measurements (use tokens; each needs a go-ahead)**
- [ ] Measure `--verify-routing` to cut the verifier's cost.
- [ ] Repeat the 5-task pilot on Sonnet only, with and without routing. Sonnet + Orion's 5 VLB pairs cost $32.58 last time.
- [ ] Only then size a larger run. 30 VLB tasks × 3 runs is projected at $702 or more for the Sonnet arms and $3,222 or
  more for the Opus arms.
- [ ] Optional: the harder Q&A set on Opus, NodeGoat and PyGoat. Identifier-style queries on RepoQA.

**Needs Love Kush or a human reviewer**
- [ ] Sign the proposed answer keys and review the Q&A questions.
- [ ] Sign off the Phase 5 scoring fixes.
- [ ] Decide on Qwen as C1's base.
- [ ] Run Gates 1 and 2 on the Mac, re-record the `pygoat_lm` golden, then tag `orion-lm-prereg-v1`.

**Housekeeping**
- [ ] Delete the stray CPU-profile folder in the repo root.
- [ ] Look at why the memory-fix session showed as failed.
- [ ] Check whether two test sessions' work landed elsewhere: NodeGoat test gating and train tests in CI.
