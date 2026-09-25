"""Discovery fleet: fan out the 4 shapes (A/B/C/D) concurrently, each as ONE `claude -p` session
with real MCP tool-calling. Claude Code loops internally over `mcp__orion__run_cypher` as many
times as it wants within a shape; `--json-schema` forces its final answer into a leads array, so
there is no text-protocol parsing here anymore (see claude_cli.py for the old PoC's `CYPHER:`/
`FINAL:` loop, now gone).

Nothing here is a confirmed finding. Every Lead is a CANDIDATE that a separate verifier session
(Task D) must independently re-derive before it can be reported.
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace
from datetime import datetime, timezone

from . import claude_cli, config, strategies
from .contracts import Lead, OnEvent, ProgressEvent, clean_location

SHAPES: tuple[str, ...] = ("A", "B", "C", "D")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _event(
    *, phase: str, shape: str | None = None, lead: int | None = None,
    turn: int | None = None, event: str, detail: str = "",
) -> ProgressEvent:
    return {
        "ts": _now(), "phase": phase, "shape": shape, "lead": lead,
        "turn": turn, "event": event, "detail": detail,
    }


def _to_leads(final_json: dict, shape: str) -> list[Lead]:
    """Map a parsed `{"leads": [...]}` structured object onto contracts.Lead.

    Pure and defensive: a malformed item is dropped, never fabricated. Never raises -- a
    surprising shape from the model becomes fewer leads, not a crash.
    """
    if not isinstance(final_json, dict):
        return []
    raw = final_json.get("leads")
    if not isinstance(raw, list):
        return []

    leads: list[Lead] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        evidence = item.get("evidence")
        if not text or not evidence:
            continue
        confidence = item.get("confidence")
        if confidence not in ("LOW", "MEDIUM", "HIGH"):
            confidence = "LOW"
        item_shape = item.get("shape")
        if item_shape not in ("A", "B", "C", "D"):
            item_shape = shape
        source_uid = item.get("source_uid") or None
        sink_uid = item.get("sink_uid") or None
        loc = clean_location(item)
        loc.pop("severity", None)           # a verdict field; leads carry confidence instead
        leads.append(Lead(index=i, shape=item_shape, text=text, evidence=evidence,
                          confidence=confidence, source_uid=source_uid, sink_uid=sink_uid, **loc))
    return leads


_CONFIDENCE_RANK = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}


def _dedup(leads: list[Lead]) -> list[Lead]:
    """Collapse duplicate leads, keeping the first occurrence and reassigning sequential indices.
    Every collapsed duplicate is one verifier session (the run's dominant cost) not spent.

    STRUCTURAL first: a lead anchored on a :CandidateFlow carries `source_uid`+`sink_uid`; two leads
    with the same (source_uid, sink_uid) are the SAME flow no matter how differently they are worded,
    so they collapse on that endpoint pair alone -- this is what the lexical key silently missed
    (agents describing one flow in different words double-counted). We key on the exact endpoint pair
    rather than clustering on partial (source-only / sink-only) overlap on purpose: two genuinely
    distinct bugs that merely share a source must NOT be merged in a precision-first tool.

    FINGERPRINT next: a lead that names its file + CWE + function (or line) is the same bug as any
    other lead with that fingerprint, whichever shape found it and however it is worded -- two
    shapes routinely report one XSS. The HIGHER-confidence lead is kept, in the earlier one's slot,
    unless the earlier one is structurally anchored (then it always stays).
    The key includes the function/line, so two bugs of one class in different functions stay apart.

    LEXICAL fallback: leads with neither anchor keep the original `(shape, text[:80])` key. The
    keyspaces are disjoint, so leads keyed differently never collide."""
    seen_struct: set[tuple[str, str]] = set()
    seen_lex: set[tuple[str, str]] = set()
    by_fingerprint: dict[tuple, int] = {}          # fingerprint -> position in `kept`
    kept: list[Lead] = []
    for lead in leads:
        fp = lead.fingerprint()
        if lead.source_uid and lead.sink_uid:
            key = (lead.source_uid, lead.sink_uid)
            if key in seen_struct:
                continue
            seen_struct.add(key)
            if fp is not None:
                by_fingerprint.setdefault(fp, len(kept))   # later unanchored duplicates fold into it
        elif fp is not None:
            pos = by_fingerprint.get(fp)
            if pos is not None:
                # An anchored lead is never displaced: its flow endpoints feed the verifier's
                # evidence subgraph. Otherwise the more confident report of the bug wins.
                incumbent = kept[pos]
                if (not incumbent.source_uid
                        and _CONFIDENCE_RANK[lead.confidence] > _CONFIDENCE_RANK[incumbent.confidence]):
                    kept[pos] = lead
                continue
            by_fingerprint[fp] = len(kept)
        else:
            key = (lead.shape, lead.text[:80])
            if key in seen_lex:
                continue
            seen_lex.add(key)
        kept.append(lead)
    return [replace(lead, index=i) for i, lead in enumerate(kept)]


async def _run_shape(scan_id: str, shape: str, on_event: OnEvent, profile=None,
                     timeout: int | None = None, dynamic_hint: bool = False) -> list[Lead]:
    on_event(_event(phase="discover", shape=shape, event="start", detail=f"shape {shape} sweep starting"))

    def shape_on_event(ev: dict) -> None:
        on_event(_event(
            phase="discover", shape=shape,
            event=ev.get("event", "tool"), detail=ev.get("detail", ""),
        ))

    system = strategies.system_for(shape, scan_id, profile=profile, dynamic_hint=dynamic_hint)
    message = (
        f'scan_id = "{scan_id}". Begin your Shape {shape} sweep now. Every '
        f'mcp__orion__run_cypher call must pass scan_id="{scan_id}" and filter the query by '
        f"scan_id:$scan_id."
    )
    session_id = str(uuid.uuid4())

    try:
        result = await asyncio.to_thread(
            claude_cli.run_agent, session_id, system, message,
            json_schema=strategies.LEADS_JSON_SCHEMA, on_event=shape_on_event,
            max_turns=config.MAX_TURNS,
            timeout=config.DISCOVER_TIMEOUT if timeout is None else timeout,
            # a transient claude -p crash on one shape shouldn't silently drop its whole lead set.
            retries=1,
        )
    except Exception as exc:  # noqa: BLE001 -- a thread/subprocess failure is never a phantom lead
        on_event(_event(phase="discover", shape=shape, event="error", detail=f"{exc.__class__.__name__}: {exc}"))
        return []

    if not isinstance(result, dict) or "_error" in result:
        detail = result.get("_error", "malformed result") if isinstance(result, dict) else "malformed result"
        on_event(_event(phase="discover", shape=shape, event="error", detail=detail))
        return []

    leads = _to_leads(result, shape)
    on_event(_event(phase="discover", shape=shape, event="done", detail=f"{len(leads)} lead(s)"))
    return leads


async def _discover_async(scan_id: str, on_event: OnEvent, profile=None,
                          timeout: int | None = None, dynamic_hint: bool = False) -> list[Lead]:
    results = await asyncio.gather(
        *(_run_shape(scan_id, shape, on_event, profile, timeout, dynamic_hint) for shape in SHAPES))
    all_leads = [lead for shape_leads in results for lead in shape_leads]
    return _dedup(all_leads)


def discover(scan_id: str, on_event: OnEvent, profile=None, timeout: int | None = None,
             dynamic_hint: bool = False) -> list[Lead]:
    """Fan out the 4 discovery shapes CONCURRENTLY (each a blocking `run_agent` subprocess call
    run in a thread), dedup, and return `list[Lead]`.

    A shape that fails (subprocess error, timeout, `is_error`, malformed/missing structured
    output) contributes zero leads plus one "error" event -- it never raises out of the gather, so
    the other shapes still complete and their leads survive.

    `profile` (graph/profiles.Profile) is optional per-stack prompt vocabulary; None keeps the
    framework-agnostic default prompt (valid for any repo).

    `timeout` is the per-shape `claude -p` wall-clock budget in seconds; None uses the reality-based
    floor `config.DISCOVER_TIMEOUT`. Callers that know the graph size pass a scaled value from
    `config.discover_timeout(node_count)` so large repos get proportionally longer sweeps.

    `dynamic_hint` (default False) appends the runtime-facts block to every shape prompt so the fleet
    uses what the runtime stage (`--runtime` / `orion trace`) wrote. Off keeps the prompt
    byte-identical to the eval baseline."""
    return asyncio.run(_discover_async(scan_id, on_event, profile, timeout, dynamic_hint))
