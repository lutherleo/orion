# Open-weight study — design changelog

Changes to arms, dataset rules, prompts, matching rule or hypotheses. Anything after the
`eval-prereg-v1` tag needs a new tag and a dated reason here. The tag has not been created yet, so
the entries below are pre-registration changes.

## 2026-09-29 — Qwen added as exploratory arms (pre-registration)

- **Added** `orion-qwen3coder` and `plain-qwen3coder`, both on `qwen3-coder:30b` (Ollama, 18.6 GB, a
  coder model expected to support tool calls), at the project owner's request. **Not yet pulled or
  verified:** confirm `tools` in `ollama show qwen3-coder:30b` capabilities before any run (gemma3:12b
  turned out to lack it).
- **Exploratory, not headline.** The original design excluded Qwen because it has **no published
  training cutoff**, so a post-cutoff CVE cannot be shown to be unseen by it. These arms are therefore
  kept out of the six registered arms (`eval/run.py` `EXPLORATORY_ARMS`). They never run by default
  (only with `--exploratory` or an explicit `--arm`), and their results are reported separately and
  never pooled into the headline comparison.
- **Found during device setup** (to be resolved before the tag): Ollama's `gemma3:12b` reports
  capabilities `completion, vision`, with **no tool support**, so the `orion-gemma4` and
  `plain-gemma4` arms cannot make Orion's MCP tool calls as registered. The Gemma tag needs to be
  re-chosen (a tool-capable tag) before `eval-prereg-v1`.

## 2026-10-27 — Oracle and krish/main merged; Qwen exploratory arms removed

- **Removed** `orion-qwen3coder` / `plain-qwen3coder` (`EXPLORATORY_ARMS`, `--exploratory`). The merge
  takes Krish's reworked harness (five arms, no Ollama path), which supersedes the 09-17 six-arm design
  and these exploratory arms with it. No run of either was ever scored.
- **Kept** from Oracle: the `joern-parse --help` probe (v4 exits 1 on `--version`) in `eval/preflight.py`,
  and `eval/wsl/`.
- **Changed** `eval/normalize.py` reads a lead's `line_start` (the merged `contracts.Lead` field), falling
  back to `line` for lm runs recorded before the merge. Scoring semantics are unchanged.
- Krish's pre-tag changes since 2026-10-18 (path in `TASK_PROMPT`, any-fix-site L1 rule, one-to-one
  matching, five arms) still need their own dated entries; to be written with Krish before the tag.

## 2026-10-29 — scoring fixes (merge plan Phase 5). Krish signs off before `orion-lm-prereg-v1`

None of these moves an answer key or the frozen location rule's ±10 window; each changes how a run
or a finding is counted. Every number below is measured or a rule, never an estimate.

- **Failed runs are missing, not scored** (`eval/report.py`). Before, a crashed / hung / error
  patched run counted as "no false alarm" and a failed vulnerable run as a miss, so an arm that
  crashed more looked safer. Now only `ok` runs enter any rate (FAR, efficiency, recall, pair-correct,
  OWASP Youden J); failed runs are listed in `status_counts` and put back in a `sensitivity` block:
  FAR with every failed patched run a false alarm (worst case) and with every one clean (the old rule),
  and E with failed vulnerable runs scored as before. The report prints both bounds per arm.
- **Hung Claude runs keep their tokens** (`eval/arms/plain.read_usage`). A run killed by the watchdog
  has no `result` event, so its T was None and it dropped out of efficiency. Its usage is now summed
  from the per-message `usage` the CLI prints (deduplicated by message id, highest value per field),
  logged with `agent_calls.exit_reason = "per_message_sum"`: a LOWER BOUND (the message in flight may be
  cut off). A `result` event still wins when present. Codex already reports per turn.
- **Each finding is validated on its own** (`eval/arms/plain._capture_findings`). Before, one bad
  finding (e.g. `"function": null`) voided the run. Now the bad ones are dropped and counted (a warning
  names them), the valid ones are scored, and the raw file is kept as `findings.raw.json`; a file with
  no valid finding at all is still a miss.
- **Range-overlap line rule** (`eval/match._loc`, `eval/normalize`). A finding reporting
  `line_start..line_end` now matches when that range OVERLAPS the fix window `[line - 10, end_line + 10]`;
  before, only its first line was tested. **Parameter for Krish:** a range longer than
  `_MAX_RANGE = 50` lines counts as its first line only, so a whole-file range cannot match everything.
- **Orion verdicts: CONFIRM filter and the verifier's location** (`eval/normalize`, absorbs and
  deletes the old `eval/convert.py`). REJECT and ERROR verdicts, and an INCONCLUSIVE one that WAS
  verified, are not findings; an unverified lead (INCONCLUSIVE "unverified (engine=lm)", the orion-lm
  arm without `--lm-verify`) is. A verdict's location is `contracts.Verdict.location()`'s (the
  verifier's reading wins; a corrected file resets lines/function) -- pinned for parity.
- **The judge can credit location-less findings** (`eval/judge.credit_locationless`), as the frozen
  matching rule always said ("LLM judge only for location-less findings"): a finding with no file is
  asked "same vulnerability as the answer key?" (`MATCH_PROMPT`, hash in `judge_record()`), and a yes
  is a `J1` match (counted as L1, never L2), one-to-one. It REFUSES to run until `JUDGE_MODEL` is
  frozen and a calibration on this task (`calibrate(..., categories=MATCH_LABELS)`) has >= 100 labels;
  whether that agreement is good enough is Krish's call. Nothing runs it automatically.

Still open for Krish (not changed): an orion-lm run whose engine could not judge SOME batches
(`discover_lm(failures=...)`, merge Phase 3) is still an `ok` run scored on the batches it judged;
and the eval arm still loads the model with its own default sampling (the CLI now defaults to 0).

Bench (legacy, not an `eval/` number): the token ledger priced cache writes at 1.25x, but `claude -p`
writes the 1-hour cache (2x); the committed Opus-alone runs cost $0.73 (NodeGoat) / $1.01 (PyGoat), not
$0.61 / $0.84. `bench/token_ledger.price_usd` now needs the 5m/1h breakdown (else the price is None,
not assumed), missing usage is None not 0, and `claude -p` agents no longer run from Orion's checkout
(they could Read `tests/ground_truth_*.py`).

## 2026-10-29 — measurement gates (merge plan Phase 6). Krish signs off before `orion-lm-prereg-v1`

One change to the scoring of record; the rest is dev tooling that never feeds an `eval.report` number.

- **Changed** `eval/normalize._extract_file_line` (prose locations): a file cited two ways, as a bare
  name in one place and as a path ending in that name in another ("server.js" in the text,
  "fixtures/NodeGoat/server.js:78" in the evidence), now counts as cited in BOTH text and evidence, and
  the longest form is returned. Before, the two spellings were two different files, so a secondary file
  that happened to contain a "/" won the tier: the committed NodeGoat arm-C session finding was located
  at config/env/all.js:8 instead of server.js:78. Only findings located by prose are affected (Orion's
  Claude engine without structured fields, the legacy bench arms); plain arms and orion-lm carry
  structured locations.
- **Added** `eval/devbench.py` + `eval/dataset/devbench/{nodegoat,pygoat}.json`: NodeGoat / PyGoat (dev
  benchmarks, never headline) scored with `eval.match`'s L1/L2 tests against fix sites read from the
  pinned fixtures. Keys are PROPOSED until a labeler signs them. **For Krish:** it assigns one-to-one by
  maximum matching (`match_max`), because `match.match_run`'s greedy pass can hand a finding to a
  neighbouring key when a run has many keys (measured on NodeGoat arm C: A10 went uncredited). The eval
  matches each run against ONE key, so `match_run` stays exact there; use `match_max` if a manifest row
  ever carries several keys.
- **Dev scoring (training selection, not eval):** `train.devscore` now asks each dev row exactly as
  `train.examples.to_sft` builds the training row (the case goes through `orion.lm.sinks.annotate`), so a
  row built before the sink line existed is no longer asked without it. `eval.bakeoff` gains
  `--temperature/--top-p/--top-k`, records the sampling each model ran with, and `--compare` (Gate 1:
  C1 vs base, paired cluster bootstrap, rule `bakeoff.GATE1`). `eval/verifygate.py` measures
  `--lm-verify` on the same dev sample (Gate 2, rule `verifygate.GATE2`). Both rules are proposals,
  written before any run: `docs/notes/merge-phase6-gates.md`.
- **Bench (legacy, not an `eval/` number):** `bench/prove.py` gains arm P, eval's own `plain-opus5` arm
  (`eval/arms/plain.plain_arm`, unchanged) run on a .git-free export of the pinned NodeGoat / PyGoat
  fixture, so the Opus-alone row reports a CWE per finding and can be compared with Orion at L2 (the
  committed prose C rows cite none; on a 15-bug repo L1 alone credits another class's finding near a fix
  site). `bench/semgrep_adapter` appends each rule's CWE ids to the evidence for the same reason.
