# Orion Dynamic Trace Layer — runtime-observed nodes/edges into the static graph

> **Superseded in structure (2026-09-25):** this layer was merged with its sibling into one package, `orion/runtime/` (see CLAUDE.md "Runtime stage"). The rationale here still holds; module names, the `{origin:'dynamic'}` stamp and the separate clears do not.

**Status:** design (2026-08-27). Adds a new phase *after* the static graph build of
`2026-07-12-orion-design.md`; the static build path, MCP tools' read contract, discovery, and
verify are unchanged except for one behavior-preserving `origin` stamp (§6) and one optional
discovery prompt hint (§7).

## 1. Problem

Orion never runs the target. The whole graph is Joern's static CPG — parse-time structure only.
That leaves three blind spots the static graph cannot express, all of which the user named directly
("see if it creates newer nodes than before", "if behaviours change or if pointers switch"):

1. **Missing call targets.** The graph *lies by omission* (CLAUDE.md): calls nested in
   arrow-functions assigned to object properties get no `CONTAINS_CALL` edge. Static analysis simply
   does not see them.
2. **Dynamic dispatch / "pointers switch".** A virtual/dynamic call site's *real* runtime target
   (polymorphism, duck typing, framework dispatch, callback wiring) is not resolvable statically —
   the graph records the call, not which concrete method it actually reached.
3. **Methods that don't exist statically.** Reflection, `eval`, monkey-patching, and
   dynamically-registered handlers create executed code with no static `CpgMethod` at all.

The value proposition is a **delta**: run the code, record what actually happened, and surface what
the runtime saw that the static graph never had. That delta is the product — not new findings by
itself, but a graph that discovery agents can query for runtime-proven paths static analysis could
not reach.

## 2. Goal & non-goals

**Goal.** A new `orion trace` phase that executes the target through an agent-generated harness,
records real runtime behavior with a language tracer, and persists it into the **same** `scan_id`
partition as `origin='dynamic'` relationships and nodes — so discovery reads them through the
existing `run_cypher` with zero read-side change, and a delta report names exactly what runtime
added over static.

**Delivered guarantees.**
- The static graph is **not mutated.** New facts are new labels; the only touch to existing emits is
  stamping `origin='static'` (mechanical, behavior-preserving). NodeGoat stays **217 FLOWS_TO** and
  14/15 — a hard parity gate (§8).
- The dynamic layer owns its own clear. `orion scan` rebuilds never wipe dynamic facts, and
  `orion trace` re-runs never wipe static facts (§6, the two-clear invariant).
- Language-neutral by construction: one `ObservedTrace` shape, two thin language tracers. Python
  first, JS second, one design (§5).

**Non-goals (this stage).**
- **Fuzzing.** No input generation for crashes/injection oracles. The harness *exercises* entry
  points to observe behavior; it does not mutate inputs hunting for faults. Fuzzing is a natural
  follow-on that sits on top of this trace substrate, out of scope here.
- **Runtime lead-proving.** Driving a confirmed source→sink flow to demonstrate reachability is a
  separate later stage; this layer produces graph facts, not verdicts.
- **Hardened sandboxing.** Per the operator's decision, isolation is a wall-clock timeout + a temp
  working directory only — see §9. The runner is structured behind a `Sandbox` seam so a container
  drops in later without touching callers, but no container ships in this stage.

## 3. Why this can't be a static graph property (the three blind spots, concretely)

- Blind spot 1 is a *known, documented* Joern limitation Orion already routes around by making the
  verifier read real source (CLAUDE.md, "the graph lies by omission"). Dynamic execution observes the
  edge directly instead of routing around its absence.
- Blind spot 2 is undecidable statically in general — the concrete target of a dynamic dispatch
  depends on runtime types/values. Only execution resolves it.
- Blind spot 3 produces code objects that never appear in the CPG. There is no static node to attach
  to; the runtime must *create* one.

This is why the layer is a second producer, not a smarter static pass.

## 4. Lifecycle & CLI

```
orion trace <repo> [--scan-id <id>] [--language py|js] [--timeout <s>] [--harness-file <path>]

  1. Resolve scan_id (default: scan_id_for(repo), same identity as `orion scan`).
     Require that a static build already exists for it (else: instruct to run `orion scan` first).
  2. Load :EntryPoint nodes for scan_id (name, file_path, line, enclosing CpgMethod.full_name).
  3. harness.generate(): a claude -p agent reads those entry points + reads real source and writes a
     driver script that imports/boots and calls them. --harness-file skips the agent (pin a driver).
  4. runner.run(): execute the driver under the language tracer, in a temp CWD, with --timeout.
  5. tracer emits an ObservedTrace (§5).
  6. merge.to_batch(): map trace frames onto existing nodes, emit OBSERVED_* edges + ObservedMethod
     nodes into a schema.Batch (origin='dynamic', same scan_id).
  7. persist_dynamic(): clear ONLY the dynamic labels for scan_id, then load the batch (§6).
  8. delta.report(): Cypher diff → human-readable "N new call edges, M new dispatch targets, K
     runtime-only methods static analysis missed", written to the run log and returned.
```

`orion scan <repo> --then-trace` is sugar that runs the static build then invokes step 2 onward in
one command. `orion trace` stays a first-class standalone phase (matches the layered working
agreement: build → orchestration → harness, each proven with real output before the next).

Every step is fallible-by-contract and follows the existing resilience rule (`claude_cli`,
CLAUDE.md): a failed/empty/timed-out harness or a crashed tracer is a **logged skip that produces an
empty delta**, never a crash and never a fabricated edge. A trace that observes nothing is a valid,
honestly-reported outcome.

## 5. Components (one purpose each; file-per-concern grain)

All under a new `orion/dynamic/` package.

- **`trace.py`** — the language-neutral data contract. `@dataclass(frozen=True)` types:
  - `ObservedCall(caller_file, caller_line, caller_name, callee_file, callee_line, callee_name)`
  - `ObservedDispatch(call_site_file, call_site_line, resolved_callee_name, resolved_callee_file, resolved_callee_line)`
  - `ObservedMethod(name, file, line)` — an executed function with no obvious static counterpart.
  - `ObservedTrace(calls: tuple[ObservedCall, ...], dispatches: tuple[...], methods: tuple[...])`
  Pure; imported by everything below and unit-tested with hand-built traces (no runtime needed).

- **`tracer_py.py`** — Python tracer. `sys.monitoring` (3.12+) with a `settrace` fallback for older
  interpreters. Records `PY_START`/`CALL` events, capturing the code object's `co_filename`,
  `co_firstlineno`, and the call site's frame, into an `ObservedTrace`. Runs **in-process** with the
  harness (no second process needed for Python).

- **`tracer_js.py`** — Node tracer. Launches the driver with `--inspect-brk` and drives the V8
  Inspector Protocol (CDP) over the debugger websocket, or, as the simpler first cut, an
  `NODE_OPTIONS` require-hook that patches `Module._load`/`Function.prototype.call` boundaries to log
  call frames to a JSONL sidecar the Python side reads back into an `ObservedTrace`. (The spec picks
  the require-hook first cut for JS; CDP is a documented upgrade if attribution proves too coarse.)

- **`runner.py`** — executes a harness behind a `Sandbox` protocol.
  - `Sandbox.run(cmd, cwd, timeout, env) -> RunResult(stdout, stderr, exit_code, timed_out)`
  - `SubprocessSandbox` — the shipped implementation: temp CWD, wall-clock timeout, target dir on the
    path; no network/resource isolation (§9). `DockerSandbox` is a named future implementer of the
    same protocol.

- **`harness.py`** — the harness-generation agent. Reuses `claude_cli.run_agent` with the same
  resilience (salvage stdout+stderr on non-zero, retry a TRANSIENT failure on a fresh `--session-id`,
  crashed call → error sentinel, never a silent success). It is given the entry-point list and MCP
  read access (`run_cypher`, `semantic_search`) plus `--add-dir` on the target for source reading. It
  emits a driver script to a temp path; a malformed/empty script is a logged skip.

- **`merge.py`** — maps `ObservedTrace` frames onto existing graph nodes and emits edges into a
  `schema.Batch`. Attribution key: `(file_path, line, name)` matched against `CpgMethod`/`CpgCall`
  already in the partition (looked up via a read query up front, so `merge` is pure over its inputs
  and unit-testable). A frame that matches a static node → `OBSERVED_CALL`/`OBSERVED_DISPATCH`; a
  frame with no static match → an `ObservedMethod` node + the observed edge to/from it.

- **`delta.py`** — the diff. Given the persisted partition, counts and lists: `OBSERVED_CALL` edges
  with no parallel static `CONTAINS_CALL`/call path, `OBSERVED_DISPATCH` targets absent from static
  resolution, and `ObservedMethod` nodes. Returns a structured summary + a one-paragraph human
  report. Emitted as `build`/`dynamic` `ProgressEvent`s so the monitor surfaces it live.

## 6. Graph shape & the two-clear invariant

New relationship types, all stamped `origin='dynamic'` and `scan_id` (via `Batch.emit_edge`, which
already stamps `scan_id` on both endpoints — B2 fix applies unchanged):

- `OBSERVED_CALL` : `(:CpgMethod)-[:OBSERVED_CALL]->(:CpgMethod)` — a caller/callee pair actually
  executed. Property `origin='dynamic'`, plus `hits` (observation count).
- `OBSERVED_DISPATCH` : `(:CpgCall)-[:OBSERVED_DISPATCH]->(:CpgMethod|:ObservedMethod)` — the real
  concrete target a dynamic call site resolved to at runtime ("pointers switch").

New node label:

- `ObservedMethod` : `("scan_id", "uid")` where `uid = synthesize_uid(scan_id, "ObservedMethod",
  file, line, 0, name)` — deterministic, so a re-trace MERGEs onto the same node. This is the literal
  "creates newer nodes than before": executed code with no static `CpgMethod`.

**`origin` on static.** Existing emits gain `origin='static'` so the filter is symmetric and "new"
is a clean predicate. This is the one touch to the static build. It is behavior-preserving —
`NODE_KEY`/edge identity is unchanged, so `_node_rows`/`_edge_rows` collapse identically and the
persisted graph is byte-for-byte the same except for the added constant property. The 217-FLOWS_TO
tripwire proves it (§8).

**Two-clear invariant (the load-bearing isolation detail).** `persist._clear` is label-scoped over
`NODE_KEY`'s labels. If dynamic labels went into `NODE_KEY`, an `orion scan` rebuild would DETACH
DELETE them. So:

- `schema.NODE_KEY` — static labels only, unchanged. `persist._clear` clears exactly these.
- `schema.DYNAMIC_NODE_KEY` — `{"ObservedMethod": ("scan_id","uid")}`. A new `persist_dynamic`
  clears **only** these labels (and detaches the `OBSERVED_*` edges with them) for the scan_id, then
  loads the dynamic batch.

Result: `orion scan` rebuilds static without touching dynamic facts; `orion trace` re-runs replace
dynamic facts without touching static. One partition, two independent, idempotent clears. `OBSERVED_*`
edges are removed by the DETACH on their `ObservedMethod`/matched endpoints during the dynamic clear;
edges between two static nodes are cleared by a dedicated `MATCH ()-[r]->() WHERE r.origin='dynamic'
AND r.scan_id=$sid DELETE r` so a dynamic clear never leaves an orphaned observed edge on a surviving
static node.

## 7. Data flow to discovery (zero read-side change)

Discovery and verify read via `run_cypher` scoped to a `scan_id`. Dynamic edges live under that same
`scan_id`, so they are visible the instant they persist — no MCP change, no new tool, no schema-read
change (`get_schema` reflects the frozen vocabulary and will simply list the new relationship types
once present).

Two additive touches:

1. **Optional discovery prompt hint** (behind a flag, off for the parity/eval baseline so the 14/15
   number stays comparable): "Relationships with `origin='dynamic'` (`OBSERVED_CALL`,
   `OBSERVED_DISPATCH`) and `:ObservedMethod` nodes were observed at RUNTIME — static analysis could
   not see them. A source→sink flow that traverses one is a runtime-proven path; prioritize it and
   still ground every claim with a query."
2. **Monitor delta** — `delta.py` emits `ProgressEvent`s (`phase:"dynamic"`) so a run is watchable
   per the working agreement ("long runs must be watchable").

## 8. Testing & the parity gate

Token-free unit tests (Orion's `tests/`, `pytest -m "not slow"`), following the existing pattern:

- `test_dynamic_trace.py` — `ObservedTrace` dataclass invariants.
- `test_dynamic_merge.py` — frame→node attribution: a frame matching a static node yields
  `OBSERVED_*`; an unmatched frame yields an `ObservedMethod` + edge. Pure, hand-built inputs.
- `test_dynamic_delta.py` — the diff math (new-vs-static counting) on a synthetic batch.
- `test_dynamic_clear.py` — the two-clear invariant as pure logic: dynamic clear touches only
  `DYNAMIC_NODE_KEY` labels + `origin='dynamic'` edges; static clear touches only `NODE_KEY`.

**Hard parity gate (pass/fail).** `tests/test_stream_build.py::test_stream_flows_parity` stays green:
a static-only build still persists **217 FLOWS_TO** on NodeGoat, proving the `origin='static'` stamp
is behavior-preserving. A static build followed by NO trace leaves the graph identical to today's.

**`@slow` end-to-end (deliberate, tokened).** On PyGoat: `orion scan` then `orion trace` produces
**≥1 `OBSERVED_DISPATCH`** whose target is absent from the static graph — the headline delta,
demonstrated with real output. Second: the NodeGoat 14/15 recall is unchanged with the dynamic hint
OFF (baseline preserved) and measured with it ON (the actual question — does runtime data help).

## 9. Safety (stated once, honestly)

Per the operator's decision, the shipped isolation floor is a **wall-clock timeout + a temp working
directory**, no container and no network isolation. `orion trace` **executes the target repo's code
on the host** (and an agent-written harness that imports it). This is acceptable for trusted targets
— the operator's own code and the known benchmarks (NodeGoat/PyGoat) — and is unacceptable for
untrusted third-party code, which would run arbitrary code on the host. The CLI prints a one-line
notice to that effect on first run. The `Sandbox` protocol in `runner.py` is the single seam where a
`DockerSandbox` (Orion already requires Docker for Neo4j) drops in to raise the floor, with no change
to any caller — that hardening is a named follow-on, not this stage.

## 10. Coverage honesty (the delta is a lower bound)

Dynamic analysis sees only what the harness exercises. The delta is a **lower bound** on runtime
behavior — "these paths were observed", never "these are all the dynamic paths". Every delta report
says so explicitly, in the same spirit as the streaming-build's honest O(repo)-memory note. An empty
delta means "the harness exercised nothing new", not "the code has no dynamic behavior".

## 11. Build order (layered, per the working agreement)

1. `trace.py` + unit tests — the contract, provable with no runtime.
2. `tracer_py.py` + `runner.py`/`SubprocessSandbox` — trace a tiny hand-written Python driver, show a
   real `ObservedTrace`.
3. `merge.py` + `delta.py` + `DYNAMIC_NODE_KEY`/`persist_dynamic` + the two `origin` stamps — persist
   into a scan_id, show the delta Cypher; run the parity gate (217 FLOWS_TO).
4. `harness.py` — the agent writes the driver for PyGoat; end-to-end `orion trace` with real output.
5. `tracer_js.py` — the same loop on NodeGoat.
6. Discovery hint + monitor delta + `@slow` recall measurement (hint OFF vs ON).

Each layer is discussed → approved → implemented → shown with real output before the next, and no
layer past the static build ships until the parity gate is green.
