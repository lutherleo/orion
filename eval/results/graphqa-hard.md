Graph-as-memory Q&A pilot -- 24 questions (status: proposed; dev evidence, AI-written templates). Failed sessions score 0; `--` = not reported.

| model | path | n | failed | P | R | F1 | exact | F1 (completed) | tokens | $ CLI | tokens / exact | $ / exact | wall s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| claude-sonnet-5 | graph | 24 | 8 (6 unfinished) | 0.66 | 0.63 | 0.64 | 11/24 | 0.97 | 15,646,959 | 13.62 | 1,422,451 | 1.238 | 6269 |
| claude-sonnet-5 | grep | 24 | 8 (6 unfinished) | 0.61 | 0.62 | 0.62 | 13/24 | 0.92 | 19,005,847 | 13.16 | 1,461,988 | 1.012 | 4440 |

Per family: F1 / exact (of n).

| model | path | chain | dead | hot | pair | reach_down | reach_up |
|---|---|---|---|---|---|---|---|
| claude-sonnet-5 | graph | 0.98 / 3 of 4 | 0.25 / 1 of 4 | 0.46 / 1 of 4 | 0.50 / 2 of 4 | 0.93 / 1 of 4 | 0.75 / 3 of 4 |
| claude-sonnet-5 | grep | 1.00 / 4 of 4 | 0.45 / 1 of 4 | 0.25 / 1 of 4 | 0.50 / 2 of 4 | 0.99 / 3 of 4 | 0.50 / 2 of 4 |

Gold-check drops while generating: -- candidates (none).
Total usage: 65 claude -p sessions (incl. retried / aborted attempts), 34,652,806 tokens (0 sessions without a usage report), $26.78 as reported by the CLI (0 without a cost).

The plain Cypher query alone (no model): 10 of 24 exact, mean F1 0.706.

Exact answers, split by whether that query is right on the question.

| model | path | query right | query wrong |
|---|---|---|---|
| claude-sonnet-5 | graph | 6 of 10 | 5 of 14 |
| claude-sonnet-5 | grep | 7 of 10 | 6 of 14 |
