# Phase 0 — local-model feasibility spike (go/no-go)

Environment: 11 GB RAM, CPU-only, WSL2 + Docker Desktop. Stack: **ollama** (container `orion-ollama`,
volume-backed) serving `qwen2.5-coder:3b`, behind **LiteLLM** (container `orion-litellm`,
`bench/phase0/litellm_config.yaml`) exposing an Anthropic `/v1/messages` endpoint on `:4000`. Both
containers on the `orion-net` Docker network. `claude -p` pointed at it via
`ANTHROPIC_BASE_URL=http://localhost:4000`, `ANTHROPIC_API_KEY=sk-orion-local`, `--model qwen-local`.

## Checks

**Check 0 — direct `/v1/messages` → ollama:** PASS.
Request `{"model":"qwen-local","messages":[{"role":"user","content":"Reply with exactly: OK"}]}` →
`{"type":"message","role":"assistant","content":[{"type":"text","text":"OK"}],"stop_reason":"end_turn"}`.

**Check 1 — plain `claude -p` through the proxy:** PASS (`is_error: false`).
Required one fix: the CLI sends adaptive `thinking`, which `qwen2.5-coder:3b` rejects
(`"does not support thinking"`); LiteLLM `additional_drop_params: ["thinking","reasoning_effort"]`
strips it. Also: containers must reach ollama by name over a shared network, not
`host.docker.internal` (which failed to connect here).

**Check 2 — MCP tool-calling:** **FAIL.**
`claude -p ... --mcp-config bench/phase0/mcp_orion.json --allowedTools mcp__orion__run_cypher`,
prompted to run a `count(m)` query against a real scan graph (scan `37c8…`, 27 CpgMethods). Parsed the
stream-json:
```
REAL_TOOL_USE_NAMES: []
CALLED_RUN_CYPHER: False
IS_ERROR: False
```
The model responded with text; it never emitted a `tool_use` block, so `run_cypher` was never called.

**Root cause — ollama returns tool calls as text, not structured `tool_calls`:**
Native `POST /api/chat` to ollama with a tool defined:
```
has_tool_calls: False
content: {"name": "get_count", "arguments": {"scan_id": "abc123"}}
```
The model selects the correct tool and arguments but emits them as a **text** JSON blob in
`message.content`; `message.tool_calls` is empty. LiteLLM therefore has no structured tool call to
translate into an Anthropic `tool_use` block, and the CLI sees plain text.

**Check 3 — `--json-schema`:** not reached (blocked by Check 2; structured tool_use is the same
mechanism the CLI uses to enforce output, and grounding is already impossible without Check 2).

**Second model — `qwen2.5-coder:7b`:** SAME failure. Native `/api/chat` with a tool returns
`has_tool_calls: False`; the model does not use the structured tool-calling channel either. So the
break is not specific to the 3B — both tested coder models, served by ollama 0.33.2 on this box, emit
tool calls as text rather than through `message.tool_calls`.

## Second pass — a model that CAN tool-call, and a working shim

Following the "try a different model / proxy" fallback:

- **`llama3.2:3b` emits STRUCTURED tool_calls** through ollama's native `/api/chat`
  (`has_tool_calls: True`, a real `tool_calls` array) — unlike the qwen coder models. So the
  tool-calling gap is model/template-specific, not fundamental.
- **LiteLLM's streaming + tools is broken:** a streaming `/v1/messages` call with a tool returns an
  EMPTY SSE stream — unusable for Orion (which streams). Replaced it with a purpose-built
  **Anthropic-Messages → ollama shim** (`bench/phase0/anthropic_ollama_shim.py`, stdlib-only): it
  translates tools + tool_result round-trips and emits chunked Anthropic SSE. `curl` validates the SSE
  as well-formed and the non-streaming path returns correctly.

## The binding blocker: CPU latency (measured)

With `llama3.2` + the shim, `claude -p` still could not complete a trivial "say HELLO" within 150 s.
The cause is **not** the SSE — it is inference latency on CPU:

| ollama `/api/chat` prompt | wall-clock (llama3.2:3b, CPU) |
|---|---|
| tiny ("say HI") | **8 s** |
| ~2k-token system prompt | **104 s** |

Claude Code sends a **large** system prompt (its harness prompt + tool definitions, ~10–20k tokens); at
that prompt-eval rate a *single* turn is **~300–600 s**, and it did not return inside the client window.
Orion's discovery runs many such turns per shape × 4 shapes, plus a verifier session per lead — each
under a 420–600 s per-call timeout. On this CPU box that does not complete.

## Verdict

**NO-GO on this hardware.** Two independent findings: (1) tool-calling is achievable — `llama3.2:3b`
emits structured tool_calls and the custom shim carries them (LiteLLM cannot); but (2) **CPU inference
of even a 3B model on Claude Code's large prompts is prohibitively slow** (8 s → 104 s from tiny → 2k
tokens; a real turn is 5–10 min and did not finish). The second is the binding constraint, and it is a
hardware limit, not a code one — the coder-model tool-calling gap is now moot.

**Unblock:** run the local model on a **GPU host** — that fixes the speed problem, and pairing it with a
runtime like vLLM (or `llama3.2` via the shim here) also gives structured tool-calling. The `bench/`
remote-scan kit targets exactly this GPU setup; `bench/phase0/anthropic_ollama_shim.py` is a committed,
reusable artifact for that run.
