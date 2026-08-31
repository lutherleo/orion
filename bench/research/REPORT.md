# Orion research eval — does GraphRAG grounding lift a local LLM to frontier vuln-finding?

**Status:** partial (2026-08-31). Infrastructure and scoring complete; Semgrep baseline run on both
benchmarks; the LLM arms are gated on a working local-model tool-calling stack (Phase 0) and a valid
Claude login. This report is committed as living evidence — it states exactly what is measured and
what is pending, and updates as arms complete. Method and plan of record: `PLAN2.md`.

## Thesis

1. **Uplift** — a *local* open-weights LLM driving Orion's graph-grounded pipeline should reach (or
   beat) a *frontier* model working alone: grounding + a fixed 4-shape discovery + an independent
   verifier substituting for raw model capability.
2. **Better than fixed rules** — Orion should find vulns a deterministic scanner (Semgrep) misses,
   without the false-positive flood — scored through Orion's *own* matcher.

Everything is scored the same way: a finding is a `(text, evidence)` pair; it credits a ground-truth
vuln iff the vuln's file appears in text+evidence AND a distinctive class token appears in the claim
(`bench/scoring.py`, lifted verbatim from the committed NodeGoat matcher). Token/cost is a first-class
axis (`bench/token_ledger.py`).

## Arms × benchmarks

| Arm | Model | Grounding | Harness |
|-----|-------|-----------|---------|
| A. Local + Orion | qwen2.5-coder (ollama, proxied) | full graph + verifier | `orion scan` |
| B. Local, no Orion | same local model | none (reads source) | `bench/ungrounded_review.py` |
| C. Frontier, no Orion | `claude-opus-5` | none (reads source) | `bench/ungrounded_review.py` |
| D. Semgrep | — | rules | `bench/semgrep_adapter.py` |

Benchmarks: **NodeGoat** (15 vulns, `tests/ground_truth_nodegoat.py`) and **PyGoat** (16 vulns,
`tests/ground_truth_pygoat.py`, labeled from source this session).

## Results

| Arm | NodeGoat recall | NG FP-cand | NG tokens / cost | PyGoat recall | PG FP-cand | PG tokens / cost | Status |
|-----|-----------------|-----------|------------------|---------------|-----------|------------------|--------|
| **C. Opus-5, no Orion** | **15 / 15** | 5 | 326k / **$0.61** | **14 / 16** | 10 | 446k / **$0.84** | ✅ measured |
| **D. Semgrep** | **4 / 15** | 26 | 0 / $0 | **7 / 16** | 85 | 0 / $0 | ✅ measured |
| **B. Local, no Orion** (llama3.2:3b) | — | — | — | **0 / 16** | 0 | 0¹ | 🟡 ran, no valid findings |
| A. Local + Orion | — | — | — | — | — | — | ⛔ CPU latency (see Phase 0) |
| *Orion (prior, committed)* | *14 / 15* | *0 true FP* | *—* | *~20 findings* | *—* | *—* | *reference* |

¹ Arm B ran end-to-end locally (llama3.2:3b via `bench/phase0/anthropic_ollama_shim.py`, ~9.4 min on
CPU) but returned **0/16**: the model failed to emit schema-valid JSON leads (`result field was not
valid JSON`, both attempts). This is partly a model limit (a 3B freeform is unreliable at structured
output) and partly a shim limit (it does not implement the constrained decoding Claude's API applies
for `--json-schema` — a fixable follow-on via ollama's `format` schema). Token/cost is 0 because the
shim reports no usage. Committed at `bench/research/pygoat/B.json`; treat as "local-on-CPU produced no
usable findings here," not a clean model-capability score.

**What the two measured arms show.**

- *Frontier LLM alone (arm C, Opus-5, no graph)* is strong: **15/15 on NodeGoat** and **14/16 on
  PyGoat**, at **$0.61 / $0.84** per repo and ~2–3 min. Notably it catches classes the *graph* cannot
  — it scored the NodeGoat A9 "components with known vulnerabilities" (a known graph data-gap) by
  simply reading `package.json`, and found PyGoat's outdated-components + insecure-design items. Its
  weakness is precision: 5 and 10 false-positive candidates respectively. So a frontier model reading
  source is a high bar — the uplift claim is specifically that a *local* model + grounding can rival
  *this*, which we could not test on-box (see Phase 0).
- *Semgrep (arm D)* is the deterministic-scanner profile in one line: **4/15 NodeGoat, 7/16 PyGoat**,
  catching pattern-matchable sink bugs (CMDI, code-exec, MD5, pickle, SQLi, SSRF, XSS) but **blind to
  every design/semantic class** (SSTI, XXE, YAML-deser, IDOR, misconfig, auth, insecure-design,
  logging) and carrying a heavy false-positive tail (26 / 85 unmatched). Far below both the frontier
  LLM and Orion's committed 14/15 @ 0 true FP — claim (2), with data on both stacks.

The efficiency axis (claim 1) needs the local arms to land: **cost-per-confirmed-vuln** for arm C is
$0.61/15 ≈ **$0.041** (NodeGoat) and $0.84/14 ≈ **$0.060** (PyGoat) — the numbers a local+Orion arm
would be compared against once it runs on adequate hardware.

## Phase 0 — local-model feasibility (go/no-go)

The "local only" thesis hinges on `claude -p` driving a local model through an Anthropic-compatible
proxy while preserving MCP tool-calling. Stack built this session: **ollama** (Docker) serving
`qwen2.5-coder:3b`, behind a **LiteLLM** proxy exposing `/v1/messages`, on 11 GB RAM / CPU-only.

Findings, in order:
1. **Plumbing works.** A direct Anthropic `/v1/messages` call returns a valid response from the local
   model; `claude -p --model qwen-local` completes with `is_error: false` (Check 1 ✅). One config fix
   was needed — the CLI sends adaptive `thinking`, which the 3B model rejects; LiteLLM
   `additional_drop_params: ["thinking"]` strips it. Containers must share a Docker network (reach
   ollama by name, not `host.docker.internal`).
2. **Structured tool-calling does NOT survive (the blocker).** With the MCP tool offered, the model
   never emits a real `tool_use` block — `CALLED_RUN_CYPHER: False`, no structured tool calls. Isolated
   to the root cause: **ollama's native `/api/chat` returns the tool call as *text* in
   `message.content`** (`{"name":"get_count","arguments":{...}}`) with `has_tool_calls: False` — the
   model does not use the structured tool-calling channel at all for `qwen2.5-coder:3b`. LiteLLM has
   nothing to translate, so the CLI sees plain text and never executes the tool.
3. **Consequence.** Orion's grounding *requires* the agent to actually call `run_cypher`; arm A cannot
   work with this model. Arm B also needs tool-calling (Read/Grep/Glob), so it is blocked too.

**Two findings, one verdict — NO-GO on this box:**
1. *Tool-calling is achievable.* The qwen **coder** models (`3b`, `7b`) emit tool calls as text
   (`has_tool_calls: False`) and fail, but **`llama3.2:3b` emits structured `tool_calls`**. LiteLLM's
   streaming+tools path is broken (empty SSE), so I wrote a stdlib **Anthropic→ollama shim**
   (`bench/phase0/anthropic_ollama_shim.py`) that carries tools + tool_result round-trips and emits
   valid chunked Anthropic SSE. So the grounding-critical tool-calling can be made to work locally.
2. *CPU latency is the binding blocker.* Even with that, `claude -p` could not finish a trivial turn:
   ollama on CPU takes **8 s** for a tiny prompt but **104 s** for a ~2k-token prompt, and Claude
   Code's real system prompt (~10–20k tokens) pushes a *single* turn to **~300–600 s** — past Orion's
   420–600 s per-call timeouts, before multiplying by turns × 4 shapes × per-lead verify.

So the local arms are infeasible **on this hardware** for a *speed* reason, not a code one. Unblock: a
GPU host (fixes speed; vLLM or the shim gives tool-calling) — the `bench/` remote kit targets exactly
that. Full evidence + the working shim: `bench/research/phase0/RESULTS.md`.

Transcript evidence is under `bench/research/phase0/`.

## Token & cost methodology

`claude_cli` now emits a `usage` event per agent call (input/output/cache tokens + the CLI's
`total_cost_usd`), which `bench.token_ledger.TokenLedger` aggregates per phase and prices from the
Anthropic table (opus-5 $5/$25, sonnet-5 $3/$15, haiku-4.5 $1/$5 per 1M; cache write ×1.25, read ×0.1).
Local tokens price at $0 (no API charge) — the local cost is wall-clock/compute, reported separately.
Through a non-Anthropic proxy the CLI's own `total_cost_usd` may be null; the ledger then prices from
tokens (and, for the local arm, would prefer proxy-reported counts). No LLM arm has produced token
figures yet.

## Honest caveats

- **Overfitting.** Orion's 4 shapes and the NodeGoat matcher were tuned on NodeGoat; PyGoat is the
  cross-stack check (new ground truth, no tuning). Report both; do not read NodeGoat alone as proof.
- **PyGoat ground truth is this session's labeling** from source — reasonable but not authoritative;
  `RECALL_BAR` there is a provisional target, not a claim.
- **Local tool-calling fidelity** is the demonstrated bottleneck, not model "intelligence" — the 3B
  model *chose* the right tool and args, but emitted them as text. This is a capability/serving-format
  gap, and it is exactly what Phase 0 exists to surface.
- **Single runs.** No arm is averaged over N yet; add N=3 mean/spread before drawing strong conclusions.

## Reproducing

```
# Semgrep baseline (no tokens, no login):
python bench/research_eval.py --arm D --benchmark pygoat   --repo fixtures/pygoat
python bench/research_eval.py --arm D --benchmark nodegoat --repo fixtures/NodeGoat
python bench/plot_results.py

# Local stack (Phase 0): ollama + LiteLLM in Docker (see bench/phase0/), then
ORION_MODEL=qwen-local ANTHROPIC_BASE_URL=http://localhost:4000 ANTHROPIC_API_KEY=sk-orion-local \
  python bench/research_eval.py --arm B --benchmark pygoat --repo fixtures/pygoat   # once Phase 0 is GO

# Frontier baseline (needs a valid `claude` login):
python bench/research_eval.py --arm C --benchmark pygoat --repo fixtures/pygoat --model claude-opus-5
```
