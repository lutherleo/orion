# Orion to-do

What's left after the benchmark pilot (`reports/benchmark-pilot-2026-11.md`), as of 2026-11-03. The pilot's results and
raw data are on branch `Ifrit`.

## Your decisions

- [ ] Review and merge the open PRs:
  - [ ] #5 Kullervo (29 bug fixes, BountyBench and VLB dev benchmarks)
  - [ ] #6 test and CLI gaps (NodeGoat skip, `train.mine` stdout, `remove_tree` on POSIX)
  - [ ] #7 `src/`-layout Python repos: bind absolute imports
  - [ ] #8 `embed.index`: survive running out of memory on long chunks
  - [ ] #9 sink rules: `Popen`, `shell=True`, `log` as a word
- [ ] Decide whether `Ifrit` gets its own PR. It has no PR yet and sits on top of `Kullervo`.
- [ ] Decide whether to merge the side-session fix branches into `Ifrit` or keep them separate.

## Side-session work with no PR

- [ ] **Plan-limit message fix: commit it.** It has never been committed: `eval/run.py` and `eval/tests/test_queue.py` are
  modified in worktree `.claude/worktrees/nervous-elbakyan-2e5acf`. `eval.run` still misses the real "5-hour limit" message.
- [ ] `exec` / `system` sink hints: 4 commits on `claude/gifted-lichterman-4a923f`, needs a PR or a merge.
- [ ] Stream vs legacy `FLOWS_TO` gap on black: 3 commits on `claude/mystifying-kare-5f755c`, needs a PR or a merge.
- [ ] Check whether the work of two sessions landed elsewhere. Their branches show only 2 commits beyond `Ifrit`, both from
  main: NodeGoat test gating (`claude/elated-gagarin-adb0c1`) and train tests in CI (`claude/jovial-perlman-ad36ae`).
- [ ] Find out why the memory-fix session (`claude/happy-meninsky-ac2c8a`) showed as failed. Its PR #8 is open.

## Engineering, from the pilot

Do these before any larger benchmark run.

- [ ] Find out why a discovery shape can fail while the scan still reports success (PyGoat, Opus + Orion lost shape A).
  All shapes failing also gives exit 0 with "0 leads".
- [ ] Give `bench/research_eval.py` arm O a progress log, so such a failure can be diagnosed afterwards.
- [ ] Verifier: stop it CONFIRMing "no X found" leads.
- [ ] Verifier: look at the right-place leads it rejected on Sonnet (express-xss-sanitizer, parse-url).
- [ ] Graph: PyGoat `FLOWS_TO` gaps at `views.py:150` and `mitre.py:243`.
- [ ] Remove the stray CPU-profile folder in the repo root (`UsershAppDataLocalTemp...cpu-prof/`). It is junk, not committed.

## Measurements (these use tokens; each needs a go-ahead)

The weekly limit was at 85% on 2026-11-03 and resets on 2026-11-05.

- [ ] Measure `--verify-routing`. The verifier is 43-74% of Orion's spend, and routing is off by default and unmeasured.
- [ ] Repeat the 5-task pilot on Sonnet only, with and without routing, after the fixes above. Sonnet + Orion's 5 VLB pairs
  cost $32.58 last time.
- [ ] Only then decide on a larger set. 30 VLB tasks × 3 runs is projected at $702 for the Sonnet pair and $3,222 for the
  Opus pair, at least.
- [ ] Optional: the harder Q&A set on Opus, NodeGoat and PyGoat (only Sonnet on black and poetry has run).

## RepoQA open items (`reports/RepoQA semantic search pilot.md`)

- [ ] Test identifier-style queries, the one case where hybrid could still win.
- [ ] Find out why 8 TypeScript needles have no chunk (7 are in test files).
- [ ] Consider the token cap (`ORION_EMBED_MAX_TOKENS`) if slow indexing of long chunks becomes a problem.
- [ ] Record the commit in the harness (`settings.orion` is only a path).
- [ ] Check `tests/test_embed_spans.py`: its two Neo4j tests were reported as failing instead of skipping.
- [ ] Repeated chunks in vector search: left unfixed by choice. Fixing it means re-measuring.

## Left as is, by choice

- PyGoat with Opus + Orion stays at 12/16 on 3 of 4 shapes; the report says so. A re-run would cost about $18.

## Needs Krish or a human reviewer

- [ ] Sign the proposed answer keys (`eval/dataset/devbench/`, VLB and BountyBench fix sites).
- [ ] Review the Q&A questions (`eval/dataset/graphqa/`, all `"status": "proposed"`).
- [ ] Sign off the Phase 5 scoring fixes (`eval/CHANGELOG.md`, 2026-10-29).
- [ ] Decide on Qwen as C1's base: the Orion-LM design excludes Qwen, and no decision is recorded.
- [ ] Run Gates 1 and 2 on the Mac (`docs/notes/merge-phase6-gates.md`).
- [ ] Re-record the `pygoat_lm` golden on the Mac: `python -m tests.test_graph_snapshot pygoat lm`.
- [ ] Tag the pre-registration (`orion-lm-prereg-v1`), after the items above.
