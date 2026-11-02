# Review of bbcd2ab, round 2: the fixes, re-checked

Done 2026-11-01 on `Kullervo` at `3e46ac2` (the round-1 fixes are `5cb8b75`; later commits touching the same files
are `6a945a6`, `34ba646`, `bf4f24d`, `20b51b8`). Every repro in [repros/](repros/) was run against the current code
and against an export of `bbcd2ab` (see [README.md](README.md) for how).

Round 2 left two small issues, R2-1 and R2-2. Both are fixed in the commit that adds this report. Testing them with
the real `claude` CLI, with no tokens spent, also showed that this report's first version got parts of both wrong.
The corrections are under each one.

Tests with the fixes in: `tests/test_scan_stop.py tests/test_runlock.py tests/test_cli_resume.py
tests/test_cli_trace_guard.py tests/test_ui_*.py tests/test_runtime_wiring.py tests/test_runtime_engine.py
tests/test_lm_engine.py` -> 709 passed, 9 skipped (694 and 9 before them). The whole token-free suite
(`pytest -m "not slow"`) -> 1578 passed, 41 skipped. Breaking each fix on purpose in a scratch copy (8 ways) made the
new tests fail every time.

## Round-1 findings: status

| # | Finding | bbcd2ab | Now | Status |
|---|---|---|---|---|
| 1 | Ctrl-C under `--watch` (`repro_watch.py`) | 18 launches, `interrupted()` False, exit 0, verdicts.json written, 6 verdicts logged | 4 launches, `interrupted()` True, KeyboardInterrupt, no verdicts.json, 0 logged | fixed |
| 1 | the same, no `--watch` (control) | 4 launches, True, KeyboardInterrupt, none written | same | was fine |
| 2 | pinned harness file vs repo descriptor (`repro_harness_pin.py`) | case 1: HttpDriver `['echo', 'repo-command-on-host']`; case 2: the repo's script and image | both: HarnessDriver on `mine.py`, default image `python:3.12-slim` | fixed |
| 3 | Windows first Stop (`repro_winstop_jobs.py`) | no job object; target still running after the scan exited | in a job object; target gone | fixed |
| 4 | Resume env (`repro_resume_env.py`) | `ORION_VERIFY_ROUTING=1`, `ORION_VERIFY_CONTEXT=1` | both `0` | fixed |
| 5 | route and header variables (`repro_route_vars_real_claude.py`) | every one reaches key and endpoint scans | every one dropped | fixed: R2-2 below |
| 6 | `--resume` locks after reading | lock taken after `_load_run` | lock taken first; an unwritable run dir refused (exit 2) | fixed (code reading) |
| 7 | `held()` needs write access; `acquire`'s open outside its try | as described | read handle, shared lock on POSIX; `_NoLock` when no lock file can be made | fixed (code reading) |

`winstop_os.py` still shows the OS behaviour (Ctrl-Break ends the python child at once, a grandchild in its own
group survives): that is Windows, not Orion. The job object is what now ends that grandchild.

## R2-1: under `--watch`, a stop could start a session nothing ever signals (fixed; very low in practice)

**The failure.** Adaptive verification's light pass has already failed once, and the stop kills its last attempt.
`verify_lead` escalates at once to a full session. If the stop is not flagged yet (`claude_cli.interrupt()`), that
session starts. It began after the SIGINT, so no signal reaches it, and the scan cannot exit until it times out
(`VERIFY_TIMEOUT`, 600 s) or someone forces a stop.

**What the first version of this report got wrong.** It tested with a fake `claude -p` that exited 130 and a stop
flagged 50 ms later, and suggested spotting that exit code in `_run_once`. Run against the real CLI (a local server
that never answers, a dummy key, an empty config folder), neither assumption held:

- **An interrupted `claude -p` exits 0.** The exit-code fix would never have fired. `claude_interrupt_exit.py`,
  mid-request:

  | CLI | Signal | Runs | Exit | Took | Result event |
  |---|---|---|---|---|---|
  | 2.1.316, WSL | SIGINT to its group | 3 | 0 | 0.01 to 0.02 s | none |
  | 2.1.291, Windows (`claude.exe`) | Ctrl-Break to its group | 3 | 0 | 0.03 s | none |

  Earlier runs, before the script's last changes, gave the same exit code: 6 more in WSL, 3 more on Windows (up to
  0.08 s). With the flags Orion passes, the WSL session did print one (`repro_stop_real_claude.py`): subtype
  `error_during_execution`, `is_error` true, exit 0, ended after about 165 ms. Every form reads as a failure.
- **On an ordinary run the flag comes first, by about 165 ms.** `repro_stop_real_claude.py --after-transient` on the
  unfixed code, real SIGINT to the scan's process group, 8 of 8 runs (4 with `--watch`, 4 without): stop flagged at
  +0.3 to +0.5 ms, session ended at +161 to +167 ms, no new session, scan gone in 0.21 to 0.26 s.

**The real trigger.** Under `--watch` the main thread flagged the stop only after `tail()`'s final drain, which
re-reads the whole progress log. A long enough log lets the session end first (same repro, unfixed code, `--watch`):

| Progress log | Runs | Stop flagged | Session ended | Fresh session | Scan |
|---|---|---|---|---|---|
| empty | 4 | +0.3 to +0.4 ms | +163 to +167 ms | none | gone in 0.21 to 0.26 s |
| 20,000 events | 2 | +43, +45 ms | +168, +169 ms | none | gone in 0.21 s |
| 100,000 events | 2 | **+257, +282 ms** | +179, +186 ms | **+183, +186 ms** | **still running at 30 s**, killed |

The largest of the 20 real scan logs on this box is 208 events. So the trigger is far from today's runs, and it also
needs the opt-in `--verify-routing`, `--watch`, and a light pass that had already failed once. The path without
`--watch` never reads the log.

**The fix.** `monitor.tail(on_interrupt=)` runs as soon as the Ctrl-C lands, before the drain; `orion scan` and
`orion trace` pass their stop flags to it. Same repro, fixed code, `--watch`:

| Progress log | Runs | Stop flagged | Fresh session | Scan |
|---|---|---|---|---|
| 100,000 events | 3 | +0.2, +0.2, +0.3 ms | none | gone in 0.36 to 0.37 s |
| 300,000 events | 3 | +0.2, +0.6 ms; **one run +292 ms** | none in two; **one in the third** | gone in 0.82 s in two; the third still running at 30 s, killed |

**What is left.** The one lost run at 300,000 events, about 1,400 times the largest real log here. Its cause is not
pinned down: a full garbage-collection pass (6 ms) and freeing the event list (50 ms) both measured too short to
explain it, while each re-read of a log that size takes about 600 ms. A `tail` that reads only new lines would remove
that re-read, but it is not worth the code at today's log sizes.

**Not run:**
- A real Ctrl-C on Windows: it cannot be aimed at one process group there. The UI's Stop on Windows ends the scan at
  once, so this path applies only to a terminal Ctrl-C.
- A stop during a tool call. Only a stop during a model request was measured.
- A real `orion trace --watch` stop. Its wiring has a token-free test only.

## R2-2: route and header variables still reached key and endpoint scans (fixed)

`repro_route_vars_real_claude.py`: endpoint mode, the real CLI (2.1.316) started with the environment
`keys.Access.child_env` builds, a local endpoint answering 400, and no credentials anywhere for it to find. Requests
that reached the endpoint named on the page:

| Variable in the UI's environment | bbcd2ab | 3e46ac2 | Now |
|---|---|---|---|
| none (control) | 1 | 1 | 1 |
| `ANTHROPIC_CUSTOM_HEADERS` | 1, **header received** | 1, **header received** | 1, no header |
| `CLAUDE_CODE_USE_BEDROCK` | **0** (asks for AWS credentials) | 1 | 1 |
| `CLAUDE_CODE_USE_VERTEX` | **0** (asks for Google credentials) | 1 | 1 |
| `CLAUDE_CODE_USE_FOUNDRY` | **0** (asks for a Foundry resource) | 1 | 1 |
| `CLAUDE_CODE_USE_MANTLE` | **0** (asks for AWS credentials) | **0** | 1 |
| `CLAUDE_CODE_USE_ANTHROPIC_AWS` | **0** (asks for a workspace ID) | **0** | 1 |
| `CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD` | **0** (asks for a project) | **0** | 1 |
| `CLAUDE_CODE_USE_GATEWAY` | 2 | 2 | 1 |

This also turns round 1's finding 5 from docs-backed into measured: each of the six switches with 0 requests outranks
the base URL and the credential set with it.

**What the first version of this report got wrong:**
- It named three variables. The CLI's own provider table has more: `_ANTHROPIC_GOOGLE_CLOUD` reroutes the same way,
  and `_GATEWAY` still used the endpoint but sent 2 requests instead of 1 (why was not looked into). Both are dropped
  too.
- It suggested dropping every `CLAUDE_CODE_USE_*` name. That would have been a bug: the same prefix names plain
  settings such as `_POWERSHELL_TOOL` and `_NATIVE_FILE_SEARCH`. So `keys.PROVIDER_VARS` is a list, and a test pins
  it. A switch added in a later CLI has to be added by hand; `repro_route_vars_real_claude.py` shows how to check one.

**The fix.** `PROVIDER_VARS` now has all seven switches, and `HEADER_VARS` adds `ANTHROPIC_CUSTOM_HEADERS`. All are
dropped in the key and endpoint modes, in any letter case, and the page says so by name, never showing the value.
Login mode keeps them as the CLI's own setup.

**Limits:**
- An endpoint that needs a custom header can no longer get one through an inherited variable, and the page offers
  only a token.
- Key mode was not run with the real CLI, because it would reach Anthropic's real API. It takes the same `child_env`
  path, covered by token-free tests.
- `HTTPS_PROXY` still passes through in every mode, on purpose: it is the machine's network setup, not a route choice.

## Nits (not changed)
- **POSIX force stop needs `ps`** (`orion/ui/jobs.py:523`, `_descendants`). Slim container images
  (`python:3.12-slim`) ship no procps. There `_descendants` returns `[]` silently, a force stop kills only the
  scan's group, and a runtime target in its own session survives. Reading `/proc/<pid>/stat` when `ps` is missing
  would cover Linux. Not run here.
- **`_reap` closes the job handle without the Jobs lock** (`orion/ui/jobs.py:782-788`). A force stop holds that lock
  through `taskkill /T` (up to 15 s), then calls `tree.end()` on a handle `_reap` may have closed meanwhile. The
  handle value can be reused, but never by another job object (`start` waits on the same lock), so today the late
  call just fails. Ending and closing under the lock, or marking the job closed, would make that safe by
  construction.

## Checked and fine at 3e46ac2
- **Stopping a scan:**
  - `tail()` returning early counts as a Ctrl-C (claude, lm and `orion trace --watch`).
  - The worker hands a KeyboardInterrupt to the main thread. An interrupted discovery saves no leads.json, and an
    interrupted verify writes no verdicts.json.
  - An ERROR after the stop is not logged, so `--resume` verifies that lead.
  - Orion-LM batches a stop skipped are failures (exit 3).
  - `HttpDriver.start` stops what it booted when the boot wait is interrupted (it catches BaseException).
- **The scan's process tree (`orion/ui/jobs.py`):**
  - The child is started suspended, put in a job object (KILL_ON_JOB_CLOSE), then resumed. The venv launcher's
    real interpreter therefore starts inside the job.
  - `_reap` ends the job when the scan exits.
  - A Windows force stop runs `taskkill /T` first, then ends the job.
  - A POSIX force stop reads the tree before SIGKILL.
- **Options and model access:**
  - `env_pins` pins the four ORION_* twins for new scans and Resume. Pinning `ORION_SHORTLISTS=0` on a resume is
    harmless, since a resume runs no discovery.
  - Resume validates a recorded model and defaults a legacy run to sonnet.
  - An endpoint model may not start with "-".
  - The provider switches are noted in login mode.
- **Runtime-stage precedence:** a pinned harness file beats the descriptor when the harness driver runs it. An
  explicit `http` / `process` driver keeps a matching descriptor, which is the same trust as `npm start` or
  `go build`.
- **The run lock:** taken before `_load_run`; unwritable run dirs are refused; the probe opens read-only and takes a
  shared lock on POSIX.
- **The page:** the Stop wording follows the job's engine and Windows' immediate stop (`state.platform` is the
  server's `sys.platform`, so WSL reads as linux).
