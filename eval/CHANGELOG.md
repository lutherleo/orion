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
