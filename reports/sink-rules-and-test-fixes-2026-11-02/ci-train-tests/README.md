# CI now runs `train/tests`: the change, the results, what is still unproven

**Commit `af58327` on branch `ci/run-train-tests` adds `train/tests` to CI, with tree-sitter installed so the
Stage 1 and Stage 2 tests really run.** The real GitHub run passed, with **0 failed, 315 passed, 3 skipped** for
that suite ([run 41540396345](https://github.com/lutherleo/orion/actions/runs/41540396345)). A clean local venv
gave the same counts before the push.

## Why

`train/tests` never ran in CI, so a stale stub in `train/tests/test_stage2.py` went unnoticed from `3efca05`
until 2026-11-02, when 17 tests were found failing. The stub itself was fixed on `fix/test-harness-skips`
(see `../test-suites/README.md` in that branch's worktree). This commit closes the gap that hid it.

## The change

One file, 7 added lines, in the `unit` job of `.github/workflows/tests.yml`. The `neo4j` job is unchanged.

```yaml
      - run: pip install -e ".[dev]" numpy
      - run: pytest -m "not slow" -q
      - run: pytest eval/tests -q
      # Stage 1 and Stage 2 skip without tree-sitter. These are the pins from the `lm` extra in pyproject.toml,
      # only these five: the rest of that extra is model runtime (47 packages, and no MLX on Linux anyway).
      # Installed after the steps above so they keep running without it.
      - run: >-
          pip install tree-sitter==0.26.0 tree-sitter-javascript==0.25.0 tree-sitter-typescript==0.23.2
          tree-sitter-python==0.25.0 tree-sitter-java==0.23.5
      - run: pytest train/tests -m "not slow" -q
```

## Results

The first column is the real run of this commit. The second is a local re-run of the `unit` job from a clean
clone, described under "How the local re-run worked". The third is the real run of the base commit, before this
change ([run 41398527518](https://github.com/lutherleo/orion/actions/runs/41398527518)).

| Step | GitHub on `af58327` | Local on `af58327` | GitHub on `64a8cb6` |
|---|---|---|---|
| `pytest -m "not slow" -q` | 1487 passed, 75 skipped | 1487 passed, 75 skipped | 1487 passed, 75 skipped |
| `pytest eval/tests -q` | 550 passed, 3 skipped | 550 passed, 3 skipped | 550 passed, 3 skipped |
| `pytest train/tests -m "not slow" -q` | **315 passed, 3 skipped** | 315 passed, 3 skipped | not in that workflow |

No step failed anywhere. Every count matches, so the existing steps did not change and the local re-run was a
fair stand-in for the runner. The new test step took 3.53 s on GitHub. The per-file and test-by-test tables below
come from the local re-run, because the GitHub log only gives totals. `-m "not slow"` deselects nothing in
`train/tests` today.

### What tree-sitter changes in `train/tests`

| Test file | Dev install only (no tree-sitter) | As the workflow runs it |
|---|---|---|
| `test_stage1_cases` | 0 passed, 24 skipped | **24 passed** |
| `test_stage2` | 12 passed, 18 skipped | **30 passed** |
| `test_stage1_build` | 3 passed, 5 skipped | **8 passed** |
| `test_convert` | 2 passed, 2 skipped | same: needs `mlx` |
| `test_unsloth_data` | 7 passed, 1 skipped | same: needs `transformers` |
| The other 20 files | 244 passed | same |
| **Total (318 tests)** | **268 passed, 50 skipped** | **315 passed, 3 skipped** |

### Test by test

| Compared | Outcome changes |
|---|---|
| `tests/` on `64a8cb6` and on `af58327` (1562 tests) | none |
| `eval/tests` on `64a8cb6` and on `af58327` (553 tests) | none |
| `train/tests` without and with tree-sitter (318 tests) | 47 skipped tests now pass, nothing else moves |

These numbers agree with the earlier report. It had `train/tests eval/tests` with tree-sitter at 867 passed,
4 skipped. Adding this run's two suites with tree-sitter gives 866 passed, 5 skipped, the same 871 tests. The
one-test gap fits `transformers`, which that report's venv had and this clean one does not.

## Dependencies a clean runner needs

| Dependency | Why | Status in the workflow |
|---|---|---|
| The five tree-sitter packages | `orion/lm/codeindex.py:57` indexes code with them | New install step |
| `numpy` | `train/mlx_train.py:24` imports it at module level, so `test_mlx_train` needs it to load | Already installed |
| `PyYAML` | `train/sources.py:383`, used by `test_sources` | **Not declared.** It arrives through `fastmcp-slim` and `jsonschema-path` |
| `git` | `train/tests/_git.py` builds real repos | On the runner image. No git identity is needed: the re-run had none |
| `sh`, `ps` | One process-group test, `train/tests/test_build.py:222` | Passed locally, not checked separately on the runner |
| `mlx` | 2 `test_convert` merge tests | Optional, skips |
| `transformers` | 1 `test_unsloth_data` test | Optional, skips |

If a dependency change ever removed PyYAML, `test_sources` should fail rather than skip, because nothing guards
that import. That is from reading the code, not from a test.

**The whole `lm` extra is not the right install.** It does resolve on Linux: a dry run on Python 3.12 picked
47 packages for `mlx-lm` and `outlines`, including `transformers` 5.22.1 and `outlines_core` 0.2.14, and `mlx`
was not among them. So it adds weight without adding the MLX tests. It would also start the tests that skip
without `transformers`, which I did not try.

## How the local re-run worked

All of this ran in WSL. Scripts are in `scripts/`.

| Part | What it did |
|---|---|
| Code | `git clone --depth 1` of the branch, as `actions/checkout` does, so no `fixtures/` |
| Python | A new 3.12.14 venv from `uv`, containing only pip |
| Steps | Read from that commit's own `tests.yml` with a YAML parser, so what ran is what the file says |
| Shell | `bash --noprofile --norc -eo pipefail`. GitHub uses `bash -e` for a plain `run:`. None of these steps has a pipe, so the difference does not matter |
| `HOME` | An empty temp folder: no git identity, no Joern, no `claude` |
| `PATH` | The venv and system folders only |
| `NEO4J_URI` | A dead port, because the `unit` job has no Neo4j and this machine may have one |
| `PYTEST_ADDOPTS` | `--junitxml` and `-rs`: extra output only, the same tests run |

`actionlint` 1.7.12 finds 0 errors in the workflow. Its `shellcheck` and `pyflakes` rules were off because those
tools are not installed here.

## Choices you may want to change

| Choice | Why | The catch | Alternative |
|---|---|---|---|
| Install tree-sitter after the `tests/` and `eval/tests` steps | Keeps those two steps exactly as they were, as asked | 19 tree-sitter tests in those suites keep skipping in CI | Move the install up. Measured: 18 in `tests/` and 1 in `eval/tests` go from skipped to passed, 0 failures |
| New steps in the existing `unit` job | Smallest change, and it is what was asked | GitHub stops at the first failed step, so a failure in `tests/` hides the `train/tests` result for that run | A separate `train` job, about 8 more lines |
| Pins written out in the workflow | No change outside the workflow | They are now in two places, and bumping the `lm` extra will not update CI | A small tree-sitter extra in `pyproject.toml` that `lm` includes, then `pip install -e ".[dev,<name>]"` |

## Not verified

- **The `mlx` and `transformers` tests.** CI still will not run those 3, so CI does not guard them.
- **Skip reasons on GitHub.** The run's log gives totals only, so the 3 skips there are assumed to be the same 3
  as locally. The totals match.

The GitHub run settled the earlier gaps: the two new steps ran on Ubuntu 24.04 with CPython 3.12.15 (the local
re-run used Ubuntu 26.04 with 3.12.14), and the tree-sitter wheels installed there. The `neo4j` job is unchanged
and passed again with 1523 passed, 39 skipped, the same as at `64a8cb6`.

## Where things stand

| Item | State |
|---|---|
| `ci/run-train-tests` | On `origin` at `af58327`, one commit on top of `64a8cb6`. CI passed. No PR |
| `fix/test-harness-skips` | On `origin` at `64a8cb6`, CI passed, no PR |
| `main` | Was `e755f75` when last checked, without these commits, so `train/tests` still fails there until they land |
| This report | Untracked in the `jovial-perlman-ad36ae` worktree, not part of the commit |
| The commit | No AI attribution, as `CLAUDE.md` requires |

Merging `ci/run-train-tests` brings the two `fix/test-harness-skips` commits with it.

## Raw files

In `raw/`. "base" is `64a8cb6`, "new" is `af58327`.

| File | What it is |
|---|---|
| `base.log`, `new.log` | Full output of each re-run, ending with `pip freeze` and a summary |
| `base-step_02.xml`, `base-step_03.xml` | JUnit for `tests/` and `eval/tests` on the base commit |
| `new-step_02.xml`, `new-step_03.xml`, `new-step_05.xml` | JUnit for `tests/`, `eval/tests` and `train/tests` on the new commit |
| `diag.log`, `diag-notree-train.xml`, `diag-tree-tests.xml` | Not workflow steps: `train/tests` without tree-sitter, `tests/` with it |
| `new-diag_3.xml` | Not a workflow step: `eval/tests` with tree-sitter |
| `base-diag_1.xml`, `new-diag_1.xml` | **Empty, ignore them.** See below |
| `comparisons.txt`, `skip-reasons.txt` | Test-by-test comparisons, and why each test skips |
| `lm-extra-dry-run.txt`, `actionlint.txt` | The `lm` extra dry run and the lint output |
| `github-run-41540396345.txt` | Key lines from the real GitHub run of `af58327` |
| `github-run-41398527518.txt` | Key lines from the real GitHub run of `64a8cb6` |

**One mistake in these logs.** PowerShell stripped the quotes from the `-m "not slow"` extra commands I passed to
the first two re-runs, so `diag_1` and `diag_2` in `base.log` and `new.log` errored at once (exit 4 and 127).
Those commands were extras, not workflow steps, so no workflow result is affected. I re-ran them properly from a
script, and that output is `diag.log`.

To repeat the re-run, from a WSL shell in this folder (not through PowerShell, which strips the quotes):

```bash
bash scripts/ci_replay.sh ci/run-train-tests new /tmp/replay-out
```
