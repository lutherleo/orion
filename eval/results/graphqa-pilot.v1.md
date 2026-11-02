Graph-as-memory Q&A pilot -- 22 questions (status: proposed; dev evidence, AI-written templates). Failed sessions score 0; `--` = not reported.

| model | path | n | failed | P | R | F1 | exact | F1 (completed) | tokens | $ CLI | tokens / exact | $ / exact | wall s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| claude-sonnet-5 | graph | 22 | 0 | 0.92 | 1.00 | 0.94 | 19/22 | 0.94 | 3,726,370 | 3.13 | 196,125 | 0.165 | 1431 |
| claude-sonnet-5 | grep | 22 | 0 | 0.91 | 0.95 | 0.92 | 19/22 | 0.92 | 1,845,918 | 1.88 | 97,154 | 0.099 | 791 |
| claude-opus-5 | graph | 22 | 0 | 0.98 | 1.00 | 0.99 | 21/22 | 0.99 | 2,916,610 | 7.31 | 138,886 | 0.348 | 1246 |
| claude-opus-5 | grep | 22 | 0 | 0.91 | 1.00 | 0.93 | 19/22 | 0.93 | 1,956,967 | 4.64 | 102,998 | 0.244 | 728 |

Per family: F1 / exact (of n).

| model | path | lookup | calls | entry_sink | flow_path | deps | routes_noauth |
|---|---|---|---|---|---|---|---|
| claude-sonnet-5 | graph | 1.00 / 4 of 4 | 1.00 / 4 of 4 | 0.92 / 2 of 3 | 0.63 / 1 of 3 | 1.00 / 4 of 4 | 1.00 / 4 of 4 |
| claude-sonnet-5 | grep | 1.00 / 4 of 4 | 1.00 / 4 of 4 | 0.92 / 2 of 3 | 0.50 / 1 of 3 | 1.00 / 4 of 4 | 1.00 / 4 of 4 |
| claude-opus-5 | graph | 1.00 / 4 of 4 | 1.00 / 4 of 4 | 0.92 / 2 of 3 | 1.00 / 3 of 3 | 1.00 / 4 of 4 | 1.00 / 4 of 4 |
| claude-opus-5 | grep | 1.00 / 4 of 4 | 1.00 / 4 of 4 | 0.92 / 2 of 3 | 0.57 / 1 of 3 | 1.00 / 4 of 4 | 1.00 / 4 of 4 |

Gold-check drops while generating: 8 candidates (calls 2, entry_sink 2, flow_path 4).
Total usage: 88 claude -p sessions (incl. retried / aborted attempts), 10,445,865 tokens (0 sessions without a usage report), $16.97 as reported by the CLI (0 without a cost).
