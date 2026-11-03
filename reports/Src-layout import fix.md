# Src-layout import fix (2026-11-02)

**Question.** A scan of a `src/`-layout Python repo misses many imported calls when it starts from the repo
root. Can it do as well as a scan started from `src/`?

**Answer: yes, and it keeps the files outside `src/`.** Call recall from the repo root went from 0.51 to 0.92 on
black and from 0.75 to 0.89 on poetry, the same as a scan from `src/`. FLOWS_TO rose to the same counts as well.

| | |
|---|---|
| **Code** | Commit `6d0105a` on `claude/festive-bouman-c8d8a2` |
| **PR** | [lutherleo/orion#7](https://github.com/lutherleo/orion/pull/7): open, CI green (`unit`, `neo4j`), mergeable, not merged |
| **Not confirmed** | PyGoat's 1075 FLOWS_TO. WSL gives 1076 with or without this change, so it needs a run on the Mac |
| **Not measured** | Whether scans find more real vulnerabilities. That needs full scans of a `src/`-layout repo with an answer key, which spend tokens |

## What was wrong

Joern names an imported function by its import path. A call binds to the real function only when the parse
root makes that path the file's own name. From the repo root every file under `src/` starts with `src/`, so the
names don't match and the call lands on an external stub.

| Import | Parsed from the repo root | Parsed from `src/` |
|---|---|---|
| `from pkg.sub import helper` | stub `pkg/sub.py:<module>.helper` | real `pkg/sub/__init__.py:<module>.helper` |
| `from pkg import work` (a re-export) | stub `pkg.py:<module>.work` | the same stub |

So the `__init__.py` gap has two sides. A function defined in `__init__.py` binds once the root is right (a live
test pins this). A function only re-exported through it stays a stub from any root.

## What changed

- **Detection (`is_python_src_layout`).** The repo has a `src/` folder and no `src/__init__.py`, and its code
  imports what is under `src/` by name more often than through `src.`.
- **Parse (`_parse`, `_lift_src`).** The `.py` files are copied to a temporary folder with `src/` taken off, and
  joern-parse runs there. A root file with the same path as a `src/` file goes under `__orion_repo_root__`, so both
  are kept. The folder is always removed afterwards.
- **Paths (`restore_paths`).** Before normalize, repo-relative names go back on files, methods, call targets,
  parameters and entry methods. Source text and stubs for outside code are left alone.
- **Coverage.** Both the streaming build and `--no-stream` do this, and the build log says when it happened.
- **Everything else is as before.** Other repos and non-Python builds parse the repo itself. A prebuilt
  `cpg.bin` is never lifted.

The code is in `orion/graph/joern_adapter.py` and `orion/graph_build.py`.

## Why this fix

Two ways were built and compared on both repos. Re-binding stubs after the build gave the same recall and
slightly better precision, but FLOWS_TO did not move, and it has more stub spellings to chase.

| | Old | **Lifted copy (chosen)** | Re-bind stubs |
|---|---|---|---|
| black recall | 0.5123 | **0.9201** | 0.9201 |
| black precision | 0.8361 | **0.8752** | 0.8770 |
| black FLOWS_TO | 3156 | **3513** | 3156 |
| poetry recall | 0.7525 | **0.8897** | 0.8897 |
| poetry precision | 0.6650 | **0.6425** | 0.6538 |
| poetry FLOWS_TO | 9291 | **9985** | 9291 |

## Results

**How it was measured.** `edge_stats` in `eval/graphqa_hard.py` compares calls found by a source oracle (Python's
AST and a written rule) with the graph's CONTAINS_CALL then RESOLVES_TO, over the functions both know (black 363,
poetry 1020). These were real builds through Orion's Neo4j in WSL, with Joern 4.0.590. No model was called.

**Trees.** The RepoQA clones are sparse and hold only the `.py` files under `src/`. The full trees come from
`git archive` and add `tests/`, `scripts/` and the rest.

| Repo | Scan | Oracle | Graph | Both | Recall | Precision | FLOWS_TO | CandidateFlow | Entry points | Files |
|---|---|---|---|---|---|---|---|---|---|---|
| black | old, root | 488 | 299 | 250 | 0.5123 | 0.8361 | 3156 | 20 | 501 | 25 |
| black | **new, root** | 488 | 513 | 449 | **0.9201** | 0.8752 | 3513 | 20 | 501 | 25 |
| black | old, from `src/` | 488 | 513 | 449 | 0.9201 | 0.8752 | 3513 | 20 | 501 | 25 |
| black | new, root, `--no-stream` | 488 | 513 | 449 | 0.9201 | 0.8752 | 3521 | 20 | 501 | 25 |
| black | old, full tree, root | 488 | 299 | 250 | 0.5123 | 0.8361 | 9250 | 170 | 1835 | 269 |
| black | **new, full tree, root** | 488 | 513 | 449 | **0.9201** | 0.8752 | 9873 | 170 | 1835 | 269 |
| black | old, full tree, from `src/` | 488 | 513 | 449 | 0.9201 | 0.8752 | 5242 | 52 | 804 | 40 |
| poetry | old, root | 707 | 800 | 532 | 0.7525 | 0.6650 | 9291 | 300 | 1898 | 183 |
| poetry | **new, root** | 707 | 979 | 629 | **0.8897** | 0.6425 | 9985 | 300 | 1898 | 183 |
| poetry | old, from `src/` | 707 | 979 | 629 | 0.8897 | 0.6425 | 9985 | 300 | 1898 | 183 |
| poetry | old, full tree, root | 707 | 800 | 532 | 0.7525 | 0.6650 | 20231 | 300 | 3421 | 392 |
| poetry | **new, full tree, root** | 707 | 979 | 629 | **0.8897** | 0.6425 | 24300 | 300 | 3421 | 392 |
| poetry | old, full tree, from `src/` | 707 | 979 | 629 | 0.8897 | 0.6425 | 9985 | 300 | 1898 | 183 |

- **Recall.** Calls the graph missed fell from 238 to 39 on black and from 175 to 78 on poetry.
- **Precision.** Black rose and poetry fell slightly, to the same number a scan from `src/` gives. The oracle's
  rule doesn't count calls on another object (`obj.f()`), so it can't check those edges either way. Nearly all
  the added edges it can't confirm are of that kind:

  | Graph edges the oracle can't confirm | Old | New |
  |---|---|---|
  | black, calls on an object | 24 | 38 |
  | black, other | 25 | 26 |
  | poetry, calls on an object | 201 | 281 |
  | poetry, other | 67 | 69 |

- **Same as a scan from `src/`.** On black, FLOWS_TO and CandidateFlow are identical. On poetry, FLOWS_TO is
  identical and 287 of 300 flows are the same, with ranks moved on 249. Sampled centrality is the likely cause,
  but this was not confirmed.
- **Why not just scan from `src/`.** That loses everything outside it. On the full black tree it covers 40 of 269
  files and gets 5242 FLOWS_TO, against 9873 for the new root scan.
- **Paths.** Every reported path is repo-relative and exists. Each new scan covers the same files as the old
  scan from the same root, and entry point counts did not change.

### CandidateFlow changes

| Repo | Tree | Old → new | Kept | Swapped |
|---|---|---|---|---|
| black | sparse | 20 → 20 | 20 | 0 |
| black | full | 170 → 170 | 170 | 0 |
| poetry | sparse | 300 → 300 (the cap) | 282 | 18 |
| poetry | full | 300 → 300 (the cap) | 277 | 23 |

The gain shows up in call edges and FLOWS_TO, not in the ranked list. Every flow on the sparse trees has path
length 1, before and after. Poetry's list is at the cap, all `code_exec`, and mostly false sinks from name hints
(see "Also found").

## Other repos are unchanged

Old and new code gave byte-identical graph snapshots on all 8 builds. Goldens were compared on the prebuilt
builds only.

| Repo | Build | Old = new | FLOWS_TO | CandidateFlow | Golden |
|---|---|---|---|---|---|
| NodeGoat | fresh, default | yes | 217 | 111 | not compared |
| NodeGoat | fresh, lm | yes | 217 | 111 | not compared |
| NodeGoat | prebuilt, default | yes | 217 | 111 | matches |
| NodeGoat | prebuilt, lm | yes | 217 | 111 | matches |
| PyGoat | fresh, default | yes | 1076 | 98 | not compared |
| PyGoat | fresh, lm | yes | 1076 | 170 | not compared |
| PyGoat | prebuilt, default | yes | 1076 | 98 | fails, also before this change |
| PyGoat | prebuilt, lm | yes | 1076 | 170 | fails, also before this change |

## Tests

- **New.** `tests/test_src_layout.py` has 16 tests. None use model tokens, and the one that needs Joern skips
  without it.
- **Windows.** The token-free suite gives 1535 passed, 43 skipped, 62 deselected.
- **WSL, with fixtures.** 19 passed and 3 failed. The 3 are the two PyGoat goldens and the 1075 parity check, and
  the same 3 fail at the base commit.
- **CI on PR #7.** `unit` and `neo4j` pass.

## Against the brief

| Done when | Status |
|---|---|
| Repo-root recall reaches about the `src/` numbers | Met, equal on both repos |
| Precision reported | Met |
| FLOWS_TO stays 217 on NodeGoat | Met |
| FLOWS_TO stays 1075 on PyGoat | **Not confirmed.** Unchanged by this work, but 1076 in WSL |
| Goldens unchanged for non-src repos | NodeGoat met. **PyGoat not confirmed**: its output is unchanged, but its goldens already fail in WSL |
| A token-free test exists | Met |
| CandidateFlow changes reported | Met |

## Not handled

- Monorepos (`packages/a/src/a`), other source roots (`lib/ansible`) and re-exports.
- Existing scans keep their old graph until rebuilt.
- The evidence is two `src/`-layout repos, measured by one oracle's rule.

## Also found

Both were outside this task and went to their own sessions. The numbers below are those sessions' own reports.
Only the commits and branch state were checked here.

**`--no-stream` gave 8 more FLOWS_TO than streaming on black.** This happened before the import fix too (3164 vs
3156 from the root, 3521 vs 3513 from `src/`).

- **Cause.** The legacy walk went on past an argument of the source call itself, which the streaming summaries
  can't see. Read by hand, 7 of black's 8 extra edges are false.
- **Fix.** Legacy now stops there too, so default scans don't change. All 14 graphs compared now agree. 8 other
  graphs had extra legacy edges as well, and the stream never had an edge legacy lacked.
- **Cost.** One real flow, `make_naked` to `Leaf` in black, is now in neither path.
- **State.** Commit `dde5648` on `claude/mystifying-kare-5f755c`, not pushed, no PR. A test merge with this PR's
  commit has no conflicts, but the two haven't been built together, so the 3521 row in Results is from before it.
- **Not fixed.** `--no-stream` can't build locust or poetry at all, because `joern-export` crashes at Java's 2 GB
  string limit. That is why this report has no `--no-stream` row for poetry.

**`exec` and `system` sink hints matched attribute names** such as `self._executor` and `self._build_system`, which
is what filled poetry's cap. They, `eval` and `spawn` now need a real called name. Flow counts on the default
profile, with black and poetry parsed from `src/`:

| Repo | `main` | With `3139205` | With this fix |
|---|---|---|---|
| NodeGoat | 111 | 73 | 73 |
| PyGoat | 98 | 93 | 91 |
| black | 20 | 16 | 3 |
| poetry | 300 (cap, 545 reached) | 300 (cap, 457 reached) | 58 |

- **Real sinks.** All 11 real command and code-execution calls found by Python's AST still get a flow, and poetry's
  six `code_exec` flows are all real command calls.
- **lm profile.** NodeGoat 73, PyGoat 159, black 98. Poetry stays at the cap, with 314 reached.
- **Behaviour change.** `evaluate(...)` no longer counts as a sink. That removes false hits like
  `marker.evaluate(env)` but also real ones like Playwright's `page.evaluate(code)`. No measured repo lost a flow.
- **Still loose.** `query`, `raw`, `redirect` and EXPRESS's plain `exec`.
- **State.** Uncommitted on `claude/gifted-lichterman-4a923f`. It needs `3139205` from the unmerged
  `fix/sink-rules-popen-log` to merge first, and the PyGoat goldens need re-recording on the Mac.

## Cleanup and side effects

- 13 temporary `srcroot-*` scans were deleted from Neo4j (1,080,140 nodes, none left), and the temporary WSL
  copies of the code were removed.
- The bench clones' working trees are still clean, but `git archive` fetched missing blobs into their object
  stores.
- Some `orion_cpg_*` and `orion_stream_*` folders may remain in WSL `/tmp`. That is how builds already behave, so
  they were left alone.

## Next steps

1. Run the PyGoat checks on the Mac. If they fail, compare with the base commit `e755f75`, because CLAUDE.md
   already says `pygoat_lm` needs re-recording there.

   ```bash
   ./.venv/bin/pytest tests/test_stream_build.py::test_stream_flows_parity_pygoat tests/test_graph_snapshot.py -q
   ```

2. Merge PR #7 with a merge commit. [PR #5](https://github.com/lutherleo/orion/pull/5) also edits `CLAUDE.md`, so
   whichever merges second may need a small fix there.
3. Rebuild the graph of any `src/`-layout repo you want the better edges on.
4. To learn whether scans find more, run full scans of a `src/`-layout repo that has an answer key. `bench/prove.py`
   won't show it, because it scans NodeGoat and PyGoat, which this change doesn't touch.
