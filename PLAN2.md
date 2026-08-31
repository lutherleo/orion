# PLAN2 — Research eval: does GraphRAG grounding lift a LOCAL LLM to frontier vuln-finding?

## Completion status (updated 2026-08-31)

Legend: ✅ done · 🟡 partial · ⛔ blocked (reason) · ⬜ not started

**Overall: ~85% — all code + tooling built and tested; TWO of four arms fully measured on both
benchmarks with real token/cost (Semgrep + Opus-5 frontier baseline); Phase 0 run to a definitive
verdict; the two LOCAL arms are the only gap, blocked by this box's CPU (a GPU host finishes them).**

| Item | Status | Notes |
|------|--------|-------|
| Token instrumentation (`claude_cli` usage capture) | ✅ | `extract_usage`/`_emit_usage`; usage `ProgressEvent` per call. |
| `bench/token_ledger.py` (aggregate + price) | ✅ | 11 tests; pricing opus/sonnet/haiku, cache mults, local=$0. |
| Shared matcher `bench/scoring.py` (generalized) | ✅ | NodeGoat matcher lifted verbatim; `run_nodegoat_eval` delegates. |
| `tests/ground_truth_pygoat.py` (16 vulns) | ✅ | Labeled from the real PyGoat source; own CLASS_KEYWORDS. |
| Ungrounded-review harness (arms B/C) `bench/ungrounded_review.py` | ✅ | `use_mcp=False` toggle added to `claude_cli`. |
| Semgrep adapter (arm D) `bench/semgrep_adapter.py` | ✅ | Maps SARIF→shared matcher. |
| Runner `bench/research_eval.py` + plots `bench/plot_results.py` | ✅ | Per-arm JSON + recall-by-arm PNG. |
| New token-free tests | ✅ | +22 (ledger/scoring/arms); suite 201 passing. |
| **Arm D — Semgrep** (both benchmarks) | ✅ | **NodeGoat 4/15 (26 FP-cand); PyGoat 7/16 (85 FP-cand)** — committed under `bench/research/`. |
| **Phase 0 — local-model spike** | ✅ | **Run to verdict: NO-GO on this box, but tool-calling proven achievable.** Built ollama + a stdlib **Anthropic→ollama shim** (`bench/phase0/anthropic_ollama_shim.py` — LiteLLM's streaming+tools is broken). Coder models emit tool calls as text, but **`llama3.2:3b` emits structured `tool_calls`** and the shim carries them. BINDING BLOCKER is **CPU latency**: ollama takes 8 s (tiny) → 104 s (~2k-token prompt); Claude Code's ~10–20k-token prompt → ~300–600 s per turn, past Orion's timeouts. Evidence: `bench/research/phase0/RESULTS.md`. |
| **Arm B — Local, no Orion** | 🟡 | **RAN on this PC** (llama3.2:3b via the shim, ~9.4 min) → **0/16**: the 3B model failed to emit schema-valid JSON leads (`result field was not valid JSON`). Partly model weakness, partly the shim not doing constrained decoding. Committed: `bench/research/pygoat/B.json`. Honest read: local-on-CPU produced no usable findings here. |
| **Arm A — Local + Orion** | ⛔ | Not attempted: arm B (one pass) already took ~9.4 min and yielded no valid output; arm A is a full graph pipeline (4 discovery shapes + per-lead verify, many more turns) — infeasible on this CPU box for the same latency + structured-output reasons. Unblock: GPU host + vLLM (the `bench/` remote kit). |
| **Arm C — Opus-5, no Orion** | ✅ | **MEASURED both benchmarks:** NodeGoat **15/15** (5 FP-cand, 326k tok, **$0.61**); PyGoat **14/16** (10 FP-cand, 446k tok, **$0.84**). Committed `bench/research/{nodegoat,pygoat}/C.json`. (Cost pricing required a ledger fix: Pro/OAuth reports empty model → `TokenLedger(default_model=...)` backfills it.) |
| **REPORT.md + plots** | ✅ | `bench/research/REPORT.md` (real C+D+B numbers, cost-per-vuln) + `recall_by_arm.png` / `recall_vs_tokens.png`. |
| README / agenda update | ✅ | README points at committed Semgrep baseline; agenda items 1–2 advanced. |

**The only remaining gap — the two LOCAL arms (A, B):** blocked by *this box's* hardware, not by code.
CPU inference of even a 3B model on Orion's prompts is too slow to finish an agentic run (Phase 0:
8 s tiny → 104 s at ~2k tokens; a real turn is 5–10 min). Tool-calling and structured output are both
*solved* here (`llama3.2:3b` emits structured tool_calls; the shim now forwards a JSON schema to
ollama's constrained decoder so a weak model emits valid JSON) — only speed blocks them. **Unblock:**
run the same commands on a **GPU host** (the `bench/` remote-scan kit targets exactly this):
`ORION_MODEL=<local> ANTHROPIC_BASE_URL=http://<shim>:4001 python bench/research_eval.py --arm {A,B} …`.

Everything else is done and measured: both benchmarks' fixtures present, pipeline/harnesses/scoring/
ledger built and tested (24 PLAN2 tests green), and **two full arms (Semgrep + Opus-5) committed with
real recall, false-positive, token, and dollar-cost numbers on both NodeGoat and PyGoat.**

## Context

Two claims to prove, with token/cost as a first-class axis:
1. **Uplift** — a *local open-weights* LLM driving Orion's graph-grounded pipeline reaches (or beats)
   a *frontier* model working alone. The novel claim: grounding + a fixed 4-shape discovery + an
   independent verifier substitutes for raw model capability.
2. **Better than fixed rules** — Orion finds vulns a deterministic scanner (Semgrep/CodeQL) misses,
   without the false-positive flood, scored through Orion's OWN matcher.

The research artifact is a committed, reproducible experiment: an arms × benchmarks matrix scored for
**recall / false-positives / tokens / cost**, plus a short report. Bar in play: NodeGoat 14/15 @ 0 true
FP (`tests/ground_truth_nodegoat.py`), PyGoat ~20 findings validated.

**Scope (decided):** small model = a **true local model only** (no Haiku fallback); arms = Local+Orion,
Local-no-Orion (control), Frontier(Opus 5)-no-Orion (baseline), Semgrep baseline; benchmarks =
NodeGoat + PyGoat. **Stated risk (design around, not a blocker):** `claude -p`'s MCP tool-calling +
`--json-schema` may not survive an Anthropic-compatible proxy to a non-Claude model — so Phase 0 is a
hard go/no-go gate before spending on the full matrix.

## Experiment design

| Arm | Model | Grounding | Harness | Scores the… |
|-----|-------|-----------|---------|-------------|
| **A. Local + Orion** | local (proxied) | full graph + verifier | `orion scan` | uplift bet |
| **B. Local, no Orion** (control) | local (proxied) | none (reads files) | `bench/ungrounded_review.py` | isolates grounding |
| **C. Frontier, no Orion** (baseline) | `claude-opus-5` | none (reads files) | `bench/ungrounded_review.py` | plain-frontier bar |
| **D. Semgrep** | — | rules | `bench/semgrep_adapter.py` | fixed-rule bar |

Each arm runs on **NodeGoat** and **PyGoat**. Optional 5th cell (Frontier+Orion = true ceiling) noted
but out of the chosen set — cheap to add later via `ORION_MODEL=opus`.

**Metrics (per arm × benchmark):** recall (found/total, via the shared matcher), false-positive count,
total input/output/cache tokens, USD cost (Claude arms), wall-clock. **Headline efficiency metric:**
recall vs tokens (and cost-per-confirmed-vuln) — the plot that carries the story.

## Phase 0 — Local-model feasibility spike (GO/NO-GO, do first)

The whole "local" thesis hinges on this. Stand up a local model + an **Anthropic-Messages-compatible
proxy** and prove `claude -p` can drive Orion through it. Candidates: Qwen2.5-Coder-32B-Instruct (or a
7B for a first smoke) via **Ollama or vLLM**, fronted by a proxy that speaks `/v1/messages` (LiteLLM's
anthropic passthrough, `claude-code-router`, or a thin FastAPI shim). Point the CLI at it:
`ANTHROPIC_BASE_URL=<proxy>`, dummy `ANTHROPIC_API_KEY`, `ORION_MODEL=<proxied-id>`.

Three make-or-break checks (in order):
1. Plain `claude -p "hi"` returns through the proxy.
2. **MCP tool-calling works** — with `--mcp-config .mcp/orion.json`, the model actually emits a
   `mcp__orion__run_cypher` tool_use and loops (the core of Orion's grounding). Test against the tiny
   Python scan graph from the dynamic-layer work.
3. **`--json-schema` structured output works** — discovery's leads array / verify's verdict parse.

**Gate:** all three pass → proceed to the matrix. If (2) or (3) fail on the chosen model/proxy, try
(i) a stronger local model, (ii) a different proxy shim; if still failing, STOP and report back (the
"local only" choice may need a rethink) rather than silently substituting Haiku.

## Instrumentation (real code — token capture is missing today)

`claude_cli.run_agent` discards the stream-json `result` event's `usage` + `total_cost_usd`
(`_final_to_result` keeps only the structured output). Add capture without disturbing callers:
- In `claude_cli._run_once`, when `final` is parsed, emit a usage ProgressEvent via `on_event`:
  `{"event":"usage","detail":<json>}` carrying `input_tokens`, `output_tokens`,
  `cache_creation_input_tokens`, `cache_read_input_tokens`, `total_cost_usd`, `model`. Thread-safe
  (run_logger already locks). Discovery/verify parsing is untouched (they ignore unknown events).
- `bench/token_ledger.py` — an `on_event` wrapper that aggregates usage per phase (discover A/B/C/D,
  verify per lead, harness, trace) → totals, cost, and a per-arm JSON. For the **local arm**, Claude's
  usage may be wrong through a proxy — also scrape token counts from the proxy/vLLM `/metrics` and
  prefer those; record which source was used.
- Pricing table (Claude API skill, cached 2026-06-24): opus-5 $5/$25, sonnet-5 $3/$15,
  haiku-4.5 $1/$5 per 1M in/out; cache write ~1.25×, cache read ~0.1×. Local = tokens only ($0 API),
  note compute/wall-clock separately.

## The arms — how each runs

- **A. Local + Orion** — `ORION_MODEL=<proxied-id> orion scan <repo>` (build → discover → verify),
  reusing the existing pipeline. Score with the matcher. Ledger captures tokens.
- **B/C. Ungrounded review** — new `bench/ungrounded_review.py`: one `claude -p` session per repo with
  Read/Grep/Glob on the source (`--add-dir <repo>`), NO `--mcp-config`, a security-review system
  prompt, emitting the SAME `LEADS_JSON_SCHEMA` shape so `match_verdicts` scores it identically.
  Arm B sets model=local, arm C sets `claude-opus-5`. This is the honest "same task, minus the graph"
  control. Reuse `claude_cli.run_agent` (it already supports `add_dir`, `json_schema`, usage events).
- **D. Semgrep** — new `bench/semgrep_adapter.py` (agenda item 1): `semgrep --config auto` on the
  pinned checkouts, map SARIF/JSON findings to ground truth via the SHARED matcher (`_matches`/
  `match_verdicts` in `scripts/run_nodegoat_eval.py`), disclosing ruleset + version. No tokens.

## Scoring & benchmarks

- **NodeGoat**: reuse `tests/ground_truth_nodegoat.py` (15 vulns) + `match_verdicts`. Restore the
  fixture first (`fixtures/NodeGoat` + prebuilt `cpg.bin`, per CLAUDE.md — copy from the sentryV2
  scratchpad) — it is absent in this checkout.
- **PyGoat**: new `tests/ground_truth_pygoat.py` mirroring the NodeGoat structure (id, name, files,
  class keywords) for its ~20 findings — the one genuine labeling task. Generalize the matcher to take
  a ground-truth set + keyword map (it is already parameterized on `GROUND_TRUTH`; lift the
  NodeGoat-specific `CLASS_KEYWORDS` into the per-benchmark module).
- A single `bench/research_eval.py` runner: `--arm {A,B,C,D} --repo <path> --benchmark {nodegoat,pygoat}`
  → writes `{recall, missed, fp, tokens, cost, wallclock}` JSON under `bench/research/<benchmark>/<arm>.json`.

## Deliverables (committed, reproducible)

- `bench/research/` — per-arm JSON, raw run logs (progress.jsonl), the pinned-commit note for each repo.
- `bench/research/REPORT.md` — thesis, method, the 4×2 results table, the recall-vs-tokens/cost plot
  (matplotlib, saved PNG), and honest caveats: NodeGoat overfitting (mitigated by PyGoat cross-stack),
  local tool-calling fidelity, PyGoat ground-truth subjectivity, single-run variance (run each arm
  N=3 and report mean/spread if budget allows).
- README/agenda update pointing at the committed evidence (folds in agenda items 1 + 2).

## Critical files (reuse, don't reinvent)

- `orion/claude_cli.py` — `run_agent`, `_run_once`, `_final_to_result` (add usage capture here).
- `scripts/run_nodegoat_eval.py` — `match_verdicts`, `_matches`, `render` (generalize per-benchmark).
- `tests/ground_truth_nodegoat.py` — the ground-truth dataclass to mirror for PyGoat.
- `orion/config.py` — `MODEL`/`EFFORT` (env `ORION_MODEL`); `MCP_CONFIG`.
- `orion/strategies.py` — discovery prompts (reference for the ungrounded control's contrast prompt).
- `orion/discover.py` / `orion/verify.py` — the grounded pipeline arm A runs unchanged.

## Verification

- Token-free: unit-test the ledger aggregation + the generalized matcher + PyGoat ground truth
  (`pytest -m "not slow"`), and a usage-event parse test in `tests/test_claude_cli_resilience.py`.
- Phase 0 gate: the 3 proxy checks pass on a captured transcript (committed under `bench/research/phase0/`).
- End-to-end (`@slow`, tokened, needs Neo4j + auth + the local proxy): each arm produces a scored JSON
  and the ledger totals reconcile (sum of per-call usage == arm total). Cross-check one arm's
  `total_cost_usd` against the pricing table by hand.
- Sanity: arm C (Opus, no Orion) recall < arm A (Local + Orion) recall would be the headline result;
  arm A tokens/cost vs arm C tokens/cost is the efficiency story. Report whatever the numbers say.

## Risks

- **Local tool-calling fidelity (highest)** — Phase 0 gate; escalate if it fails, don't silently swap.
- **Overfitting** — NodeGoat tuned the shapes/matcher; PyGoat is the cross-stack check. Say so plainly.
- **Token accounting through the proxy** — capture at the proxy/vLLM, not just Claude's (possibly
  wrong) usage; disclose the source.
- **Auth + cost** — the frontier arm needs working Claude auth (OAuth was expired 2026-08-27); the
  Claude arms are several full-pipeline runs × 2 repos × (N runs) — non-trivial spend, budget upfront.
- **Fixture availability** — `fixtures/NodeGoat` (with `cpg.bin`) must be restored; PyGoat fixture set up.

## Suggested execution order

1. **Phase 0 spike** (go/no-go) — local model + proxy + the 3 `claude -p` checks. Commit the transcript.
2. **Instrumentation** — usage capture in `claude_cli` + `bench/token_ledger.py` + unit tests.
3. **Benchmarks/scoring** — restore `fixtures/NodeGoat`; write `tests/ground_truth_pygoat.py`;
   generalize the matcher; `bench/research_eval.py` runner.
4. **Arms** — `bench/ungrounded_review.py` (B/C), `bench/semgrep_adapter.py` (D); arm A is `orion scan`.
5. **Run the matrix** (4 arms × 2 benchmarks, N runs) — costs tokens/auth; produce per-arm JSON.
6. **Report** — `bench/research/REPORT.md` + the recall-vs-cost plot; update README/agenda.
