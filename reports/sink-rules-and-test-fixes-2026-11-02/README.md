# Sink rules and test fixes, 2026-11-02: results

The write-ups are two private Claude docs: [Orion Sink Rules and Test Fixes Report](https://claude.ai/artifact/QFQf19ZrfmazXszW7SB6rZ), and [Orion Test-Suite Fixes Report](https://claude.ai/artifact/JNYiuh8d72shbCVb7b9wK6) for the test-fix detail. This folder holds the raw results behind them. It is not committed, so it would go if this worktree is removed.

| Folder | What it holds | Start at |
|---|---|---|
| `sink-rules/` | Flows before and after, goldens, rendered prompts, the diff of `3139205`, test output, scripts | `sink-rules/README.md` |
| `test-suites/` | Test-fix session: raw pytest and junit output per commit, bug reproductions | `test-suites/README.md` |
| `both-branches-together/` | The sink branch merged with PR #6's `521a33d` on a throwaway copy (tree `221a887`) | The table below |

## `both-branches-together/`

| Suite (`-m "not slow"`) | WSL | Windows |
|---|---|---|
| `tests/` | 0 failed, 1626 passed, 36 skipped | 0 failed, 1618 passed, 44 skipped |
| `train/tests eval/tests` | 0 failed, 851 passed, 46 skipped | 0 failed, 841 passed, 56 skipped |

- `wsl-*` and `win-*` are the runs, a `.txt` and a junit `.xml` each. `orion` is `tests/`; `te` is `train/tests eval/tests`.
- `wsl-flows-summary.txt` and `wsl-flows-vs-sink-branch.txt`: flow counts on the merged copy, identical to the sink branch.
- `wsl-goldens.txt`: both NodeGoat goldens match. The PyGoat goldens differ until they are re-recorded on the Mac.
- `prompts-pr6-vs-merged-sha256.txt`: discovery prompts with and without the sink rules, byte-identical.
- `earlier-run-e5601ad-overlapping/`: an earlier copy, made before `64a8cb6` and PR #6, with WSL and Windows running against one Neo4j at once. It shows the `test_scan_record_is_invisible_to_get_schema` failure that did not come back, and the `test_mine` failure later fixed.
