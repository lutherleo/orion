# Test-suite failures at `e755f75`: causes, fixes, results

**Where this landed:** [PR #6](https://github.com/lutherleo/orion/pull/6) into `Kullervo`, commits `f244ee0` and
`521a33d`. Open PR #5 (`Kullervo`) already had most of these fixes, so #6 holds only what #5 lacks, plus a fix for a
new bug found in #5. See ["What landed"](#what-landed-pr-6-into-kullervo) at the end for the results.

Everything before that section describes the first pass against `main`. Every failure has a named cause,
and no failures are left in any of those runs, on WSL or Windows.

**First-pass branch:** `fix/test-harness-skips`, two commits on top of `e755f75`, pushed but with no PR.
Superseded by #6.

| Commit | What changed |
|---|---|
| `e5601ad` | Test code and a one-line `.gitattributes`. No product code. |
| `64a8cb6` | The two bug fixes in `train/mine.py`, `train/snapshot.py` and `train/stage1.py`, plus their regression tests. |

## Before and after

| Suite | Environment | Base `e755f75` | After `e5601ad` (tests) | After `64a8cb6` (tests + bug fixes) |
|---|---|---|---|---|
| `pytest -m "not slow"` | WSL, fresh clone | **4 failed**, 1526 passed, 32 skipped | 0 failed, 1526 passed, 36 skipped | **0 failed**, 1526 passed, 36 skipped |
| `pytest -m "not slow"` | Windows | 0 failed, 1520 passed, 42 skipped | 0 failed, 1520 passed, 42 skipped | **0 failed**, 1520 passed, 42 skipped |
| `train/tests eval/tests` | WSL, fresh clone | **41 failed**, 825 passed, 3 skipped | 0 failed, 817 passed, 52 skipped | **0 failed**, 819 passed, 52 skipped |
| `train/tests eval/tests` | WSL venv on the Windows (CRLF) checkout | **43 failed**, 823 passed, 3 skipped | 0 failed, 817 passed, 52 skipped | **0 failed**, 819 passed, 52 skipped |
| `train/tests eval/tests` | Windows | **58 failed**, 804 passed, 7 skipped | 1 failed, 800 passed, 68 skipped | **0 failed**, 803 passed, 68 skipped |

The same train and eval suites with the pinned tree-sitter packages on the path (installed in a scratch
folder, not in either venv), so the skipped tests really run:

| Suite | Environment | Base `e755f75` | After `e5601ad` | After `64a8cb6` |
|---|---|---|---|---|
| `train/tests eval/tests` + tree-sitter | WSL, fresh clone | 19 failed, 848 passed, 2 skipped | 0 failed, 865 passed, 4 skipped | **0 failed**, 867 passed, 4 skipped |
| `train/tests eval/tests` + tree-sitter | Windows | not run | 5 failed, 844 passed, 20 skipped | **0 failed**, 851 passed, 20 skipped |

All runs add `-m "not slow"`. Train and eval are 869 tests, and 871 at `64a8cb6` (2 new regression tests).
`tests/` is 1562 tests plus 62 deselected as slow.

- **WSL:** a fresh LF clone (`~/orion-harness-check`, since removed) with `~/orion/.venv`.
- **Windows:** this worktree with `C:\Users\h\Downloads\orion\.venv`.
- **Raw files:** `base-e755f75/`, `after-e5601ad/` and `after-64a8cb6/`, a `.txt` and a junit `.xml` per
  run. `wsl-te` and `win-te` are the train and eval runs, `wsl-orion` and `win-orion` are `tests/`. The
  base tree-sitter run has only the `.xml`.

## Failure groups

The counts add up to each base run's total: WSL 4, and for train and eval 41, 43 and 58.

| Failing tests | WSL | WSL on CRLF | Windows | Cause | What I did |
|---|---|---|---|---|---|
| `test_graph_build` (2), `test_embed_spans` (2) | 4 | not run | 0 | `fixtures/` is gitignored, so `joern-parse` raises on the missing path. Windows skips earlier because Joern can't run there. | New `needs_nodegoat` fixture: skip |
| `test_stage1_cases` (17), `test_stage1_build` (5) | 22 | 22 | 22 | tree-sitter (the `lm` extra) is not installed, so `RepoIndex` finds no functions. | `needs_tree_sitter` fixture: skip. All pass with tree-sitter on both systems, on Windows once bug 2 was fixed. |
| `test_stage2` (17) | 17 | 17 | 17 | **Stale test stub, not tree-sitter.** `StubDB` matched `"CpgMethod"` before `"c.uid IN"`. `flow_slice`'s query has named `CpgMethod` since Phase 3 (`3efca05`), so it got method rows and every case came out `no_snippet`. These failed even with tree-sitter installed. | Reordered the stub. All pass with tree-sitter on both systems. They also need tree-sitter, so they skip without it. |
| `test_convert` merge tests (2) | 2 | 2 | 2 | `mlx` is not installed. | `importorskip`. **Not verified:** MLX is not on either machine. |
| `test_shim` (2) | 0 | 2 | skip | With `autocrlf`, the shim's first line is `#!/bin/bash\r`, so running it from WSL gives `FileNotFoundError`. This is the gap between 41 and the reported 43. | `.gitattributes`: `eval/shim/claude text eol=lf`. All 3 pass from the CRLF checkout now. |
| `test_build` (10) | 0 | 0 | 10 | `build_pair` imports the POSIX-only `resource` module; one test needs `sh`, `ps` and process groups. | `skipif` on Windows |
| `test_guard` process-group kill (1) | 0 | 0 | 1 | `signal.SIGKILL` and `killpg` don't exist on Windows. | `skipif` on Windows |
| `test_unsloth_data` (1) | 0 | 0 | 1 | `transformers` is not in the Windows venv. | `importorskip`. It still passes in WSL. |
| `test_plain`, `test_precheck`, `test_run_wiring` (1 each) | 0 | 0 | 3 | The tests hard-coded `/` in paths the code builds with `os.path.join`. | Fixed the tests. They pass on Windows now. |
| `test_sources` CVEfixes dump (1) | 0 | 0 | 1 | The test wrote its dump in Windows text mode, so the parser read CRLF lines (`KeyError: 'hash'`). | Fixed the test (`newline="\n"`). It passes on Windows now. |
| `test_mine` end to end (1) | 0 | 0 | 1 | **Real bug 1**, below. | Fixed in `64a8cb6` |

## The two real bugs, fixed in `64a8cb6`

Both are Windows-only. Both have a regression test that failed before the fix and passes after.

1. **`train/mine.py` crashed on the `≤` in its tables.**
   - `tables.md` was written with the default encoding, which is cp1252 on Windows. Fixing the file alone
     wasn't enough: `python -m train.mine build` prints the same tables, so it would still crash whenever
     stdout was redirected.
   - **Fix:** the file is written as UTF-8, and the CLI sets its stdout to UTF-8.
   - **Test:** `test_build_cli_with_a_non_utf8_default_encoding` runs the real CLI with a non-UTF-8 default
     forced, so it guards this on every OS. It failed in WSL too before the fix.
2. **Stage 1 snapshots were never deleted on Windows (`train/stage1.py`, `train/snapshot.py`).**
   - Git's object files are read-only and Windows won't delete a read-only file, so
     `shutil.rmtree(dest, ignore_errors=True)` silently left the clone behind. The next fetch on that
     folder then failed.
   - **Fix:** new `snapshot.remove_tree`. On Windows it clears the read-only flag on a file it was refused
     and retries. It never raises, it leaves symlinks alone, and on POSIX it does exactly what the old call did.
   - **Test:** `test_a_clone_with_read_only_git_objects_can_be_replaced_and_removed` uses a real clone. The
     4 `test_stage1_build` tests that hit this on Windows with tree-sitter now pass.
   - `repro_rmtree.py` in this folder shows the plain `rmtree` behaviour on its own.

## Things to know

- **8 tests that passed without tree-sitter now skip without it.** All 8 still run with tree-sitter.
  - 6 passed only on empty results, which proved nothing: `test_seeded_negatives_are_deterministic`,
    `test_untouched_neighbour_is_not_a_target` (3 cases), `test_new_function_inserted_between_functions_is_not_a_target`
    and `test_only_unchanged_targets_left_is_no_target`.
  - 2 don't really need tree-sitter and are caught by the shared guard: `test_primary_cwe_prefers_dataflow_then_first`
    and `test_no_gold_flow_gives_no_examples`.
- **No other test got worse.** Comparing the junit files, no test newly fails, and `64a8cb6` only adds the
  2 new tests to the ids from `e5601ad`.
- **Existing checkouts keep their CRLF shim until it is rewritten.** The `.gitattributes` line applies to
  new checkouts. In an old one, once this is merged, delete `eval/shim/claude` and run
  `git checkout -- eval/shim/claude`.

## Worth a look, not changed

- **5 tests write to the shared Neo4j without the opt-in.** With the fixture, Joern and Neo4j all present,
  the 3 in `test_graph_build` and 2 in `test_embed_spans` write without `ORION_LIVE_NEO4J=1`, against the
  rule in `tests/conftest.py`. `~/orion` has that setup.
- **CI doesn't run `train/tests`.** `.github/workflows/tests.yml` runs `tests/` and `eval/tests` only, so
  the stale stub went unnoticed. CI also has no tree-sitter, so adding `train/tests` alone would mostly skip
  Stage 1 and Stage 2.
- **`eval/repo.py:16` has the same `rmtree` call on a "leftover partial clone".** On Windows it would hit
  bug 2. I did not test or change it: `eval/` can't use the `train/` helper, because `train/` imports `eval/`.

## Not run

- The 4 NodeGoat tests against a real fixture and a live Neo4j, because they write to the shared database.
  I only checked that the new guard lets them through: with the fixture present and Neo4j pointed at a dead
  port, they get past it and skip on "Neo4j not reachable" (`after-e5601ad/wsl-guard-fixture-present.txt`).
- The `test_convert` merge tests, because neither machine has MLX.
- `tests/` with tree-sitter installed (18 tests skip without it), in both environments.
- Base `train/tests eval/tests` with tree-sitter on Windows.
- `pytest -m "not slow"` from the CRLF checkout, base or after.
- A real Stage 1 or mining run on Windows. The bug fixes are checked by tests on small local repos only.
- Anything on the Mac.

## What landed: PR #6 into `Kullervo`

Commit `58f6575` in open PR #5 already makes most of the fixes above, in places better: `remove_tree` lives in
`eval.repo`, so it also covers `eval/repo.py`, and `resource` is handled rather than skipped. A trial merge of
the first-pass branch conflicted in 10 files. So [PR #6](https://github.com/lutherleo/orion/pull/6) targets
`Kullervo` and holds only two commits.

`f244ee0` (6 files, +57 -8):

- the `needs_nodegoat` guard for the 4 tests that still failed on #5
- the `train.mine` CLI stdout fix: #5 fixed `tables.md` but not the `print`, and the new test fails on #5's code
  in both WSL and Windows
- a regression test for the CLI, and one for clone removal (#5 had no `remove_tree` test; it passes on #5's
  code too, so it is a guard only)

`521a33d` (2 files, +116 -1): the `remove_tree` fix below, with 7 new tests.

| Suite | Environment | #5 tip `20b51b8` | `f244ee0` | PR #6 `521a33d` |
|---|---|---|---|---|
| `pytest -m "not slow"` | WSL, fresh clone | **4 failed**, 1565 passed, 32 skipped | 0 failed, 1565 passed, 36 skipped | **0 failed**, 1565 passed, 36 skipped |
| `pytest -m "not slow"` | Windows | 0 failed, 1557 passed, 44 skipped | 0 failed, 1557 passed, 44 skipped | 0 failed, 1557 passed, 44 skipped |
| `train/tests eval/tests` | WSL, fresh clone | 0 failed, 844 passed, 44 skipped | 0 failed, 846 passed, 44 skipped | 0 failed, 851 passed, 46 skipped |
| `train/tests eval/tests` | WSL on the Windows (CRLF) checkout | 0 failed, 844 passed, 44 skipped | 0 failed, 846 passed, 44 skipped | 0 failed, 851 passed, 46 skipped |
| `train/tests eval/tests` | Windows | 0 failed, 836 passed, 52 skipped | 0 failed, 838 passed, 52 skipped | 0 failed, 841 passed, 56 skipped |
| `train/tests eval/tests` + tree-sitter | WSL, fresh clone | 0 failed, 884 passed, 4 skipped | 0 failed, 886 passed, 4 skipped | 0 failed, 891 passed, 6 skipped |
| `train/tests eval/tests` + tree-sitter | Windows | 0 failed, 876 passed, 12 skipped | 0 failed, 878 passed, 12 skipped | 0 failed, 881 passed, 16 skipped |

Raw files: `pr5-tip-20b51b8/`, `delta-f244ee0/` and `remove-tree-fix/`. Comparing their junit files, the train and
eval counts grow by exactly the 9 new tests, the 4 NodeGoat failures became skips, and no test got worse.
`remove-tree-fix/junit-diff-vs-f244ee0.txt` has the comparison for the last step: no test changed status.

### New bug in #5's `eval.repo.remove_tree`, fixed in `521a33d`

`_retry_writable` ran on every platform and for every failing call. Reproduced on POSIX in WSL. Before the fix,
`repro_pr5_remove_tree.py` (in this folder) printed:

```
1. unreadable subfolder: remove_tree RAISED TypeError open() missing required argument 'flags' (pos 2)
2. target outside the tree: mode before 0o644, after 0o200
```

After it (`remove-tree-fix/repro-after-fix-wsl.txt`):

```
1. unreadable subfolder: no exception; root still there: True
2. target outside the tree: mode before 0o644, after 0o644
```

- **It could raise,** though its docstring says it never does, and the old `rmtree(ignore_errors=True)` didn't.
- **It could change files outside the tree,** by following a symlink in a read-only folder, or a symlink passed as
  the path. Git doesn't create read-only folders itself, so the first needs one made some other way.
- **It changed modes inside the tree.** What it failed to delete was left at `0200`.

Modes after `remove_tree`, from `remove-tree-fix/repro_modes.py` (`modes-wsl.txt`):

| What | Before | #5's code | The fix |
|---|---|---|---|
| File outside the tree, through a symlink | `0644` | `0200` | `0644` |
| Folder outside the tree, symlink passed as the path | `0755` | `0200` | `0755` |
| The tree itself | `0700` | `0200` | `0700` |
| Read-only folder in the tree | `0500` | `0200` | `0500` |
| File in that folder | `0644` | `0200` | `0644` |

It now retries only `os.unlink` and `os.rmdir`, only on `PermissionError`, only on Windows, and never on a symlink
or junction. Windows still deletes read-only files and folders, but a read-only symlink or junction now stays.

**Each condition has a test.** `remove-tree-fix/mutants-wsl.txt` and `mutants-windows.txt` remove them one at a time:

| Removed | WSL | Windows |
|---|---|---|
| Nothing | 10 passed, 2 skipped | 8 passed, 4 skipped |
| `_WINDOWS` check | 1 failed | passes (POSIX tests skip) |
| Which-call check | 1 failed | 1 failed |
| `PermissionError` check | 1 failed | 1 failed |
| Symlink check | 1 failed | passes (no symlink privilege) |
| Junction check | passes (no junctions) | 1 failed |
| All of them: #5's code | 5 failed | 3 failed |

On #5's code, 2 of those failures in each environment are only because the new `_WINDOWS` switch doesn't exist there.
The other 3 in WSL and 1 on Windows fail on real behaviour.

**A wrong first run:** at `e436e4c` (never pushed) the WSL suites showed 7 warnings. They came from temp folders my
earlier runs on the unfixed code had left at `0200`, which pytest couldn't remove. I made the tests restore modes even
when they fail, amended to `521a33d`, cleared those folders and re-ran everything. All numbers above are from
`521a33d`, and they show no warnings.

**Not run for this fix:** a real symlink on Windows (this account can't create one, so that check runs only on Linux
with `_WINDOWS` forced on), and Python 3.13 or later (everything ran on 3.12; from 3.13, `os.chmod` on Windows follows
links by default). CI runs on Linux, so the two Windows-only tests ran only on the Windows box.

### The two follow-up branches

Both are local and not pushed. Both were cut from the first-pass branch, so a plain merge conflicts. Each is one
commit, and a trial replay of each onto `521a33d` applied cleanly (not run after replaying):

- `ci/run-train-tests` (`af58327`): CI runs `train/tests` with tree-sitter.
- `test/neo4j-write-opt-in` (`5f49f11`): the 5 NodeGoat tests need `ORION_LIVE_NEO4J=1`.
