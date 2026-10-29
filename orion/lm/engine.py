"""`discover_lm`: the in-process Orion-LM discovery stage, signature-parallel to `discover.discover`.

Reads the scan's :CandidateFlow slices (orion.lm.slice), enriches each into a CodeCase (orion.lm.cases:
the sink's function plus source/caller/callee snippets from one RepoIndex built per scan), works them in
rank order in batches of `k` (DEFAULT_K = 1: one case per session), runs
one `prompter.run_session` per batch against an already-loaded model (`model.generate`), and maps the
model's final `leads` onto `contracts.Lead` (shape "LM") with `file/function/line/cwe/path` filled and
`source_uid`/`sink_uid` copied from the flow the lead names by `flow_id` -- so the shared structural
dedup (same key as `discover._dedup`) collapses two leads on one flow.

Rules this module is built around:
  * Never imports `claude_cli` (nor `orion.discover`, which imports it at module top); dedup is the
    Claude-free shared `orion.dedup` (structural, then fingerprint, then lexical -- the same rules as
    the Claude fleet's).
  * A failure is an `error` event + zero leads for THAT batch; other batches still run, the scan never
    crashes and no lead is ever fabricated.
  * A lead whose `flow_id` is missing or not in its batch is attached to the batch's first flow and tagged
    with evidence starting "flow_id unresolved" -- never dropped silently.
  * Every prompter step goes to `on_step(step)` untouched (Task F3 logs them; `orion/` must not import
    `eval/`).

`_load_flows` and `_session` are module-level seams so the token-free test can stand in for Neo4j and
the model.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import time

from orion.contracts import Confidence, Lead, OnEvent, ProgressEvent, clean_location
from orion.dedup import dedup_keep
from orion import config
from orion.lm import cases, prompter
from orion.lm import slice as lm_slice
from orion.lm.cases import CaseBudget
from orion.lm.codeindex import RepoIndex

SHAPE = "LM"
DEFAULT_K = 1  # one enriched case per session
DEFAULT_MAX_FLOWS = 50  # top-ranked flows worked per scan (train/stage2.py samples its negatives from this range)
DEFAULT_MAX_FINDINGS = 30  # shortlist rows judged as code-only cases per scan (--shortlists)
RUNTIME_POOL = 300  # with runtime data, the flows worked are re-ranked from this many (pathfind's cap)
MIN_CONTEXT_CAP = 512  # floor for the prompter's budget cap; below this no session can do useful work
_CONFIDENCES: tuple[Confidence, ...] = ("LOW", "MEDIUM", "HIGH")
# Step kinds -> progress-event names. run_cypher is a `query`; the rest of a session is `tool`, except
# the kinds that mean the model misbehaved or the context ran out, which are `warn`.
_STEP_EVENTS = {"run_cypher": "query", "read_code": "tool", "final": "tool", "forced_final": "tool",
                "malformed": "warn", "context_overflow": "warn"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _event(*, event: str, detail: str = "", lead: int | None = None, turn: int | None = None,
           phase: str = "discover") -> ProgressEvent:
    """Same dict shape as `discover._event` / `cli._event`; shape is fixed for this engine, phase is
    "discover" except for the lm verifier's ("verify")."""
    return {"ts": _now(), "phase": phase, "shape": SHAPE, "lead": lead,
            "turn": turn, "event": event, "detail": detail}


def _raw_cap(model) -> int:
    return int(getattr(model, "context_cap", 8000)) - prompter.CONTEXT_MARGIN


def prompter_cap(model) -> int:
    """The prompter's budget cap for `model`: `prompter.CONTEXT_MARGIN` below its hard `context_cap` (8000 when
    the object has none), floored at MIN_CONTEXT_CAP. `discover_lm` and the training rounds both use it."""
    return max(_raw_cap(model), MIN_CONTEXT_CAP)


# ---- seams (monkeypatched by the token-free test) ------------------------------------------------

def _load_flows(db, scan_id: str, max_flows: int, *, runtime: bool = False) -> list[dict]:
    """Top `max_flows` CandidateFlows of the scan, each as a `flow_slice` dict (raises RuntimeError on a
    query error -- the caller turns that into an `error` event).

    `runtime` (merge Phase 4, opt-in: `--runtime` / `--use-runtime`): the top `max_flows` are taken from
    pathfind's whole list (up to RUNTIME_POOL) after `_runtime_order` -- flows whose sink ran first. With
    no executed call in the graph the result is exactly the plain top `max_flows`."""
    if not runtime:
        flows = lm_slice.candidate_flows(db, scan_id, max_flows=max_flows)
    else:
        pool = lm_slice.candidate_flows(db, scan_id, max_flows=RUNTIME_POOL)
        ran = lm_slice.executed_uids(db, scan_id)
        flows = (_runtime_order(pool, ran) if ran else pool)[:max_flows]
    return [lm_slice.flow_slice(db, scan_id, f) for f in flows]


def _runtime_order(flows: list[dict], ran: set[str]) -> list[dict]:
    """Flows whose sink call EXECUTED first, then by the share of their path calls that executed, then
    pathfind's own rank. Deterministic; the flow ids (F<rank>) keep pathfind's rank."""
    def key(f: dict):
        path = f.get("path_uids") or []
        covered = sum(1 for u in path if u in ran) / len(path) if path else 0.0
        rank = f.get("rank") if isinstance(f.get("rank"), int) else 1 << 30
        return (f.get("sink_uid") not in ran, -covered, rank)
    return sorted(flows, key=key)


def _load_findings(db, scan_id: str, max_findings: int) -> list[dict]:
    """The top shortlist rows (`--shortlists`), a seam like `_load_flows`."""
    return lm_slice.candidate_findings(db, scan_id, max_findings=max_findings)


def _finding_cases(rows: list[dict], index: RepoIndex, budget: CaseBudget, flows: list[dict],
                   on_event: OnEvent) -> list[dict]:
    """Shortlist rows as code-only CodeCases (`cases.case_from_function`, the Stage 1 shape C1 was
    trained on): the function enclosing each row's file:line, with its callers/callees and Orion's sink
    line. A function a flow already targets, or one an earlier row already gave, is skipped."""
    covered = {(sn.get("file"), sn.get("function")) for f in flows for sn in (f.get("snippets") or [])
               if sn.get("role") == "target"}
    out, seen = [], set()
    for r in rows:
        file, line = r.get("file"), r.get("line")
        if not isinstance(file, str) or not isinstance(line, int) or isinstance(line, bool):
            continue
        span = index.enclosing(file, line)
        if span is None or (span.file, span.start, span.end) in seen or (span.file, span.name) in covered:
            continue
        seen.add((span.file, span.start, span.end))
        try:
            case = cases.case_from_function(index, file, span, anchor=line,
                                            case_id=f"S{r.get('shape')}{r.get('rank')}", budget=budget)
        except Exception as exc:
            on_event(_event(event="warn", detail=f"shortlist case for {file}:{line} failed: "
                                                 f"{exc.__class__.__name__}: {exc}"))
            continue
        if case.get("snippets"):
            out.append(case)
    return out


def _session(generate, db, repo_path: str, scan_id: str, batch: list[dict], **kw) -> tuple[list[dict], list[dict]]:
    return prompter.run_session(generate, db, repo_path, scan_id, batch, **kw)


# ---- lead mapping ------------------------------------------------------------------------------

def _confidence(value) -> Confidence:
    v = str(value or "").strip().upper()
    return v if v in _CONFIDENCES else "LOW"  # type: ignore[return-value]


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _str_or_none(value):
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _path(flow: dict) -> list[dict]:
    """Flow nodes with a `role`: first=source, last=sink, else step. A one-node path is the sink (a
    CandidateFlow is defined by its sink call; the path always ends there)."""
    nodes = [n for n in (flow.get("nodes") or []) if isinstance(n, dict)]
    last = len(nodes) - 1
    return [dict(n, role="sink" if i == last else "source" if i == 0 else "step") for i, n in enumerate(nodes)]


def _node_ref(n: dict) -> str:
    return f"{n.get('file')}:{n.get('function')}:{n.get('line')}"


def _to_lead(raw: dict, flow: dict, index: int, *, unresolved: bool) -> Lead:
    file = _str_or_none(raw.get("file"))
    function = _str_or_none(raw.get("function"))
    line = _int_or_none(raw.get("line"))
    cwe = _str_or_none(raw.get("cwe"))
    confidence = _confidence(raw.get("confidence"))
    flow_id = flow.get("flow_id")
    where = f"{file or '?'}:{line if line is not None else '?'}" + (f" in {function}" if function else "")
    text = f"{cwe or 'possible vulnerability'} at {where} (flow {flow_id}, {flow.get('sink_category') or 'unknown'} sink)"
    path = _path(flow)
    summary = (f"model lead: cwe={cwe} file={file} function={function} line={line} confidence={confidence}; "
               f"flow {flow_id}: sink_category={flow.get('sink_category')} source_uid={flow.get('source_uid')} "
               f"sink_uid={flow.get('sink_uid')}; path: " + (" -> ".join(_node_ref(n) for n in path) or "(no nodes)"))
    if unresolved:
        summary = (f"flow_id unresolved (model gave {raw.get('flow_id')!r}; attached to the batch's first flow "
                   f"{flow_id}). " + summary)
    # The structured fields go through the same validation as the Claude agents' (a negative line, a
    # bare "89" CWE or a "./" path is dropped or normalised, never guessed); `evidence` keeps the
    # model's own words.
    loc = clean_location({"file": file, "line_start": line, "function": function, "cwe": cwe})
    return Lead(index=index, shape=SHAPE, text=text, evidence=summary, confidence=confidence,
                source_uid=flow.get("source_uid") or None, sink_uid=flow.get("sink_uid") or None,
                file=loc.get("file"), function=loc.get("function"), line_start=loc.get("line_start"),
                cwe=loc.get("cwe"), path=path)


def _map_batch(raw_leads: list, batch: list[dict], start_index: int) -> list[Lead]:
    """Model dicts -> Leads for one batch. Resolved leads come first so a tagged, unresolved lead never
    shadows a real one for the same flow when `_dedup` keeps the first occurrence."""
    by_id = {f.get("flow_id"): f for f in batch}
    resolved, unresolved = [], []
    for raw in raw_leads:
        if not isinstance(raw, dict):
            continue  # not a lead; nothing to attach, nothing to fabricate
        flow = by_id.get(raw.get("flow_id"))
        if flow is None:
            unresolved.append((raw, batch[0]))
        else:
            resolved.append((raw, flow))
    leads = []
    for raw, flow in resolved:
        leads.append(_to_lead(raw, flow, start_index + len(leads), unresolved=False))
    for raw, flow in unresolved:
        leads.append(_to_lead(raw, flow, start_index + len(leads), unresolved=True))
    return leads


def _step_hook(on_event: OnEvent, on_step, turn: int, phase: str = "discover"):
    """Per-batch callback handed to the session: one progress event per step, then the caller's `on_step`
    (a failing logger is a `warn`, never a crash). Called by the prompter the moment a step is recorded."""
    def hook(step) -> None:
        kind = step.get("kind") if isinstance(step, dict) else None
        detail = f"batch {turn} step {step.get('step_no')}: {kind}" if isinstance(step, dict) else str(step)
        if isinstance(step, dict) and step.get("query"):
            detail += f" {step['query']}"
        if isinstance(step, dict) and step.get("error"):
            detail += f" [error: {step['error']}]"
        on_event(_event(event=_STEP_EVENTS.get(kind, "tool"), turn=turn, detail=detail, phase=phase))
        if on_step is not None:
            try:
                on_step(step)
            except Exception as exc:
                on_event(_event(event="warn", turn=turn, detail=f"on_step failed: {exc.__class__.__name__}: {exc}",
                                phase=phase))
    return hook


def _build_index(repo_path: str, on_event: OnEvent) -> RepoIndex | None:
    """The scan's one RepoIndex (an `index: ...` tool event), or None with a `warn` when it fails."""
    t0 = time.perf_counter()
    try:
        index = RepoIndex(repo_path)
    except Exception as exc:
        on_event(_event(event="warn", detail=f"code index failed: {exc.__class__.__name__}: {exc}; "
                                             f"judging path-only cases"))
        return None
    on_event(_event(event="tool", detail=f"index: {len(index.files)} files, {len(index.skipped)} skipped, "
                                         f"{time.perf_counter() - t0:.2f}s"))
    return index


def _enrich(flows: list[dict], repo_path: str, on_event: OnEvent, budget: CaseBudget,
            index: RepoIndex | None) -> list[dict]:
    """Flows -> CodeCases. The index is built once; any failure degrades to the bare flow, never drops it."""
    if not flows:
        return flows
    if index is None:
        index = _build_index(repo_path, on_event)
        if index is None:
            return flows
    out = []
    for flow in flows:
        try:
            out.append(cases.case_from_flow(flow, index, budget=budget))
        except Exception as exc:
            on_event(_event(event="warn", detail=f"enrichment failed for {flow.get('flow_id')}: "
                                                 f"{exc.__class__.__name__}: {exc}; judged path-only"))
            out.append(flow)
    return out


# ---- the stage ----------------------------------------------------------------------------------

def _failure(turn: int, batch: list[dict], reason: str) -> dict:
    """One batch the engine could not judge: which flows, where the first one's sink is, and why."""
    sink = next((n for n in reversed((batch[0].get("nodes") or []) if batch else []) if isinstance(n, dict)), {})
    return {"turn": turn, "flow_ids": [str(f.get("flow_id")) for f in batch], "reason": reason,
            "file": sink.get("file"), "line": sink.get("line")}


def _session_failure(steps: list[dict]) -> str | None:
    """Why a session that RETURNED still judged nothing: the context overflowed, or the forced final was
    not a valid final. None when the session ended with a real answer (an empty `leads` list included)."""
    for step in steps or []:
        if step.get("kind") == "context_overflow":
            return f"context overflow: {step.get('error')}"
        if step.get("kind") == "forced_final" and step.get("error"):
            return f"forced final failed: {step.get('error')}"
    return None


def discover_lm(scan_id: str, repo_path: str, on_event: OnEvent, *, profile=None, model=None, k: int = DEFAULT_K,
                max_flows: int = DEFAULT_MAX_FLOWS, on_step=None, stop=None, flows=None, db=None,
                case_budget: CaseBudget | None = None, index: RepoIndex | None = None,
                failures: list | None = None, shortlists: bool | None = None,
                max_findings: int = DEFAULT_MAX_FINDINGS, runtime: bool = False) -> list[Lead]:
    """Run the Orion-LM discovery stage and return deduped `list[Lead]` (shape "LM").

    `model` is an already-loaded wrapper (`orion.lm.model.load(...)`): only its `.generate(messages)` is
    used, and the prompter's budget cap is set `prompter.CONTEXT_MARGIN` below its `context_cap` (8000
    when the object has none). `flows` bypasses the graph read (slice-shaped dicts, e.g. an OWASP case
    file); either way only the top `max_flows` are worked, in order, in batches of `k`, one session each.
    Every flow becomes a CodeCase via `cases.case_from_flow` (budget `case_budget`, default CaseBudget())
    before batching; `index` is a prebuilt RepoIndex, else one is built over `repo_path` once per call and
    reported as an `index: ...` tool event. A flow whose enrichment raises is judged path-only (a `warn`
    event), never dropped.
    `stop` (threading.Event) is checked before each batch and forwarded to the session. `db` is an open
    GraphDB; None constructs one (and closes it at the end). `profile` is accepted for signature parity
    with `discover.discover` and is not used: the LM prompt is framework-agnostic by construction.

    Any exception in loading flows or in one batch's session becomes an `error` event and zero leads
    for that batch; the remaining batches still run and the function always returns.

    `failures`, when given, receives one record per batch the model could NOT judge -- the flow read
    failed, the session raised, its context overflowed, or its forced final was invalid -- as
    {turn, flow_ids, reason, file, line} (a `warn` event too, for the two kinds that end a session
    without raising). Zero leads from such a batch is not a clean negative; the CLI reports each one
    as an ERROR verdict. An empty `final` is a real answer and records nothing. Before each session
    the model's RNG is re-seeded (`seed_session`, when the wrapper has one), so a batch's answer does
    not depend on how much sampling the batches before it did.

    Opt-in (merge Phase 4): `shortlists` (default config.SHORTLISTS) also judges the top `max_findings`
    :CandidateFinding rows (shapes B/C/D) as code-only cases after the flows (`_finding_cases`);
    `runtime` re-ranks the flows by what executed (`_load_flows`). Off, the stage is unchanged.
    """
    if model is None:
        raise ValueError("discover_lm needs a loaded model (orion.lm.model.load(path)); got None")
    # A wrapper exposes `.generate`; a bare callable is accepted as-is. Anything else fails inside the
    # session, where it becomes an `error` event per batch rather than a crash here.
    generate = model.generate if hasattr(model, "generate") else model
    raw_cap = _raw_cap(model)
    context_cap = prompter_cap(model)
    k = max(1, int(k))
    max_flows = max(0, int(max_flows))

    on_event(_event(event="start", detail=f"orion-lm discovery starting (k={k}, max_flows={max_flows})"))
    if context_cap != raw_cap:
        on_event(_event(event="warn", detail=f"prompter context_cap {raw_cap} floored to {context_cap}; "
                                             f"the model's context_cap is too small for a useful session"))

    own_db = db is None
    if own_db:
        from orion.graphdb import GraphDB  # local import: the driver connects lazily, on first query
        db = GraphDB()
    leads: list[Lead] = []
    turn_of: dict[int, int] = {}  # id(lead) -> batch number, for the post-dedup `lead` events
    stopped = False
    try:
        if flows is None:
            try:
                flows = _load_flows(db, scan_id, max_flows, **({"runtime": True} if runtime else {}))
            except Exception as exc:  # a graph/query failure is an error event, not a crash
                on_event(_event(event="error", detail=f"loading CandidateFlows failed: {exc.__class__.__name__}: {exc}"))
                if failures is not None:
                    failures.append(_failure(0, [], f"loading CandidateFlows failed: "
                                                    f"{exc.__class__.__name__}: {exc}"))
                flows = []
        flows = list(flows)[:max_flows]
        budget = case_budget or CaseBudget()
        tried_index = False
        if flows:
            if index is None:
                index, tried_index = _build_index(repo_path, on_event), True
            if index is not None:
                flows = _enrich(flows, repo_path, on_event, budget, index)
        if shortlists is None:
            shortlists = config.SHORTLISTS
        if shortlists:
            # --shortlists (opt-in): the shapes B/C/D candidates the build stored, judged as code-only
            # cases after the flows. An extra, never a reason to fail the scan.
            try:
                rows = _load_findings(db, scan_id, max_findings)
            except Exception as exc:
                on_event(_event(event="warn", detail=f"shortlist rows unavailable: {exc.__class__.__name__}: {exc}"))
                rows = []
            if rows and index is None and not tried_index:
                index = _build_index(repo_path, on_event)
            if rows and index is not None:
                extra = _finding_cases(rows, index, budget, flows, on_event)
                on_event(_event(event="tool", detail=f"shortlists: {len(extra)} code-only cases from {len(rows)} rows"))
                flows = flows + extra
            elif rows:
                on_event(_event(event="warn", detail="shortlist rows need the code index; skipped"))

        for turn, start in enumerate(range(0, len(flows), k), start=1):
            if stop is not None and stop.is_set():
                on_event(_event(event="warn", turn=turn, detail="stop requested; remaining batches skipped"))
                stopped = True
                break
            batch = flows[start:start + k]
            ids = ", ".join(str(f.get("flow_id")) for f in batch)
            step_hook = _step_hook(on_event, on_step, turn)
            seed = getattr(model, "seed_session", None)
            if callable(seed):
                seed()
            try:
                # Steps reach `step_hook` as they happen, so a batch that fails mid-session still reports
                # the steps it took; the returned `steps` are the same records and are NOT replayed.
                raw_leads, steps = _session(generate, db, repo_path, scan_id, batch,
                                            context_cap=context_cap, stop=stop, on_step=step_hook)
            except Exception as exc:
                on_event(_event(event="error", turn=turn,
                                detail=f"batch {turn} ({ids}) failed: {exc.__class__.__name__}: {exc}"))
                if failures is not None:
                    failures.append(_failure(turn, batch, f"{exc.__class__.__name__}: {exc}"))
                continue
            why = _session_failure(steps)
            if why is not None:
                on_event(_event(event="warn", turn=turn, detail=f"batch {turn} ({ids}) not judged: {why}"))
                if failures is not None:
                    failures.append(_failure(turn, batch, why))
            batch_leads = _map_batch(list(raw_leads or []), batch, len(leads))
            for lead in batch_leads:
                turn_of[id(lead)] = turn
            leads.extend(batch_leads)
    finally:
        if own_db:
            db.close()

    kept = dedup_keep(leads)
    deduped = [replace(lead, index=i) for i, lead in enumerate(kept)]
    for original, lead in zip(kept, deduped):  # indices match the returned list
        on_event(_event(event="lead", lead=lead.index, turn=turn_of.get(id(original)), detail=lead.text))
    tail = "; stopped early" if stopped else ""
    on_event(_event(event="done", detail=f"{len(deduped)} candidate leads from {len(flows)} flows{tail}"))
    return deduped
