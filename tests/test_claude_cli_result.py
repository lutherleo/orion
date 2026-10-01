"""`claude_cli._final_to_result` on a fenced reply. Local models served through Ollama answer the
--json-schema request inside a ```json fence (found by the eval's Phase 0 gate); exactly one fenced
JSON object is accepted, anything looser stays the `_error` sentinel. Pure."""
from __future__ import annotations

from orion import claude_cli


def _final(result: str) -> dict:
    return {"type": "result", "subtype": "success", "is_error": False, "result": result}


def test_one_fenced_object_parses():
    for text in ('```json\n{"file_count": 25, "query": "MATCH (f) RETURN f"}\n```\n',
                 '```\n{"file_count": 25, "query": "q"}\n```',
                 '  ```JSON\r\n{"file_count": 25, "query": "q"}\r\n```  '):
        assert claude_cli._final_to_result(_final(text))["file_count"] == 25


def test_unfenced_json_still_parses():
    assert claude_cli._final_to_result(_final('{"a": 1}')) == {"a": 1}


def test_anything_looser_than_one_fenced_object_stays_an_error():
    for text in (
        "Here is the answer:\n```json\n{\"a\": 1}\n```",            # prose before the fence
        "```json\n{\"a\": 1}\n```\nHope that helps!",                # prose after it
        "```json\n{\"a\": 1}\n```\n```json\n{\"b\": 2}\n```",        # two fences
        "```json\nnot json at all\n```",                             # fenced non-JSON
        "```json\n[1, 2]\n```",                                      # fenced non-object
    ):
        assert "_error" in claude_cli._final_to_result(_final(text)), text


def test_structured_output_still_wins():
    final = {**_final("```json\n{\"a\": 1}\n```"), "structured_output": {"b": 2}}
    assert claude_cli._final_to_result(final) == {"b": 2}
