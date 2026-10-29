# Orion local UI + bring-your-own key — design (merge plan Phase 7)

Status: **BUILT and PROVED 2026-10-29** (approved, built and run the same day). Decisions are in §9; what
the build changed or added is in §10; the proof's results are at the end of §8.

## 1. Goal

Run Orion from a browser on your own machine instead of the terminal: pick a repo, start a scan with the
usual options, watch it live, stop it, and read the report. Optionally use your own model API key in place
of a Claude login. Added by Love Kush 2026-10-28.

## 2. What it is (in one picture)

```
browser (Windows)  --http://127.0.0.1:8770-->  orion ui  (in WSL, next to Neo4j + Joern)
                                                  |  spawns, one at a time
                                                  v
                                       python -m orion scan <repo> <flags>   (the normal CLI)
                                                  |  writes as it runs
                                                  v
                                       .orion/runs/<scan_id>/<ts>/
                                         progress.jsonl  leads.json  verdicts.jsonl
                                         verdicts.json   report.txt  results.sarif
```

- `orion ui` is a NEW SUBCOMMAND of the same CLI. It holds no scanning logic. Every scan is the ordinary
  `orion scan` run as a child process, so a scan started from the browser is byte-for-byte a terminal scan
  (same flags, same run dir, same `--resume`).
- The page reads only what the CLI already writes: `progress.jsonl` (live log), `leads.json`,
  `verdicts.jsonl` (verdicts as they land), then `verdicts.json` / `report.txt` / `results.sarif`.
- Checked 2026-10-29: a server bound to 127.0.0.1 inside WSL is reachable from Windows at
  `http://127.0.0.1:<port>` (WSL localhost forwarding; HTTP 200 both ways). So the UI runs in WSL, where
  Neo4j and Joern are, and opens in a Windows browser. It also runs on the Mac unchanged.

## 3. Screens

| Screen | What it shows | What you can do |
|---|---|---|
| **Runs** (home) | Every run dir: repo, engine, model, started, state (running / done / stopped-resumable / failed), CONFIRM / INCONCLUSIVE / REJECT / ERROR counts | Open a run; Resume a stopped one; pick two runs of one repo to Compare; New scan |
| **New scan** | A form: repo path (checked to exist), engine (Claude / Orion-LM), model, and the scan options as checkboxes (below) | Start |
| **Live run** | Phase strip (build graph -> index -> discover A/B/C/D -> verify n of m -> report), the event log as it streams, elapsed time, tokens and the CLI's own reported cost so far (from the `usage` events the run already logs) | Stop (asks the scan to stop; a second press ends it at once); Resume later |
| **Report** | The verdict table: decision, severity, CWE, file:line, function, route; filter by decision; each row expands to reason + evidence + a read-only code view of the lines it cites | Download SARIF / report.txt / verdicts.json |
| **Compare** | Two runs of the same repo side by side: verdicts that appeared, vanished, or changed decision, matched by the dedup fingerprint (`orion/dedup.py`: file, CWE, function or line) | Open either run |
| **Settings** | Model access: Anthropic API key, or a compatible endpoint (base URL + token + model); where the key is kept (keychain / memory); default model | Set / forget the key |

Scan options offered (each maps to exactly one existing flag; anything else is not sendable):
`--engine`, model (`ORION_MODEL` for Claude, `--model` path for lm), `--language`, `--verify-routing`,
`--verify-context`, `--shortlists`, `--semantic-mode`, `--fail-on`, `--lm-temperature`, `--lm-verify`, and
the runtime group `--runtime` + `--runtime-sandbox` + `--runtime-driver` (shown with a red warning: it RUNS
the target repo's code; the sandbox rules are unchanged). Orion-LM is greyed out with the reason when MLX
is not importable (WSL / Windows).

## 4. Bring-your-own key

Two ways to give the Claude engine model access; both keep one model per scan (discovery and verifier).

- **Anthropic API key:** put in the scan child's environment as `ANTHROPIC_API_KEY`. `claude -p` then
  bills that API account instead of the Claude login; the page says so before you start.
- **Compatible endpoint** (marked experimental): base URL + token + model name, for a gateway that
  speaks the Anthropic API or a local Ollama. The child gets `ANTHROPIC_BASE_URL`, the token as
  `ANTHROPIC_AUTH_TOKEN`, `ORION_MODEL` = the model, and `ANTHROPIC_DEFAULT_{OPUS,SONNET,HAIKU}_MODEL`
  = the same model, so fp-check's nested sub-agents cannot escape to Anthropic (the open-weight study's
  wiring). Only http(s) URLs. The page warns that the repo's code is sent to that endpoint. Unproven:
  the study's Ollama path never passed its NodeGoat gate, so a scan through it is a trial, not a result.
- **Kept in the OS keychain** (`keyring`, service `orion-ui`): Windows Credential Manager, macOS Keychain,
  Linux Secret Service. Checked 2026-10-29: inside WSL `keyring` has NO backend (`keyring.backends.fail`),
  so there the key is held in memory only, and the Settings page says "not saved: no keychain on this
  machine". Never a plaintext-file fallback (a `keyrings.alt` plaintext backend is refused). "Forget key"
  deletes it from the keychain and memory. A key already in the server's environment at launch is used
  and shown as "from environment".
- **Never shown or logged:** never written to a run dir, log, SARIF, config file or git; never on a
  command line (argv is visible to `ps`), only in the child's env; the API returns only "set / ends in
  ...abcd"; every line streamed to the browser is scrubbed of it.
- **Entering it is yours:** you type the key into the Settings page yourself (Claude never types a key).
- Orion-LM needs no key (local model).

## 5. Safety

A local page that can start scans (and, with `--runtime`, run a repo's code) must not be drivable by
other websites open in the same browser. So:
- Bind **127.0.0.1 only**, never 0.0.0.0.
- A random **launch token** (printed in the URL `orion ui` opens) is required on every API call;
  the page keeps it for the tab. Other origins cannot read it.
- **Host header** must be `127.0.0.1:<port>` or `localhost:<port>` (stops DNS-rebinding); **Origin**
  must match on every state-changing request; no CORS headers at all.
- Scan options are a fixed allowlist mapped to flags; the repo path must be an existing directory. The
  code view only opens files under the run's own repo (resolved path, no `..` or symlink escape, size cap).
- **One scan at a time** (Neo4j heap + Joern + embeddings on a 16 GB box); a second Start is refused with
  the running scan's name.

## 6. Build

| Piece | Where | Notes |
|---|---|---|
| Server | `orion/ui/server.py` | stdlib `http.server` (ThreadingHTTPServer), JSON API + one Server-Sent-Events stream per run; no new dependency (§9, D-stack) |
| Runs | `orion/ui/runs.py` | list / read run dirs (pure; tested on synthetic dirs) |
| Jobs | `orion/ui/jobs.py` | build argv from the allowlist, spawn in a new process group, learn the run dir from the CLI's own `run log: <dir>/progress.jsonl` line, Stop = SIGINT to the group (CTRL_BREAK on Windows), a second Stop kills it; Resume = `orion scan --resume <run_dir>` |
| Keys | `orion/ui/keys.py` | keychain via `keyring` (plaintext/fail backends refused -> memory), the child env for each access mode, the scrubber |
| Compare | `orion/ui/runs.py` | diff two runs' verdicts by `orion/dedup.py`'s fingerprint |
| Page | `orion/ui/static/` | one HTML file + plain JS + CSS, no build step, no CDN (works offline) |
| CLI | `orion ui [--port 8770] [--open]` | prints the URL with the token |

Stopping a Claude-engine scan mid-verify leaves `leads.json` + `verdicts.jsonl`, so Resume
(`--resume <run_dir>`) re-verifies only what is missing: nothing new is needed for that.

## 7. Tests (token-free)

Argv allowlist (an unknown option is refused; nothing free-form reaches the command line); run listing
and state on synthetic run dirs; Host / Origin / token checks; the key never appears in argv, the run dir
or the streamed log (a stub scan that prints its env); code-view path escapes refused; Stop signals the
whole group (stub process); one-scan-at-a-time; the endpoint mode sets every model alias (no escape to
Anthropic) and refuses a non-http(s) URL; a plaintext keyring backend is refused; Compare on synthetic
runs (appeared / vanished / changed).

## 8. Proof

Open `orion ui` from WSL in the built-in browser and run one full NodeGoat scan from it end to end: live
log, then the report, SARIF download, code view; then Stop + Resume on a second run, and Compare the two.
Approved 2026-10-29 to spend the tokens. It needs model access first: `claude` logged in inside WSL, or
your key typed into Settings by you.

Done 2026-10-29 in WSL through the built-in browser (claude login, sonnet, default options):

| Run | What happened |
|---|---|
| 1 | Live log, phase strip, tokens and cost as it ran. 21 leads -> 18 CONFIRM, 3 REJECT, 0 ERROR in about 4 minutes; $2.08 as reported by the CLI. Report, code view at file:line (`contributions.js` 32-34 highlighted) and the SARIF download (2.1.0, 18 results) checked. |
| 2 | Stopped from the page during verification. 11 s later no `orion scan`, `claude -p` or MCP server process was left, no session had been retried, the run lock was free and 5 verdicts were saved. Resume finished it: 23 leads -> 21 CONFIRM, 2 REJECT, 0 ERROR; $1.56 for both parts. |
| Compare | 11 unchanged, 11 appeared, 10 vanished, 0 changed. Several appeared/vanished pairs are the same bug named differently by the two runs' agents (a function name or a line one apart), which the exact fingerprint keeps apart; a looser second pass (same file and CWE, overlapping lines) is a follow-up. |

Two page fixes came out of the proof: the live view names the repo and model before discovery writes
leads.json, and "Verified n of m" counts saved verdicts (a session the Stop killed logs a verdict event
that is never saved).

## 9. Decisions (Love Kush, 2026-10-29)

| | Decision |
|---|---|
| Key scope | Anthropic key AND a compatible endpoint (base URL + token + model), the endpoint marked experimental |
| Key storage | OS keychain; memory only where there is none (WSL today), never plaintext |
| v1 extras | All four: Resume, code view at file:line, runtime options (behind a warning), Compare two runs |
| Proof | A full NodeGoat scan from the UI is approved (spends tokens) |
| Stack | stdlib server + plain JS, no new dependency (my call; Starlette + uvicorn, already installed through fastmcp, would also work) |

The go-ahead to build was given 2026-10-29.

## 10. As built (2026-10-29)

Built to this design, then reviewed through five lenses (security, lifecycle, API contract, portability,
tests); every finding was checked against the code, and these are the confirmed ones and how they were fixed.

| Found | Fixed |
|---|---|
| Stop's SIGINT also kills the running `claude -p` sessions, and `run_agent` retried each one as a transient failure on a fresh session: the first Stop restarted them and the scan went on (Ctrl-C in a terminal too) | `claude_cli.interrupt()`: once discovery's or verify's fan-out is cancelled, no session is started or retried (all scans) |
| The 60 s "may still be running in a terminal" guard was shorter than one verifier call (600 s), so a live terminal scan could be resumed twice | `orion/runlock.py`: a scan holds an OS lock on `<run_dir>/.lock`; `--resume` refuses a held dir; the UI shows it running "outside this UI" |
| Resume dropped the scan's verify flags, and after leaving endpoint mode it sent the endpoint's model name to Anthropic | `leads.json` records `model` + `settings` (verify options, `--fail-on`, whether an endpoint was used; not its URL); the UI resumes with exactly those and refuses a change of route |
| A run's model was never shown | the same record; the job carries its model for the live view |
| An endpoint with no token let claude fall back to its own login and send that token to the endpoint | the placeholder token `none` is always sent; the key and endpoint modes drop `CLAUDE_CODE_OAUTH_TOKEN` |
| The scanned repo's `.orion/runtime.json` overrode the driver the page asked for, so "harness + docker" could still run the repo's own commands on the host | an explicit driver wins (a `warn` event); the page's warning says which choices run code on the host |
| The key reached the target's own processes when the runtime stage ran them on the host | `runtime.base.target_env` strips `ANTHROPIC_*` / `CLAUDE_CODE_OAUTH_TOKEN` (defence in depth: host-run code runs as you) |
| `orion ui --open` put the launch token on the browser's command line (readable by every local user), and a console browser blocked startup | the browser gets a private 0600 redirect page, opened off the main thread |
| Started with SIGINT ignored (a background job of a script), the UI passed that on and Stop's first press did nothing | a do-nothing SIGINT handler, which exec resets to the default for each scan |
| Windows force stop left the scan's `claude.exe` children running; npm's `claude.cmd` was reported usable though scans run `claude.exe`; in WSL a Windows `claude` was reported usable | `taskkill /T`; the engine check looks for what a scan actually runs |
| Port 80 refused every request; a `~user` path for an unknown user was a 500; Safari before 16.4 could not parse the page; Resume stayed greyed after a scan ended; the runtime warning showed twice; Settings had no default model | fixed; the default model is kept in the browser (localStorage), the only thing stored there |
