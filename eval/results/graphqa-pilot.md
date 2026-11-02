Graph-as-memory Q&A pilot -- 20 questions (status: proposed; dev evidence, AI-written templates). Failed sessions score 0; `--` = not reported.

| model | path | n | failed | P | R | F1 | exact | F1 (completed) | tokens | $ CLI | tokens / exact | $ / exact | wall s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| claude-sonnet-5 | graph | 20 | 0 | 1.00 | 1.00 | 1.00 | 20/20 | 1.00 | 2,094,049 | 1.65 | 104,702 | 0.083 | 754 |
| claude-sonnet-5 | grep | 20 | 0 | 1.00 | 1.00 | 1.00 | 20/20 | 1.00 | 1,336,045 | 1.41 | 66,802 | 0.070 | 386 |
| claude-opus-5 | graph | 20 | 0 | 1.00 | 1.00 | 1.00 | 20/20 | 1.00 | 2,178,926 | 5.65 | 108,946 | 0.282 | 1138 |
| claude-opus-5 | grep | 20 | 0 | 1.00 | 1.00 | 1.00 | 20/20 | 1.00 | 1,394,374 | 3.32 | 69,719 | 0.166 | 398 |

Per family: F1 / exact (of n).

| model | path | lookup | calls | entry_sink | flow_path | deps | routes_noauth |
|---|---|---|---|---|---|---|---|
| claude-sonnet-5 | graph | 1.00 / 4 of 4 | 1.00 / 4 of 4 | 1.00 / 3 of 3 | 1.00 / 1 of 1 | 1.00 / 4 of 4 | 1.00 / 4 of 4 |
| claude-sonnet-5 | grep | 1.00 / 4 of 4 | 1.00 / 4 of 4 | 1.00 / 3 of 3 | 1.00 / 1 of 1 | 1.00 / 4 of 4 | 1.00 / 4 of 4 |
| claude-opus-5 | graph | 1.00 / 4 of 4 | 1.00 / 4 of 4 | 1.00 / 3 of 3 | 1.00 / 1 of 1 | 1.00 / 4 of 4 | 1.00 / 4 of 4 |
| claude-opus-5 | grep | 1.00 / 4 of 4 | 1.00 / 4 of 4 | 1.00 / 3 of 3 | 1.00 / 1 of 1 | 1.00 / 4 of 4 | 1.00 / 4 of 4 |

Gold-check drops while generating: 26 candidates (calls 2, entry_sink 2, flow_path 22).
Total usage: 100 claude -p sessions (incl. retried / aborted attempts), 11,978,328 tokens (0 sessions without a usage report), $19.91 as reported by the CLI (0 without a cost).
Of that, 20 sessions (4,974,934 tokens, $7.87) answered an earlier rev of a question that was reworded or replaced since; they are not scored above.
