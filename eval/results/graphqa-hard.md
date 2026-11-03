Graph-as-memory Q&A pilot -- 24 questions (status: proposed; dev evidence, AI-written templates). Failed sessions score 0; `--` = not reported.

| model | path | n | failed | P | R | F1 | exact | F1 (completed) | tokens | $ CLI | tokens / exact | $ / exact | wall s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| claude-sonnet-5 | graph | 24 | 2 | 0.88 | 0.86 | 0.86 | 16/24 | 0.94 | 20,782,548 | 18.34 | 1,298,909 | 1.146 | 8568 |
| claude-sonnet-5 | grep | 24 | 2 | 0.81 | 0.82 | 0.81 | 17/24 | 0.88 | 28,295,185 | 20.15 | 1,664,423 | 1.185 | 6212 |

Per family: F1 / exact (of n).

| model | path | chain | dead | hot | pair | reach_down | reach_up |
|---|---|---|---|---|---|---|---|
| claude-sonnet-5 | graph | 0.98 / 3 of 4 | 0.63 / 2 of 4 | 0.88 / 2 of 4 | 1.00 / 4 of 4 | 0.93 / 2 of 4 | 0.75 / 3 of 4 |
| claude-sonnet-5 | grep | 1.00 / 4 of 4 | 0.90 / 2 of 4 | 0.71 / 2 of 4 | 0.75 / 3 of 4 | 1.00 / 4 of 4 | 0.50 / 2 of 4 |

Gold-check drops while generating: -- candidates (none).
Total usage: 91 claude -p sessions (incl. retried / aborted attempts), 49,077,733 tokens (0 sessions without a usage report), $38.49 as reported by the CLI (0 without a cost).

The plain Cypher query alone (no model): 10 of 24 exact, mean F1 0.706.

Exact answers, split by whether that query is right on the question.

| model | path | query right | query wrong |
|---|---|---|---|
| claude-sonnet-5 | graph | 9 of 10 | 7 of 14 |
| claude-sonnet-5 | grep | 9 of 10 | 8 of 14 |
