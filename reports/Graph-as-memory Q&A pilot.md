# Graph-as-memory Q&A pilot

Does Claude answer questions about code structure better through Orion's graph than by reading the source? Built and
run 2026-10-31 to 2026-11-02 (`eval/graphqa.py`, `eval/graphqa_hard.py`). **This is dev evidence only:** the questions
are AI-written and stay `"status": "proposed"` until a human reviews them, and each cell is one session.

## The answer

**No sign that the graph makes answers more accurate. On the harder questions it matched reading the source, cost about
the same and took 1.4x as long. It was clearly cheaper only on whole-repo counts.** This is the finished Sonnet run: all
24 black and poetry questions, both paths. NodeGoat, PyGoat and Opus were not run on this set.

| 24 questions, black and poetry, Sonnet | graph path | grep path |
|---|---|---|
| Exact answers | 16 | 17 |
| Ran out of turns, no answer | 2 | 2 |
| Mean F1 (no answer = 0) | 0.86 | 0.81 |
| Tokens | 20,435,272 | 26,227,694 |
| Cost, CLI-reported | $17.91 | $18.78 |
| Wall time | 8,568 s | 6,212 s |

- **The graph's higher F1 isn't real.** Two grep sessions searched an empty folder and answered nothing (see
  "Hand-checks"). Without those two questions, grep leads: 17 exact of 22 against 14, mean F1 0.885 against 0.850.
- **The graph's saving comes entirely from the `hot` questions.** Those 4 need a whole-repo count, and the graph used 0.39x
  grep's tokens and 0.59x its cost. On the other 20, the graph used 1.13x the tokens and 1.21x the cost.
- **One or two answers either way is noise.** Each cell is one session, so this supports "no clear advantage", not "grep
  is better".
- **The easier sets couldn't separate the paths.** On the earlier 20-question set, both paths and both models were exact
  on all 20, and the graph used about 1.56x the tokens.

**The clearest finding cost no model tokens.** A plain `orion scan` of a `src/`-layout Python repo saw only 0.51 (black)
and 0.75 (poetry) of the calls in the source. Scanning from `src/` gave 0.92 and 0.89. [PR #7](https://github.com/lutherleo/orion/pull/7)
proposes the fix and is still open.

## Terms

- **Graph path:** the agent has only Orion's three read-only tools (`run_cypher`, `semantic_search`, `get_schema`). It
  can't read source files.
- **Grep path:** the agent has only `Read`, `Grep` and `Glob`, on a clean export of the same commit.
- **Gold:** the answer a session is scored against.
- **Oracle:** the program that computes gold from the source, without reading the graph.
- **Plain query:** the graph's own answer from one fixed Cypher query, with no model involved.
- **Exact:** the answer set equals gold. **F1** is the usual balance of precision and recall on that set.
- **Round trip:** one model request. It may contain several tool calls.

## Harder set: setup

The first two sets took their gold from the graph itself and dropped any question where the graph failed a source check,
so they could only say whether the graph helps where it is already right. The harder set removes that bias.

| | |
|---|---|
| Gold | From the source: a Python `ast` call graph (a lexical pass for JS) following a rule each question states in full. The plain query's answer is stored as `reference` and only labels the question |
| Fixtures | NodeGoat, PyGoat, black @ f03ee11 (scan `264f3f2f...`), poetry @ 21ffd99 (scan `18c849f2...`). Black and poetry are scanned from `src/` |
| Why black and poetry | Call chains in the source are 1 hop on PyGoat and 2 on NodeGoat, but up to 10 on poetry |
| Families | `reach_down`, `reach_up`, `chain`, `hot` (highest fan-in or fan-out), `dead` (never mentioned elsewhere), `pair` (X and Y, X but not Y) |
| Questions | 40: NodeGoat 10, PyGoat 6, black 12, poetry 12. The plain query alone is exact on 15 |
| Model | `claude-sonnet-5`, effort xhigh, 900 s per session, one session per question and path |
| Turn limit | Hard stop at 30 round trips. Agents are told 25 |
| Run scope | Black and poetry only (24 questions, 48 sessions), because the other two can't separate the paths |

The call rule is deliberately syntactic. `f(...)`, `module.f(...)`, `Class.f(...)`, `self.f(...)`, `cls.f(...)` and
`super().f(...)` count. A call on any other object, creating an instance, a decorator and a function passed as a value
do not. So the precision figures below measure disagreement with that rule, not with what runs: many extra graph edges
are calls on objects bound by name, and some of those may be right at runtime.

## Harder set: results

### By family

| family | questions | exact, graph | exact, grep | tokens, graph | tokens, grep | $, graph | $, grep |
|---|---|---|---|---|---|---|---|
| `chain` | 4 | 3 | 4 | 2,348,901 | 1,774,667 | 2.91 | 1.52 |
| `reach_down` | 4 | 2 | 4 | 3,250,890 | 1,032,048 | 2.21 | 1.04 |
| `reach_up` | 4 | 3 | 2 | 2,415,157 | 3,348,542 | 1.92 | 1.73 |
| `hot` | 4 | 2 | 2 | 4,809,094 | 12,389,267 | 4.52 | 7.70 |
| `dead` | 4 | 2 | 2 | 7,017,012 | 6,234,158 | 5.57 | 5.76 |
| `pair` | 4 | 4 | 3 | 594,218 | 1,449,012 | 0.78 | 1.01 |

By repo, exact answers were: black (12 questions) graph 9, grep 10; poetry (12) graph 7, grep 7. The graph's leads in
`reach_up` and `pair` are each one session wide, and in both the grep miss is an empty-folder session, so neither is firm.
Grep's leads in `chain` and `reach_down` are real misses by graph agents.

These totals use each question's final session. Attempts cut off by the usage limit ($1.80 across both paths) are left out
here because when the limit hit has nothing to do with the path. They count in "Usage spent" and in
`eval/results/graphqa-hard.md`, which shows 20,782,548 tokens and $18.34 for graph, 28,295,185 and $20.15 for grep.

### Does the graph help when its plain query is wrong?

| 24 questions | graph path, exact | grep path, exact |
|---|---|---|
| Plain query exact (10) | 9 | 9 |
| Plain query wrong (14) | 7 | 8 |

The plain query alone is exact on 10 of 24, with mean F1 0.71, so agents add real value over it: graph agents got 7 of 14
right where it was wrong. They also lost one question it had right (`poetry-chain-1`). They read no source on this path, so
they repair the graph from its stored call text.

### Every question

"out" means the session hit the 30 round-trip stop and answered nothing, which scores 0.

| question | answer size | plain F1 | graph F1 | round trips | tokens | $ | grep F1 | round trips | tokens | $ |
|---|---|---|---|---|---|---|---|---|---|---|
| `black-chain-1` | 7 | 1.00 | 1.00 | 19 | 905,285 | 0.84 | 1.00 | 20 | 801,815 | 0.56 |
| `black-chain-2` | 11 | 1.00 | 1.00 | 11 | 499,735 | 1.12 | 1.00 | 8 | 274,843 | 0.28 |
| `black-dead-1` | 4 | 0.53 | 1.00 | 23 | 1,367,665 | 1.21 | 1.00 | 15 | 842,970 | 1.16 |
| `black-dead-2` | 17 | 0.45 | out | 30 | 2,308,910 | 1.60 | 0.81 | 26 | 2,339,663 | 1.76 |
| `black-hot-1` | 4 | 0.75 | 0.86 | 28 | 1,628,514 | 1.15 | out | 30 | 4,428,299 | 2.66 |
| `black-hot-2` | 3 | 0.67 | 1.00 | 20 | 1,614,583 | 1.69 | 1.00 | 28 | 4,187,828 | 2.38 |
| `black-pair-1` | 5 | 1.00 | 1.00 | 8 | 234,686 | 0.30 | 1.00 | 7 | 194,959 | 0.20 |
| `black-pair-2` | 8 | 1.00 | 1.00 | 5 | 130,377 | 0.20 | 1.00 | 13 | 439,732 | 0.34 |
| `black-reach_down-1` | 7 | 0.92 | 0.86 | 19 | 696,089 | 0.56 | 1.00 | 12 | 338,151 | 0.27 |
| `black-reach_down-2` | 18 | 0.00 | 1.00 | 23 | 947,358 | 0.65 | 1.00 | 8 | 270,210 | 0.28 |
| `black-reach_up-1` | 6 | 0.27 | 1.00 | 11 | 346,456 | 0.48 | 1.00 | 23 | 781,386 | 0.51 |
| `black-reach_up-2` | 14 | 1.00 | 1.00 | 18 | 523,642 | 0.42 | 1.00 | 23 | 814,708 | 0.38 |
| `poetry-chain-1` | 6 | 1.00 | 0.91 | 13 | 500,673 | 0.49 | 1.00 | 10 | 310,105 | 0.33 |
| `poetry-chain-2` | 5 | 1.00 | 1.00 | 11 | 443,208 | 0.45 | 1.00 | 10 | 387,904 | 0.35 |
| `poetry-dead-1` | 3 | 0.19 | 1.00 | 21 | 979,833 | 0.86 | 1.00 | 18 | 1,224,104 | 0.99 |
| `poetry-dead-2` | 4 | 0.10 | 0.53 | 27 | 2,360,604 | 1.90 | 0.80 | 20 | 1,827,421 | 1.84 |
| `poetry-hot-1` | 3 | 1.00 | 1.00 | 12 | 503,237 | 0.62 | 1.00 | 25 | 928,105 | 0.87 |
| `poetry-hot-2` | 4 | 0.50 | 0.67 | 20 | 1,062,760 | 1.06 | 0.86 | 26 | 2,845,035 | 1.79 |
| `poetry-pair-1` | 3 | 1.00 | 1.00 | 4 | 69,697 | 0.06 | 0.00 | 4 | 76,998 | 0.04 |
| `poetry-pair-2` | 15 | 1.00 | 1.00 | 6 | 159,458 | 0.22 | 1.00 | 19 | 737,323 | 0.44 |
| `poetry-reach_down-1` | 9 | 0.73 | 0.88 | 18 | 558,400 | 0.40 | 1.00 | 4 | 118,710 | 0.21 |
| `poetry-reach_down-2` | 10 | 0.41 | 1.00 | 24 | 1,049,043 | 0.60 | 1.00 | 10 | 304,977 | 0.29 |
| `poetry-reach_up-1` | 4 | 0.50 | 1.00 | 17 | 382,239 | 0.28 | 0.00 | 4 | 76,855 | 0.04 |
| `poetry-reach_up-2` | 19 | 0.91 | out | 30 | 1,162,820 | 0.73 | out | 30 | 1,675,593 | 0.80 |

Round trips are counted from session transcripts; everything else comes from the CLI. Mean round trips were 17.4 for graph
and 16.4 for grep. Grep made more tool calls per round trip (1.80 against 1.33), mostly by reading many files at once on
the `hot` and `dead` questions. Graph's first round trip goes on loading its tools, and the limit counts it.

### Hand-checks

I checked every non-exact answer against the source. At least one path matched gold exactly on 19 of the 24 questions,
which is independent support for the oracle, since neither path sees it.

| question | path | difference | verdict |
|---|---|---|---|
| `black-reach_down-2` | both | added `visit` | **Gold was wrong, now corrected.** The oracle skipped subscripted base classes (`class LineGenerator(Visitor[Line])`). Both paths are now exact |
| `poetry-reach_up-1` | grep | empty answer after 11.8 s | **A session error, not a limit of grep.** It called `Grep` with no path, which searched its empty working folder. It got "No matches found" twice and answered `[]`. Scored 0 as run |
| `poetry-pair-1` | grep | empty answer after 13.0 s | **The same error.** Four `Grep` calls with no path found nothing, and it concluded neither name exists. Scored 0 as run. `calculate_operations` is on 8 lines of the repo |
| `poetry-dead-2` | both | both added `get_python_implementation`, `get_version_info` | **A wording gap; gold left as is.** Each name is on 3 `def` lines and nowhere else. "Appears on no other line of code" excludes them, but the question's list of mentions (a call, an import, a plain reference) doesn't name another definition, and both paths read it the other way |
| `poetry-dead-2` | graph | also added `pip`, `__del__`, `__eq__`, `__post_init__`, `__repr__` | `pip` is a real error: the name is on 31 lines. The dunders are the same wording gap as `black-dead-2`: `__del__` is on one line only |
| `poetry-hot-2` | graph | missed `_display_packages_information`, `create_venv` | Real misses by the stated rule: 8 and 9 callees against a threshold of 8. The plain query had both, among 8 wrong names |
| `poetry-hot-2` | grep | missed `clone` | Real miss by the stated rule: `Git.clone` has 8 callees |
| `black-reach_down-1` | graph | missed `has_triple_quotes`, added `is_valid_index` | Real miss: `black/trans.py:1172` calls it. `is_valid_index` is a closure held in a local variable, so the rule excludes it |
| `black-hot-1` | graph | missed `TErr` | Real miss: the oracle counts 9 callers against a threshold of 8 |
| `black-dead-2` | grep | 8 extra names | Mostly real. `visit_Expr`, `visit_simple_stmt` and `visit_suite` are each defined twice, so their names do appear on another line. **A wording gap too:** the question doesn't exclude dunders, and `__bool__` and `__contains__` appear on one line only. It wouldn't be exact either way |
| `poetry-reach_down-1` | graph | missed `_execute`, `_filter_operations` | Real miss: `installer.py:330` and `:340`. The plain query had both |
| `poetry-chain-1` | graph | missed `_from_sdist_file` | Real miss: `info.py:518`. The plain query was exact |
| `black-dead-2` graph, `black-hot-1` grep, `poetry-reach_up-2` both | | ran out of turns | No answer. Telling agents the limit (25) did not make them stop in time |

**Gold changed once after answers came in.** The oracle fix changed one of the 40 golds, adding `visit` to
`black-reach_down-2`, which gives each path one more exact answer. The old value is kept in that question's `gold_before`.
The question stays in the set because it had already been asked, though asked fresh it would be skipped, as `visit_default`
then names two functions. The fix also moves black's edge count below.

**The empty-folder problem is wide.** 19 of the 24 grep sessions made at least one search with no path (47 searches in all).
17 of them recovered and 2 ended in an empty answer. The grep prompt names the repo folder but doesn't say to pass it. I
kept the prompt unchanged for the last 12 sessions so all 24 ran the same way, and it needs fixing before this set is used
for a final number.

**The `dead` wording needs fixing too.** It should say whether another definition of the same name counts as a mention, and
that dunders are out. Both gaps lowered scores on `black-dead-2` and `poetry-dead-2`.

## Finding: the scan root decides how many calls the graph sees

This compares calls the oracle finds against the graph's `CONTAINS_CALL` then `RESOLVES_TO` edges. It needed Joern and
Neo4j but no model tokens. The clones are sparse, so a repo root holds only the package source.

| repo | scanned from | oracle calls | graph calls | both | recall | precision |
|---|---|---|---|---|---|---|
| black | repo root (plain `orion scan`) | 488 | 299 | 250 | 0.51 | 0.84 |
| black | `src/black` | 488 | 299 | 250 | 0.51 | 0.84 |
| black | `src/` | 488 | 513 | 449 | 0.92 | 0.88 |
| poetry | repo root (plain `orion scan`) | 707 | 800 | 532 | 0.75 | 0.67 |
| poetry | `src/poetry` | 707 | 800 | 532 | 0.75 | 0.67 |
| poetry | `src/` | 707 | 979 | 629 | 0.89 | 0.64 |

- **Cause:** Joern names an imported function by its import path, so an import binds to the real method only when the scan
  root makes that path the file's own.
- **Why it matters beyond this study:** real scans run from the repo root, and fewer resolved calls leave less for
  reachability and pathfind to follow. PR #7 reports FLOWS_TO rising from 3,156 to 3,513 on black and from 9,291 to 9,985
  on poetry. The effect on findings hasn't been measured.
- **Numbers after the oracle fix:** the table predates it. Black from `src/` is now 496 oracle calls, 450 in both, recall
  0.91, precision 0.88. Poetry is unchanged. The black repo-root rows weren't re-measured, because those scans were deleted.
- **Other fixtures:** NodeGoat is recall 0.43, precision 0.95. PyGoat is recall 1.00, precision 0.62.
- **What this means for the study:** the fixtures scan from `src/`, which gives the graph path its best case. The first
  smoke test ran on the old root, and there the graph path finished 0 of 4 questions.

## Smoke tests before the run

Both used Sonnet on the same 4 questions, on both paths.

| | scan root | turn limit | outcome |
|---|---|---|---|
| Smoke 1 | `src/black`, `src/poetry` | 15 | 6 of 8 sessions ran out with no answer. Graph finished 0 of 4 questions, grep 2 of 4 |
| Smoke 2 | `src/` | 30, 25 told | 6 of 8 answered, 5 of them exact. Both paths ran out on `poetry-reach_up-2` |

Smoke 2 used the final questions and settings, so its sessions count in the run. Its fourth question, `nodegoat-hot-1`, was
exact on both paths ($1.63 graph, $0.62 grep) and is outside the 18-question tables.

## The first two sets

Fixtures were NodeGoat @ c5cb68a and PyGoat @ 19d17cc, with Sonnet and Opus, effort xhigh, a 15-turn limit, on both paths.
Six families: lookup, calls, entry_sink, flow_path, deps and routes_noauth. Gold was the graph's own answer, with every
item checked against its source line, and a question dropped when the check failed.

### v2: 20 questions (2026-11-01)

| model | path | F1 | exact | tokens | $ CLI | tokens / exact | wall s |
|---|---|---|---|---|---|---|---|
| sonnet-5 | graph | 1.00 | 20/20 | 2,094,049 | 1.65 | 104,702 | 754 |
| sonnet-5 | grep | 1.00 | 20/20 | 1,336,045 | 1.41 | 66,802 | 386 |
| opus-5 | graph | 1.00 | 20/20 | 2,178,926 | 5.65 | 108,946 | 1138 |
| opus-5 | grep | 1.00 | 20/20 | 1,394,374 | 3.32 | 69,719 | 398 |

Every row is exact on every question, so this set measures cost only. The graph path used 1.57x (Sonnet) and 1.56x (Opus)
the tokens of grep, 1.17x and 1.70x the cost, and 2.0x and 2.9x the wall time.

### v1: 22 questions, 88 sessions (2026-10-31)

| model | path | F1 | exact | tokens | $ CLI | wall s |
|---|---|---|---|---|---|---|
| sonnet-5 | graph | 0.94 | 19/22 | 3,726,370 | 3.13 | 1431 |
| sonnet-5 | grep | 0.92 | 19/22 | 1,845,918 | 1.88 | 791 |
| opus-5 | graph | 0.99 | 21/22 | 2,916,610 | 7.31 | 1246 |
| opus-5 | grep | 0.93 | 19/22 | 1,956,967 | 4.64 | 728 |

The paths differed only on `flow_path`, where gold was Orion's own FLOWS_TO definition. Opus through the graph scored 3 of
3 by querying that same definition, while the grep rows followed callback data, which is arguably right.

### What changed from v1 to v2

1. **`entry_sink` wording.** "A code_exec sink call (`eval`, `exec`, `system`, `spawn`)" read as examples, so every row also
   counted `subprocess.Popen(shell=True)`. With the wording fixed, every row answered as gold did.
2. **`flow_path` callbacks.** FLOWS_TO follows return values only, but NodeGoat's handlers get their data as a DAO callback
   parameter. The check now treats that parameter as the call's result, which drops both v1 NodeGoat flow questions.
3. **`flow_path` false sinks.** The sink line must now hold a call of its kind. The `log` hint had filed route
   registrations such as `app.get("/login", ...)` as log sinks (13 NodeGoat flows), plus `profile.js:82` as render.

Together these took drops from 8 to 26, and NodeGoat has no flow question left. Each drop is a graph gap the source check
caught: calls known only as `<lambda>N`, renders and logs with no graph flow, `res = res.decode()` at `mitre.py:243`,
`name = request.POST.get('name')` at `views.py:150`, and the callback and false-sink cases above.

## Harness problems found along the way

| problem | effect | status |
|---|---|---|
| A session out of turns on an exit-1 CLI was retried as transient | Paid for twice | Fixed |
| A session cut off mid-way by "5-hour limit · resets ..." wasn't recognised as a limit | A re-run could score it as the model's failure | Fixed in `eval/graphqa.py` |
| The error tail can cut any part of that message before "· resets" | One more retry than needed, seen twice on `poetry-hot-1` | Fixed |
| Subscripted base classes skipped by the oracle | One wrong gold | Fixed, see "Hand-checks" |
| Black's sparse clone holds only `black/`, but the export also has `blackd/` and `blib2to3/` | The two paths would see different code | Questions now scope those folders out |
| Oracle errors caught in review while building: JS `this.f` methods, `USER_RE.test(...)` linked to Gruntfile's `test`, names inside f-strings, a "comes from a library" overclaim | Wrong gold or wording | Fixed before any run |
| Grep agents can search their empty working folder | Two empty answers, 47 pathless searches | **Open** |
| `dead` questions don't say how to treat another definition or a dunder | Costs answers on two questions | **Open** |
| 16 usage-limit waits are counted over the whole run, never reset | The run stopped once and had to be started again | Open, harmless: it resumes from the log |

## Usage spent

All CLI-reported, with none missing. Rows are rounded, so the total can differ by a cent.

| Run | Log rows | Tokens | $ CLI |
|---|---|---|---|
| First smoke test (2026-10-31, not in a log) | 2 | 161,187 | 0.20 |
| v1 (2026-10-31) | 88 | 10,445,865 | 16.97 |
| v2 reworded questions (2026-11-01) | 12 | 1,532,463 | 2.94 |
| Harder set, smoke 1 (2026-11-02) | 23 | 9,457,680 | 9.64 |
| Harder set, smoke 2 (2026-11-02) | 8 | 11,999,960 | 8.67 |
| Harder set, run (2026-11-02) | 85 | 39,626,131 | 32.05 |
| **Total** | **218** | **73,223,286** | **70.48** |

A log row is one attempt. In the harder-set run, 42 sessions finished. Of the other 43 rows, 38 were usage-limit waits
that stopped before any model ran, and 5 were real sessions cut off by the limit (2,414,767 tokens, $1.80, included above).
The run hit the 5-hour limit three times. The plan's weekly limit stood at 83% at the end, with other sessions also drawing
on it.

## Caveats

- **Sonnet only, one session per cell.** Opus wasn't run on the harder set. 24 questions can't support a fine comparison:
  a difference of one or two exact answers is noise.
- **Two grep sessions failed for a reason unrelated to the question,** as described above. They are scored as run.
- **AI-written questions, unreviewed.** A human must review them before any result counts.
- **Gold changed after seeing answers,** once, as described above.
- **Two repos.** The Python oracle covers black and poetry here, so nothing is claimed for other languages or larger repos.
- **The graph path is restricted.** It has no source access, whereas a real Orion verifier reads source. This compares
  the graph alone with the source alone, not Orion's full pipeline.
- **Best case for the graph.** The scans start from `src/`, which plain `orion scan` doesn't do until PR #7 lands.
- **Known fixtures.** Black and poetry are public and well known, so models may recall some of their structure.

## Where the rest of the work stands

These statuses are as those sessions reported them; I haven't checked their branches. Nothing below is merged.

| follow-up | status |
|---|---|
| `src/`-layout imports | [PR #7](https://github.com/lutherleo/orion/pull/7), open. PyGoat isn't confirmed at FLOWS_TO 1075 (1076 in WSL, before and after the change) |
| `subprocess.Popen` not a sink; `log` hint false sinks | Commit 3139205 on `fix/sink-rules-popen-log` |
| `exec` and `system` hints matching attribute names | Uncommitted on `claude/gifted-lichterman-4a923f`. Reported: poetry's flows fall from the 300 cap to 62, black's from 16 to 3 |
| `eval/run.py` missing the real limit message | Fixed but uncommitted on `claude/nervous-elbakyan-2e5acf`. No VLB row holding the message is recorded as anything but `aborted`. Three VLB snapshots still need re-running |
| Test gaps on dev boxes | [PR #6](https://github.com/lutherleo/orion/pull/6), open |
| Stream vs legacy FLOWS_TO gap on black | Commit dde5648 on `claude/mystifying-kare-5f755c`, unpushed |
| FLOWS_TO misses callback-delivered data | Not started. This is how every NodeGoat page gets its data |

## Next steps

My recommendation is to stop spending tokens on this question for now. Sonnet showed no accuracy gap to chase, so an Opus
run is unlikely to change the decision and would use a lot of the weekly limit. Land the graph fixes instead.

If the study continues:

1. Have a human review the questions.
2. Fix the grep prompt and the `dead` wording. Both change what a model is shown, so those questions get a new `rev` and
   must be asked again.
3. Land PR #7, so a plain scan gets the numbers measured here.

## How to run

Run from WSL in the Windows checkout, with `PYTHONPATH` set to it and `~/orion/.venv/bin/python`. Run or resume the sessions:

```bash
python -m eval.graphqa_hard run --models claude-sonnet-5 --fixtures black,poetry --jobs 2
```

Score them (token-free):

```bash
python -m eval.graphqa_hard score --fixtures black,poetry
```

- **Other commands:** `edges` and `questions [--build]` are token-free. Don't re-run `questions` on these sets without
  checking the diff: it would drop `black-reach_down-2`, now that its answer has a shared name.
- **Resuming:** `run` skips sessions already logged `ok` or `failed`, so this log has nothing left to run. A usage limit
  waits 30 minutes and re-runs.
- **The first two sets:** `python -m eval.graphqa questions | run | score`.
- **Tests:** `eval/tests/test_graphqa.py` and `eval/tests/test_graphqa_hard.py` (50 pass, no tokens, no Neo4j).

## Files

| file | holds |
|---|---|
| `eval/graphqa.py`, `eval/graphqa_hard.py` | The harness |
| `eval/dataset/graphqa/questions.hard.json` | The 40 harder questions |
| `eval/dataset/graphqa/questions.json`, `questions.v1.json` | v2 and v1 questions |
| `eval/results/graphqa-hard.sessions.jsonl` | The harder run's log, including smoke 2 |
| `eval/results/graphqa-hard.smoke1.sessions.jsonl` | Smoke 1's log |
| `eval/results/graphqa-hard.{json,md}` | Scored output for black and poetry |
| `eval/results/graphqa-pilot.*`, `graphqa-pilot.v1.*` | v2 and v1 results |

The first version of this work is commit b43f8fe on `Ifrit`. Everything since is not committed: the oracle fix, the
limit-message fix, the corrected gold, the last 12 sessions, the re-score and this report's rewrite.
