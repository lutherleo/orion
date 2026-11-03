# Sink rules 2026-11-02: results

Commit `3139205` on local branch `fix/sink-rules-popen-log`, cut from `e755f75`. Not pushed.

All measurements were made in WSL (`Ubuntu`) from each fixture's `cpg.bin`, with no Neo4j unless stated.
"Before" is the base commit `e755f75`; "after" is the commit above.

## What changed

- A real call is judged by its name and its text up to the first `(`, not its arguments. Operator calls
  keep their whole text.
- New `Profile.flow_sinks`, flow ranking only: GENERIC `Popen` / `popen` always, and subprocess `run`,
  `call`, `check_call`, `check_output` with `shell=True`, as `code_exec`. EXPRESS `$where`, so it still
  counts inside any query call.
- EXPRESS `log` matches the word, so `/login`, `isLoggedIn` and `dialog` are no longer log sinks.
- `_classify_sink` keeps the old rule for `orion/lm/sinks.py`, so C1's code-only sink lines do not move.

## Flows per category

| Category | NodeGoat (default and lm) | PyGoat default | PyGoat lm |
|---|---|---|---|
| **Total** | **111 → 73** | **98 → 93** | **170 → 161** |
| `code_exec` | 3 → 3 | 9 → 12 | 9 → 12 |
| `deserialize` | – | – | 7 → 7 |
| `sql` | – | 26 → 18 | 26 → 18 |
| `nosql` | 10 → 10 | – | – |
| `ssrf` | – | – | 9 → 9 |
| `template` | – | – | 1 → 1 |
| `prototype_pollution` | – | – | 1 → 1 |
| `path` | – | – | 54 → 50 |
| `redirect` | 8 → 5 | 63 → 63 | 63 → 63 |
| `render` | 30 → 21 | – | – |
| `log` | 60 → 34 | – | – |

`flows/flow-comparison.txt` lists every sink that was added, dropped or re-filed.

New `code_exec` sinks on PyGoat default: `introduction/views.py:430` (`cmd_lab`),
`introduction/mitre.py:233` (`command_out`), `challenge/views.py:50` and `:81`. On PyGoat lm the same four
already existed as `path` rows, through the `open(` hint, and are re-filed.

## Constraint checks

| Check | Result | Evidence |
|---|---|---|
| FLOWS_TO, NodeGoat | 217, identical to the old golden | `goldens-nodegoat-vs-old.txt` |
| FLOWS_TO, PyGoat | 1076 before and after on WSL's CPG, compared by count only | `flows/flow-comparison.txt` |
| Discovery prompts | 120 variants plus the schema byte-identical (SHA-256 `338E4F76…`) | `prompts/` |
| C1 code-only sink lines | Unchanged, pinned by `tests/test_sink_rules.py` | `commit-3139205-code.diff` |
| NodeGoat goldens | Re-recorded and match; 73 flows, a strict subset of the old 111 | `goldens-state-after-rerecord.txt` |
| Live NodeGoat build and `orion reanalyze` on Neo4j | 1 passed | `tests/wsl-live-nodegoat-reanalyze.txt` |

## Tests at this commit

| Suite | Windows | WSL |
|---|---|---|
| `pytest -m "not slow"` | 1581 passed, 42 skipped | 1587 passed, 32 skipped, 4 failed |
| `train/tests` + `eval/tests` | 804 passed, 7 skipped, 58 failed | 823 passed, 3 skipped, 43 failed |

None of these failures comes from this commit:

- The train and eval failures are the same tests on the base commit, in both environments (`tests/*-base-commit.txt`).
- The 4 WSL failures fail the same way on the base commit, because a fresh checkout has no `fixtures/NodeGoat` (`tests/wsl-4-failures-on-base-commit.txt`).

The two `*-not-slow-tail.txt` files hold only the end of each run's output; the rest was not kept.
The work on these failures is in `../test-suites/`.

## Not run

- **PyGoat goldens:** not re-recorded. They depend on the Joern build, and WSL's differs from the Mac's (FLOWS_TO 1076 vs 1075). Their slow tests fail until these run on the Mac:
  `python -m tests.test_graph_snapshot pygoat default` and `python -m tests.test_graph_snapshot pygoat lm`
- **Effect on what scans find:** not measured. That needs `bench/prove.py` on the Mac, which spends tokens.

## Files

| Path | What it is |
|---|---|
| `flows/before/`, `flows/after/` | Every flow per fixture and variant, as JSON |
| `flows/flow-comparison.txt` | Per-category counts, then each sink added, dropped or re-filed |
| `goldens-state-after-rerecord.txt` | All four goldens compared with this box, after the NodeGoat re-record |
| `goldens-nodegoat-vs-old.txt` | Re-recorded NodeGoat goldens against the old ones |
| `prompts/` | Every rendered discovery prompt from both commits |
| `tests/` | Test output, including the base-commit comparison runs |
| `commit-3139205-code.diff`, `commit-3139205-stat.txt` | The change, without the golden JSON in the diff |
| `scripts/` | The scripts that produced all of the above (`try_rules.py` is the earlier prototype) |
