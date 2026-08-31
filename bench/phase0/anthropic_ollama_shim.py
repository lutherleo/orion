#!/usr/bin/env python
"""Minimal Anthropic-Messages -> ollama translator (PLAN2 Phase 0), stdlib-only.

Why this exists: `claude -p` speaks the Anthropic Messages API and streams; ollama serves the local
model with NATIVE tool-calling + streaming, but LiteLLM's Anthropic passthrough breaks on
streaming+tools (returns an empty SSE stream). This shim replaces LiteLLM: it accepts POST /v1/messages
(streaming or not), translates to ollama POST /api/chat, and re-emits Anthropic SSE — including
tool_use blocks — so Orion's MCP tool-calling survives to a local model that emits structured tool_calls
(e.g. llama3.2:3b).

Run:  OLLAMA_URL=http://localhost:11434 SHIM_MODEL=llama3.2:3b python anthropic_ollama_shim.py 4001
Point claude at it:  ANTHROPIC_BASE_URL=http://localhost:4001  ANTHROPIC_API_KEY=anything

Scope: text + tool_use + tool_result round-trips, streaming and non-streaming. Not a full API (no
images, no cache_control semantics — those are dropped). Deterministic and debuggable, unlike the
LiteLLM path.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/")
MODEL = os.environ.get("SHIM_MODEL", "llama3.2:3b")


# ── translation: Anthropic request -> ollama /api/chat payload ────────────────────────────────
def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content
                       if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _to_ollama_messages(system, messages) -> list[dict]:
    out: list[dict] = []
    if system:
        out.append({"role": "system", "content": _text_of(system)})
    for m in messages:
        role = m.get("role")
        content = m.get("content")
        if role == "assistant" and isinstance(content, list):
            # split assistant text and tool_use blocks
            text = _text_of(content)
            tool_calls = [{"function": {"name": b.get("name"),
                                        "arguments": b.get("input", {})}}
                          for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]
            msg: dict = {"role": "assistant", "content": text}
            if tool_calls:
                msg["tool_calls"] = tool_calls
            out.append(msg)
        elif role == "user" and isinstance(content, list) and any(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            # each tool_result -> an ollama {role:tool} message
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    c = b.get("content")
                    out.append({"role": "tool", "content": _text_of(c) if isinstance(c, list) else str(c)})
            txt = _text_of(content)
            if txt:
                out.append({"role": "user", "content": txt})
        else:
            out.append({"role": role, "content": _text_of(content)})
    return out


def _to_ollama_tools(tools) -> list[dict]:
    out = []
    for t in tools or []:
        out.append({"type": "function", "function": {
            "name": t.get("name"),
            "description": t.get("description", ""),
            "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
        }})
    return out


def _requested_schema(body: dict) -> dict | None:
    """The JSON schema the caller wants the output constrained to, if any. The Anthropic Messages API
    carries it at output_config.format.schema (structured outputs); the claude CLI's --json-schema maps
    there. Returned so we can hand it to ollama's `format` param for constrained decoding — which is how
    a weak local model still emits valid JSON (arm B's failure was unconstrained freeform)."""
    oc = body.get("output_config") or {}
    fmt = oc.get("format") or {}
    schema = fmt.get("schema") or fmt.get("json_schema")
    return schema if isinstance(schema, dict) else None


def _ollama_payload(body: dict) -> dict:
    payload = {
        "model": MODEL,
        "messages": _to_ollama_messages(body.get("system"), body.get("messages", [])),
        "tools": _to_ollama_tools(body.get("tools")),
        "stream": True,
        "options": {"num_predict": min(int(body.get("max_tokens", 1024)), 4096)},
    }
    schema = _requested_schema(body)
    if schema is not None:
        payload["format"] = schema        # ollama constrained decoding -> valid JSON from a weak model
    return payload


# ── SSE helpers ───────────────────────────────────────────────────────────────────────────────
def _sse_bytes(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"        # so Content-Length framing is honored by the claude client

    def log_message(self, *a):  # quiet
        pass

    def _read_body(self) -> dict:
        n = int(self.headers.get("content-length", 0))
        return json.loads(self.rfile.read(n) or b"{}")

    def _log(self, msg: str) -> None:
        sys.stderr.write(msg + "\n"); sys.stderr.flush()

    def do_GET(self):  # noqa: N802 — some clients probe /v1/models etc.
        self._log(f"GET {self.path}")
        payload = {"data": [{"id": MODEL, "type": "model", "display_name": MODEL}]}
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):  # noqa: N802
        self._log(f"POST {self.path}")
        # Token-counting probe some clients issue before a message — answer it cheaply.
        if self.path.startswith("/v1/messages/count_tokens"):
            try:
                body = self._read_body()
            except (ValueError, json.JSONDecodeError):
                body = {}
            approx = sum(len(_text_of(m.get("content"))) for m in body.get("messages", [])) // 4 + 1
            data = json.dumps({"input_tokens": approx}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if not self.path.startswith("/v1/messages"):
            self.send_response(404)
            self.send_header("content-length", "0")
            self.end_headers()
            return
        try:
            body = self._read_body()
        except (ValueError, json.JSONDecodeError):
            self.send_response(400); self.send_header("content-length", "0"); self.end_headers(); return
        stream = bool(body.get("stream"))
        n_tools = len(body.get("tools") or [])
        try:
            texts, tool_calls = self._call_ollama(body)
        except Exception as exc:  # noqa: BLE001 — surface as an Anthropic-ish error
            self._log(f"ollama ERROR: {exc}")
            self._error(str(exc)); return
        self._log(f"ollama ok: in_tools={n_tools} text_len={len(''.join(texts))} tool_calls={len(tool_calls)} stream={stream}")
        stop_reason = "tool_use" if tool_calls else "end_turn"
        try:
            if stream:
                self._stream_out("".join(texts), tool_calls, stop_reason)
            else:
                self._json_out("".join(texts), tool_calls, stop_reason)
            self._log("response written ok")
        except Exception as exc:  # noqa: BLE001
            self._log(f"write ERROR: {exc}")

    def _chunk(self, data: bytes) -> None:
        self.wfile.write(f"{len(data):X}\r\n".encode() + data + b"\r\n")
        self.wfile.flush()

    def _call_ollama(self, body: dict):
        """POST to ollama /api/chat (streaming NDJSON); accumulate text + tool_calls."""
        payload = _ollama_payload(body)
        req = urllib.request.Request(f"{OLLAMA_URL}/api/chat",
                                     data=json.dumps(payload).encode(),
                                     headers={"content-type": "application/json"})
        texts: list[str] = []
        tool_calls: list[dict] = []
        with urllib.request.urlopen(req, timeout=600) as resp:
            for raw in resp:
                raw = raw.strip()
                if not raw:
                    continue
                chunk = json.loads(raw)
                msg = chunk.get("message") or {}
                if msg.get("content"):
                    texts.append(msg["content"])
                for tc in msg.get("tool_calls") or []:
                    fn = tc.get("function") or {}
                    args = fn.get("arguments")
                    if isinstance(args, str):
                        try: args = json.loads(args)
                        except json.JSONDecodeError: args = {}
                    tool_calls.append({"name": fn.get("name"), "input": args or {}})
                if chunk.get("done"):
                    break
        return texts, tool_calls

    def _content_blocks(self, text: str, tool_calls: list[dict]) -> list[dict]:
        blocks: list[dict] = []
        if text:
            blocks.append({"type": "text", "text": text})
        for i, tc in enumerate(tool_calls):
            blocks.append({"type": "tool_use", "id": f"toolu_{i}", "name": tc["name"], "input": tc["input"]})
        return blocks

    def _json_out(self, text: str, tool_calls: list[dict], stop_reason: str) -> None:
        msg = {"id": "msg_shim", "type": "message", "role": "assistant", "model": MODEL,
               "content": self._content_blocks(text, tool_calls), "stop_reason": stop_reason,
               "stop_sequence": None, "usage": {"input_tokens": 0, "output_tokens": 0}}
        data = json.dumps(msg).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _stream_out(self, text: str, tool_calls: list[dict], stop_reason: str) -> None:
        # We already have the full model output (ollama is drained before we emit), so build the whole
        # SSE body and send it with a Content-Length — stdlib http.server does no chunked encoding, and
        # a bodiless-framed stream is what made the claude client abort. One buffered SSE payload is
        # unambiguous and the client parses it as a complete stream.
        parts = [_sse_bytes("message_start", {"type": "message_start", "message": {
            "id": "msg_shim", "type": "message", "role": "assistant", "model": MODEL,
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0}}})]
        idx = 0
        if text:
            parts.append(_sse_bytes("content_block_start", {"type": "content_block_start", "index": idx,
                                    "content_block": {"type": "text", "text": ""}}))
            parts.append(_sse_bytes("content_block_delta", {"type": "content_block_delta", "index": idx,
                                    "delta": {"type": "text_delta", "text": text}}))
            parts.append(_sse_bytes("content_block_stop", {"type": "content_block_stop", "index": idx}))
            idx += 1
        for i, tc in enumerate(tool_calls):
            parts.append(_sse_bytes("content_block_start", {"type": "content_block_start", "index": idx,
                 "content_block": {"type": "tool_use", "id": f"toolu_{i}", "name": tc["name"], "input": {}}}))
            parts.append(_sse_bytes("content_block_delta", {"type": "content_block_delta", "index": idx,
                 "delta": {"type": "input_json_delta", "partial_json": json.dumps(tc["input"])}}))
            parts.append(_sse_bytes("content_block_stop", {"type": "content_block_stop", "index": idx}))
            idx += 1
        parts.append(_sse_bytes("message_delta", {"type": "message_delta", "delta": {
            "stop_reason": stop_reason, "stop_sequence": None}, "usage": {"output_tokens": 0}}))
        parts.append(_sse_bytes("message_stop", {"type": "message_stop"}))
        # Chunked transfer encoding: the canonical SSE transport. A Content-Length'd event-stream made
        # the claude client abort; chunks + a terminating 0-chunk is what its streaming parser expects.
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.send_header("transfer-encoding", "chunked")
        self.end_headers()
        for p in parts:
            self._chunk(p)
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def _error(self, message: str) -> None:
        data = json.dumps({"type": "error", "error": {"type": "api_error", "message": message}}).encode()
        self.send_response(500)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 4001
    print(f"anthropic->ollama shim on :{port} -> {OLLAMA_URL} model={MODEL}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
