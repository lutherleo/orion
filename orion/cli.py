"""`orion scan <repo>` end to end: build the graph, discover leads, verify them, report.

    orion scan ./NodeGoat                  # build the graph, then discover + verify + report
    orion scan --scan-id <id>               # skip the build, run against an existing scan graph
    orion scan ./NodeGoat --watch           # follow live progress while the scan runs
    orion scan ./NodeGoat --json out.json   # also write the verdicts as JSON
    orion scan ./NodeGoat --quiet           # suppress per-event prints (still logs to file)

Discovery and verification are two SEPARATE `claude -p` sessions (see the design doc) fanned out
and joined here; this module owns only the wiring -- build -> index -> discover -> verify ->
report -- and the run's progress log.

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


def _run_scan(args: argparse.Namespace) -> int:
    # Cheap arg validation BEFORE the heavy lazy imports below, so a typo'd path fails instantly
    # instead of after loading the embedding model / Neo4j driver.
    if not args.scan_id and not args.repo:
        print("orion scan: provide a repo path or --scan-id", file=sys.stderr)
        return 2
    # A missing repo path must fail here with a clear message -- otherwise it reaches joern-parse,
    # which dies with a Java AssertionError stack trace ("Input path does not exist") that reads like
    # an Orion crash. This also catches the aftermath of a failed `git clone` (empty/absent dir).
    if args.repo and not Path(args.repo).is_dir():
        print(f"orion scan: repo path not found or not a directory: {args.repo}", file=sys.stderr)
        return 2

    # Deferred on purpose -- see module docstring.
    from . import config, discover, embed, graph_build, graphdb, report, verify
    from .monitor import run_logger, tail

    needs_build = not args.scan_id
    scan_id = args.scan_id or graph_build.scan_id_for(args.repo)
    run_dir = _run_dir(scan_id)

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

    if needs_build:
        on_event(_event("build", "start", detail=f"building graph for {args.repo}"))
        # Overlap the semantic index with persist: build runs _index_semantic(batch) concurrently
        # with the persist write (item 4), so the two independent costs no longer serialize.
        graph_build.build(args.repo, args.language, on_event,
                          stream=args.stream, queue_size=args.queue_size,
                          on_batch=_index_semantic)
        on_event(_event("build", "done", detail="graph build complete"))
    else:
        on_event(_event("build", "done", detail=f"using existing scan_id {scan_id}"))
        if args.repo:
            _index_semantic()   # --scan-id path: read spans from the already-persisted graph

    # Best-effort: ensure the GLOBAL exploit-reference corpus is indexed (once) so the verifier's
    # exploit_search tool works. Builds ONLY if the metadata file is present locally -- never
    # downloads mid-scan, never fatal. Run `orion index-exploits` to fetch + build it explicitly.
    try:
        if embed.ensure_exploit_index():
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

    # Opt-in runtime enrichment (--runtime): EXECUTE the target and fold observed coverage back into
    # the graph before discovery reads it. Best-effort by contract -- a failure is an event, never an
    # abort, and it only ADDS props/edges (never touches NODE_KEY labels), so the static graph and its
    # FLOWS_TO parity are untouched. Needs a repo checkout to boot/build; skipped on --scan-id only.
    if getattr(args, "runtime", False):
        if args.repo:
            from . import runtime
            runtime.enrich(scan_id, args.repo, profile, on_event, budget=args.runtime_budget)
        else:
            on_event(_event("runtime", "warn",
                            detail="--runtime needs a repo checkout to execute; skipped on --scan-id"))

    def _pipeline():
        # Size the per-shape discovery timeout to the graph: a bigger graph is a bigger search space
        # and needs longer sweeps (see config.discover_timeout). Sizing is best-effort -- if the
        # count query fails we fall back to the reality-based floor, never abort the scan.
        node_count = 0
        try:
            db = graphdb.GraphDB()
            try:
                node_count = db.node_count(scan_id)
            finally:
                db.close()
        except Exception as exc:  # noqa: BLE001 -- best-effort sizing; the floor is a safe default
            on_event(_event("discover", "warn",
                            detail=f"graph node count unavailable, using base discovery timeout: {exc}"))
        d_timeout = config.discover_timeout(node_count)
        on_event(_event("discover", "start",
                        detail=f"discovery fleet starting ({node_count} nodes, per-shape timeout {d_timeout}s)"))
        # Either runtime layer turns on the runtime-facts prompt block: `--use-dynamic` (a prior
        # `orion trace`) or `--runtime` (the enrichment just above). Neither set keeps the eval baseline.
        dynamic_hint = getattr(args, "use_dynamic", False) or getattr(args, "runtime", False)
        leads = discover.discover(scan_id, on_event, profile, timeout=d_timeout,
                                  dynamic_hint=dynamic_hint)
        on_event(_event("discover", "done", detail=f"{len(leads)} candidate leads"))

        on_event(_event("verify", "start", detail=f"verifying {len(leads)} leads"))
        verdicts = verify.verify_all(scan_id, leads, repo_for_verify, on_event)
        on_event(_event("verify", "done", detail=f"{len(verdicts)} verdicts"))
        return verdicts

    if args.watch:
        stop = threading.Event()
        result: dict = {}

        def _worker() -> None:
            try:
                result["verdicts"] = _pipeline()
            except Exception as exc:  # noqa: BLE001 -- surface, don't swallow
                result["error"] = exc
            finally:
                stop.set()

        thread = threading.Thread(target=_worker, daemon=True)
        thread.start()
        try:
            tail(run_dir, stop)
        except KeyboardInterrupt:
            stop.set()
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

    if args.json:
        payload = [dataclasses.asdict(v) for v in verdicts]
        Path(args.json).write_text(json.dumps(payload, indent=2, default=str))
        print(f"\nwrote {len(payload)} verdicts to {args.json}")

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="orion")
    sub = parser.add_subparsers(dest="cmd", required=True)

    scan = sub.add_parser("scan", help="scan a repo (or an existing scan graph) for candidate leads")
    scan.add_argument("repo", nargs="?", help="path to the repo to build a graph for")
    scan.add_argument("--scan-id", dest="scan_id", help="use an existing scan graph instead of building one")
    scan.add_argument("--watch", action="store_true", help="follow live progress while the scan runs")
    scan.add_argument("--json", dest="json", metavar="OUT", help="also write verdicts as JSON to OUT")
    scan.add_argument("--quiet", action="store_true", help="suppress per-event progress prints (still logs to file)")
    scan.add_argument("--language", dest="language", metavar="FRONTEND",
                      help="Joern frontend id (jssrc/pythonsrc/golang/javasrc); overrides repo auto-detection")
    scan.add_argument("--stream", dest="stream", action="store_true",
                      help="use the streaming per-function build (avoids the 85x export blob)")
    scan.add_argument("--no-stream", dest="stream", action="store_false")
    scan.set_defaults(stream=True)
    scan.add_argument("--queue-size", dest="queue_size", type=int, default=64,
                      help="functions held in flight by the streaming build (default 64)")
    scan.add_argument("--use-dynamic", dest="use_dynamic", action="store_true",
                      help="tell discovery to use runtime-observed OBSERVED_* edges from a prior "
                           "`orion trace` (off by default; keeps the eval baseline prompt unchanged)")
    scan.add_argument("--runtime", action="store_true",
                      help="EXECUTES the target: after the static build, boot/build and drive it to "
                           "enrich the graph with observed coverage (executed/hit_count + OBSERVED_CALL). "
                           "Implies --use-dynamic. Off by default; runs on the host.")
    scan.add_argument("--runtime-budget", dest="runtime_budget", type=int, default=200,
                      help="max inputs the runtime fuzz loop drives (default 200)")

    idx = sub.add_parser("index-exploits",
                         help="build/refresh the global Metasploit exploit-reference corpus (one-time)")
    idx.add_argument("--refresh", action="store_true",
                     help="re-download the Metasploit metadata index before building")

    tr = sub.add_parser("trace",
                        help="dynamic layer: run the target and record runtime-observed nodes/edges "
                             "into the existing scan graph (requires a prior `orion scan`)")
    tr.add_argument("repo", nargs="?", help="path to the target repo (source sandbox for the harness)")
    tr.add_argument("--scan-id", dest="scan_id",
                    help="scan graph to augment (defaults to the deterministic id of repo)")
    tr.add_argument("--language", dest="language", metavar="py|js",
                    help="tracer language; overrides repo auto-detection (py or js)")
    tr.add_argument("--harness-file", dest="harness_file", metavar="PATH",
                    help="use a pinned driver script instead of the harness agent (skips tokens)")
    tr.add_argument("--timeout", dest="timeout", type=float, default=120.0,
                    help="wall-clock seconds for the traced run (default 120)")
    tr.add_argument("--watch", action="store_true", help="follow live progress while the trace runs")
    tr.add_argument("--quiet", action="store_true", help="suppress per-event progress prints")
    tr.add_argument("--json", dest="json", metavar="OUT", help="also write the delta summary as JSON")

    args = parser.parse_args(argv)

    if args.cmd == "scan":
        return _run_scan(args)
    if args.cmd == "index-exploits":
        return _run_index_exploits(args)
    if args.cmd == "trace":
        return _run_trace(args)
    return 0


_FRONTEND_TO_TRACER = {"pythonsrc": "py", "jssrc": "js"}


def _run_trace(args: argparse.Namespace) -> int:
    """`orion trace`: the dynamic phase. Augments an EXISTING scan graph with runtime-observed facts."""
    if not args.scan_id and not args.repo:
        print("orion trace: provide a repo path or --scan-id", file=sys.stderr)
        return 2
    if args.repo and not Path(args.repo).is_dir():
        print(f"orion trace: repo path not found or not a directory: {args.repo}", file=sys.stderr)
        return 2
    if not args.harness_file and not args.repo:
        print("orion trace: a repo path is required unless --harness-file is given", file=sys.stderr)
        return 2

    from . import graph_build
    from .dynamic import delta as delta_mod
    from .dynamic import run as dyn_run
    from .monitor import run_logger, tail

    repo = args.repo or "."
    scan_id = args.scan_id or graph_build.scan_id_for(args.repo)

    # Resolve the tracer language: explicit --language wins, else detect from the repo's markers.
    language = args.language
    if language not in ("py", "js"):
        if language:
            print(f"orion trace: unknown --language {language!r}; use py or js", file=sys.stderr)
            return 2
        from .graph import joern_adapter
        frontend, _ = joern_adapter.resolve_language(repo, None)
        language = _FRONTEND_TO_TRACER.get(frontend)
        if language is None:
            print(f"orion trace: no dynamic tracer for this stack (detected {frontend}); "
                  f"Python and JS are supported — pin with --language", file=sys.stderr)
            return 2

    run_dir = _run_dir(scan_id)
    on_event = run_logger(run_dir, quiet=True if args.watch else args.quiet)
    print(f"scan_id: {scan_id}")
    print(f"run log: {run_dir}/progress.jsonl")
    # Safety notice (design §9): the trace EXECUTES the target's code on this host.
    print("note: `orion trace` runs the target repo's code locally (timeout + temp cwd only, no "
          "sandbox) — only trace repos you trust.")

    def _do() -> dict:
        return dyn_run.trace_repo(scan_id, repo, language, harness_file=args.harness_file,
                                  timeout=args.timeout, on_event=on_event)

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
        try:
            tail(run_dir, stop)
        except KeyboardInterrupt:
            stop.set()
        thread.join()
        if "error" in result:
            raise result["error"]
        summary = result.get("summary", {})
    else:
        summary = _do()

    print("\n" + "=" * 70)
    print(delta_mod.report_text(_delta_defaults(summary)))
    if summary.get("note"):
        print(f"\n(note: {summary['note']})")

    if args.json:
        Path(args.json).write_text(json.dumps(summary, indent=2, default=str))
        print(f"\nwrote delta summary to {args.json}")
    return 0


def _delta_defaults(summary: dict) -> dict:
    """report_text expects the full key set; a skip summary may omit samples. Fill defaults."""
    return {
        "new_methods": summary.get("new_methods", 0),
        "observed_calls": summary.get("observed_calls", 0),
        "observed_dispatches": summary.get("observed_dispatches", 0),
        "calls_to_new_methods": summary.get("calls_to_new_methods", 0),
        "method_samples": summary.get("method_samples", []),
        "dispatch_samples": summary.get("dispatch_samples", []),
    }


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


if __name__ == "__main__":
    sys.exit(main())
