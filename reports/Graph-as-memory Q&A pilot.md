# Graph-as-memory Q&A pilot (`eval/graphqa.py`)

Does Claude answer code-structure questions better through Orion's graph than by reading the source? Built and
run 2026-10-31, revised 2026-11-01. **Dev evidence only**: NodeGoat and PyGoat are public and old, and the
questions are AI-written (`"status": "proposed"` until a human reviews them).

**Answer so far: no better, and about 1.6x the tokens.** In v2, both models are exact on all 20 questions through
both paths, and the graph path costs 1.56-1.57x the tokens of grep. In v1 the paths differed only on source-to-sink
paths, and only because that gold was Orion's own FLOWS_TO definition rather than what the question asked;
revision 2 fixed that. The set is now too easy to separate the paths, so a real answer needs harder questions
(a larger repo, multi-hop structure).

**Harder set (2026-11-02): 40 questions built, 4 smoke-tested, not yet run in full (see "Harder set" below).** It has
already found one thing that doesn't depend on the run: a plain `orion scan` of a `src/`-layout Python repo sees only
0.51 (black) and 0.75 (poetry) of the calls in the source. Scanned from `src/`, it sees 0.92 and 0.89.

## Setup

| | |
|---|---|
| Fixtures | `fixtures/NodeGoat` @ c5cb68a (scan `85ecd596...`), `fixtures/pygoat` @ 19d17cc (scan `e1d5897f...`), WSL Neo4j |
| Orion code | `orion/` at 20b51b8 (HEAD 3e46ac2 changed only `eval/` and `bench/`) |
| graph path | Orion's 3 read-only MCP tools only (`run_cypher`, `semantic_search`, `get_schema`), scoped to the scan |
| grep path | `Read` / `Grep` / `Glob` only, on a `git archive HEAD` export (no `.git`, no `node_modules`, no `cpg.bin`) |
| Models | `claude-sonnet-5`, `claude-opus-5`; effort xhigh; 15 turns; 900 s; answer = `{"answer": [string]}` via `--json-schema` |
| Sessions | one `claude -p` per question x model x path, through `orion.claude_cli.run_agent` (`retries=0`; graphqa does its own) |
| Scoring | set precision / recall / F1 and exact match after normalising (paths, `file:LINE`, `name@version`, routes); a failed session scores 0 |

## Questions and gold checks

Six families, 2 questions each per fixture (24 planned). Each question stores the Cypher that computes its gold.
The graph lies by omission, so every gold item is checked against its source line. Where the family allows it,
the gold set is also checked for completeness against a lexical scan of the export.

| Family | Asks | Completeness check |
|---|---|---|
| lookup | file:line that defines function X | no other definition of X in the repo |
| calls | call sites of X / repo functions X calls | every `X(` line; every repo-defined name called in X's body |
| entry_sink | which listed entry points contain a sink call of kind K fed by untrusted input | entry points whose body has a lexical K call = gold |
| flow_path | lines whose call results reach a CandidateFlow's sink (its FLOWS_TO ancestors) | def-use pass backward and forward in the file |
| deps | declared dependencies (all / major version 0) | independent parse of the manifest |
| routes_noauth | routes registered without an auth check | lexical parse of the route file / views |

- **Drop:** the graph gave a non-empty gold set that failed the check (counted).
- **Skip:** no question at all: empty gold, a one-line flow, a repeated answer set (tallied by reason).
- **Selection bias:** only questions whose graph gold survives are asked. The pilot says whether the graph helps
  where it is right, not how often it is right.

## Revision 2 (2026-11-01): three template fixes

Each question now has a `rev`; sessions are keyed by (id, rev). Only changed questions were asked again.

1. **entry_sink wording.** v1 said "a code_exec sink call (`eval`, `exec`, `system`, `spawn`)", which reads as
   examples. All four model/path rows also counted `cmd_lab` / `command_out`, which run
   `subprocess.Popen(shell=True)`. The gold is `s.name IN $sink_names`, so v2 says the names are the whole list:
   "a call to `eval`, `exec`, `system` or `spawn` ... Only those names count". Gold unchanged; 3 questions reworded.
2. **flow_path callbacks.** FLOWS_TO follows return values only. In NodeGoat's route handlers the rendered data
   arrives as a DAO callback parameter (`dao.getByUserId(userId, (error, contrib) => { ... res.render(..., {...contrib})`),
   so the v1 gold for `allocations.js:25` and `contributions.js:21` held only the sink and line 4
   (`require("../../config/config")`). The def-use check now treats a JS callback parameter as its call's result.
   It also no longer stops at an attribute assignment (`contrib.userId = userId`), and it reads a spread
   (`...contrib`). Both v1 NodeGoat flow questions now drop.
3. **flow_path false sinks.** The sink line must hold a call of its kind, as entry_sink already required. The
   plain `log` hint files route registrations (`app.get("/login", ...)`, `isLoggedIn`) as log sinks; 13 NodeGoat
   flows ended at one, plus `profile.js:82` (`profile.updateUser(`) filed as render.

Effect: NodeGoat has no flow_path question left (all 18 multi-line candidates drop), so v2 has 20 questions. 17
questions are byte-identical to v1 and keep their v1 sessions. v1's two NodeGoat flow questions are superseded:
their sessions stay in the log and the total usage, but are not scored.

## Drops

| | v1 (2026-10-31) | v2 (2026-11-01) |
|---|---|---|
| Questions | 22 | 20 |
| Drops | 8: calls 2, entry_sink 2, flow_path 4 | 26: calls 2, entry_sink 2, flow_path 22 |
| Skips | NodeGoat 42, PyGoat 83 | NodeGoat 80, PyGoat 83 (more flow candidates tried once the first ones drop) |

Every drop is a real graph gap that the source check caught:

- **calls (2, NodeGoat):** UserDAO's body calls `this.getNextSequence` and others, which the graph knows only as
  `<lambda>N`; AllocationsHandler calls `next`, which the repo defines twice.
- **entry_sink (2, NodeGoat):** BenefitsHandler and ResearchHandler render, and tryDropCollection logs, without a
  graph flow.
- **flow_path, PyGoat (4):** FLOWS_TO misses `res = res.decode()` (`mitre.py:243`) and
  `name = request.POST.get('name')` (`views.py:150`, which feeds 3 SQL sinks).
- **flow_path, NodeGoat (18, v2):** 4 callback-delivered values (`allocations.js:23`, `contributions.js:17`,
  `memos.js:25`, `profile.js:20`) and 14 false sinks (above).

## Results

### v1: 22 questions, 88 sessions (2026-10-31)

| model | path | F1 | exact | tokens | $ CLI | wall s |
|---|---|---|---|---|---|---|
| sonnet-5 | graph | 0.94 | 19/22 | 3,726,370 | 3.13 | 1431 |
| sonnet-5 | grep | 0.92 | 19/22 | 1,845,918 | 1.88 | 791 |
| opus-5 | graph | 0.99 | 21/22 | 2,916,610 | 7.31 | 1246 |
| opus-5 | grep | 0.93 | 19/22 | 1,956,967 | 4.64 | 728 |

- lookup, calls, deps, routes_noauth: every row exact on every question.
- entry_sink: every row missed `pygoat-entry_sink-2` the same way (the `subprocess.Popen` reading, fix 1).
- flow_path was the only family where the paths differed. Opus through the graph scored 3/3 by querying FLOWS_TO,
  which is the gold's own definition. The grep rows followed the callback data (fix 2), which is arguably right.

Full tables: `eval/results/graphqa-pilot.v1.md`, `eval/results/graphqa-pilot.v1.json`; questions:
`eval/dataset/graphqa/questions.v1.json`.

### v2: 20 questions (2026-11-01)

17 questions keep their v1 sessions; the 3 reworded entry_sink questions were asked again (12 sessions, all `ok`).

| model | path | F1 | exact | tokens | $ CLI | tokens / exact | wall s |
|---|---|---|---|---|---|---|---|
| sonnet-5 | graph | 1.00 | 20/20 | 2,094,049 | 1.65 | 104,702 | 754 |
| sonnet-5 | grep | 1.00 | 20/20 | 1,336,045 | 1.41 | 66,802 | 386 |
| opus-5 | graph | 1.00 | 20/20 | 2,178,926 | 5.65 | 108,946 | 1138 |
| opus-5 | grep | 1.00 | 20/20 | 1,394,374 | 3.32 | 69,719 | 398 |

- Every row is exact on every question, so this set no longer separates the two paths. It measures cost only.
- The graph path used 1.57x (Sonnet) / 1.56x (Opus) the tokens of grep, 1.17x / 1.70x the CLI cost, and
  2.0x / 2.9x the wall time. Opus's graph wall time includes one 316 s session (`pygoat-entry_sink-1`).
- With the wording fixed, every row left out `cmd_lab` / `command_out` on `pygoat-entry_sink-2`. So the v1 miss
  was the wording, not the models.

Full tables: `eval/results/graphqa-pilot.md`, `eval/results/graphqa-pilot.json`.

## Harder set (`eval/graphqa_hard.py`, 2026-11-02): built and smoke-tested

**How it differs from v1 and v2**

- **Gold comes from the source, never from the graph.** A Python `ast` call graph (a lexical pass for JS) follows a
  rule that each question states in full. The plain Cypher answer is stored as `reference` and only labels the
  question (`graph_agrees`). A question the graph gets wrong stays in the set, which removes the selection bias above.
- **Larger code.** The oracle's call chains are 1 hop on PyGoat, 2 on NodeGoat and up to 10 on poetry, so black and
  poetry (RepoQA's clones) were added.
- **Six new families:** reach_down, reach_up, chain, hot (highest fan-in / fan-out), dead (never mentioned elsewhere)
  and pair (X and Y, X but not Y).
- **40 questions:** NodeGoat 10, PyGoat 6, black 12, poetry 12. The plain query alone is exact on 15.
- **Turn limit:** a hard stop at 30 model round trips, and agents are told 25.

**Finding: the scan root decides how many calls the graph sees.** Calls the source oracle finds, against the graph's
CONTAINS_CALL then RESOLVES_TO edges (no model tokens). The clones are sparse, so each repo root holds only the package
source.

| repo | scanned from | oracle calls | graph calls | both | recall | precision |
|---|---|---|---|---|---|---|
| black | repo root (plain `orion scan`) | 488 | 299 | 250 | 0.51 | 0.84 |
| black | `src/black` | 488 | 299 | 250 | 0.51 | 0.84 |
| black | `src/` | 488 | 513 | 449 | 0.92 | 0.88 |
| poetry | repo root (plain `orion scan`) | 707 | 800 | 532 | 0.75 | 0.67 |
| poetry | `src/poetry` | 707 | 800 | 532 | 0.75 | 0.67 |
| poetry | `src/` | 707 | 979 | 629 | 0.89 | 0.64 |

For comparison, NodeGoat is recall 0.43 / precision 0.95, and PyGoat 1.00 / 0.62. Joern names an imported function by
its import path (`poetry/utils/env.py:<module>.build_environment` appears as a stub in the graph), so an import binds
to the real method only when the scan root makes that path the file's own. The fixtures now scan from `src/`, which
gives the graph path its best case. Poetry's lower precision there is mostly calls on objects bound by name (281 of
the 350 extra edges).

**Smoke test (Sonnet, 4 questions, both paths, one session each).** This is a cost and limit check, not a result.

| question (gold size) | path | outcome | round trips | tokens | $ CLI | wall s |
|---|---|---|---|---|---|---|
| nodegoat-hot-1 (4) | graph | exact | 23 | 1,980,508 | 1.63 | 641 |
| nodegoat-hot-1 (4) | grep | exact | 11 | 567,850 | 0.62 | 221 |
| black-hot-2 (3) | graph | exact | 20 | 1,614,583 | 1.69 | 750 |
| black-hot-2 (3) | grep | exact | 28 | 4,187,828 | 2.38 | 522 |
| poetry-chain-1 (6) | graph | F1 0.91, missed `_from_sdist_file` | 13 | 500,673 | 0.49 | 209 |
| poetry-chain-1 (6) | grep | exact | 10 | 310,105 | 0.33 | 134 |
| poetry-reach_up-2 (19) | graph | ran out, no answer | 30 | 1,162,820 | 0.73 | 368 |
| poetry-reach_up-2 (19) | grep | ran out, no answer | 30 | 1,675,593 | 0.80 | 241 |

Round trips are counted from the session transcripts; everything else comes from the CLI.

- **The first smoke test (old scan root, 15 round trips):** 6 of 8 sessions ran out and answered nothing. Graph
  finished 0 of 4 questions, grep 2 of 4, both exact.
- **The limit is the same on both paths but binds differently.** Grep reads many files in one round trip, while graph
  agents mostly query step by step.
- **Telling agents the budget did not stop them on the largest question.** On `poetry-reach_up-2` both went past the 25
  they were told and hit the hard stop. Such a session counts as a failure.

**Harness bugs the smoke tests found (fixed in `eval/graphqa.py`)**

- A session that ran out of turns on an exit-1 CLI was retried as if transient, which paid for it twice.
- A session cut off mid-way by the plan limit ("5-hour limit · resets 12:10am") was not recognised as a limit, so a
  re-run cut off the same way would have been scored as the model's failure. It now waits and re-runs.

**Full run, stopped by the plan limit (2026-11-02): 18 of 24 questions done, not scored yet.** Sonnet, black + poetry. A
hand tally from the log, interim only:

| | exact | ran out of turns |
|---|---|---|
| graph | 11 of 18 | 2 (`black-dead-2`, `poetry-reach_up-2`) |
| grep | 13 of 18 | 2 (`black-hot-1`, `poetry-reach_up-2`) |

- **Left:** `poetry-hot-1`, `poetry-hot-2`, `poetry-dead-1`, `poetry-dead-2`, `poetry-pair-1`, `poetry-pair-2`, both paths
  (12 sessions). Their rows in the log are `aborted` / `retried`, so a run re-asks them.
- **Resume, then score:** `python -m eval.graphqa_hard run --models claude-sonnet-5 --fixtures black,poetry --jobs 2`, then
  `python -m eval.graphqa_hard score --fixtures black,poetry`.
- **Still to hand-check:** `poetry-reach_up-1` grep answered an empty list after 11.8 s (F1 0), which looks wrong and may
  be an artefact; `black-reach_down-2` (both paths add `visit`) may be an oracle or wording error; `black-dead-2` grep adds
  dunders.
- **Usage in this run so far:** 25,201,204 tokens, $20.36 CLI-reported. These are not yet in the table below.

## Usage spent

All CLI-reported (`total_cost_usd` and token counts from each session's usage events; none missing).

| Run | Sessions | Tokens | $ CLI |
|---|---|---|---|
| Smoke test (2026-10-31, 2 sessions, not in the log) | 2 | 161,187 | 0.20 |
| v1 pilot (2026-10-31) | 88 | 10,445,865 | 16.97 |
| v2 reworded questions (2026-11-01) | 12 | 1,532,463 | 2.94 |
| Harder set, smoke 1 (2026-11-02; 14 with usage, 9 stopped by the limit before any model ran) | 23 | 9,457,680 | 9.64 |
| Harder set, smoke 2 (2026-11-02) | 8 | 11,999,960 | 8.67 |
| **Total** | **133** | **33,597,155** | **38.42** |

The v1/v2 session log holds 100 of the first 102. 20 of those (4,974,934 tokens, $7.87) answered v1 revs of the 5
changed questions and are not scored in v2. Smoke 1 (old scan root, old questions) is kept in
`eval/results/graphqa-hard.smoke1.sessions.jsonl`. Smoke 2 used the final questions and settings, so it starts the
run's log, `eval/results/graphqa-hard.sessions.jsonl`.

No session failed, hit a usage limit or needed a retry, in v1 or v2.

## Caveats

- AI-written templates and picks; a human must review every question before a result counts.
- Selection bias toward code the graph models well (above).
- One run per cell (N = 1); 20 questions; two small, old, public fixtures.
- Grep agents read a clean export; graph agents read Orion's scan of the same commit. Both see only tracked files.

## How to run (WSL, from the Windows checkout with WSL's venv)

```bash
cd /mnt/c/Users/h/Downloads/orion && PYTHONPATH=$PWD ~/orion/.venv/bin/python -m eval.graphqa questions --fixtures-root ~/orion
```

```bash
cd /mnt/c/Users/h/Downloads/orion && PYTHONPATH=$PWD ~/orion/.venv/bin/python -m eval.graphqa run --jobs 2
```

```bash
python -m eval.graphqa score
```

- `questions` reads the previous `questions.json` to carry each question's `rev` forward.
- `run` spends tokens and is resumable: it skips (id, rev, model, path) sessions already logged `ok` / `failed`.
  A usage limit waits 30 min and reruns; another failure is retried once after 30 s; max-turns / timeout is a
  real failure.
- `score` is token-free.
- Tests: `eval/tests/test_graphqa.py` (token-free, no Neo4j).
- The harder set: `python -m eval.graphqa_hard edges | questions [--build] | run [--models] [--jobs] [--only] | score`,
  writing `eval/dataset/graphqa/questions.hard.json` and `eval/results/graphqa-hard.*`. `edges` and `questions` are
  token-free. Tests: `eval/tests/test_graphqa_hard.py`.

## Orion follow-ups this found (not fixed here)

- **A `src/`-layout Python repo loses imported calls.** A plain `orion scan` of black sees 0.51 of its calls, against
  0.92 from `src/` (table above). Fewer resolved calls also means less for reachability and pathfind to follow, and
  real scans run from the repo root. Not measured on findings.
- **`eval.run`'s limit signatures miss the real message.** `_RAW_RE` does not match "5-hour limit · resets ...", and
  `eval/extbench.py` imports the same pattern, so a benchmark session cut off mid-way may not be recorded as a limit.
- **`subprocess.Popen` is not a sink.** The GENERIC profile's `code_exec` names are only `eval`, `exec`, `system`,
  `spawn` (`orion/graph/profiles.py:141`), so PyGoat's command-injection lab gets no CandidateFlow.
- **`log` hint false sinks.** The hint matches "login" / `isLoggedIn`, so at least 13 NodeGoat route
  registrations are CandidateFlows of kind log (CLAUDE.md notes the hint, not how many flows it makes). A sink
  can also be the outer call around the real one: `profile.js:82` (`profile.updateUser(`) is filed as render.
- **FLOWS_TO misses callback-delivered data.** It misses the NodeGoat DAO callbacks above, which is how every
  NodeGoat page gets its data, and two plain PyGoat assignments.
