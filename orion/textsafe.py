"""Neutralising untrusted repository text before it lands in a model's prompt.

The scanned repo is untrusted: a file can contain lines that look like a tool result, a ready-made
answer, a closing data marker, or the model's own chat-control tokens. Two layers, both applied by
`neutralise` to every repo-controlled string at the point of rendering:

  1. Chat-control tokens. A prompt is re-tokenised after rendering, and tokenizers parse their added
     tokens (`<|im_end|>`, `<|im_start|>`, `<think>`, Gemma's `<start_of_turn>`...) out of plain text,
     so a repo comment holding one could end the user turn and forge an assistant turn (an injected
     "final: no leads"). Every such token gets a backslash after its `<` (`<|im_end|>` ->
     `<\\|im_end|>`, `</think>` -> `<\\/think>`), which no tokenizer matches as a special token.
  2. Data-marker runs. Every run of three or more `<` or `>` gets a backslash after EACH character, so
     no `<<<END CODE>>>` can close a CODE block early (see `_MARKER_RUN`).

Both rewrites are deterministic and stable (neutralising twice changes nothing more), and they touch
real source too, always the same way. Moved from orion/lm/format.py on 2026-10-28 (layer 2 verbatim;
layer 1 new) so the Claude prompts can use it without importing orion.lm; lm/format re-exports it.
"""
from __future__ import annotations

import re

# Markers around every piece of text that came out of the scanned repository (read_code lines, flow
# code snippets). The repo is untrusted: it can contain lines that look like RESULT:/ERROR: turns or a
# ready-made {"action":...}. The markers tell the model where data ends; ACTION_RULES says it is data.
CODE_OPEN = "<<<CODE>>>"
CODE_CLOSE = "<<<END CODE>>>"

# Marker injection guard. Untrusted text may itself contain `<<<END CODE>>>` (or `<<<CODE>>>`), which
# would close the block early. Every run of three or more `<` or `>` gets a backslash after EACH
# character (`<<<` -> `<\<\<\`, `<<<<` -> `<\<\<\<\`), so no `<<<`/`>>>` can survive — a plain
# str.replace of the first three would leave `<<<<` as `<\<<<`, still a marker. Runs of one or two
# are untouched (they cannot form a marker). Deterministic, so training examples stay consistent.
# This rewrites REAL source too (`List<List<List<X>>>>`, CUDA `kernel<<<grid, block>>>`, heredoc or
# merge-conflict markers), always the same way, so the model sees `<\<\<\` wherever the file has a
# literal `<<<` — Krish's system prompt can say so. Every repo-controlled string that lands in a
# message (file names, function names, uids, code, rows, tool error text) goes through `_s`/`_neutralise`
# at the point of rendering, inside or outside a CODE block, so the marker count is always honest.
_MARKER_RUN = re.compile(r"<{3,}|>{3,}")

# Chat-control token shapes of the model families Orion-LM runs or bakes off: the `<|name|>` family
# (Qwen `<|im_start|>`/`<|im_end|>`/`<|endoftext|>`, Llama 3 `<|eot_id|>`, Granite `<|start_of_role|>`,
# FIM `<|fim_prefix|>`, ...) and the bare tags Qwen and Gemma add (`<think>`, `<tool_call>`,
# `<tool_response>`, `<start_of_turn>`, `<end_of_turn>`, `<bos>`, `<eos>`), opening or closing. HTML
# like `<s>` or `<b>` is not touched.
_CHAT_TOKEN = re.compile(
    r"<(?=\|[A-Za-z0-9_.:-]{1,64}\|>)"
    r"|<(?=/?(?:think|tool_call|tool_response|start_of_turn|end_of_turn|bos|eos)>)")


def neutralise(text: str) -> str:
    """`text` with chat-control tokens broken and marker runs escaped (see the module doc)."""
    text = _CHAT_TOKEN.sub("<\\\\", text)
    return _MARKER_RUN.sub(lambda m: "".join(ch + "\\" for ch in m.group()), text)


def code_block(text: str) -> str:
    """Wrap untrusted repository text in CODE markers (multi-line: markers on their own lines)."""
    return f"{CODE_OPEN}\n{neutralise(text)}\n{CODE_CLOSE}"


def code_inline(text: str) -> str:
    """Wrap a one-line untrusted snippet in CODE markers."""
    return f"{CODE_OPEN}{neutralise(text)}{CODE_CLOSE}"
