"""`orion scan <repo>` end to end: build the graph, discover leads, verify them, report.

    orion scan ./NodeGoat                  # build the graph, then discover + verify + report
    orion scan --scan-id <id>               # skip the build, run against an existing scan graph
    orion scan ./NodeGoat --watch           # follow live progress while the scan runs
    orion scan ./NodeGoat --json out.json   # also write the verdicts as JSON
    orion scan ./NodeGoat --quiet           # suppress per-event prints (still logs to file)
    orion scan --resume .orion/runs/<id>/<ts>   # finish an interrupted scan without re-discovering
    orion scan ./NodeGoat --fail-on confirm # exit 1 if any lead is CONFIRMed (CI gate)
    orion scan ./NodeGoat --engine lm --model <path>   # in-process Orion-LM discovery, no claude

Discovery and verification are two SEPARATE `claude -p` sessions (see the design doc) fanned out
and joined here; this module owns only the wiring -- build -> index -> discover -> verify ->
report -- and the run's progress log.

`--engine lm` (default: `claude`) swaps discovery for the in-process Orion-LM engine
(`orion/lm/engine.py`, MLX-LM; never `claude_cli`) and SKIPS the verifier: every lead is reported
as INCONCLUSIVE "unverified (engine=lm)". The lm sink profile (`profiles.with_lm_sinks`) is applied
at BUILD time so the graph carries the extra CandidateFlows (and gets its own scan_id). With
`--scan-id`, the scan's OrionScan record must say it finished building with the profile the engine
needs, or the scan is refused with the command that rebuilds it.

    orion gc --keep 3 [--prefix P] [--dry-run] [--include-unrecorded]   # delete old scan partitions
    orion reanalyze --scan-id <id> [--engine claude|lm]   # recompute CandidateFlows, no Joern

graph_build / embed / discover / verify pull in the Neo4j driver, the local embedding model, and
the `claude` CLI subprocess wrapper. Those are imported LAZILY inside `_run_scan`, not at module
scope, so `orion --help` and `import orion.cli` stay cheap and don't fail merely because one of
those modules is mid-edit elsewhere.
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime
import json
import os
import sys
import threading
import time
from pathlib import Path


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _event(phase: str, event: str, *, shape=None, lead=None, turn=None, detail: str = "") -> dict:
    """Build one ProgressEvent dict (see contracts.ProgressEvent for the key contract)."""
    return {
        "ts": _now(),
        "phase": phase,
        "shape": shape,
        "lead": lead,
        "turn": turn,
        "event": event,
        "detail": detail,
    }


def _run_dir(scan_id: str) -> str:
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    path = Path(".orion") / "runs" / scan_id / ts
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


# ── run-dir artifacts: a scan's leads and verdicts are on disk as soon as they exist ─────────────
# <run_dir>/leads.json      {"scan_id", "repo", "engine", "model", ["settings",] "leads": [...]}
#                           -- written right after discovery
# <run_dir>/verdicts.jsonl  one verdict per line, appended as EACH lead finishes verifying
# <run_dir>/verdicts.json + report.txt + results.sarif           -- written at the end
# <run_dir>/.lock           locked while a scan writes the dir (orion/runlock.py): one scan per run dir
# A crash mid-verify therefore loses nothing already verified, and `--resume <run_dir>` finishes the
# run without re-running discovery or re-paying for verdicts already in hand.
_LEADS_FILE, _VERDICTS_LOG = "leads.json", "verdicts.jsonl"
_FAIL_LEVELS = {"none": set(), "confirm": {"CONFIRM"}, "inconclusive": {"CONFIRM", "INCONCLUSIVE"}}


def _save_leads(run_dir: str, scan_id: str, repo: str | None, leads: list, *,
                engine: str = "claude", model: str | None = None, settings: dict | None = None) -> None:
    """`engine` and `model` (the claude model name, or the lm model path) are recorded so `--resume`
    never finishes a run on a different engine than the one that discovered its leads (one model per
    scan); `settings` (`_verify_settings`) is what a resume must repeat to finish the run as it began."""
    payload = {"scan_id": scan_id, "repo": repo, "engine": engine, "model": model,
               **({"settings": settings} if settings is not None else {}),
               "leads": [dataclasses.asdict(lead) for lead in leads]}
    Path(run_dir, _LEADS_FILE).write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def _verify_settings(args: argparse.Namespace, config) -> dict:
    """A claude run's verify-time settings, recorded in leads.json: the verifier options in effect (flag or
    env), the --fail-on level, and whether the model was reached through a compatible endpoint
    (ANTHROPIC_BASE_URL set; the URL itself is not recorded). `orion ui` resumes a run with exactly these."""
    return {"verify_routing": bool(getattr(args, "verify_routing", False) or config.VERIFY_ROUTING),
            "verify_context": bool(getattr(args, "verify_context", False) or config.VERIFY_CONTEXT),
            "semantic_mode": config.SEMANTIC_MODE,
            "fail_on": getattr(args, "fail_on", None) or "none",
            "endpoint": bool((os.environ.get("ANTHROPIC_BASE_URL") or "").strip())}


def _writable(run_dir: str) -> bool:
    """Whether this user can add to a run dir: the dir itself and its verdicts log, when there is one."""
    log = Path(run_dir, _VERDICTS_LOG)
    return os.access(run_dir, os.W_OK) and (not log.exists() or os.access(log, os.W_OK))


def _run_meta(run_dir: str) -> dict:
    return json.loads(Path(run_dir, _LEADS_FILE).read_text(encoding="utf-8"))


def _run_engine(run_dir: str) -> str:
    """The engine a run dir's leads came from; runs recorded before engines were logged are claude."""
    return _run_meta(run_dir).get("engine") or "claude"


def _append_verdict(run_dir: str, verdict) -> None:
    with open(Path(run_dir, _VERDICTS_LOG), "a", encoding="utf-8") as f:
        f.write(json.dumps(dataclasses.asdict(verdict), default=str) + "\n")


def _load_run(run_dir: str):
    """(scan_id, repo, leads, finished) from a run dir. `finished` maps lead index -> the LATEST
    non-ERROR verdict logged for it (an ERROR is retried on resume). A torn last line from a crash
    mid-write is skipped. Raises FileNotFoundError/ValueError on a run dir with no leads.json."""
    from .contracts import Lead, Verdict

    meta = json.loads(Path(run_dir, _LEADS_FILE).read_text(encoding="utf-8"))
    leads = [Lead(**d) for d in meta["leads"]]
    by_index = {lead.index: lead for lead in leads}
    finished: dict = {}
    log = Path(run_dir, _VERDICTS_LOG)
    for line in (log.read_text(encoding="utf-8").splitlines() if log.exists() else []):
        try:
            d = json.loads(line)
            lead = by_index[d["lead"]["index"]]
        except (ValueError, KeyError, TypeError):
            continue
        # Every logged Verdict field (location, severity, route...), not just the original four --
        # a resumed scan must report exactly what was verified. Unknown keys from a newer log are
        # ignored rather than crashing an older Orion.
        known = {f.name for f in dataclasses.fields(Verdict)} - {"lead"}
        v = Verdict(lead=lead, **{k: d[k] for k in known if k in d})
        v.decision = v.decision or "ERROR"
        if v.decision == "ERROR":
            finished.pop(lead.index, None)
        else:
            finished[lead.index] = v
    return meta["scan_id"], meta.get("repo"), leads, finished


def _exit_code(verdicts: list, fail_on: str) -> int:
    """1 when a verdict at or above `fail_on` exists (CI gating), else 0."""
    levels = _FAIL_LEVELS.get(fail_on, set())
    return 1 if any(v.decision in levels for v in verdicts) else 0


def lm_verdicts(leads: list) -> list:
    """The lm engine's leads as verdicts. There is no independent verifier on this engine (yet), so
    every lead is reported INCONCLUSIVE "unverified (engine=lm)" -- never an invented CONFIRM.
    `report._ORDER` ranks INCONCLUSIVE, so the report and --json work unchanged. Shared with the eval
    harness's in-process orion-lm arm (eval/arms/lm.py), whose findings.json must match --json."""
    from .contracts import Verdict
    return [Verdict(lead, "INCONCLUSIVE", "unverified (engine=lm)") for lead in leads]


def write_verdicts_json(path, verdicts: list) -> int:
    """Write `verdicts` as the `--json` payload (a JSON list of dataclasses.asdict(Verdict)) and
    return how many were written."""
    payload = [dataclasses.asdict(v) for v in verdicts]
    Path(path).write_text(json.dumps(payload, indent=2, default=str))
    return len(payload)


def lm_failure_verdicts(failures: list[dict], *, start: int) -> list:
    """One ERROR verdict per batch the lm engine could not judge (discover_lm's `failures`), on a
    placeholder lead numbered from `start` and placed at the batch's sink when known. Zero leads from
    such a batch is not a clean negative, so it is never reported as one."""
    from .contracts import Lead, Verdict
    out = []
    for i, f in enumerate(failures):
        ids = ", ".join(f.get("flow_ids") or []) or "the candidate flows"
        line = f.get("line") if isinstance(f.get("line"), int) and f.get("line") >= 1 else None
        lead = Lead(index=start + i, shape="LM", text=f"not judged: {ids}", evidence=str(f.get("reason")),
                    confidence="LOW", file=f.get("file") or None, line_start=line)
        out.append(Verdict(lead, "ERROR", f"engine=lm could not judge {ids}: {f.get('reason')}"))
    return out


def _load_lm_model(path: str, **sampling):
    """Load the Orion-LM model wrapper for `path` (MLX-LM, llama-cpp fallback) with `sampling`
    (`temperature`, ...). A seam: tests replace it, and the MLX import stays inside the lm branch --
    `orion.lm.model`'s top level is stdlib-only."""
    from .lm import model as lm_model
    return lm_model.load(path, **sampling)


def _run_scan(args: argparse.Namespace) -> int:
    # Cheap arg validation BEFORE the heavy lazy imports below, so a typo'd path fails instantly
    # instead of after loading the embedding model / Neo4j driver.
    if getattr(args, "semantic_mode", None):
        from . import config as _config               # light; also reaches the agents' MCP server via env
        _config.set_semantic_mode(args.semantic_mode)
    resume = getattr(args, "resume", None)
    run_lock = None
    if resume:
        # Lock first, then read: a scan still finishing this run dir could otherwise log verdicts between
        # the read and the lock, and those leads would be verified twice.
        from . import runlock
        if Path(resume, _LEADS_FILE).is_file():
            if not _writable(resume):
                print(f"orion scan: cannot write to {resume}: a resume appends to its {_VERDICTS_LOG}, so it "
                      "would pay for verdicts it cannot keep", file=sys.stderr)
                return 2
            run_lock = runlock.acquire(resume)
            if run_lock is None:
                print(f"orion scan: {resume} is being written by another scan right now; resume it once that "
                      "scan has stopped", file=sys.stderr)
                return 2
        try:
            scan_id, saved_repo, saved_leads, finished = _load_run(resume)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            print(f"orion scan: cannot resume {resume}: no readable {_LEADS_FILE} ({exc})",
                  file=sys.stderr)
            return 2
        saved_engine, wanted = _run_engine(resume), getattr(args, "engine", "claude")
        if saved_engine != wanted:
            print(f"orion scan: {resume} is an --engine {saved_engine} run; it can only be resumed on "
                  f"that engine (one model per scan)", file=sys.stderr)
            return 2
        if saved_engine == "lm":
            print(f"orion scan: {resume} is an --engine lm run, which has no verify step to resume; "
                  "its verdicts are in verdicts.jsonl", file=sys.stderr)
            return 2
        args.scan_id = scan_id
        args.repo = args.repo or saved_repo
    if not args.scan_id and not args.repo:
        print("orion scan: provide a repo path or --scan-id", file=sys.stderr)
        return 2
    # A missing repo path must fail here with a clear message -- otherwise it reaches joern-parse,
    # which dies with a Java AssertionError stack trace ("Input path does not exist") that reads like
    # an Orion crash. This also catches the aftermath of a failed `git clone` (empty/absent dir).
    if args.repo and not Path(args.repo).is_dir():
        print(f"orion scan: repo path not found or not a directory: {args.repo}", file=sys.stderr)
        return 2
    lm_engine = getattr(args, "engine", "claude") == "lm"
    # There is no default model path: `--engine lm` without `--model` is a usage error, not a crash
    # deep inside the pipeline after the graph has been built.
    if lm_engine and not args.lm_model:
        print("orion scan: --engine lm needs --model <path to the Orion-LM model>", file=sys.stderr)
        return 2
    # The repo path is the engine's `read_code` sandbox. Without it the only fallback would be the
    # CWD -- Orion's own checkout (CLAUDE.md, eval/, answer keys) -- which must never be what the
    # model reads. So unlike the claude path, --scan-id alone is not enough here.
    if lm_engine and not args.repo:
        print("orion scan: --engine lm needs the repo path for read_code even with --scan-id", file=sys.stderr)
        return 2

    # Deferred on purpose -- see module docstring.
    from . import claude_cli, config, discover, embed, graph_build, graphdb, report, verify
    from . import sarif as sarif_mod
    from .monitor import run_logger, tail

    # The lm sink profile must be in the graph, so it is chosen BEFORE the build and handed to
    # graph_build.build(profile=). select_profile ignores the language today (it only keys on
    # package.json/express), so passing none here mirrors the discovery-profile pick below; build
    # resolves the Joern frontend itself and would select the same base profile.
    lm_profile = None
    if lm_engine and args.repo:
        from .graph import profiles
        lm_profile = profiles.with_lm_sinks(profiles.select_profile(args.repo))

    # --scan-id trusts only a scan whose OrionScan record says it finished building with the profile
    # this engine needs; anything else (no record, a crashed build, the other profile) is refused here,
    # before any discovery runs, with the command that rebuilds it.
    if args.scan_id:
        from .graph import scans
        problem = scans.check_usable(args.scan_id, engine="lm" if lm_engine else "claude", repo=args.repo)
        if problem:
            print(f"orion scan: {problem}", file=sys.stderr)
            return 2

    needs_build = not args.scan_id
    # The lm profile changes the CandidateFlows, so its graph gets its own scan_id (scan_id_for adds
    # the profile name); the claude path calls scan_id_for exactly as before.
    scan_id_kwargs = {"profile": lm_profile} if lm_profile is not None else {}
    scan_id = args.scan_id or graph_build.scan_id_for(args.repo, **scan_id_kwargs)
    run_dir = resume or _run_dir(scan_id)   # a resumed run keeps appending to its own dir
    # One scan per run dir: two writers would verify the same leads twice and interleave their logs. The
    # handle stays open (so the lock stays held) until this function returns; the OS releases it however
    # the process ends. A resume took it above, before reading the run.
    if run_lock is None:
        from . import runlock
        run_lock = runlock.acquire(run_dir)
        if run_lock is None:
            print(f"orion scan: {run_dir} is being written by another scan right now; resume it once that "
                  "scan has stopped", file=sys.stderr)
            return 2

    # In --watch mode the foreground `tail` is the one rendering progress (reading the same
    # JSONL), so the background pipeline's own logger stays quiet to avoid printing every event
    # twice. Otherwise the logger prints directly unless the caller asked for --quiet.
    on_event = run_logger(run_dir, quiet=True if args.watch else args.quiet)

    print(f"scan_id: {scan_id}")
    print(f"run log: {run_dir}/progress.jsonl")

    def _index_semantic(batch=None) -> None:
        # Best-effort semantic index. When `batch` is given it runs CONCURRENTLY with persist (item
        # 4, via build's on_batch hook) reading spans from the in-memory batch; otherwise it reads
        # the already-persisted graph (the --scan-id path). Embedding is never fatal to a scan.
        concurrent = batch is not None
        on_event(_event("build", "start", detail="indexing semantic embeddings"
                        + (" (concurrent with persist)" if concurrent else "")))
        try:
            embed.index(args.repo, scan_id, batch=batch)
            on_event(_event("build", "done", detail="semantic index complete"))
        except Exception as exc:  # noqa: BLE001 -- embedding is best-effort, never fatal
            on_event(_event(
                "build", "error",
                detail=f"semantic index failed, continuing graph-only: {exc}",
            ))

    if resume:
        on_event(_event("build", "done", detail=f"resuming {resume}: {len(saved_leads)} leads, "
                                                 f"{len(finished)} already verified"))
        recorded = _run_meta(resume).get("model")      # None for runs recorded before 2026-10-29
        if recorded and recorded != config.MODEL:
            on_event(_event("verify", "warn",
                            detail=f"this run's leads were found with model {recorded}, and it is being "
                                   f"resumed with {config.MODEL} (one model per scan: set "
                                   f"ORION_MODEL={recorded} to keep it)"))
    elif needs_build:
        on_event(_event("build", "start", detail=f"building graph for {args.repo}"))
        # Overlap the semantic index with persist: build runs _index_semantic(batch) concurrently
        # with the persist write (item 4), so the two independent costs no longer serialize.
        # The claude path passes no `profile` (build auto-selects, exactly as before); the lm path
        # pins the extended-sink profile so pathfind emits the extra CandidateFlows.
        build_kwargs = {"profile": lm_profile} if lm_profile is not None else {}
        if lm_profile is not None:
            on_event(_event("build", "start", detail=f"lm sink profile {lm_profile.name} applied to the build"))
        # The lm engine never queries the semantic index, so it is not built for it (and the embedding
        # model is never loaded next to MLX on a 16 GB machine).
        graph_build.build(args.repo, args.language, on_event,
                          stream=args.stream, queue_size=args.queue_size,
                          on_batch=None if lm_engine else _index_semantic, **build_kwargs)
        on_event(_event("build", "done", detail="graph build complete"))
    else:
        on_event(_event("build", "done", detail=f"using existing scan_id {scan_id}"))
        if args.repo and not lm_engine:
            _index_semantic()   # --scan-id path: read spans from the already-persisted graph

    # Best-effort: ensure the GLOBAL exploit-reference corpus is indexed (once) so the verifier's
    # exploit_search tool works. Builds ONLY if the metadata file is present locally -- never
    # downloads mid-scan, never fatal. Run `orion index-exploits` to fetch + build it explicitly.
    try:
        if lm_engine:
            pass                # the exploit corpus serves the Claude verifier's exploit_search only
        elif embed.ensure_exploit_index():
            on_event(_event("build", "done", detail="exploit-reference corpus ready"))
        else:
            on_event(_event("build", "done",
                            detail="exploit corpus not indexed (run `orion index-exploits` to enable)"))
    except Exception as exc:  # noqa: BLE001 -- corpus is advisory; never break a scan over it
        on_event(_event("build", "error", detail=f"exploit corpus index skipped: {exc}"))

    # fp-check (the verifier's source-reading step) sandboxes to this path via --add-dir; fall
    # back to "." when only --scan-id was given and no repo checkout is known.
    repo_for_verify = args.repo or "."

    # Per-stack prompt vocabulary when we know the repo; None keeps the agnostic default prompt.
    profile = None
    if args.repo:
        from .graph import profiles
        profile = profiles.select_profile(args.repo)
    if lm_profile is not None:
        profile = lm_profile   # the same object the graph was built with

    # Opt-in runtime enrichment (--runtime): EXECUTE the target and fold observed coverage back into
    # the graph before discovery reads it. Best-effort by contract -- a failure is an event, never an
    # abort, and it only ADDS props/nodes/edges (never touches NODE_KEY labels), so the static graph
    # and its FLOWS_TO parity are untouched. Needs a repo checkout to boot/build; skipped on --scan-id.
    if getattr(args, "runtime", False) and not resume:
        if args.repo:
            from . import runtime
            runtime.enrich(scan_id, args.repo, on_event, budget=args.runtime_budget,
                           driver=args.runtime_driver, harness_file=args.harness_file,
                           sandbox=args.runtime_sandbox)
        else:
            on_event(_event("runtime", "warn",
                            detail="--runtime needs a repo checkout to execute; skipped on --scan-id"))

    def _node_count() -> int:
        # Size the per-shape discovery timeout to the graph: a bigger graph is a bigger search space
        # and needs longer sweeps (see config.discover_timeout). Sizing is best-effort -- if the
        # count query fails we fall back to the reality-based floor, never abort the scan.
        try:
            db = graphdb.GraphDB()
            try:
                return db.node_count(scan_id)
            finally:
                db.close()
        except Exception as exc:  # noqa: BLE001 -- best-effort sizing; the floor is a safe default
            on_event(_event("discover", "warn",
                            detail=f"graph node count unavailable, using base discovery timeout: {exc}"))
            return 0

    def _discover():
        node_count = _node_count()
        d_timeout = config.discover_timeout(node_count)
        on_event(_event("discover", "start",
                        detail=f"discovery fleet starting ({node_count} nodes, per-shape timeout {d_timeout}s)"))
        # Runtime facts in the graph turn on the runtime prompt block: `--runtime` (the enrichment
        # just above) or `--use-dynamic` (a prior `orion trace`). Neither keeps the eval baseline.
        dynamic_hint = getattr(args, "use_dynamic", False) or getattr(args, "runtime", False)
        leads = discover.discover(scan_id, on_event, profile, timeout=d_timeout,
                                  dynamic_hint=dynamic_hint,
                                  shortlists=getattr(args, "shortlists", None) or None)
        if claude_cli.interrupted():
            # Stopped mid-discovery (--watch: the shapes return what they had): saving these leads would
            # make an unfinished discovery look final, and --resume never re-discovers.
            raise KeyboardInterrupt
        on_event(_event("discover", "done", detail=f"{len(leads)} candidate leads"))
        _save_leads(run_dir, scan_id, args.repo, leads, model=config.MODEL,
                    settings=_verify_settings(args, config))
        return leads

    def _pipeline():
        if lm_engine:          # its own discover + (no) verify; an lm run is never resumed (see above)
            return _pipeline_lm(_node_count())
        leads, done = (saved_leads, finished) if resume else (_discover(), {})
        todo = [lead for lead in leads if lead.index not in done]
        on_event(_event("verify", "start", detail=f"verifying {len(todo)} leads"
                        + (f" ({len(done)} already verified)" if done else "")))
        fresh = verify.verify_all(scan_id, todo, repo_for_verify, on_event,
                                  on_verdict=lambda v: _append_verdict(run_dir, v),
                                  routing=getattr(args, "verify_routing", None) or None,
                                  context=getattr(args, "verify_context", None) or None)
        if claude_cli.interrupted():
            # Stopped mid-verify (--watch): the run stays resumable instead of reading as finished.
            raise KeyboardInterrupt
        by_index = {**done, **{v.lead.index: v for v in fresh}}
        verdicts = [by_index[lead.index] for lead in leads if lead.index in by_index]
        on_event(_event("verify", "done", detail=f"{len(verdicts)} verdicts"))
        return verdicts

    lm_stop = threading.Event()      # Ctrl-C: the lm engine finishes its current session, then stops
    lm_failures: list[dict] = []     # batches the lm engine could not judge (-> ERROR verdicts)

    def _pipeline_lm(node_count: int):
        # The in-process Orion-LM engine: one local model, no `claude -p`. The model is loaded HERE --
        # after the build -- so Joern and the model are never resident together (16 GB machine). MLX is
        # imported lazily by _load_lm_model; `orion.lm.engine` itself is stdlib + graphdb only.
        from .lm import engine as lm_engine_mod

        on_event(_event("discover", "start",
                        detail=f"Orion-LM discovery starting ({node_count} nodes, engine=lm, "
                               f"loading model {args.lm_model}, temperature {args.lm_temperature})"))
        model = _load_lm_model(args.lm_model, temperature=args.lm_temperature)
        # args.repo, never the "." fallback: it is the read_code sandbox (guarded in pre-flight).
        leads = lm_engine_mod.discover_lm(
            scan_id, args.repo, on_event, profile=profile, model=model, stop=lm_stop, failures=lm_failures,
            shortlists=getattr(args, "shortlists", None) or None,          # opt-in: B/C/D rows as cases
            runtime=bool(getattr(args, "use_dynamic", False) or getattr(args, "runtime", False)))
        on_event(_event("discover", "done", detail=f"{len(leads)} candidate leads"
                        + (f", {len(lm_failures)} batches not judged" if lm_failures else "")))
        errors = lm_failure_verdicts(lm_failures, start=len(leads))
        _save_leads(run_dir, scan_id, args.repo, leads + [v.lead for v in errors],
                    engine="lm", model=args.lm_model)

        if getattr(args, "lm_verify", False):
            # The opt-in second pass, on the SAME model (one model per scan).
            from .lm import verify as lm_verify
            on_event(_event("verify", "start", detail=f"lm-verify: re-judging {len(leads)} leads"))
            verdicts = lm_verify.verify_lm(scan_id, leads, args.repo, on_event, model=model, stop=lm_stop,
                                           on_verdict=lambda v: _append_verdict(run_dir, v))
            on_event(_event("verify", "done", detail=f"{len(verdicts)} verdicts"))
        else:
            # No verifier asked for: say so loudly and report every lead as INCONCLUSIVE rather than
            # inventing a CONFIRM (lm_verdicts).
            on_event(_event("verify", "done", detail="skipped (engine=lm; --lm-verify not given)"))
            verdicts = lm_verdicts(leads)
            for v in verdicts:
                _append_verdict(run_dir, v)
        for v in errors:
            _append_verdict(run_dir, v)
        return verdicts + errors

    if args.watch or lm_engine:
        # A worker thread runs the pipeline; the foreground follows the log (--watch) or just waits.
        # Ctrl-C asks the lm engine to stop after its current session (a second Ctrl-C leaves at once).
        stop = threading.Event()
        result: dict = {}

        def _worker() -> None:
            try:
                result["verdicts"] = _pipeline()
            except BaseException as exc:  # noqa: BLE001 -- surface, don't swallow (an interrupt too:
                result["error"] = exc     # the main thread re-raises it, so the run never reads as finished)
            finally:
                stop.set()

        thread = threading.Thread(target=_worker, daemon=True)
        thread.start()
        interrupted = False
        try:
            if args.watch:
                tail(run_dir, stop)
                interrupted = not stop.is_set()     # tail swallows a Ctrl-C and returns before the worker ends
            else:
                while not stop.wait(0.5):
                    pass
        except KeyboardInterrupt:
            interrupted = True
        if interrupted:
            lm_stop.set()
            if lm_engine:
                print("\nstopping: finishing the current Orion-LM session (Ctrl-C again to quit now)",
                      file=sys.stderr)
            else:
                # The worker's claude -p sessions got the same Ctrl-C: none is retried, none started.
                claude_cli.interrupt()
        thread.join()
        if "error" in result:
            raise result["error"]
        verdicts = result.get("verdicts", [])
    else:
        verdicts = _pipeline()

    on_event(_event("report", "start", detail=f"rendering {len(verdicts)} verdicts"))
    text = report.render(verdicts)
    on_event(_event("report", "done", detail="report rendered"))

    print("\n" + "=" * 70)
    print(text)

    payload = json.dumps([dataclasses.asdict(v) for v in verdicts], indent=2, default=str)
    Path(run_dir, "verdicts.json").write_text(payload, encoding="utf-8")
    Path(run_dir, "report.txt").write_text(text, encoding="utf-8")
    sarif = json.dumps(sarif_mod.to_sarif(verdicts), indent=2)
    Path(run_dir, "results.sarif").write_text(sarif, encoding="utf-8")
    print(f"\nreport, verdicts and SARIF saved under {run_dir}")
    if args.json:
        n = write_verdicts_json(args.json, verdicts)
        print(f"wrote {n} verdicts to {args.json}")
    if getattr(args, "sarif", None):
        Path(args.sarif).write_text(sarif, encoding="utf-8")
        print(f"wrote SARIF to {args.sarif}")

    code = _exit_code(verdicts, getattr(args, "fail_on", "none"))
    if code == 0 and lm_failures:
        print(f"\norion scan: incomplete -- {len(lm_failures)} Orion-LM batch(es) not judged "
              "(ERROR verdicts above)", file=sys.stderr)
        return 3
    return code


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="orion")
    sub = parser.add_subparsers(dest="cmd", required=True)

    scan = sub.add_parser("scan", help="scan a repo (or an existing scan graph) for candidate leads")
    scan.add_argument("repo", nargs="?", help="path to the repo to build a graph for")
    scan.add_argument("--scan-id", dest="scan_id", help="use an existing scan graph instead of building one")
    scan.add_argument("--watch", action="store_true", help="follow live progress while the scan runs")
    scan.add_argument("--json", dest="json", metavar="OUT", help="also write verdicts as JSON to OUT")
    scan.add_argument("--quiet", action="store_true", help="suppress per-event progress prints (still logs to file)")
    scan.add_argument("--sarif", metavar="OUT",
                      help="also write CONFIRM/INCONCLUSIVE findings as SARIF 2.1.0 to OUT (GitHub "
                           "code scanning); a copy is always saved as <run_dir>/results.sarif")
    scan.add_argument("--resume", metavar="RUN_DIR",
                      help="finish an interrupted scan from its run dir: reuse its leads, verify only "
                           "the leads without a verdict (or with an ERROR), then report")
    scan.add_argument("--verify-routing", dest="verify_routing", action="store_true",
                      help="adaptive verification: clear-cut single-location leads get a light pass "
                           "(same model, smaller budget); anything it can't settle escalates to the "
                           "full pass. Off by default (ORION_VERIFY_ROUTING)")
    scan.add_argument("--verify-context", dest="verify_context", action="store_true",
                      help="give the verifier the code around each lead's claimed location (its enclosing "
                           "function, callers and callees, cut by the tree-sitter code index; needs the "
                           "`lm` extra). Off by default (ORION_VERIFY_CONTEXT)")
    scan.add_argument("--shortlists", dest="shortlists", action="store_true",
                      help="give discovery shapes B/C/D the build's precomputed candidate lists "
                           "(missing controls, hedge comments, ReDoS regexes, deps) inlined in their "
                           "first message. Off by default (ORION_SHORTLISTS)")
    scan.add_argument("--semantic-mode", dest="semantic_mode", choices=("vector", "hybrid", "keyword"),
                      help="how the agents' semantic_search ranks: vector (meaning; the default), hybrid "
                           "(meaning + exact keywords, fused) or keyword (ORION_SEMANTIC_MODE)")
    scan.add_argument("--fail-on", dest="fail_on", choices=tuple(_FAIL_LEVELS), default="none",
                      help="exit 1 if any verdict is at or above this level (CI gating; default none)")
    scan.add_argument("--language", dest="language", metavar="FRONTEND",
                      help="Joern frontend id (jssrc/pythonsrc/golang/javasrc); overrides repo auto-detection")
    scan.add_argument("--stream", dest="stream", action="store_true",
                      help="use the streaming per-function build (avoids the 85x export blob)")
    scan.add_argument("--no-stream", dest="stream", action="store_false")
    scan.set_defaults(stream=True)
    scan.add_argument("--queue-size", dest="queue_size", type=int, default=64,
                      help="functions held in flight by the streaming build (default 64)")
    scan.add_argument("--use-dynamic", "--use-runtime", dest="use_dynamic", action="store_true",
                      help="tell discovery to use runtime facts a prior `orion trace` wrote (off by "
                           "default; keeps the eval baseline prompt unchanged)")
    scan.add_argument("--runtime", action="store_true",
                      help="EXECUTES the target: after the static build, drive it and enrich the graph "
                           "with what ran (executed/hit_count, :ObservedMethod, OBSERVED_CALL/DISPATCH). "
                           "Implies --use-dynamic. Off by default; runs on the host.")
    scan.add_argument("--runtime-budget", dest="runtime_budget", type=int, default=200,
                      help="max inputs the runtime drive loop sends (default 200)")
    scan.add_argument("--runtime-sandbox", dest="runtime_sandbox", choices=_RUNTIME_SANDBOXES,
                      default="auto", help="where --runtime runs a harness script: docker (isolated), host, or auto = docker when available (default)")
    scan.add_argument("--runtime-driver", dest="runtime_driver", choices=_RUNTIME_DRIVERS,
                      default="auto", help="how --runtime exercises the target (default: auto-detect)")
    scan.add_argument("--harness-file", dest="harness_file", metavar="PATH",
                      help="with --runtime: drive with this pinned script instead of the harness agent")
    scan.add_argument("--engine", choices=["claude", "lm"], default="claude",
                      help="discovery engine: `claude` (default; `claude -p` fleet + verifier) or `lm` "
                           "(in-process Orion-LM model via --model; verifier skipped, leads reported "
                           "INCONCLUSIVE)")
    scan.add_argument("--model", dest="lm_model", metavar="PATH",
                      help="path to the Orion-LM model (engine=lm)")
    scan.add_argument("--lm-temperature", dest="lm_temperature", type=float, default=0.0,
                      help="engine=lm sampling temperature (default 0: greedy, the same answer every run)")
    scan.add_argument("--lm-verify", dest="lm_verify", action="store_true",
                      help="engine=lm: re-judge every lead in a second pass on the SAME model, giving "
                           "CONFIRM/REJECT/INCONCLUSIVE/ERROR verdicts (off by default until measured; "
                           "without it lm leads are INCONCLUSIVE \"unverified\")")

    re_ = sub.add_parser("reanalyze",
                         help="recompute a scan's CandidateFlows from the graph in Neo4j (no Joern)")
    re_.add_argument("--scan-id", dest="scan_id", required=True)
    re_.add_argument("--engine", choices=["claude", "lm"], default=None,
                     help="must match the profile the scan was built with (default: the recorded one)")

    gc = sub.add_parser("gc", help="delete old scan partitions, keeping the N newest recorded scans")
    gc.add_argument("--keep", type=int, required=True, metavar="N",
                    help="recorded scans to keep (newest first); at least 1")
    gc.add_argument("--prefix", default="", help="only consider scan_ids starting with this")
    gc.add_argument("--dry-run", dest="dry_run", action="store_true",
                    help="list what would be deleted, delete nothing")
    gc.add_argument("--include-unrecorded", dest="include_unrecorded", action="store_true",
                    help="also delete scans with no OrionScan record (built before records existed); "
                         "refused until a --dry-run --include-unrecorded has listed them")

    tr = sub.add_parser("trace",
                        help="runtime stage on its own: drive the target and record what ran into an "
                             "existing scan graph (requires a prior `orion scan`)")
    tr.add_argument("repo", nargs="?", help="path to the target repo (what gets executed)")
    tr.add_argument("--scan-id", dest="scan_id",
                    help="scan graph to augment (defaults to the deterministic id of repo)")
    tr.add_argument("--sandbox", dest="sandbox", choices=_RUNTIME_SANDBOXES, default="auto",
                    help="where a harness script runs: docker (no network, read-only source), host, "
                         "or auto = docker when its daemon answers (default)")
    tr.add_argument("--driver", dest="driver", choices=_RUNTIME_DRIVERS, default="auto",
                    help="harness (script calling entry points) | http (boot + fuzz routes) | "
                         "process (build exe + fuzz argv/stdin); default auto-detect")
    tr.add_argument("--language", dest="language", metavar="py|js",
                    help="harness language; overrides repo auto-detection (py or js)")
    tr.add_argument("--harness-file", dest="harness_file", metavar="PATH",
                    help="use a pinned driver script instead of the harness agent (skips tokens)")
    tr.add_argument("--budget", dest="budget", type=int, default=200,
                    help="max inputs the drive loop sends (default 200)")
    tr.add_argument("--timeout", dest="timeout", type=float, default=120.0,
                    help="wall-clock seconds per harness run (default 120)")
    tr.add_argument("--watch", action="store_true", help="follow live progress while the trace runs")
    tr.add_argument("--quiet", action="store_true", help="suppress per-event progress prints")
    tr.add_argument("--json", dest="json", metavar="OUT", help="also write the delta summary as JSON")

    idx = sub.add_parser("index-exploits",
                         help="build/refresh the global Metasploit exploit-reference corpus (one-time)")
    idx.add_argument("--refresh", action="store_true",
                     help="re-download the Metasploit metadata index before building")

    ui_ = sub.add_parser("ui", help="local web UI on 127.0.0.1: start, watch and stop scans and read their "
                                    "reports in a browser")
    ui_.add_argument("--port", type=int, default=8770,       # == orion.ui.server.DEFAULT_PORT (imported lazily)
                     help="port on 127.0.0.1 (default 8770; 0 = any free port)")
    ui_.add_argument("--open", action="store_true", help="also open the page in the default browser")
    ui_.add_argument("--verbose", action="store_true",
                     help="log each request's method, path and status (never its query, which holds the "
                          "launch token, nor its body)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.cmd == "scan":
        return _run_scan(args)
    if args.cmd == "index-exploits":
        return _run_index_exploits(args)
    if args.cmd == "trace":
        return _run_trace(args)
    if args.cmd == "gc":
        return _run_gc(args)
    if args.cmd == "reanalyze":
        return _run_reanalyze(args)
    if args.cmd == "ui":
        return _run_ui(args)
    return 0


_RUNTIME_DRIVERS = ("auto", "harness", "http", "process")   # == runtime.targets.DRIVERS
_RUNTIME_SANDBOXES = ("auto", "docker", "host")              # == runtime.targets.SANDBOXES


def _run_trace(args: argparse.Namespace) -> int:
    """`orion trace`: the runtime stage on its own, against an EXISTING scan graph."""
    if not args.scan_id and not args.repo:
        print("orion trace: provide a repo path or --scan-id", file=sys.stderr)
        return 2
    if args.repo and not Path(args.repo).is_dir():
        print(f"orion trace: repo path not found or not a directory: {args.repo}", file=sys.stderr)
        return 2
    if not args.harness_file and not args.repo:
        print("orion trace: a repo path is required unless --harness-file is given", file=sys.stderr)
        return 2
    if args.language and args.language not in ("py", "js"):
        print(f"orion trace: unknown --language {args.language!r}; use py or js", file=sys.stderr)
        return 2

    from . import graph_build, runtime
    from .monitor import run_logger, tail
    from .runtime import report

    repo = args.repo or "."
    scan_id = args.scan_id or graph_build.scan_id_for(args.repo)

    run_dir = _run_dir(scan_id)
    on_event = run_logger(run_dir, quiet=True if args.watch else args.quiet)
    print(f"scan_id: {scan_id}")
    print(f"run log: {run_dir}/progress.jsonl")
    # Safety notice: the runtime stage EXECUTES the target's code.
    print("note: `orion trace` executes the target's code. Harness scripts run in a locked-down Docker "
          "container when available (--sandbox); a web app or binary started by --driver http/process "
          "runs on this host -- only drive repos you trust.")

    drive_stop = threading.Event()    # Ctrl-C: the drive loop ends; the target is still stopped, what ran is kept

    def _do() -> dict:
        return runtime.enrich(scan_id, repo, on_event, budget=args.budget, driver=args.driver,
                              language=args.language, harness_file=args.harness_file,
                              sandbox=args.sandbox,
                              timeout=args.timeout, stop=drive_stop) or {}

    if args.watch:
        stop = threading.Event()
        result: dict = {}

        def _worker() -> None:
            try:
                result["summary"] = _do()
            except Exception as exc:  # noqa: BLE001 -- surface, don't swallow
                result["error"] = exc
            finally:
                stop.set()

        thread = threading.Thread(target=_worker, daemon=True)
        thread.start()
        interrupted = False
        try:
            tail(run_dir, stop)
            interrupted = not stop.is_set()     # tail swallows a Ctrl-C and returns before the worker ends
        except KeyboardInterrupt:
            interrupted = True
        if interrupted:
            drive_stop.set()
            from . import claude_cli
            claude_cli.interrupt()              # the harness agent's killed claude -p is not retried
        thread.join()
        if "error" in result:
            raise result["error"]
        summary = result.get("summary", {})
    else:
        summary = _do()

    print("\n" + "=" * 70)
    print(report.report_text(summary))
    if not summary:
        print(f"\n(runtime stage skipped or failed -- see {run_dir}/progress.jsonl)")

    if args.json:
        Path(args.json).write_text(json.dumps(summary, indent=2, default=str))
        print(f"\nwrote runtime summary to {args.json}")
    return 0


def _run_reanalyze(args: argparse.Namespace) -> int:
    """`orion reanalyze`: rebuild one scan's CandidateFlows from Neo4j (see graph/reanalyze.py)."""
    from .graph import reanalyze
    try:
        out = reanalyze.reanalyze(args.scan_id, args.engine)
    except reanalyze.ReanalyzeRefused as exc:
        print(f"orion reanalyze: {exc}", file=sys.stderr)
        return 2
    pf = out["pathfind"]
    print(f"reanalyzed {out['scan_id']} (profile {out['profile']}): {out['removed']} CandidateFlows removed, "
          f"{out['written']} written ({pf['sources']} sources, {pf['sinks']} sink hits)")
    return 0


def _run_gc(args: argparse.Namespace) -> int:
    """`orion gc`: delete scan partitions beyond the --keep newest recorded ones (see scans.gc)."""
    from .graph import scans
    try:
        out = scans.gc(keep=args.keep, prefix=args.prefix, dry_run=args.dry_run,
                       include_unrecorded=args.include_unrecorded)
    except scans.GcRefused as exc:
        print(f"orion gc: {exc}", file=sys.stderr)
        return 2
    verb = "would delete" if args.dry_run else "deleted"
    for sid in out["kept"]:
        print(f"kept          {sid}")
    for row in out["deleted"]:
        removed = row.get("removed")
        what = (", ".join(f"{k} {v}" for k, v in removed.items() if v) or "nothing") if removed else ""
        print(f"{verb:<13} {row['scan_id']} ({row['status'] or 'unrecorded'}){': ' + what if what else ''}")
    if out["unrecorded"] and not args.include_unrecorded:
        print(f"{len(out['unrecorded'])} unrecorded scan(s) not touched; list them with "
              "--dry-run --include-unrecorded")
    elif args.include_unrecorded and args.dry_run:
        print(f"{len(out['unrecorded'])} unrecorded scan(s) listed in {scans.UNRECORDED_PLAN}; "
              "run again without --dry-run to delete them")
    return 0


def _run_index_exploits(args: argparse.Namespace) -> int:
    """Fetch (if missing/--refresh) and index the global exploit-reference corpus."""
    from . import embed, exploit_corpus

    path = exploit_corpus.DEFAULT_METADATA_PATH
    if args.refresh or not Path(path).exists():
        print(f"fetching Metasploit metadata index -> {path}")
        exploit_corpus.fetch_metadata(path)
    print("indexing exploit-reference corpus (embedding a few thousand modules, one-time)...")
    n = embed.index_exploits(path)
    print(f"indexed {n} exploit-reference documents into the '{embed.EXPLOIT_LABEL}' corpus")
    return 0


def _browser_page(url: str) -> Path:
    """A private page (0600, in a 0700 temp dir) that sends the browser on to `url`. `--open` hands the browser
    this file instead of `url`, so the launch token never sits on the browser's command line, which every local
    user can read (/proc/<pid>/cmdline). Removed when orion ui stops."""
    import html
    import tempfile

    folder = Path(tempfile.mkdtemp(prefix="orion-ui-"))
    page = folder / "open.html"
    target = html.escape(url, quote=True)
    fd = os.open(page, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(f'<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="0; url={target}">'
                f'<title>Orion</title><p><a href="{target}">Open Orion</a></p>\n')
    return page


def _run_ui(args: argparse.Namespace) -> int:
    """`orion ui`: serve the local web UI (orion/ui/) on 127.0.0.1 until Ctrl-C. Every scan it starts is this
    CLI's own `orion scan` in a child process, run from the current directory. Quitting stops a running scan
    first -- the scan runs in its own process group, so nothing else would -- and says so; SIGTERM, SIGHUP and
    Ctrl-Break take the same path as Ctrl-C, and a second Ctrl-C while the scan winds down kills it."""
    import shutil
    import signal
    import webbrowser

    from .ui import server as ui_server      # deferred: only this command loads the UI

    def _say(message: str) -> None:
        try:
            print(message, file=sys.stderr, flush=True)
        except (OSError, ValueError):        # the terminal may be gone (SIGHUP); quitting goes on regardless
            pass

    if not 0 <= args.port <= 65535:
        _say(f"orion ui: --port must be 0-65535 (0 = any free port), not {args.port}")
        return 2
    try:
        ui = ui_server.make_server(port=args.port, verbose=args.verbose)
    except OSError as exc:
        _say(f"orion ui: cannot listen on 127.0.0.1:{args.port} ({exc.strerror or exc}); choose another port "
             "with --port N (0 = any free port)")
        return 2
    print(f"Orion UI: {ui.url}", flush=True)
    print(f"runs: {ui.runs.root} (scans run from {ui.cwd}); Ctrl-C quits", flush=True)

    def _quit(signum, frame):
        raise KeyboardInterrupt

    previous = {}
    for name in ("SIGTERM", "SIGHUP", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                previous[sig] = signal.signal(sig, _quit)
            except (OSError, ValueError):    # not the main thread, or not settable on this platform
                pass
    # Started with SIGINT ignored (a background job of a script, say), the UI would pass that on: an ignored
    # signal stays ignored across exec, so every scan child would ignore Stop's SIGINT too. A handler is reset
    # to the default on exec instead, so a do-nothing one keeps this process ignoring Ctrl-C as it was started
    # to while its scans can still be interrupted.
    if os.name == "posix" and signal.getsignal(signal.SIGINT) == signal.SIG_IGN:
        previous[signal.SIGINT] = signal.signal(signal.SIGINT, lambda signum, frame: None)

    page = None
    if args.open:
        try:
            page = _browser_page(ui.url)
        except OSError as exc:
            _say(f"orion ui: could not write the page that opens the browser ({exc}); open the URL above")
    try:
        if page is not None:

            def _open_browser() -> None:
                # Off the main thread: a console browser (lynx, w3m) blocks until it quits, and the server
                # must already be serving the page it asks for.
                try:
                    opened = webbrowser.open(page.as_uri())
                except Exception:  # noqa: BLE001 -- no browser is not a reason to stop serving
                    opened = False
                # A browser can start and still not read the page (a snap Firefox cannot see /tmp; a Windows
                # browser cannot open a WSL path), and that is never reported back: say what to do either way.
                _say("orion ui: could not open a browser; open the URL above" if not opened else
                     "orion ui: opened a browser; if it shows no page, open the URL above")

            threading.Thread(target=_open_browser, name="orion-ui-open", daemon=True).start()
        ui.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            if ui.jobs.running():
                _say("orion ui: stopping the running scan first (Ctrl-C again kills it)")
            try:
                ui.shutdown()
            except KeyboardInterrupt:
                _say("orion ui: killing the scan")
                try:
                    ui.jobs.stop()           # any stop after the first kills
                except (RuntimeError, OSError):
                    pass
                ui.shutdown(timeout=5.0)
        finally:
            for sig, handler in previous.items():   # None = a handler not set from Python: the default
                signal.signal(sig, handler if handler is not None else signal.SIG_DFL)
            if page is not None:
                shutil.rmtree(page.parent, ignore_errors=True)
    _say("orion ui: stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
