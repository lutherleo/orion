# Merge plan Phase 6: the three measurement gates

Each gate answers one open decision of the Oracle + krish/main merge (decisions D2, D4, D5). Its rule was
written down BEFORE any run (proposed 2026-10-29; Krish and Love Kush confirm or change it first), so the
numbers cannot choose it. Gates 1 and 2 run on Krish's Mac (MLX). Gate 3 runs on any box with Neo4j, Joern,
the fixtures and a logged-in `claude`. Gates 1 and 2 use no Claude tokens; Gate 3 does.

| Gate | Question | Where | Command | Rule (proposed) |
|---|---|---|---|---|
| 1 (D4) | Is C1 good enough to keep its frozen code-only sink rules? | Mac | `eval.bakeoff --compare` | KEEP_C1 if the pair_correct gain's 95% CI is above 0 AND patched_silence drops by at most 5 pp (CI); else RETRAIN (C2 on the 2026-10-28 sink rules) |
| 2 (D2) | Should `--lm-verify` be on by default? | Mac | `eval.verifygate` | DEFAULT_ON if the verifier loses <= 2% of the real vulns discovery found AND the false-alarm cut's CI is above 0; else KEEP_OPT_IN |
| 3 (D5) | How does Orion + Claude (arm O) do against Opus alone (arm P), scored by eval's matcher at L2? | WSL / Mac | `bench/prove.py` | none: a measurement, reported either way |

## Gate 1: C1 vs its base (dev bake-off, temperature 0)

```bash
python -m eval.bakeoff --one models/orion-lm-base --dev train/data/stage1.jsonl --temperature 0 --out gate1-base.json
python -m eval.bakeoff --one models/orion-lm --dev train/data/stage1.jsonl --temperature 0 --out gate1-c1.json
python -m eval.bakeoff --compare gate1-base.json gate1-c1.json
```

- The same 300-row seeded dev sample (seed 0, whole pairs) for both models. `--compare` refuses two rows
  scored on different samples or at different temperatures.
- Each row records the sampling the model actually ran with. Without `--temperature` a model runs at its
  own defaults (temperature 1.0 when it has no generation_config.json), which is why the gate passes 0.
- The CI is a paired cluster bootstrap (B = 10,000, cluster = pair, both models see the same resample).
- **Changed 2026-10-29:** the dev judge now asks each row exactly as a training row is built
  (`train.examples.to_sft`: the case goes through `orion.lm.sinks.annotate` first). Rows built before the
  sink line existed (it landed 2026-10-24, with C1) were asked WITHOUT the `[sink]` line C1 was trained on.
  Bake-off numbers recorded before this change are not comparable with new ones.
- Record the table in `docs/notes/orion-lm-bakeoff.md`.

## Gate 2: `--lm-verify` on dev

```bash
python -m eval.verifygate --model models/orion-lm --dev train/data/stage1.jsonl --temperature 0 --out gate2.json
```

- Same seeded dev sample as Gate 1, tool-less like the dev judge. Discovery asks each row; every lead it
  raises is re-judged alone by the verifier's own task turn (`orion/lm/verify.task_message`) and mapped by
  the runtime's rule (CONFIRM / INCONCLUSIVE when LOW / REJECT / ERROR). A row stays flagged only when a
  lead is CONFIRMed, which is what `eval.normalize` counts once the verifier ran.
- Prints discovery vs discovery + verifier for every metric with the paired CI, how many of discovery's
  real vulns the verifier kept or lost, how many false alarms it removed, and both passes' tokens.
- Real-graph check after the dev gate: `orion scan fixtures/NodeGoat --engine lm --model models/orion-lm`
  with and without `--lm-verify`, then `python -m eval.devbench --benchmark nodegoat <run_dir> <run_dir>`.

## Gate 3: Orion + Claude vs Opus alone, through eval's matcher

```bash
claude                                  # once, interactively: log in (prove.py's preflight checks it)
# once: copy the login into eval's clean Claude home for arm P (eval/README.md, "One-time auth copy")
python bench/prove.py --dry-run         # token-free: both logins, Neo4j, Joern, fixtures
python bench/prove.py                   # arm O x {sonnet, opus} + arm P x {nodegoat, pygoat}: spends tokens
python bench/prove.py --rescore         # token-free: rescore whatever results are on disk
```

- **Arm P** is eval's own Opus-alone arm (`plain-opus5`: the frozen plain prompt and findings.json schema,
  one CWE per finding, `claude-opus-5` at effort xhigh, from `eval/run.py`), run on a `.git`-free export of
  the pinned fixture in an opaque temp dir that eval's leak guard checks (no CLAUDE.md or .git above it).
  `--no-plain` skips it.

- prove.py prints the legacy table (`bench/scoring.py`) and then the same results scored by
  `eval/devbench.py`: eval's L1 / L2 tests (file + function or line +/-10, + CWE family), one-to-one,
  against answer keys read from the pinned fixtures (`eval/dataset/devbench/`).
- **The keys are proposed, not signed.** Each fix site was read from the fixture (NodeGoat's own
  "Fix for" markers and the vulnerable statements) and `python -m eval.devbench --benchmark nodegoat
  --check-keys fixtures/NodeGoat` proves every cited line is still the code it quotes. A labeler signs
  them (`status: signed`, `signed_by`) before a number from them is quoted as more than draft evidence.
- These are DEV benchmarks: old, public bugs any frontier model has seen. Never an `eval.report` number.
- **One-to-one by maximum matching**, not `eval.match.match_run`'s greedy order. The greedy order is exact
  for eval (one key per run) but, with 15 keys, it handed arm C's open-redirect finding (index.js:70, also
  inside A7's window) to A7, so A10 went uncredited. `devbench.match_max` credits the most vulns, then
  the most at L2, then the lowest finding indices.
Committed results rescored on 2026-10-29 (`python bench/prove.py --rescore`, token-free, draft keys):

| Benchmark | Arm | Legacy matcher | eval L1 (location only) | eval L2 (+ CWE) | missed (L1) | duplicates / unmatched |
|---|---|---|---|---|---|---|
| NodeGoat | C (Opus 5, no Orion) | 15 / 15 | 15 / 15 | 0 / 15 | - | 3 / 3 |
| NodeGoat | D (Semgrep) | 4 / 15 | 5 / 15 | 0 / 15 | A1-2, A1-3, A2-2, A4, A6, A7, A8, A9, SSRF, ReDoS | 10 / 20 |
| PyGoat | C (Opus 5, no Orion) | 14 / 16 | 15 / 16 | 0 / 16 | COMPONENTS | 9 / 6 |
| PyGoat | D (Semgrep) | 7 / 16 | 13 / 16 | 0 / 16 | XSS, LOGGING, COMPONENTS | 39 / 83 |

**Read L1 with care: it does not check the bug type.** NodeGoat and PyGoat each hold 15-16 bugs in a few
files, so a finding of ANOTHER class next to a fix site is credited. The committed C and D findings carry
no CWE id (C writes prose; the saved Semgrep findings kept only rule text), so they can never reach L2 and
their L1 credit is location only. Measured on the PyGoat D row: 5 of its 13 L1 credits (XXE, IDOR,
DESIGN-OTP, DESER-YAML, CODEEXEC) are `@csrf_exempt` decorator findings a few lines above the vulnerable
view, and SSTI is credited by a `request-data-write` finding (my reading of the rule names). C has a
finding of the right type for nearly every bug, but some credits land on a neighbour's finding (NodeGoat
ReDoS <- the profile.js XSS finding; PyGoat CRYPTO <- a hard-coded JWT key finding next to the MD5 line).

So the Gate 3 comparison is made at **L2**, and every arm must report a CWE per finding. Orion's
findings do; arm P (above) is the Opus-alone row that does, in place of the committed prose C rows.
Semgrep's adapter now keeps each rule's CWE ids (`bench/semgrep_adapter.py`), so a Semgrep re-run (no
tokens) can reach L2 too; the committed D rows predate that.

- Key-set judgement calls a labeler should check (each key's `notes` has the details): NodeGoat files
  password storage under A2-2 and wires missing controls (A5 helmet, A7 role check, A8 csurf) at their
  config / route lines; PyGoat includes real instances in `introduction/mitre.py` and `dockerized_labs/`
  (the ground-truth docstring names only views.py, settings.py, requirements.txt and apis.py), leaves
  out PyGoat's `ssrf_lab` (a local file read, CWE-22), and drops two sites (gentckt, filter_blog) whose
  windows would overlap another key's.
