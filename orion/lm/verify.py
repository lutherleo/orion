"""`verify_lm`: Orion-LM's opt-in second pass (`orion scan --engine lm --lm-verify`), on the SAME
model as discovery (one model per scan; merge decision D2, opt-in until measured on dev).

Each lead `discover_lm` returned is re-judged ALONE in a fresh session: the lead's flow, enriched into
a CodeCase like discovery's (sink function + source/caller/callee snippets), under a TASK turn that
says an earlier pass flagged it and asks for an independent re-check. Nothing else differs from
discovery -- same system turn, same tools (run_cypher / read_code), same `final {leads}` action and
constrained decoding -- because the model was trained on that format and never on a verdict schema;
a new schema would be untested output the decoder has never produced.

The model's answer maps onto `contracts.Verdict(route="lm-verify")`:
  final names the candidate, confidence MEDIUM/HIGH  -> CONFIRM (the model's file/line/cwe, cleaned)
  final names the candidate, confidence LOW          -> INCONCLUSIVE
  final with no leads                                -> REJECT
  the session raised, overflowed, or its forced final was invalid -> ERROR -- never a CONFIRM
A lead with no matching CandidateFlow is judged on its own path nodes. Claude-free.
"""
from __future__ import annotations

from orion.contracts import Lead, OnEvent, Verdict, clean_location
from orion.lm import engine, prompter
from orion.lm import format as fmt
from orion.lm.cases import CaseBudget
from orion.lm.codeindex import RepoIndex

ROUTE = "lm-verify"


def task_message(case: dict, lead: Lead) -> dict:
    """The verifier's TASK turn: the claim under test, then the candidate rendered as discovery renders it.
    Every claim field came from the model or the repo, so it is neutralised like any repo text."""
    where = f"{lead.file or '?'}:{lead.line_start if lead.line_start is not None else '?'}"
    if lead.function:
        where += f" in {lead.function}"
    claim = f"{lead.cwe or 'a vulnerability'} at {where}"
    content = ("TASK: An earlier pass flagged the candidate below as a real vulnerability "
               f"({fmt._s(claim)}). Check it again, independently: query the graph with run_cypher and read "
               "the source with read_code as needed, then emit final with one lead for the candidate (its "
               "flow_id) ONLY if it really is vulnerable, or an empty leads list if it is not.\n\n"
               + fmt._render_flow(case))
    return {"role": "user", "content": content}


def _flow_for(lead: Lead, by_endpoints: dict) -> dict:
    """The lead's CandidateFlow slice, else a flow built from the lead's own path (never a guess)."""
    flow = by_endpoints.get((lead.source_uid, lead.sink_uid))
    if flow is not None:
        return flow
    nodes = [{k: n.get(k) for k in ("file", "function", "line", "code")} for n in (lead.path or [])]
    return {"flow_id": f"L{lead.index}", "rank": None, "sink_category": None,
            "source_uid": lead.source_uid, "sink_uid": lead.sink_uid, "nodes": nodes}


def _verdict(lead: Lead, flow_id: str, raw_leads: list[dict], steps: list[dict]) -> Verdict:
    n_steps = len(steps or [])
    if not raw_leads:
        return Verdict(lead, "REJECT", f"engine=lm re-judged flow {flow_id} not vulnerable ({n_steps} steps)",
                       route=ROUTE)
    raw = next((r for r in raw_leads if str(r.get("flow_id")) == str(flow_id)), raw_leads[0])
    note = "" if str(raw.get("flow_id")) == str(flow_id) else \
        f" (flow_id {raw.get('flow_id')!r} unresolved; the only candidate was {flow_id})"
    loc = clean_location({"file": raw.get("file"), "line_start": raw.get("line"),
                          "function": raw.get("function"), "cwe": raw.get("cwe")})
    confidence = engine._confidence(raw.get("confidence"))
    decision = "INCONCLUSIVE" if confidence == "LOW" else "CONFIRM"
    return Verdict(lead, decision,
                   f"engine=lm re-judged flow {flow_id} vulnerable, confidence {confidence} "
                   f"({n_steps} steps){note}",
                   evidence=f"model final: {raw!r}", file=loc.get("file"), line_start=loc.get("line_start"),
                   cwe=loc.get("cwe"), route=ROUTE)


def verify_lm(scan_id: str, leads: list[Lead], repo_path: str, on_event: OnEvent, *, model, db=None,
              index: RepoIndex | None = None, case_budget: CaseBudget | None = None, stop=None,
              on_verdict=None, max_flows: int = engine.DEFAULT_MAX_FLOWS) -> list[Verdict]:
    """One verdict per lead, in order (see the module doc). `on_verdict(v)` gets each as it lands, so a
    crash loses nothing already verified. `stop` (threading.Event) ends the pass: leads not yet started
    are ERROR "not verified (stopped)". `db` is an open GraphDB (None: one is opened and closed here)."""
    generate = model.generate if hasattr(model, "generate") else model
    context_cap = engine.prompter_cap(model)
    own_db = db is None
    if own_db:
        from orion.graphdb import GraphDB
        db = GraphDB()
    verdicts: list[Verdict] = []

    def emit(v: Verdict) -> None:
        verdicts.append(v)
        on_event(engine._event(event="verdict", lead=v.lead.index, detail=f"{v.decision}: {v.reason}",
                               phase="verify"))
        if on_verdict is not None:
            on_verdict(v)

    try:
        try:
            flows = engine._load_flows(db, scan_id, max_flows)
        except Exception as exc:  # judge on the leads' own paths rather than give up
            on_event(engine._event(event="warn", phase="verify",
                                   detail=f"lm-verify: CandidateFlows unavailable "
                                          f"({exc.__class__.__name__}: {exc}); using the leads' paths"))
            flows = []
        by_endpoints = {(f.get("source_uid"), f.get("sink_uid")): f for f in flows}
        cases = engine._enrich([_flow_for(lead, by_endpoints) for lead in leads], repo_path, on_event,
                               case_budget or CaseBudget(), index)
        for lead, case in zip(leads, cases):
            if stop is not None and stop.is_set():
                emit(Verdict(lead, "ERROR", "not verified (stopped)", route=ROUTE))
                continue
            flow_id = str(case.get("flow_id"))
            seed = getattr(model, "seed_session", None)
            if callable(seed):
                seed()
            try:
                raw_leads, steps = prompter.run_session(
                    generate, db, repo_path, scan_id, [case], context_cap=context_cap, stop=stop,
                    task=task_message(case, lead),
                    on_step=engine._step_hook(on_event, None, lead.index, phase="verify"))
            except Exception as exc:
                emit(Verdict(lead, "ERROR", f"lm-verify session failed: {exc.__class__.__name__}: {exc}",
                             route=ROUTE))
                continue
            why = engine._session_failure(steps)
            if why is not None:
                emit(Verdict(lead, "ERROR", f"lm-verify not judged: {why}", route=ROUTE))
                continue
            emit(_verdict(lead, flow_id, list(raw_leads or []), steps))
    finally:
        if own_db:
            db.close()
    return verdicts
