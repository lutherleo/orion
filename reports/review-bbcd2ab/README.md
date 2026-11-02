# Review of bbcd2ab (Phase 7, `orion ui`)

An independent, read-only review of commit `bbcd2ab` on `Oracle` (2026-10-29), done from another session on the
Windows box. It looked for correctness and security bugs in the shared scan pipeline that Phase 7 changed and in
`orion/ui/`. The fixes landed in `5cb8b75`. [round2.md](round2.md) re-checks them on `Kullervo` at `3e46ac2`
(2026-11-01), then measures and fixes the two issues left, with the real `claude` CLI and no tokens.

Tests the review was asked to run, at `bbcd2ab`: `tests/test_scan_stop.py tests/test_runlock.py
tests/test_cli_resume.py tests/test_ui_*.py tests/test_runtime_wiring.py` -> 563 passed, 6 skipped. None of the
bugs below was covered by a test.

## Findings at bbcd2ab, most serious first

| # | Finding | Where (at bbcd2ab) | Evidence | Fixed in 5cb8b75 |
|---|---|---|---|---|
| 1 | Ctrl-C under `--watch` never calls `claude_cli.interrupt()` | `orion/cli.py:479-491`, `orion/monitor.py:116` | `repro_watch.py` | yes |
| 2 | A pinned `--harness-file` loses to the scanned repo's `.orion/runtime.json` | `orion/runtime/targets.py:109` | `repro_harness_pin.py` | yes |
| 3 | Windows: the first Stop leaves the runtime target running | `orion/ui/jobs.py:501-512` | `winstop_os.py`, `repro_winstop_jobs.py` | yes |
| 4 | UI Resume cannot keep a recorded "off" for verify_routing / verify_context | `orion/ui/server.py:449-451` | `repro_resume_env.py` | yes |
| 5 | Cloud-provider variables override the access mode picked on the page | `orion/ui/keys.py:55-58` | Claude Code docs; measured in round 2 (`repro_route_vars_real_claude.py`) | partly; the rest in round 2 |
| 6 | Low: `--resume` reads the run before it takes the run lock | `orion/cli.py:201` vs `:273` | code reading | yes |
| 7 | Nit: `runlock.held()` opens the lock file for writing; `acquire`'s open is outside its try | `orion/runlock.py:57, 74` | code reading, Windows check | yes |

### 1. Ctrl-C under `--watch` never calls `claude_cli.interrupt()`
`monitor.tail` catches KeyboardInterrupt and returns normally, so the new `except KeyboardInterrupt:` branch in
`_run_scan` (with `claude_cli.interrupt()`) never runs. Under `--watch` the pipeline runs in a worker thread,
whose `asyncio.run` installs no SIGINT handler, so the two new CancelledError hooks never fire either.

Scenario: `orion scan repo --watch`, then Ctrl-C while verifying. The terminal's SIGINT kills the running
`claude -p` sessions, `run_agent` retries each on a fresh session, and the scan goes on and pays twice for the
killed sessions. This is the bug the commit said it fixed "in a terminal too".

`repro_watch.py` at bbcd2ab: 6 leads gave 18 `claude -p` launches, `interrupted()` stayed False, and the CLI
exited 0 with `verdicts.json` written. The same repro with `--no-watch` (the UI's path) stopped cleanly: 4
launches, `interrupted()` True, KeyboardInterrupt, no `verdicts.json`.

Same root cause, older (since `3efca05`): `lm_stop` was never set under `--watch`, although CLAUDE.md said the lm
engine stops "with or without --watch". `orion trace --watch` had the same shape.

Suggested fix: count an early return from `tail()` as a Ctrl-C, then make the worker abort instead of finishing.
Otherwise an interrupted discovery would save a partial or empty `leads.json` that `--resume` never refills, and
every remaining lead would become ERROR with exit 0.

### 2. A pinned `--harness-file` loses to the repo's `.orion/runtime.json`
The new precedence check fired only for an explicit `--runtime-driver`.
- Case 1: `--runtime --harness-file mine.py --runtime-sandbox docker`, driver left at its default `auto`, and a
  repo descriptor `{"kind": "http", "boot": [...]}`. `select` returned HttpDriver with the repo's boot command,
  which runs on the host.
- Case 2: with `--runtime-driver harness`, a descriptor of kind harness replaced the pinned script with the repo's
  own and picked the Docker image.

Both cases are confirmed by `repro_harness_pin.py`. Terminal only: the UI offers no harness file.
Suggested fix: treat a pinned harness file as an explicit "harness" choice.

### 3. Windows: the first Stop leaves the runtime target running
Ctrl-Break ends the scan's python at once (exit 0xC000013A), so no `finally` runs and `drv.stop(target)` is
skipped. The target `HttpDriver.start` put in a process group of its own keeps running and holding its port. The
job then reads "exited", Stop answers "no scan is running", and `taskkill /T` (second press only) is never reached.

Confirmed on this box: `winstop_os.py` shows the OS behaviour without Orion, and `repro_winstop_jobs.py` shows it
through `orion.ui.jobs` (at bbcd2ab: no job object, target still running after the scan exited). POSIX had the
same leak on Force stop: SIGKILL went to the scan's group, and the target is in a session of its own.

Suggested fix: a Windows job object for the scan, or `taskkill /T` while the parent is still alive.

### 4. UI Resume cannot keep a recorded "off" for verify_routing / verify_context
A bool flag can only switch an option on, and the UI's own environment passed through `child_env`. So
`ORION_VERIFY_ROUTING=1` / `ORION_VERIFY_CONTEXT=1` in the server's environment turned both on for a run recorded
without them. `repro_resume_env.py` at bbcd2ab: recorded False/False, no flag in argv, child env has both = 1. A
new scan had the same problem: an unticked box was overridden by the env.

### 5. Cloud-provider variables override the access mode picked on the page
`ACCESS_VARS` / `LOGIN_VARS` did not cover `CLAUDE_CODE_USE_BEDROCK` / `_VERTEX` / `_FOUNDRY`.

Claude Code's [authentication precedence](https://code.claude.com/docs/en/authentication.md#authentication-precedence)
puts cloud-provider credentials first whenever one of those switches is set, above `ANTHROPIC_AUTH_TOKEN` and
`ANTHROPIC_API_KEY`. For Bedrock, the [gateway page](https://code.claude.com/docs/en/llm-gateway-connect.md) says
Claude Code drops the `Authorization` header unless `CLAUDE_CODE_SKIP_BEDROCK_AUTH=1`.

Scenario: the user's shell exports `CLAUDE_CODE_USE_BEDROCK=1`, they start `orion ui` and pick "Anthropic key".
Scans then sign in with the AWS credentials and bill Bedrock, not the key the page shows. In endpoint mode the
endpoint's token is dropped, and the docs do not say whether `ANTHROPIC_BASE_URL` is still used. leads.json
records `endpoint: false` either way. Not run in round 1. Round 2 measured it with no credentials present: with any of
those switches set, the real CLI sends nothing to the base URL (round2.md, R2-2).

The env-vars page also lists `CLAUDE_CODE_USE_MANTLE`, `CLAUDE_CODE_USE_ANTHROPIC_AWS`, their base URLs,
`ANTHROPIC_CUSTOM_HEADERS` and `HTTPS_PROXY` as route or header variables.

### 6. Low: `--resume` read the run before it took the run lock
Scenario: a resume started while another scan is finishing reads a stale "finished" set and gets the lock once that
scan exits. It then re-verifies, and pays again for, the leads verified in between.

### 7. Nit: `runlock.held()` opened the lock file for writing
A lock file the UI cannot write (a scan run with sudo) read as "not held", so a live scan showed as stopped with
Resume offered. `acquire`'s `open` was outside its `try`, so an unwritable run dir crashed `orion scan` with a
traceback. A read-only handle works for the probe: checked on Windows, it gets errno 13 while the lock is held.

Other nits:
- The page's Stop text said a scan "finishes its current step", but a Claude scan's running sessions are killed
  (POSIX) and the whole scan ends at once (Windows).
- `keys._MODEL_RE` accepted an endpoint model starting with "-", which an endpoint-mode Resume would pass to
  `claude --model` unchecked.

## Checked and fine at bbcd2ab
- The scan pipeline: `run_agent`'s retry loop (the stop is checked before each attempt, no backoff after it), and
  the non-watch stop path (asyncio.Runner cancels, the hooks call `interrupt()`, KeyboardInterrupt; an interrupted
  discovery saves no leads.json; killed sessions' verdicts are not logged).
- The UI's Python side: `server.resume`'s route and model rules, the NO_TOKEN / LOGIN_VARS wiring and the
  scrubber, `_send_stop`'s poll-first and own-group guard, `resume_argv`'s absolute-path and allowlist checks.
- The runtime stage: `targets.select` for an explicit driver, and `target_env`.
- The page: `pathParts` (a linear regex, no lookbehind), the localStorage default model (validated, falls back),
  the per-screen stateHook, and the server count that overrides nVerdicts.

## Not verifiable on Windows
- `--open`'s redirect page lives in the system temp folder. A snap browser (Ubuntu's default Firefox) has a private
  `/tmp`, and a Windows browser cannot open a WSL `/tmp` path. `webbrowser.open` still returns True, so the user
  got no hint. (5cb8b75 now prints what to do either way.)
- The POSIX Stop path (killpg, start_new_session), flock, and the ignored-SIGINT handler read correctly, but were
  not run.
- Whether `claude` itself exits promptly on SIGINT or Ctrl-Break needed a live session. Round 2 measured it: in under
  250 ms, with exit 0 (`claude_interrupt_exit.py`, `repro_stop_real_claude.py`).

## The repro scripts
Token-free: nothing is sent to a model. Most scripts fake `claude -p` and the UI's process launcher. The three marked
"real CLI" run the installed `claude` against a local server, with a dummy key and an empty config folder, so no login
is read. Scripts that use Orion take an optional checkout path (default: this repo; `--no-watch`-style flags may come
before it), print which `orion` code they ran, and keep their data in temp folders. To run one against `bbcd2ab`,
export that tree and pass its path:

```bash
git archive bbcd2ab orion | tar -x -C /tmp/at-bbcd2ab
```

```bash
.venv/Scripts/python.exe reports/review-bbcd2ab/repros/repro_watch.py /tmp/at-bbcd2ab
```

| Script | What it shows |
|---|---|
| `repro_watch.py [--no-watch]` | Ctrl-C during a `--watch` resume (finding 1); `--no-watch` is the control |
| `repro_harness_pin.py` | `targets.select` with a pinned harness file against a repo descriptor (finding 2) |
| `winstop_os.py` | Windows: Ctrl-Break ends a Python child at once and misses a grandchild in its own group (finding 3, no Orion) |
| `repro_winstop_jobs.py` | Windows: the same through `orion.ui.jobs.Jobs` start + Stop (finding 3) |
| `repro_resume_env.py` | the child env of a UI Resume when the UI inherited `ORION_VERIFY_*=1` (finding 4) |
| `repro_endpoint_env.py` | which route and header variables each access mode passes on to a scan (finding 5, R2-2) |
| `repro_route_vars_real_claude.py` | real CLI, POSIX: where a scan really goes with each of those variables set (finding 5, R2-2) |
| `repro_stop_real_claude.py [--watch] [--after-transient] [--log-events=N] [RUNS]` | real CLI, POSIX: real SIGINT to a real scan (R2-1) |
| `claude_interrupt_exit.py [RUNS] [CLAUDE_BINARY]` | real CLI: how `claude -p` ends when interrupted (R2-1). On Windows name the real `claude.exe`: npm's `.cmd` shim hides its exit code |

The real-CLI scripts ran in WSL with `~/orion/.venv/bin/python` from `/tmp`, on this checkout's code through `/mnt/c`.
