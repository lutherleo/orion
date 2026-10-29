/* Orion local UI: the page behind `orion ui` (merge plan Phase 7). Plain JS, no framework, no build step,
   nothing loaded from anywhere but the local server.

   Why it is built this way:
   - The scanned repo is untrusted, and almost every string the server sends comes from it: file names,
     code, lead text, verifier evidence, log lines, even the repo path. All of it reaches the page as text
     nodes through h() below (createTextNode, or textContent for whole blocks); no API that parses HTML is
     used anywhere. h() also refuses attribute names that start with "on" and the style attribute, and an
     href can only point back into this page or this server, so no server string can become markup, a
     handler or a navigation target.
   - The server's Content-Security-Policy allows only same-origin script and style with nothing inline, so
     index.html holds no script body, no style attribute and no handler: every listener is attached here
     and every visual state is a class name or the hidden attribute (no style writes at all).
   - The launch token arrives once in the URL (?t=...). It moves to sessionStorage (this tab only, never
     storage that outlives the tab) and leaves the address bar at once; every fetch sends it as X-Orion-Token. EventSource
     and download links cannot set headers, so those two routes carry it as ?t=, which the server accepts
     there only.
   - A scan can run for an hour and log thousands of events, so the live view folds each event into a
     running tally (phase states, verify progress, tokens and cost) instead of re-reading the log, batches
     its DOM writes, and keeps only the last 1000 log lines on screen.
   - The pure helpers (event formatting, usage sums, phase states, verify progress, locations, the options
     payload, routes, the access wording) touch no DOM. The footer exports them so tests/test_ui_static.py
     runs them under node, including parity checks against orion/monitor.py and orion/contracts.py.
*/
(function () {
  "use strict";

  // ── Shared vocabulary: the server's JSON (orion/ui/*.py) and the CLI's run-dir files ────────────────
  const DECISIONS = ["CONFIRM", "INCONCLUSIVE", "REJECT", "ERROR"];
  // report._ORDER: CONFIRM first (the payload), then INCONCLUSIVE (needs a look), REJECT, ERROR.
  const DECISION_RANK = { CONFIRM: 0, INCONCLUSIVE: 1, REJECT: 2, ERROR: 3 };
  const PHASES = ["build", "runtime", "discover", "verify", "report"];
  const PHASE_LABELS = { build: "Build", runtime: "Runtime", discover: "Discover", verify: "Verify", report: "Report" };
  const RUN_STATES = { running: "Running", stopping: "Stopping", done: "Done", stopped: "Stopped", incomplete: "Incomplete" };
  const RUN_STATE_HELP = {
    running: "The scan is running, in this orion ui or in another process (a terminal) that holds the run folder.",
    stopping: "Asked to stop: the scan is finishing its current step.",
    done: "Finished: the verdicts, report and SARIF are saved.",
    stopped: "Stopped after discovery: its leads and the verdicts so far are saved.",
    incomplete: "Stopped or crashed before discovery finished.",
  };
  const ACTIVE = ["running", "stopping"];
  const DOWNLOADS = ["report.txt", "results.sarif", "verdicts.json", "leads.json", "progress.jsonl"];
  const DOWNLOAD_LABELS = {
    "report.txt": "Report", "results.sarif": "SARIF", "verdicts.json": "Verdicts",
    "leads.json": "Leads", "progress.jsonl": "Event log",
  };
  const MODES = ["login", "anthropic_key", "endpoint"];
  const MODE_LABELS = { login: "Claude login", anthropic_key: "Anthropic API key", endpoint: "Compatible endpoint (experimental)" };
  const MODE_HELP = {
    login: "The claude CLI's own account on this machine. Nothing to enter.",
    anthropic_key: "Billed to your Anthropic API account instead of the Claude login.",
    endpoint: "A gateway that speaks the Anthropic API, or a local Ollama. Your code is sent there.",
  };
  const USAGE_FIELDS = ["input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"];
  const LEADS_RE = /^(\d+) candidate leads?\b/;   // discover/done: "12 candidate leads[ from 9 flows]"
  const LOG_KINDS = ["start", "done", "error", "warn", "verdict", "usage", "lead", "tool", "query", "timing"];
  const SEVERITIES = ["CRITICAL", "HIGH", "MEDIUM", "LOW"];
  const RUNTIME_WARNING = "Runs the target repo's own code. Only the harness driver with Sandbox docker keeps it in a " +
    "container: the http and process drivers, Sandbox host, and auto without Docker run it on this machine as you, " +
    "with your files and logins in reach. With Driver auto the repo itself picks the driver (its .orion/runtime.json " +
    "or npm start).";
  const DEFAULT_MODEL_KEY = "orion-default-model";   // Settings' default model for new Claude scans (this browser)
  const CUSTOM_MODEL = " custom";   // the select's "Custom..." value: a leading space, so no real model name collides
  const TOKEN_KEY = "orion-token";
  const MAX_LOG_LINES = 1000;
  const MAX_OUTPUT_LINES = 2000;    // the server keeps the same ring of job output
  const EVIDENCE_PREVIEW = 4000;
  const LOG_LINE_MAX = 4000;
  const EVENT_PAGES_MAX = 32;       // a report reads at most ~32 MB of progress.jsonl (1 MB pages)
  const DOT = " · ";
  const DASH = "–";
  const ELLIPSIS = "…";
  const enc = encodeURIComponent;

  // ── Pure helpers (no DOM; exported for the node tests) ───────────────────────────────────────────
  function present(v) { return v !== null && v !== undefined; }
  function isNum(v) { return typeof v === "number" && Number.isFinite(v); }
  function pad2(n) { return (n < 10 ? "0" : "") + n; }

  function fmtInt(n) {
    if (!isNum(n)) return "0";
    const s = String(Math.round(Math.abs(n))).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
    return n < 0 ? "-" + s : s;
  }

  function fmtElapsed(ms) {
    const s = Math.floor((isNum(ms) && ms > 0 ? ms : 0) / 1000);
    const hours = Math.floor(s / 3600), mins = Math.floor((s % 3600) / 60), secs = s % 60;
    if (hours) return hours + "h " + pad2(mins) + "m";
    if (mins) return mins + "m " + pad2(secs) + "s";
    return secs + "s";
  }

  function parseTime(iso) {
    if (typeof iso !== "string" || !iso) return null;
    const t = Date.parse(iso);
    return Number.isFinite(t) ? t : null;
  }

  // Local "YYYY-MM-DD HH:MM" (run timestamps are UTC on disk; people read their own clock).
  function fmtTime(iso) {
    const t = parseTime(iso);
    if (t === null) return typeof iso === "string" ? iso : "";
    const d = new Date(t);
    return d.getFullYear() + "-" + pad2(d.getMonth() + 1) + "-" + pad2(d.getDate()) + " " +
      pad2(d.getHours()) + ":" + pad2(d.getMinutes());
  }

  function fmtClock(iso) {
    const t = parseTime(iso);
    if (t === null) return "";
    const d = new Date(t);
    return pad2(d.getHours()) + ":" + pad2(d.getMinutes()) + ":" + pad2(d.getSeconds());
  }

  function basename(p) {
    if (typeof p !== "string" || !p) return "";
    const parts = p.split(/[\\/]+/).filter(Boolean);
    return parts.length ? parts[parts.length - 1] : p;
  }

  function sentence(s) {
    const t = String(present(s) ? s : "").trim();
    return !t || /[.?!:]$/.test(t) ? t : t + ".";
  }

  // jobs.MODEL_RE plus its "not starting with -" rule: what the server accepts as a Claude model name.
  function validModelName(m) {
    return typeof m === "string" && /^[A-Za-z0-9._:/@\[\]-]{1,120}$/.test(m) && m.charAt(0) !== "-";
  }

  // One `usage` event (claude_cli._emit_usage): its detail is a JSON string. A field the CLI did not report
  // is null and counts for nothing; never read as 0 spent.
  function usageOf(evt) {
    if (!evt || typeof evt !== "object" || evt.event !== "usage") return null;
    let d = evt.detail;
    if (typeof d === "string") {
      try { d = JSON.parse(d); } catch (e) { return null; }
    }
    if (!d || typeof d !== "object" || Array.isArray(d)) return null;
    let tokens = null;
    for (const k of USAGE_FIELDS) if (isNum(d[k])) tokens = (tokens || 0) + d[k];
    return { tokens: tokens, cost: isNum(d.total_cost_usd) ? d.total_cost_usd : null };
  }

  function newUsage() { return { calls: 0, tokens: 0, tokenCalls: 0, cost: 0, costCalls: 0 }; }

  function addUsage(sum, evt) {
    const u = usageOf(evt);
    if (!u) return false;
    sum.calls += 1;
    if (u.tokens !== null) { sum.tokens += u.tokens; sum.tokenCalls += 1; }
    if (u.cost !== null) { sum.cost += u.cost; sum.costCalls += 1; }
    return true;
  }

  function sumUsage(events) {
    const sum = newUsage();
    for (const e of events || []) addUsage(sum, e);
    return sum;
  }

  function tokensText(sum) { return sum && sum.tokenCalls ? fmtInt(sum.tokens) + " tokens" : "not reported"; }

  function costText(sum) {
    return sum && sum.costCalls ? "$" + sum.cost.toFixed(2) + " reported by the CLI" : "not reported";
  }

  // The terminal's line for an event (orion/monitor._format_event), so a run reads the same in both places;
  // usage events are summarised as "[usage] N tokens" instead of their raw JSON.
  function formatEvent(evt) {
    if (!evt || typeof evt !== "object") return String(evt);
    if (evt.event === "usage") {
      const u = usageOf(evt);
      return "[usage] " + (u && u.tokens !== null ? fmtInt(u.tokens) + " tokens" : "tokens not reported");
    }
    const bits = ["[" + (present(evt.phase) ? evt.phase : "?") + "/" + (present(evt.event) ? evt.event : "?") + "]"];
    if (evt.shape) bits.push("shape=" + evt.shape);
    if (present(evt.lead)) bits.push("lead=" + evt.lead);
    if (present(evt.turn)) bits.push("turn=" + evt.turn);
    if (evt.detail) bits.push("- " + evt.detail);
    return bits.join(" ");
  }

  // The running tally the live view folds every progress event into.
  function newTally() {
    const phases = {};
    for (const p of PHASES) phases[p] = { seen: false, errors: 0, lastEvent: null, lastScoped: false };
    return {
      phases: phases, latest: -1, total: null, verdictKeys: Object.create(null), verdicts: 0,
      usage: newUsage(), firstTs: null, lastTs: null, events: 0,
    };
  }

  function tallyEvent(t, evt) {
    if (!evt || typeof evt !== "object") return t;
    t.events += 1;
    if (typeof evt.ts === "string" && evt.ts) {
      if (t.firstTs === null) t.firstTs = evt.ts;
      t.lastTs = evt.ts;
    }
    if (evt.event === "usage") { addUsage(t.usage, evt); return t; }
    const i = PHASES.indexOf(evt.phase);
    if (i < 0) return t;   // phase-less lines (the verifier's raw tool calls) move no phase
    const p = t.phases[evt.phase];
    p.seen = true;
    p.lastEvent = evt.event;
    // A shape's or a lead's own done (discover/done shape=A) is not the whole phase's done.
    p.lastScoped = !!evt.shape || present(evt.lead);
    if (evt.event === "error") p.errors += 1;
    if (i > t.latest) t.latest = i;
    if (evt.phase === "discover" && evt.event === "done" && typeof evt.detail === "string") {
      const m = LEADS_RE.exec(evt.detail);
      if (m) t.total = Number(m[1]);
    }
    if (evt.event === "verdict") {
      // Distinct leads: a resumed run retries its ERROR leads, and one lead must not count twice.
      const key = present(evt.lead) ? "lead " + evt.lead : "event " + t.events;
      if (!t.verdictKeys[key]) { t.verdictKeys[key] = true; t.verdicts += 1; }
    }
    return t;
  }

  // pending / active / done / error per phase. Phases run in order, so a phase is done once a later one has
  // started; the furthest phase is done only on its own unscoped "done". Any error event marks its phase.
  function phaseList(t) {
    const out = [];
    PHASES.forEach(function (name, i) {
      const p = t.phases[name];
      if (name === "runtime" && !p.seen) return;   // shown only once a runtime event is seen
      let state;
      if (p.errors > 0) state = "error";
      else if (i < t.latest) state = "done";
      else if (!p.seen) state = "pending";
      else state = p.lastEvent === "done" && !p.lastScoped ? "done" : "active";
      out.push({ name: name, label: PHASE_LABELS[name], state: state, errors: p.errors, current: p.seen && i === t.latest });
    });
    return out;
  }

  function phaseStates(events) {
    const t = newTally();
    for (const e of events || []) tallyEvent(t, e);
    return phaseList(t);
  }

  function phaseStateText(p, running) {
    if (p.state === "pending") return "Waiting";
    if (p.state === "active") return "Running";
    if (p.state === "done") return "Done";
    const n = p.errors === 1 ? "1 error" : fmtInt(p.errors) + " errors";
    return p.current && running ? "Running, " + n : n;
  }

  // Verify progress "n of m": n = distinct leads with a verdict event, m = the discover/done count
  // ("N candidate leads"), else the run's n_leads.
  function verifyCounts(t, fallbackTotal) {
    const total = t.total !== null ? t.total : (isNum(fallbackTotal) ? fallbackTotal : null);
    return { done: t.verdicts, total: total };
  }

  function verifyProgress(events, fallbackTotal) {
    const t = newTally();
    for (const e of events || []) tallyEvent(t, e);
    return verifyCounts(t, fallbackTotal);
  }

  function progressText(vp) {
    return vp.total === null ? fmtInt(vp.done) + " verified" : fmtInt(vp.done) + " of " + fmtInt(vp.total);
  }

  // contracts.Verdict.location(): the verifier's file/lines/cwe win; a corrected file resets the lead's
  // line range and function.
  function effectiveLocation(v) {
    const verdict = v || {};
    const lead = verdict.lead || {};
    const moved = !!(verdict.file && verdict.file !== lead.file);
    return {
      file: verdict.file || lead.file || null,
      line_start: verdict.line_start || (moved ? null : lead.line_start || null),
      line_end: verdict.line_end || (moved ? null : lead.line_end || null),
      function: moved ? null : lead.function || null,
      cwe: verdict.cwe || lead.cwe || null,
    };
  }

  function whereText(loc) {
    if (!loc || !loc.file) return "";
    if (!loc.line_start) return String(loc.file);
    const end = loc.line_end && loc.line_end !== loc.line_start ? "-" + loc.line_end : "";
    return loc.file + ":" + loc.line_start + end;
  }

  // report.render's order: decision, then the higher-centrality sink, then the lead index (stable).
  function sortVerdicts(list) {
    const rank = function (d) { return Object.prototype.hasOwnProperty.call(DECISION_RANK, d) ? DECISION_RANK[d] : 9; };
    const num = function (x) { return isNum(x) ? x : 0; };
    return (Array.isArray(list) ? list : []).slice().sort(function (a, b) {
      const ra = rank(a && a.decision), rb = rank(b && b.decision);
      if (ra !== rb) return ra - rb;
      const ca = num(a && a.sink_centrality), cb = num(b && b.sink_centrality);
      if (ca !== cb) return cb - ca;
      return num(a && a.lead && a.lead.index) - num(b && b.lead && b.lead.index);
    });
  }

  function countDecisions(verdicts) {
    const c = { CONFIRM: 0, INCONCLUSIVE: 0, REJECT: 0, ERROR: 0 };
    for (const v of verdicts || []) {
      if (v && Object.prototype.hasOwnProperty.call(c, v.decision)) c[v.decision] += 1;
    }
    return c;
  }

  function runHref(id) { return "#/run/" + enc(id); }
  function compareHref(a, b) { return "#/compare/" + enc(a) + "/" + enc(b); }

  function parseRoute(hash) {
    const nf = { name: "notfound", params: {} };
    let raw = typeof hash === "string" ? hash : "";
    if (raw.charAt(0) === "#") raw = raw.slice(1);
    raw = raw.replace(/\/+$/, "");
    if (raw === "") return { name: "runs", params: {} };
    if (raw.charAt(0) !== "/") return nf;
    let parts;
    try { parts = raw.slice(1).split("/").map(decodeURIComponent); } catch (e) { return nf; }
    if (parts.some(function (s) { return s === ""; })) return nf;
    const head = parts[0];
    if (parts.length === 1 && head === "runs") return { name: "runs", params: {} };
    if (parts.length === 1 && (head === "new" || head === "job" || head === "settings")) return { name: head, params: {} };
    if (head === "run" && parts.length === 2) return { name: "run", params: { id: parts[1] } };
    if (head === "compare" && parts.length === 3) return { name: "compare", params: { a: parts[1], b: parts[2] } };
    return nf;
  }

  // Does option `name` of the server's spec (jobs.OPTIONS) apply to `engine`? A sub-option with no list of
  // its own follows its parent (the runtime group); an option with neither is offered to every engine.
  function appliesTo(spec, name, engine, depth) {
    const opt = spec && Object.prototype.hasOwnProperty.call(spec, name) ? spec[name] : null;
    if (!opt || typeof opt !== "object" || (depth || 0) > 4) return false;
    if (Array.isArray(opt.engines)) return opt.engines.indexOf(engine) >= 0;
    if (opt.requires && opt.requires !== name) return appliesTo(spec, opt.requires, engine, (depth || 0) + 1);
    return true;
  }

  function choiceDefault(opt) {
    const choices = Array.isArray(opt.choices) ? opt.choices : [];
    if (present(opt.default) && choices.indexOf(opt.default) >= 0) return opt.default;
    return choices.length ? choices[0] : null;
  }

  function rangeText(opt) {
    if (isNum(opt.min) && isNum(opt.max)) return "Enter a value from " + opt.min + " to " + opt.max + ".";
    return isNum(opt.min) ? "Enter " + opt.min + " or more." : "Enter " + opt.max + " or less.";
  }

  function patternOk(pattern, s) {
    try { return new RegExp(pattern).test(s); } catch (e) { return true; }   // the server checks it anyway
  }

  // The request's "options" object: only the current engine's options, a sub-option only while its parent
  // is ticked, and nothing left at its default (an untouched form sends {}), so the scan's argv stays the
  // plain `orion scan` one. Numbers go as JSON numbers, ticks as real booleans; bad input comes back in
  // `errors` (field name -> message) and is never sent.
  function collectOptions(spec, engine, values) {
    const options = {}, errors = {};
    spec = spec || {};
    values = values || {};
    for (const name of Object.keys(spec)) {
      const opt = spec[name];
      if (!appliesTo(spec, name, engine)) continue;
      if (opt.requires && (!appliesTo(spec, opt.requires, engine) || values[opt.requires] !== true)) continue;
      const v = values[name];
      if (opt.kind === "bool") {
        if (v === true) options[name] = true;
      } else if (opt.kind === "choice") {
        const choices = Array.isArray(opt.choices) ? opt.choices : [];
        if (typeof v === "string" && v !== choiceDefault(opt)) {
          if (choices.indexOf(v) >= 0) options[name] = v;
          else errors[name] = "Pick one of the listed values.";
        }
      } else if (opt.kind === "text") {
        const s = typeof v === "string" ? v.trim() : "";
        if (s) {
          if (opt.pattern && !patternOk(opt.pattern, s)) errors[name] = "This value is not accepted here.";
          else options[name] = s;
        }
      } else if (opt.kind === "int" || opt.kind === "float") {
        const s = present(v) ? String(v).trim() : "";
        if (s) {
          const n = Number(s);
          if (!Number.isFinite(n)) errors[name] = "Enter a number.";
          else if (opt.kind === "int" && !Number.isInteger(n)) errors[name] = "Enter a whole number.";
          else if ((isNum(opt.min) && n < opt.min) || (isNum(opt.max) && n > opt.max)) errors[name] = rangeText(opt);
          else options[name] = n;
        }
      }
    }
    return { options: options, errors: errors };
  }

  function modeLabel(mode) { return MODE_LABELS[mode] || MODE_LABELS.login; }

  function engineLabel(e) { return e === "lm" ? "Orion-LM" : "Claude"; }

  function hintText(isSet, hint, unset) {
    if (hint) return "ends in " + hint;
    return isSet ? "set (too short to show its last characters)" : (unset || "not set");
  }

  // The header's one-line summary of the model access the next scan uses.
  function accessSummary(a) {
    if (!a || typeof a !== "object") return "";
    if (a.mode === "anthropic_key") return "API key " + (a.key_hint || "set") + (a.source === "env" ? " (from environment)" : "");
    if (a.mode === "endpoint") return "Endpoint " + ((a.endpoint && a.endpoint.model) || "") + " (experimental)";
    return "Claude login";
  }

  // The New scan form's line, said before Start: who pays, and where the code goes.
  function accessLine(a) {
    if (!a || a.mode === "login" || MODES.indexOf(a.mode) < 0) return "Claude login: the claude CLI's own account";
    if (a.mode === "anthropic_key") return "Anthropic key " + (a.key_hint || "(set)") + ", billed to your API account";
    const ep = a.endpoint || {};
    return "Endpoint " + (ep.base_url || "(not set)") + ", model " + (ep.model || "(not set)") +
      " — experimental; your code is sent there";
  }

  // Where the current access is kept (keys.Access.public(): persisted + keychain).
  function keptText(a) {
    const kc = (a && a.keychain) || {};
    const why = kc.reason ? " (" + kc.reason + ")" : "";
    if (!a) return "";
    if (a.source === "env") return "Read from the server's environment; not saved";
    if (a.mode === "login" && a.source !== "ui") return "Nothing to keep";
    if (a.persisted) return "Saved in the OS keychain (" + (kc.backend || "unknown backend") + ")";
    if (!kc.available) return "Not saved: no keychain on this machine; held until orion ui stops" + why;
    return "Not saved: held until orion ui stops" + why;
  }

  // Only same-page hashes and same-server paths; anything else (a scheme, //host, a backslash, whitespace)
  // becomes "#".
  function safeHref(v) {
    const s = String(v);
    if (/[\u0000- \u007f\\]/.test(s)) return "#";
    if (s.charAt(0) === "#") return s;
    if (s.charAt(0) === "/" && s.charAt(1) !== "/") return s;
    return "#";
  }

  // ── DOM building: every string becomes a text node ───────────────────────────────────────────────
  const PROPS = {
    value: "value", checked: "checked", disabled: "disabled", hidden: "hidden", selected: "selected",
    open: "open", for: "htmlFor", readonly: "readOnly", required: "required",
  };
  const URL_ATTRS = ["href", "src", "action", "formaction", "poster", "data", "xlink:href"];

  function h(tag, attrs) {
    const el = document.createElement(tag);
    if (attrs) {
      if (present(attrs.type)) el.setAttribute("type", String(attrs.type));   // before value: inputs sanitise by type
      for (const k of Object.keys(attrs)) {
        const v = attrs[k];
        if (k === "type" || v === null || v === undefined || v === false) continue;
        if (k === "on") {
          for (const ev of Object.keys(v)) el.addEventListener(ev, v[ev]);
        } else if (/^on/i.test(k) || /^(style|srcdoc)$/i.test(k)) {
          throw new Error("h(): the " + k + " attribute is not allowed");
        } else if (k === "class") {
          el.className = Array.isArray(v) ? v.filter(Boolean).join(" ") : String(v);
        } else if (URL_ATTRS.indexOf(k.toLowerCase()) >= 0) {
          el.setAttribute(k, safeHref(v));
        } else if (Object.prototype.hasOwnProperty.call(PROPS, k)) {
          el[PROPS[k]] = v;
        } else {
          el.setAttribute(k, v === true && k.indexOf("aria-") !== 0 ? "" : String(v));
        }
      }
    }
    for (let i = 2; i < arguments.length; i++) append(el, arguments[i]);
    return el;
  }

  function isNode(x) { return typeof x === "object" && x !== null && typeof x.nodeType === "number"; }

  function append(el, child) {
    if (child === null || child === undefined || child === false || child === true) return;
    if (Array.isArray(child)) { for (const c of child) append(el, c); return; }
    el.appendChild(isNode(child) ? child : document.createTextNode(String(child)));
  }

  function clear(el) {
    while (el.firstChild) el.removeChild(el.firstChild);
    return el;
  }

  let uidCounter = 0;
  function nextUid() { uidCounter += 1; return uidCounter; }
  function safeId(name) { return String(name).replace(/[^A-Za-z0-9_-]/g, "_"); }
  function byId(id) { return document.getElementById(id); }
  function parseJson(s) { try { return JSON.parse(s); } catch (e) { return null; } }
  function dash() { return h("span", { class: "muted" }, DASH); }
  function mono(text) { return h("span", { class: "mono" }, text); }

  // The pieces of a path, each separator kept at the end of its piece ("a/b/c" -> "a/", "b/", "c"). No
  // lookbehind regex: Safari before 16.4 cannot parse one, and a parse error would blank the whole page.
  function pathParts(p) {
    return String(present(p) ? p : "").match(/[^\\/]*[\\/]|[^\\/]+$/g) || [""];
  }

  // A path as text with a line-break chance after each separator, so it wraps at "/" rather than mid-name.
  function pathText(p) {
    const out = [];
    for (const part of pathParts(p)) {
      if (out.length) out.push(h("wbr"));
      out.push(part);
    }
    return out;
  }

  function monoPath(p, cls) { return h("span", { class: "mono" + (cls ? " " + cls : "") }, pathText(p)); }

  function setTitle(t) { document.title = t ? t + DOT + "Orion" : "Orion"; }

  function notice(kind) {
    return h("div", { class: "notice notice-" + kind }, h("p", null, Array.prototype.slice.call(arguments, 1)));
  }

  function pageHead(title, lede, actions, eyebrow) {
    return h("div", { class: "page-head" },
      h("div", { class: "page-title" },
        eyebrow ? h("p", { class: "eyebrow" }, eyebrow) : null,
        h("h1", { tabindex: "-1" }, title),
        lede ? h("p", { class: "lede" }, lede) : null),
      actions && actions.length ? h("div", { class: "actions" }, actions) : null);
  }

  function decisionBadge(d) {
    const known = DECISIONS.indexOf(d) >= 0;
    return h("span", { class: "badge d-" + (known ? d.toLowerCase() : "other") }, present(d) && d !== "" ? String(d) : "?");
  }

  function stateBadge(state) {
    const known = Object.prototype.hasOwnProperty.call(RUN_STATES, state);
    return h("span", { class: "badge s-" + (known ? state : "other"), title: known ? RUN_STATE_HELP[state] : null },
      known ? RUN_STATES[state] : String(state || "unknown"));
  }

  function countChips(counts) {
    const c = counts || {};
    return DECISIONS.map(function (d) {
      const n = isNum(c[d]) ? c[d] : 0;
      return h("span", { class: "count d-" + d.toLowerCase() + (n ? "" : " is-zero") }, fmtInt(n) + " " + d.toLowerCase());
    });
  }

  function runName(r) { return (r && (basename(r.repo) || r.scan_id || r.id)) || "run"; }

  function timeEl(iso) {
    return iso ? h("time", { datetime: iso, title: iso }, fmtTime(iso)) : h("span", { class: "muted" }, "unknown");
  }

  // The runs table's Started cell: the date over the time, so the column stays narrow.
  function startedCell(iso) {
    const text = fmtTime(iso);
    const m = /^(\S+) (\S+)$/.exec(text);
    if (!iso || !m) return timeEl(iso);
    return h("time", { datetime: iso, title: iso }, h("span", { class: "nowrap" }, m[1]), " ",
      h("span", { class: "nowrap sub-time" }, m[2]));
  }

  function setFieldError(id, message) {
    const input = byId(id), err = byId(id + "-error");
    if (input) input.setAttribute("aria-invalid", "true");
    if (err) {
      err.textContent = message;
      err.hidden = false;
      if (input) {
        const ids = (input.getAttribute("aria-describedby") || "").split(/\s+/).filter(Boolean);
        if (ids.indexOf(err.id) < 0) ids.push(err.id);
        input.setAttribute("aria-describedby", ids.join(" "));
      }
    }
  }

  function clearFieldErrors(root) {
    for (const el of root.querySelectorAll("[aria-invalid]")) el.removeAttribute("aria-invalid");
    for (const el of root.querySelectorAll(".field-error")) { el.textContent = ""; el.hidden = true; }
  }

  function fieldErrorEl(id) { return h("p", { class: "field-error", id: id + "-error", hidden: true }); }

  function pushLine(pre, text, cap) {
    pre.appendChild(document.createTextNode(text + "\n"));
    while (pre.childNodes.length > cap) pre.removeChild(pre.firstChild);
  }

  // GET /api/job hands back the ring's lines plus `next` (the absolute index after the last one), so the
  // first line's index is next - lines.length; only lines past `have` are new.
  function appendJobOutput(pre, lines, next, have) {
    if (!Array.isArray(lines) || !isNum(next)) return have;
    const first = next - lines.length;
    for (let i = Math.max(have, first); i < next; i++) pushLine(pre, String(lines[i - first]), MAX_OUTPUT_LINES);
    return Math.max(have, next);
  }

  function logLine(evt) {
    const kind = evt && LOG_KINDS.indexOf(evt.event) >= 0 ? evt.event : "other";
    let text = formatEvent(evt);
    if (text.length > LOG_LINE_MAX) text = text.slice(0, LOG_LINE_MAX) + " " + ELLIPSIS;
    // The gutter is always there (blank for the verifier's ts-less tool and usage lines), so lines align.
    const clock = evt && typeof evt === "object" ? fmtClock(evt.ts) : "";
    return h("div", { class: "ev ev-" + kind },
      h("span", { class: "ev-time", "aria-hidden": clock ? null : "true" }, clock),
      h("span", { class: "ev-text" }, text));
  }

  // ── App state, the API and the status line ────────────────────────────────────────────────────────
  const app = {
    token: null, tokenRefused: false, state: null, statePromise: null, offline: false,
    picks: new Set(), navs: 0, firstRoute: true,
    form: { repo: "", engine: "claude", model: null, custom: "", lmModel: "", values: {} },
  };
  const screen = { seq: 0, timers: [], intervals: [], sources: [], cleanups: [], stateHook: null, focus: false };
  const status = { keepUntil: 0, timer: null };

  function readStoredToken() {
    try { return window.sessionStorage.getItem(TOKEN_KEY); } catch (e) { return null; }
  }

  // Settings' default model: a per-browser convenience in localStorage (no secret; never sent anywhere until
  // a scan is started with it). Storage can be off or throw; then there is simply no default.
  function readDefaultModel() {
    let m = null;
    try { m = window.localStorage.getItem(DEFAULT_MODEL_KEY); } catch (e) { m = null; }
    return validModelName(m) ? m : null;
  }

  function storeDefaultModel(m) {
    try { window.localStorage.setItem(DEFAULT_MODEL_KEY, m); return true; } catch (e) { return false; }
  }

  function storeToken(t) {
    try { window.sessionStorage.setItem(TOKEN_KEY, t); } catch (e) { /* storage off: this load still has it */ }
  }

  function dropToken() {
    try { window.sessionStorage.removeItem(TOKEN_KEY); } catch (e) { /* nothing was stored */ }
  }

  function initToken() {
    let fromUrl = null;
    try { fromUrl = new URLSearchParams(location.search).get("t"); } catch (e) { fromUrl = null; }
    if (fromUrl) storeToken(fromUrl);
    if (location.search) history.replaceState(null, "", location.pathname + location.hash);
    return fromUrl || readStoredToken();
  }

  function apiError(status, message) {
    const e = new Error(message);
    e.status = status;
    return e;
  }

  async function api(method, path, body) {
    const init = {
      method: method, cache: "no-store",
      headers: { "X-Orion-Token": app.token || "", "Content-Type": "application/json" },
    };
    if (body !== undefined) init.body = JSON.stringify(body);
    let res;
    try {
      res = await fetch(path, init);
    } catch (e) {
      app.offline = true;
      renderHeader();
      throw apiError(0, "Cannot reach orion ui. Check that it is still running in your terminal.");
    }
    let data = null;
    try {
      const text = await res.text();
      data = text ? JSON.parse(text) : null;
    } catch (e) {
      data = null;
    }
    if (app.offline) { app.offline = false; renderHeader(); }
    if (!res.ok) {
      if (res.status === 401) tokenRefused();
      const msg = data && typeof data.error === "string" && data.error
        ? data.error
        : "The server answered " + res.status + (res.statusText ? " " + res.statusText : "") + ".";
      throw apiError(res.status, msg);
    }
    return data;
  }

  function tokenRefused() {
    if (!app.token) return;
    app.token = null;
    app.tokenRefused = true;
    dropToken();
    route();
  }

  function say(message, kind, keep) {
    const box = byId("status");
    if (!box) return;
    clear(box);
    clearTimeout(status.timer);
    status.keepUntil = app.navs + (keep ? 1 : 0);
    const isError = kind === "error";
    box.appendChild(h("div", { class: "msg " + (isError ? "msg-error" : "msg-ok") },
      h("p", null, message),
      h("button", { type: "button", class: "btn btn-small btn-quiet", on: { click: clearStatus } }, "Dismiss")));
    if (!isError) status.timer = setTimeout(clearStatus, 8000);
  }

  function clearStatus() {
    const box = byId("status");
    if (box) clear(box);
  }

  // ── Header: model access and the running scan ─────────────────────────────────────────────────────
  function jobActive() {
    const j = app.state && app.state.job;
    return !!(j && ACTIVE.indexOf(j.state) >= 0);
  }

  // A resume job has no repo of its own (jobs.Job.repo is None), so fall back to its scan id.
  function jobName(job) { return basename(job && job.repo) || (job && job.scan_id) || "a resumed run"; }

  function renderHeader() {
    const st = app.token ? app.state : null;
    const acc = byId("access-summary"), pill = byId("job-pill"), conn = byId("conn-pill");
    if (acc) {
      acc.textContent = st ? accessSummary(st.access) : "";
      acc.hidden = !st;
      acc.setAttribute("title", "Model access for the next scan. Change it in Settings.");
    }
    if (pill) {
      const job = st && st.job;
      if (job && ACTIVE.indexOf(job.state) >= 0) {
        pill.textContent = (job.state === "stopping" ? "Stopping: " : "Scanning: ") + jobName(job);
        pill.setAttribute("href", job.run_id ? runHref(job.run_id) : "#/job");
        pill.hidden = false;
      } else {
        pill.hidden = true;
      }
    }
    if (conn) conn.hidden = !app.offline;
  }

  async function refreshState() {
    const d = await api("GET", "/api/state");
    app.state = d;
    renderHeader();
    if (screen.stateHook) screen.stateHook(d);
    return d;
  }

  function ensureState() {
    if (app.state) return Promise.resolve(app.state);
    if (!app.statePromise) {
      app.statePromise = refreshState().finally(function () { app.statePromise = null; });
    }
    return app.statePromise;
  }

  function stateLoop() {
    setTimeout(async function () {
      if (app.token) {
        try { await refreshState(); } catch (e) { /* the header shows a lost connection */ }
      }
      stateLoop();
    }, jobActive() ? 3000 : 15000);
  }

  // ── Screen lifecycle and routing ──────────────────────────────────────────────────────────────────
  function leave() {
    screen.seq += 1;
    for (const id of screen.timers) clearTimeout(id);
    for (const id of screen.intervals) clearInterval(id);
    for (const es of screen.sources) { try { es.close(); } catch (e) { /* already closed */ } }
    for (const fn of screen.cleanups) { try { fn(); } catch (e) { /* best effort */ } }
    screen.timers = [];
    screen.intervals = [];
    screen.sources = [];
    screen.cleanups = [];
    screen.stateHook = null;
  }

  function later(fn, ms) {
    const id = setTimeout(fn, ms);
    screen.timers.push(id);
    return id;
  }

  function isLive(seq) { return seq === screen.seq; }

  // A screen's finished view; after a navigation it takes focus to its heading for keyboard and
  // screen-reader users. A loading view never takes it.
  function show() {
    const view = byId("view");
    clear(view);
    for (let i = 0; i < arguments.length; i++) append(view, arguments[i]);
    if (screen.focus) {
      screen.focus = false;
      const h1 = view.querySelector("h1");
      if (h1) h1.focus();
    }
  }

  function showLoading(title, text) {
    const view = byId("view");
    clear(view);
    view.appendChild(h("section", { "aria-busy": "true" }, pageHead(title, null), h("p", { class: "muted" }, text)));
  }

  function errorView(title, err) {
    return h("section", null, pageHead(title, null),
      notice("danger", err && err.message ? err.message : String(err)),
      h("p", null, h("button", { type: "button", class: "btn", on: { click: function () { route(); } } }, "Try again")));
  }

  function focusStaysInView() {
    const a = document.activeElement;
    const view = byId("view");
    if (!a || a === document.body || (view && view.contains(a))) screen.focus = true;
  }

  function markNav(name) {
    const current = name === "run" || name === "compare" ? "runs" : name;
    for (const a of document.querySelectorAll("[data-nav]")) {
      if (a.getAttribute("data-nav") === current) a.setAttribute("aria-current", "page");
      else a.removeAttribute("aria-current");
    }
  }

  // A replace (the job page handing over to its run) is the same step for the person, so a message said
  // just before it survives it.
  function navigate(hash, replace) {
    if (replace) { history.replaceState(null, "", hash); app.replacing = true; route(); }
    else if (location.hash === hash) route();
    else location.hash = hash;
  }

  function route() {
    leave();
    if (app.replacing) app.replacing = false;
    else app.navs += 1;
    if (app.navs > status.keepUntil) clearStatus();
    screen.focus = !app.firstRoute;
    app.firstRoute = false;
    if (!app.token) { markNav(null); renderHeader(); showNoToken(); return; }
    const r = parseRoute(location.hash);
    markNav(r.name);
    const screens = {
      runs: showRuns, new: showNewScan, job: showJob, run: showRun, compare: showCompare,
      settings: showSettings, notfound: showNotFound,
    };
    screens[r.name](r.params);
  }

  // ── Screens ───────────────────────────────────────────────────────────────────────────────────────
  function showNoToken() {
    setTitle(null);
    show(h("section", { class: "card narrow" },
      h("h1", { tabindex: "-1" }, "This page needs its launch link"),
      h("p", null, "Open the link that ", h("code", null, "orion ui"), " printed in your terminal."),
      h("p", { class: "muted" }, app.tokenRefused
        ? "The server refused this tab's token, so orion ui has probably restarted since the tab opened."
        : "The link carries the token for this launch of orion ui. Without it the page cannot talk to the scanner.")));
  }

  function showNotFound() {
    setTitle("Not found");
    show(h("section", null,
      pageHead("No page here", "This address does not match any page of orion ui."),
      h("p", null, h("a", { class: "btn", href: "#/" }, "Go to runs"))));
  }

  // Runs ─────────────────────────────────────────────────────────────────────────────────────────────
  async function showRuns() {
    const seq = screen.seq;
    setTitle("Runs");
    showLoading("Runs", "Reading the run folders" + ELLIPSIS);
    let data;
    try {
      data = (await Promise.all([api("GET", "/api/runs"), ensureState()]))[0];
    } catch (e) {
      if (isLive(seq)) show(errorView("Runs", e));
      return;
    }
    if (!isLive(seq)) return;
    const st = app.state || {};
    const body = h("div");
    let runs = (data && data.runs) || [];
    let last = JSON.stringify(runs);
    show(pageHead("Runs", st.runs_root ? ["Run folders under ", mono(st.runs_root)] : null,
      [h("a", { class: "btn btn-primary", href: "#/new" }, "New scan")]), body);
    renderRunsBody(body, runs, false, reload);
    // The Resume buttons follow the job: the list itself may already show the run stopped while the state
    // poll still had the scan running, and nothing in the list changes after that.
    let busyShown = jobActive();
    screen.stateHook = function () {
      if (!isLive(seq) || jobActive() === busyShown) return;
      busyShown = jobActive();
      renderRunsBody(body, runs, true, reload);
    };

    async function reload(manual) {
      try {
        const d = await api("GET", "/api/runs");
        if (!isLive(seq)) return;
        const fresh = (d && d.runs) || [];
        const json = JSON.stringify(fresh);
        if (json !== last || manual) { last = json; runs = fresh; renderRunsBody(body, runs, true, reload); }
        if (manual) say("Runs refreshed.");
      } catch (e) {
        if (manual && isLive(seq)) say(e.message, "error");
      }
    }
    // While a scan runs (or a listed run still shows as active) the list follows it every 3 s.
    async function tick() {
      if (!isLive(seq)) return;
      if (jobActive() || runs.some(function (r) { return ACTIVE.indexOf(r.state) >= 0; })) await reload(false);
      if (isLive(seq)) later(tick, 3000);
    }
    later(tick, 3000);
  }

  function renderRunsBody(body, runs, refresh, reload) {
    const a = document.activeElement;
    const focusKey = refresh && a && a.getAttribute ? a.getAttribute("data-key") : null;
    const ids = new Set(runs.map(function (r) { return r.id; }));
    for (const id of Array.from(app.picks)) if (!ids.has(id)) app.picks.delete(id);
    clear(body);
    if (!runs.length) {
      const st = app.state || {};
      body.appendChild(h("div", { class: "empty" },
        h("span", { class: "empty-mark", "aria-hidden": "true" }),
        h("h2", null, "Start your first scan"),
        h("p", null, "Scans started here, or with ", h("code", null, "orion scan"), " in ", mono(st.cwd || "this folder"),
          ", show up in this list."),
        h("a", { class: "btn btn-primary", href: "#/new" }, "New scan")));
      return;
    }
    const hint = h("span", { class: "help", id: "compare-hint" });
    const compareBtn = h("button", {
      type: "button", class: "btn", "aria-describedby": "compare-hint", "data-key": "compare",
      on: { click: function () { openCompare(runs); } },
    }, "Compare");
    function syncPicks() {
      const n = app.picks.size;
      compareBtn.disabled = n !== 2;
      hint.textContent = n === 2 ? "2 runs picked." : n === 0 ? "Pick two runs to compare them."
        : n === 1 ? "Pick one more run." : "Pick only two runs.";
    }
    const heads = ["Pick", "State", "Repo", "Started"].concat(DECISIONS.map(function (d) {
      return d.charAt(0) + d.slice(1).toLowerCase();
    }), ["Actions"]);
    body.appendChild(h("div", { class: "toolbar" }, compareBtn, hint,
      h("button", { type: "button", class: "btn btn-quiet", "data-key": "refresh", on: { click: function () { reload(true); } } }, "Refresh")));
    body.appendChild(h("div", { class: "table-wrap" },
      h("table", { class: "runs" },
        h("caption", { class: "sr-only" }, "Scan runs, newest first"),
        h("thead", null, h("tr", null, heads.map(function (t, i) {
          const numeric = i >= 4 && i < 4 + DECISIONS.length;
          return h("th", { scope: "col", class: numeric ? "num" : (i === 0 ? "c-pick" : null) },
            i === 0 ? h("span", { class: "sr-only" }, t) : t);
        }))),
        h("tbody", null, runs.map(function (r) { return runRow(r, syncPicks); })))));
    syncPicks();
    if (focusKey) {
      for (const el of body.querySelectorAll("[data-key]")) {
        if (el.getAttribute("data-key") === focusKey) { el.focus(); break; }
      }
    }
  }

  function runRow(r, onPick) {
    const counts = r.counts || {};
    const started = fmtTime(r.started) || "an unknown time";
    const pick = h("input", {
      type: "checkbox", checked: app.picks.has(r.id), "data-key": "pick " + r.id,
      "aria-label": "Pick the " + runName(r) + " run from " + started + " to compare",
      on: { change: function (e) { if (e.target.checked) app.picks.add(r.id); else app.picks.delete(r.id); onPick(); } },
    });
    const busy = jobActive();
    const resume = r.resumable ? h("button", {
      type: "button", class: "btn btn-small", "data-key": "resume " + r.id, disabled: busy,
      title: busy ? "A scan is running. Resume this one when it ends." : null,
      on: { click: function (e) { resumeRun(r.id, e.currentTarget); } },
    }, "Resume") : null;
    const progress = r.state !== "done" && isNum(r.n_leads)
      ? h("div", { class: "sub" }, fmtInt(r.n_verdicts || 0) + " of " + fmtInt(r.n_leads) + " verified") : null;
    const engine = engineLabel(r.engine) + (r.model ? DOT + (r.engine === "lm" ? basename(r.model) : r.model) : "");
    return h("tr", null,
      h("td", { class: "c-pick" }, pick),
      h("td", { class: "c-state" }, stateBadge(r.state), progress,
        r.elsewhere ? h("div", { class: "sub" }, "outside this UI") : null,
        r.problem ? h("div", { class: "sub warn" }, r.problem) : null),
      h("td", { class: "c-repo" },
        h("a", { class: "repo-name", href: runHref(r.id), title: r.repo || null, "data-key": "name " + r.id }, runName(r)),
        h("div", { class: "sub" }, engine),
        r.repo ? h("div", { class: "sub run-path" }, monoPath(r.repo)) : null),
      h("td", { class: "c-started" }, startedCell(r.started)),
      DECISIONS.map(function (d) {
        const n = isNum(counts[d]) ? counts[d] : 0;
        return h("td", { class: "num" + (n ? " n-" + d.toLowerCase() : " zero") }, fmtInt(n));
      }),
      h("td", { class: "c-actions" },
        h("a", { class: "btn btn-small", href: runHref(r.id), "data-key": "open " + r.id }, "Open"), resume));
  }

  function openCompare(runs) {
    const pair = runs.filter(function (r) { return app.picks.has(r.id); });
    if (pair.length !== 2) return;
    pair.sort(function (x, y) {
      return String(x.started || "").localeCompare(String(y.started || "")) || String(x.id).localeCompare(String(y.id));
    });
    navigate(compareHref(pair[0].id, pair[1].id));
  }

  async function resumeRun(id, btn) {
    if (btn) btn.disabled = true;
    try {
      const d = await api("POST", "/api/runs/" + enc(id) + "/resume");
      if (app.state && d && d.job) { app.state.job = d.job; renderHeader(); }
      say("Resuming the run: the leads without a verdict get verified now." +
        (d && typeof d.note === "string" && d.note ? " Note: " + sentence(d.note) : ""), "ok", true);
      refreshState().catch(function () { /* header only */ });
      navigate(d && d.job && d.job.run_id ? runHref(d.job.run_id) : "#/job");
    } catch (e) {
      say(e.message, "error");
      if (btn && btn.isConnected) btn.disabled = false;
    }
  }

  // New scan ─────────────────────────────────────────────────────────────────────────────────────────
  async function showNewScan() {
    const seq = screen.seq;
    setTitle("New scan");
    showLoading("New scan", "Reading the scan options" + ELLIPSIS);
    let st;
    try { st = await refreshState(); } catch (e) { if (isLive(seq)) show(errorView("New scan", e)); return; }
    if (isLive(seq)) renderNewScan(st, seq);
  }

  function renderNewScan(st, seq) {
    const f = app.form;
    const spec = (st && st.options) || {};
    const engines = (st && st.engines) || {};
    const lmOk = !!(engines.lm && engines.lm.available);
    if (f.engine === "lm" && !lmOk) f.engine = "claude";
    const models = Array.isArray(st.models) && st.models.length ? st.models.map(String) : ["sonnet"];
    if (f.model === null) {                   // a fresh form starts on Settings' default model, if one is saved
      const preferred = readDefaultModel();
      if (preferred && models.indexOf(preferred) >= 0) f.model = preferred;
      else if (preferred) { f.model = CUSTOM_MODEL; f.custom = preferred; }
    }
    if (f.model === null || (f.model !== CUSTOM_MODEL && models.indexOf(f.model) < 0)) f.model = models[0];
    let starting = false;
    let accessJson = null;

    const repoInput = h("input", {
      type: "text", id: "f-repo", value: f.repo, placeholder: "fixtures/NodeGoat", autocomplete: "off",
      spellcheck: "false", "aria-describedby": "f-repo-help",
      on: { input: function (e) { f.repo = e.target.value; } },
    });
    const busy = h("div", { class: "notice notice-warn", hidden: true });
    const modelBox = h("div", { class: "field" });
    const optionsBox = h("div", { class: "options" });
    const accessBox = h("div");
    const startBtn = h("button", { type: "submit", class: "btn btn-primary" }, "Start scan");

    function engineChoice(value, label, help, enabled, info) {
      const id = "f-engine-" + value;
      let note = null;
      if (!enabled) note = "Not available here" + (info && info.reason ? ": " + sentence(info.reason) : ".");
      else if (info && info.available === false) note = "Scans cannot run the claude CLI here" + (info.reason ? ": " + sentence(info.reason) : ".");
      return h("div", { class: "choice" + (enabled ? "" : " is-disabled") },
        h("input", {
          type: "radio", name: "engine", id: id, value: value, checked: f.engine === value, disabled: !enabled,
          "aria-describedby": id + "-help",
          on: { change: function () { f.engine = value; renderModel(); renderOptions(); renderAccess(true); } },
        }),
        h("label", { for: id }, label),
        h("p", { class: "help", id: id + "-help" }, help, note ? h("span", { class: "help-warn" }, note) : null));
    }

    function currentAccess() { return (app.state && app.state.access) || {}; }

    function renderModel() {
      clear(modelBox);
      if (f.engine === "lm") {
        append(modelBox, [
          h("label", { for: "f-lm-model" }, "Model folder"),
          h("input", {
            type: "text", id: "f-lm-model", value: f.lmModel, placeholder: "models/orion-lm", autocomplete: "off",
            spellcheck: "false", "aria-describedby": "f-lm-model-help",
            on: { input: function (e) { f.lmModel = e.target.value; } },
          }),
          h("p", { class: "help", id: "f-lm-model-help" },
            "The folder that holds the Orion-LM model. A relative path starts from the folder orion ui runs in."),
          fieldErrorEl("f-lm-model")]);
        return;
      }
      const access = currentAccess();
      if (access.mode === "endpoint") {
        const ep = access.endpoint || {};
        append(modelBox, [
          h("p", { class: "label" }, "Model"),
          h("p", { class: "fixed-value" }, mono(ep.model || "not set"), ", set by your endpoint in ",
            h("a", { href: "#/settings" }, "Settings"), ". Discovery and the verifier both use it.")]);
        return;
      }
      const customInput = h("input", {
        type: "text", id: "f-model-custom", value: f.custom, autocomplete: "off", spellcheck: "false",
        placeholder: "a model id the claude CLI accepts",
        on: { input: function (e) { f.custom = e.target.value; } },
      });
      const customWrap = h("div", { class: "sub-field", hidden: f.model !== CUSTOM_MODEL },
        h("label", { for: "f-model-custom" }, "Model name"), customInput, fieldErrorEl("f-model-custom"));
      const select = h("select", {
        id: "f-model", "aria-describedby": "f-model-help",
        on: {
          change: function (e) {
            f.model = e.target.value;
            customWrap.hidden = f.model !== CUSTOM_MODEL;
            if (!customWrap.hidden) customInput.focus();
          },
        },
      }, models.map(function (m) { return h("option", { value: m }, m); }), h("option", { value: CUSTOM_MODEL }, "Custom" + ELLIPSIS));
      select.value = f.model;
      append(modelBox, [
        h("label", { for: "f-model" }, "Model"), select,
        h("p", { class: "help", id: "f-model-help" }, "Discovery and the verifier use the same model."),
        customWrap]);
    }

    function optionLabel(name, opt) {
      if (typeof opt.label === "string" && opt.label) return opt.label;
      const words = String(name).replace(/_/g, " ");
      return words.charAt(0).toUpperCase() + words.slice(1);
    }

    function optionField(name) {
      const opt = spec[name];
      const id = "opt-" + safeId(name);
      const label = optionLabel(name, opt);
      const help = opt.help ? h("p", { class: "help", id: id + "-help" }, opt.help) : null;
      const described = function (extra) {
        const ids = [opt.help ? id + "-help" : null].concat(extra || []).filter(Boolean);
        return ids.length ? ids.join(" ") : null;
      };
      if (opt.kind === "bool") {
        const children = Object.keys(spec).filter(function (n) {
          return spec[n] && spec[n].requires === name && appliesTo(spec, n, f.engine);
        });
        const subBox = children.length
          ? h("div", { class: "sub-options", hidden: f.values[name] !== true }, children.map(optionField)) : null;
        const warning = opt.danger
          ? h("p", { class: "danger-note", id: id + "-danger" }, typeof opt.warning === "string" && opt.warning ? opt.warning : RUNTIME_WARNING)
          : null;
        return h("div", { class: "opt" + (opt.danger ? " opt-danger" : "") },
          h("div", { class: "choice" },
            h("input", {
              type: "checkbox", id: id, checked: f.values[name] === true,
              "aria-describedby": described(opt.danger ? [id + "-danger"] : null),
              on: { change: function (e) { f.values[name] = e.target.checked; if (subBox) subBox.hidden = !e.target.checked; } },
            }),
            h("label", { for: id }, label), warning, help),
          subBox);
      }
      if (opt.kind === "choice") {
        const choices = Array.isArray(opt.choices) ? opt.choices.map(String) : [];
        const def = choiceDefault(opt);
        const select = h("select", {
          id: id, "aria-describedby": described(),
          on: { change: function (e) { f.values[name] = e.target.value; } },
        }, choices.map(function (c) { return h("option", { value: c }, c === String(def) ? c + " (default)" : c); }));
        const cur = present(f.values[name]) ? String(f.values[name]) : (present(def) ? String(def) : "");
        select.value = choices.indexOf(cur) >= 0 ? cur : (choices[0] || "");
        return h("div", { class: "opt field field-narrow" }, h("label", { for: id }, label), select, help, fieldErrorEl(id));
      }
      if (opt.kind === "int" || opt.kind === "float") {
        const hasRange = isNum(opt.min) || isNum(opt.max);
        const range = hasRange ? h("p", { class: "help", id: id + "-range" },
          isNum(opt.min) && isNum(opt.max) ? "From " + opt.min + " to " + opt.max + "."
            : isNum(opt.min) ? opt.min + " or more." : opt.max + " or less.") : null;
        const input = h("input", {
          type: "number", id: id, value: present(f.values[name]) ? String(f.values[name]) : "",
          min: isNum(opt.min) ? String(opt.min) : null, max: isNum(opt.max) ? String(opt.max) : null,
          step: opt.kind === "int" ? "1" : "any", inputmode: opt.kind === "int" ? "numeric" : "decimal",
          placeholder: present(opt.default) ? "default " + opt.default : "default",
          "aria-describedby": described(hasRange ? [id + "-range"] : null),
          on: {
            input: function (e) {
              const bad = e.target.validity && e.target.validity.badInput;
              f.values[name] = bad ? "not a number" : e.target.value;
            },
          },
        });
        return h("div", { class: "opt field field-narrow" }, h("label", { for: id }, label), input, help, range, fieldErrorEl(id));
      }
      if (opt.kind === "text") {
        const input = h("input", {
          type: "text", id: id, value: typeof f.values[name] === "string" ? f.values[name] : "", autocomplete: "off",
          spellcheck: "false", placeholder: typeof opt.placeholder === "string" ? opt.placeholder : "empty for the default",
          "aria-describedby": described(),
          on: { input: function (e) { f.values[name] = e.target.value; } },
        });
        return h("div", { class: "opt field field-narrow" }, h("label", { for: id }, label), input, help, fieldErrorEl(id));
      }
      return null;   // a kind this page does not know is never offered (or sent)
    }

    function renderOptions() {
      clear(optionsBox);
      const names = Object.keys(spec).filter(function (n) {
        return spec[n] && typeof spec[n] === "object" && !spec[n].requires && appliesTo(spec, n, f.engine);
      });
      if (!names.length) { optionsBox.appendChild(h("p", { class: "muted" }, "No options for this engine.")); return; }
      for (const n of names) append(optionsBox, optionField(n));
    }

    function renderAccess(force) {
      const access = currentAccess();
      const json = f.engine + JSON.stringify(access);
      if (!force && json === accessJson) return;
      accessJson = json;
      clear(accessBox);
      if (f.engine === "lm") {
        accessBox.appendChild(h("p", { class: "access-line notice notice-info" },
          h("span", { class: "label-inline" }, "Model access"), "Orion-LM runs on this machine and needs none."));
        return;
      }
      accessBox.appendChild(h("p", { class: "access-line notice " + (access.mode === "endpoint" ? "notice-warn" : "notice-info") },
        h("span", { class: "label-inline" }, "Model access"), accessLine(access), ". ",
        h("a", { href: "#/settings" }, "Change it in Settings")));
    }

    function syncBusy() {
      const job = app.state && app.state.job;
      const running = !!(job && ACTIVE.indexOf(job.state) >= 0);
      clear(busy);
      if (running) {
        append(busy, h("p", null, "A scan is running: ", h("strong", null, basename(job.repo) || "a resumed run"), ". ",
          h("a", { href: job.run_id ? runHref(job.run_id) : "#/job" }, "Watch it"),
          ". Start another one when it ends."));
      }
      busy.hidden = !running;
      startBtn.disabled = running || starting;
    }

    async function start(e) {
      e.preventDefault();
      clearFieldErrors(form);
      const access = currentAccess();
      const repo = f.repo.trim();
      let firstBad = null;
      const bad = function (id, msg) { setFieldError(id, msg); if (!firstBad) firstBad = id; };
      if (!repo) bad("f-repo", "Enter the folder to scan.");
      let model = null;
      if (f.engine === "lm") {
        model = f.lmModel.trim();
        if (!model) bad("f-lm-model", "Enter the Orion-LM model folder.");
      } else if (access.mode !== "endpoint") {
        model = f.model === CUSTOM_MODEL ? f.custom.trim() : f.model;
        if (!model) bad("f-model-custom", "Enter a model name.");
      }
      const picked = collectOptions(spec, f.engine, f.values);
      for (const name of Object.keys(picked.errors)) bad("opt-" + safeId(name), picked.errors[name]);
      if (firstBad) {
        const el = byId(firstBad);
        if (el) el.focus();
        say("Fix the marked fields, then start the scan again.", "error");
        return;
      }
      starting = true;
      startBtn.disabled = true;
      startBtn.textContent = "Starting" + ELLIPSIS;
      try {
        const res = await api("POST", "/api/scans", { repo: repo, engine: f.engine, model: model, options: picked.options });
        if (app.state && res && res.job) { app.state.job = res.job; renderHeader(); }
        say("Started the scan.", "ok", true);
        navigate(res && res.job && res.job.run_id ? runHref(res.job.run_id) : "#/job");
      } catch (err) {
        if (isLive(seq)) say(err.message, "error");
      } finally {
        starting = false;
        if (isLive(seq)) { startBtn.textContent = "Start scan"; syncBusy(); }
      }
    }

    const form = h("form", { class: "form", novalidate: true, on: { submit: start } },
      pageHead("New scan", "Runs the ordinary orion scan in the background, one scan at a time. You can watch it, stop it and resume it from here."),
      busy,
      h("div", { class: "field" },
        h("label", { for: "f-repo" }, "Repository folder"), repoInput,
        h("p", { class: "help", id: "f-repo-help" }, "A folder on the machine orion ui runs on. A relative path starts from ",
          mono(st.cwd || "the folder orion ui runs in"), "."),
        fieldErrorEl("f-repo")),
      h("fieldset", null, h("legend", null, "Engine"),
        engineChoice("claude", "Claude", "claude -p agents look for leads, then a separate verifier checks each one.", true, engines.claude),
        engineChoice("lm", "Orion-LM", "A local model; no key needed. Its leads stay unverified unless lm verify is ticked.", lmOk, engines.lm)),
      modelBox,
      h("fieldset", null, h("legend", null, "Options"), optionsBox),
      accessBox,
      h("div", { class: "form-actions" }, startBtn));

    show(form);
    renderModel();
    renderOptions();
    renderAccess(true);
    syncBusy();
    screen.stateHook = function () {
      if (!isLive(seq)) return;
      syncBusy();
      const before = accessJson;
      renderAccess(false);
      if (accessJson !== before && f.engine !== "lm") renderModel();
    };
  }

  // A started scan before its run folder is known ─────────────────────────────────────────────────────
  async function showJob() {
    const seq = screen.seq;
    setTitle("Starting scan");
    const lede = h("p", { class: "lede" }, "Waiting for the scan to create its run folder" + ELLIPSIS);
    const out = h("pre", { class: "output", tabindex: "0", "aria-label": "Scan output" });
    let have = 0;
    show(h("section", null, pageHead("Starting scan", null), lede,
      h("section", { class: "panel-plain" }, h("h2", null, "Scan output"), out)));

    async function poll() {
      if (!isLive(seq)) return;
      let d;
      try {
        d = await api("GET", "/api/job");
      } catch (e) {
        if (isLive(seq)) { lede.textContent = e.message; later(poll, 2000); }
        return;
      }
      if (!isLive(seq)) return;
      const job = d && d.job;
      if (!job) {
        show(h("section", null,
          pageHead("No scan started", "No scan has been started from this page since orion ui launched."),
          h("p", null, h("a", { class: "btn btn-primary", href: "#/new" }, "New scan"))));
        return;
      }
      have = appendJobOutput(out, d.output, d.next, have);
      if (job.run_id) {
        // The live view names the repo and model from this job until leads.json does.
        if (app.state) { app.state.job = job; renderHeader(); }
        navigate(runHref(job.run_id), true);
        return;
      }
      if (job.state === "exited") {
        focusStaysInView();
        show(h("section", null,
          pageHead("The scan did not start", null),
          notice("danger", "The scan exited before it created a run folder: " + sentence(job.exit_note || ("exited with " + job.returncode))),
          h("section", { class: "panel-plain" }, h("h2", null, "Scan output"), out),
          h("p", { class: "actions-row" },
            h("a", { class: "btn btn-primary", href: "#/new" }, "Back to new scan"),
            h("a", { class: "btn", href: "#/" }, "Runs"))));
        return;
      }
      if (job.run_dir && !job.run_id) {
        lede.textContent = "The scan writes to " + job.run_dir + ", which is outside the runs folder this page reads.";
      }
      later(poll, 500);
    }
    poll();
  }

  // One run: live while its scan runs here, else the report ────────────────────────────────────────────
  async function showRun(params) {
    const seq = screen.seq;
    const id = params.id;
    setTitle("Run");
    showLoading("Run", "Reading the run" + ELLIPSIS);
    let detail;
    try {
      detail = (await Promise.all([api("GET", "/api/runs/" + enc(id)), ensureState()]))[0];
    } catch (e) {
      if (!isLive(seq)) return;
      if (e.status === 404) {
        show(h("section", null, pageHead("No such run", "There is no run folder for this address. It may have been deleted."),
          h("p", null, h("a", { class: "btn", href: "#/" }, "Go to runs"))));
      } else {
        show(errorView("Run", e));
      }
      return;
    }
    if (!isLive(seq)) return;
    if (ACTIVE.indexOf(detail.state) >= 0) showLive(id, detail, seq);
    else showReport(id, detail, seq, null);
  }

  function stat(label, dd, wide) {
    return h("div", { class: "stat" + (wide ? " stat-wide" : "") }, h("dt", null, label), dd);
  }

  function showLive(id, run, seq) {
    const L = {
      tally: newTally(), lastId: -1, nextOut: 0, job: null, counts: run.counts || null, autoscroll: true,
      outScroll: true, pending: [], outPending: [], flushTimer: null, exitTimer: null, ended: false,
      nLeads: isNum(run.n_leads) ? run.n_leads : null, phaseEls: null, phaseNames: "", elapsedTimer: null,
      nVerdicts: null,
    };
    if (app.state && app.state.job && app.state.job.run_id === id) L.job = app.state.job;
    // Until discovery writes leads.json the run dir names no repo or model: the job that writes it does.
    const detail = Object.assign({}, run, {
      repo: run.repo || (L.job && L.job.repo) || null,
      model: run.model || (L.job && L.job.model) || null,
    });
    setTitle("Scanning " + runName(detail));

    const badge = h("span", { class: "badge s-running" }, "Running");
    const phases = h("ol", { class: "phases", "aria-label": "Scan phases" });
    const elapsedEl = h("dd", null, DASH);
    const verifyEl = h("dd", null, DASH);
    const countsEl = h("dd", { class: "counts" });
    const tokensEl = h("dd", null, "not reported");
    const costEl = h("dd", null, "not reported");
    const logEl = h("div", { class: "log", role: "log", "aria-live": "off", "aria-label": "Event log", tabindex: "0" });
    const jump = h("button", {
      type: "button", class: "btn btn-small", hidden: true,
      on: { click: function () { L.autoscroll = true; jump.hidden = true; logEl.scrollTop = logEl.scrollHeight; } },
    }, "Jump to latest");
    const outEl = h("pre", { class: "output", tabindex: "0", "aria-label": "Scan output" });
    const streamNote = h("div", { class: "notice notice-warn", hidden: true });

    const stopBtn = h("button", { type: "button", class: "btn btn-danger", disabled: true, on: { click: askStop } }, "Stop");
    const confirmText = h("p", null, "");
    const confirmYes = h("button", { type: "button", class: "btn btn-danger-solid", on: { click: doStop } }, "Stop scan");
    const confirmNo = h("button", { type: "button", class: "btn", on: { click: cancelStop } }, "Keep running");
    const confirmBox = h("div", {
      class: "confirm", hidden: true, role: "group", "aria-label": "Confirm the stop",
      on: { keydown: function (e) { if (e.key === "Escape") cancelStop(); } },
    }, confirmText, h("div", { class: "actions-row" }, confirmYes, confirmNo));

    const lede = [engineLabel(detail.engine)];
    if (detail.model) lede.push(DOT, detail.engine === "lm" ? basename(detail.model) : detail.model);
    lede.push(DOT, badge);
    if (detail.repo) lede.push(DOT, monoPath(detail.repo));

    show(h("section", { class: "live" },
      pageHead(runName(detail), lede, [stopBtn], "Live run"),
      confirmBox, streamNote,
      h("div", { class: "card phase-card" }, phases),
      h("dl", { class: "stats" },
        stat("Elapsed", elapsedEl), stat("Verified", verifyEl), stat("Verdicts", countsEl, true),
        stat("Tokens", tokensEl), stat("Cost", costEl)),
      h("section", { class: "log-section" },
        h("div", { class: "section-head" }, h("h2", null, "Event log"), jump), logEl),
      h("details", { class: "panel" }, h("summary", null, "Scan output"), h("div", { class: "panel-body" }, outEl))));

    logEl.addEventListener("scroll", function () {
      const atEnd = logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 24;
      L.autoscroll = atEnd;
      jump.hidden = atEnd;
    });
    outEl.addEventListener("scroll", function () {
      L.outScroll = outEl.scrollHeight - outEl.scrollTop - outEl.clientHeight < 24;
    });
    screen.cleanups.push(function () { clearTimeout(L.flushTimer); clearTimeout(L.exitTimer); });

    function startMs() {
      const fromJob = L.job && L.job.run_id === id ? parseTime(L.job.started) : null;
      return fromJob || parseTime(detail.started) || parseTime(L.tally.firstTs);
    }
    function updateElapsed() {
      const s = startMs();
      elapsedEl.textContent = s ? fmtElapsed(Date.now() - s) : DASH;
    }

    function renderPhaseStrip() {
      const list = phaseList(L.tally);
      const names = list.map(function (p) { return p.name; }).join(",");
      if (!L.phaseEls || L.phaseNames !== names) {
        clear(phases);
        L.phaseEls = {};
        L.phaseNames = names;
        for (const p of list) {
          const stateEl = h("span", { class: "phase-state" });
          const li = h("li", { class: "phase" }, h("span", { class: "star", "aria-hidden": "true" }),
            h("span", { class: "phase-name" }, p.label), stateEl);
          L.phaseEls[p.name] = { li: li, stateEl: stateEl, cls: "", text: "" };
          phases.appendChild(li);
        }
      }
      for (const p of list) {
        const el = L.phaseEls[p.name];
        const cls = "phase is-" + p.state;
        const text = phaseStateText(p, !L.ended);
        if (el.cls !== cls) { el.li.className = cls; el.cls = cls; }
        if (el.text !== text) { el.stateEl.textContent = text; el.text = text; }
        if (p.current && !L.ended) el.li.setAttribute("aria-current", "step");
        else el.li.removeAttribute("aria-current");
      }
    }

    function scheduleFlush() {
      if (L.flushTimer === null) L.flushTimer = setTimeout(flush, 120);
    }

    function flush() {
      clearTimeout(L.flushTimer);
      L.flushTimer = null;
      if (!isLive(seq)) return;
      if (L.pending.length) {
        const batch = L.pending.length > MAX_LOG_LINES ? L.pending.slice(-MAX_LOG_LINES) : L.pending;
        L.pending = [];
        if (batch.length >= MAX_LOG_LINES) clear(logEl);
        const frag = document.createDocumentFragment();
        for (const evt of batch) frag.appendChild(logLine(evt));
        logEl.appendChild(frag);
        while (logEl.childNodes.length > MAX_LOG_LINES) logEl.removeChild(logEl.firstChild);
        if (L.autoscroll) logEl.scrollTop = logEl.scrollHeight;
      }
      if (L.outPending.length) {
        const lines = L.outPending.length > MAX_OUTPUT_LINES ? L.outPending.slice(-MAX_OUTPUT_LINES) : L.outPending;
        L.outPending = [];
        for (const line of lines) pushLine(outEl, line, MAX_OUTPUT_LINES);
        if (L.outScroll) outEl.scrollTop = outEl.scrollHeight;
      }
      renderPhaseStrip();
      // The server's count of leads with a SAVED verdict when it has sent one: a session the Stop killed leaves
      // a verdict event in the log but nothing saved, and Resume verifies that lead again.
      const vc = verifyCounts(L.tally, L.nLeads);
      verifyEl.textContent = progressText(L.nVerdicts !== null ? { done: L.nVerdicts, total: vc.total } : vc);
      tokensEl.textContent = tokensText(L.tally.usage);
      costEl.textContent = costText(L.tally.usage);
      clear(countsEl);
      append(countsEl, countChips(L.counts));
    }

    function syncStop() {
      const job = L.job && L.job.run_id === id ? L.job : null;
      const running = !!(job && ACTIVE.indexOf(job.state) >= 0);
      stopBtn.disabled = !running;
      stopBtn.textContent = job && job.stop_count >= 1 ? "Force stop" : "Stop";
      const stopping = !!(job && job.state === "stopping");
      badge.className = "badge s-" + (stopping ? "stopping" : "running");
      badge.textContent = stopping ? "Stopping" : "Running";
    }

    function askStop() {
      const force = !!(L.job && L.job.stop_count >= 1);
      confirmText.textContent = force
        ? "End the scan now? It is killed at once, and the step in progress is lost."
        : "Stop this scan? It finishes its current step, then exits. A Claude scan stopped after discovery can be resumed later.";
      confirmYes.textContent = force ? "Force stop" : "Stop scan";
      confirmBox.hidden = false;
      stopBtn.hidden = true;
      confirmYes.focus();
    }

    function cancelStop() {
      confirmBox.hidden = true;
      stopBtn.hidden = false;
      stopBtn.focus();
    }

    async function doStop() {
      confirmYes.disabled = true;
      try {
        const r = await api("POST", "/api/job/stop");
        if (!isLive(seq)) return;
        if (r && r.job) { L.job = r.job; if (app.state) { app.state.job = r.job; renderHeader(); } }
        say(r && r.signal === "kill" ? "Force-stopped the scan."
          : "Asked the scan to stop. It finishes its current step first; Force stop ends it at once.");
      } catch (err) {
        if (isLive(seq)) say(err.message, "error");
      } finally {
        if (isLive(seq)) {
          confirmYes.disabled = false;
          confirmBox.hidden = true;
          stopBtn.hidden = false;
          syncStop();
          stopBtn.focus();
        }
      }
    }

    function showStreamNote(text, offerReconnect) {
      clear(streamNote);
      append(streamNote, h("p", null, text, offerReconnect ? [" ", h("button", {
        type: "button", class: "btn btn-small", on: { click: function () { streamNote.hidden = true; connect(); } },
      }, "Reconnect")] : null));
      streamNote.hidden = false;
    }

    function onProgress(e) {
      if (!isLive(seq) || L.ended) return;
      const raw = e.lastEventId;
      const n = raw !== "" && present(raw) ? Number(raw) : NaN;
      if (Number.isFinite(n)) {
        if (n <= L.lastId) return;   // already seen (a reconnect resumes at or before the last id)
        L.lastId = n;
      }
      const evt = parseJson(e.data);
      if (!evt || typeof evt !== "object") return;
      tallyEvent(L.tally, evt);
      L.pending.push(evt);
      if (L.pending.length > 2 * MAX_LOG_LINES) L.pending = L.pending.slice(-MAX_LOG_LINES);
      scheduleFlush();
    }

    function onOutput(e) {
      const d = parseJson(e.data);
      if (!isLive(seq) || !d || !isNum(d.i) || d.i < L.nextOut) return;
      L.nextOut = d.i + 1;
      L.outPending.push(String(d.line));
      if (L.outPending.length > 2 * MAX_OUTPUT_LINES) L.outPending = L.outPending.slice(-MAX_OUTPUT_LINES);
      scheduleFlush();
    }

    function onJob(e) {
      const d = parseJson(e.data);
      if (!isLive(seq) || !d || typeof d !== "object") return;
      L.job = d;
      if (app.state) { app.state.job = d; renderHeader(); }
      syncStop();
      // `end` follows once the server has drained the log; if it never comes, read the report anyway.
      if (d.state === "exited" && L.exitTimer === null) L.exitTimer = setTimeout(function () { finish(); }, 4000);
    }

    function onVerdicts(e) {
      const d = parseJson(e.data);
      if (!isLive(seq) || !d || typeof d.counts !== "object" || !d.counts) return;
      L.counts = d.counts;
      if (isNum(d.n)) L.nVerdicts = d.n;
      scheduleFlush();
    }

    function closeStreams() {
      for (const es of screen.sources) { try { es.close(); } catch (x) { /* already closed */ } }
      screen.sources = [];
    }

    function connect() {
      if (!isLive(seq) || L.ended) return;
      closeStreams();
      let url = "/api/runs/" + enc(id) + "/stream?t=" + encodeURIComponent(app.token || "");
      if (L.lastId >= 0) url += "&offset=" + enc(String(L.lastId));
      let es;
      try { es = new EventSource(url); } catch (err) { showStreamNote("This browser cannot open the live stream.", false); return; }
      screen.sources.push(es);
      es.addEventListener("open", function () { if (isLive(seq)) streamNote.hidden = true; });
      es.addEventListener("progress", onProgress);
      es.addEventListener("output", onOutput);
      es.addEventListener("job", onJob);
      es.addEventListener("verdicts", onVerdicts);
      es.addEventListener("end", function () { es.close(); finish(); });
      es.addEventListener("error", function () {
        if (!isLive(seq) || L.ended) return;
        if (es.readyState === 2) streamClosed();   // EventSource.CLOSED: the browser gave up on it
        else showStreamNote("Reconnecting to the live stream" + ELLIPSIS, false);
      });
    }

    async function streamClosed() {
      showStreamNote("The live stream closed.", true);
      let d;
      try { d = await api("GET", "/api/runs/" + enc(id)); } catch (err) { return; }
      if (isLive(seq) && !L.ended && ACTIVE.indexOf(d.state) < 0) finish();
    }

    async function finish() {
      if (L.ended || !isLive(seq)) return;
      L.ended = true;
      closeStreams();
      flush();
      let d;
      try {
        d = await api("GET", "/api/runs/" + enc(id));
      } catch (err) {
        if (!isLive(seq)) return;
        L.ended = false;
        showStreamNote("The scan ended, but its report cannot be read yet: " + err.message, true);
        return;
      }
      if (!isLive(seq)) return;
      if (ACTIVE.indexOf(d.state) >= 0) {   // the job is still winding down: follow it a little longer
        L.ended = false;
        L.exitTimer = null;
        later(connect, 1500);
        return;
      }
      try { await refreshState(); } catch (err) { /* the header only */ }
      if (!isLive(seq)) return;
      for (const t of screen.intervals) clearInterval(t);
      screen.intervals = [];
      focusStaysInView();
      showReport(id, d, seq, { job: L.job });
    }

    syncStop();
    flush();
    updateElapsed();
    screen.intervals.push(setInterval(updateElapsed, 1000));
    (async function () {
      try {
        const d = await api("GET", "/api/job");
        if (!isLive(seq)) return;
        if (d && d.job && d.job.run_id === id) {
          L.job = d.job;
          syncStop();
          if (Array.isArray(d.output)) {
            for (const line of d.output) L.outPending.push(String(line));
            if (isNum(d.next)) L.nextOut = d.next;
            scheduleFlush();
          }
        }
      } catch (err) {
        /* the stream still works without the job's own view */
      }
      if (isLive(seq)) connect();
    })();
  }

  // Report ───────────────────────────────────────────────────────────────────────────────────────────
  function longText(text, emptyMessage) {
    const s = typeof text === "string" ? text : (present(text) ? String(text) : "");
    if (!s) return h("p", { class: "muted" }, emptyMessage);
    const pre = h("pre", { class: "evidence" }, s.length > EVIDENCE_PREVIEW ? s.slice(0, EVIDENCE_PREVIEW) + ELLIPSIS : s);
    if (s.length <= EVIDENCE_PREVIEW) return pre;
    const more = h("button", {
      type: "button", class: "btn btn-small btn-quiet",
      on: { click: function () { pre.textContent = s; more.remove(); pre.focus(); } },
    }, "Show all " + fmtInt(s.length) + " characters");
    pre.setAttribute("tabindex", "-1");
    return h("div", null, pre, more);
  }

  function codeView(d) {
    const hl = Array.isArray(d.highlight) ? d.highlight : [];
    const a = Number(hl[0]), b = Number(present(hl[1]) ? hl[1] : hl[0]);
    const lines = h("span", { class: "code-lines" });
    for (const ln of Array.isArray(d.lines) ? d.lines : []) {
      const on = Number.isFinite(a) && ln.n >= a && ln.n <= b;
      lines.appendChild(h("span", { class: on ? "cl hl" : "cl" },
        h("span", { class: "ln", "aria-hidden": "true" }, String(ln.n)),
        h("span", { class: "tx" }, String(present(ln.text) ? ln.text : ""))));
    }
    const cited = Number.isFinite(a) ? (a === b ? "line " + a : "lines " + a + DASH + b) : null;
    return h("figure", { class: "code-fig" },
      h("figcaption", null, monoPath(d.file || ""), DOT, "lines " + d.start + DASH + d.end, cited ? DOT + "cited " + cited : null),
      h("pre", { class: "code", tabindex: "0", "aria-label": "Code from " + (d.file || "the cited file") }, lines));
  }

  // "Show code": the cited lines, read-only, from the run's own repo (the server refuses anything outside it).
  function codeToggle(rid, file, line, end, label) {
    const box = h("div", { class: "code-box", hidden: true });
    const btn = h("button", { type: "button", class: "btn btn-small", "aria-expanded": "false" }, label || "Show code");
    let loaded = false;
    btn.addEventListener("click", async function () {
      const open = btn.getAttribute("aria-expanded") !== "true";
      btn.setAttribute("aria-expanded", String(open));
      btn.textContent = open ? "Hide code" : (label || "Show code");
      box.hidden = !open;
      if (!open || loaded) return;
      loaded = true;
      clear(box).appendChild(h("p", { class: "muted" }, "Reading the code" + ELLIPSIS));
      let q = "?file=" + enc(file) + "&line=" + enc(String(line));
      if (end) q += "&end=" + enc(String(end));
      try {
        const d = await api("GET", "/api/runs/" + enc(rid) + "/code" + q);
        clear(box).appendChild(codeView(d || {}));
      } catch (e) {
        loaded = false;
        clear(box).appendChild(h("p", { class: "inline-error" }, "Cannot show this code: " + e.message));
      }
    });
    return h("div", { class: "code-toggle" }, btn, box);
  }

  function pathList(rid, path) {
    return h("ol", { class: "path" }, path.map(function (node) {
      const n = node && typeof node === "object" ? node : {};
      const line = isNum(n.line) ? n.line : null;
      return h("li", null,
        n.role ? h("span", { class: "role" }, String(n.role)) : null,
        monoPath((n.file || "?") + (line ? ":" + line : "")),
        n.function ? [" ", h("span", { class: "mono fn" }, String(n.function))] : null,
        n.code ? h("code", { class: "snippet" }, String(n.code)) : null,
        n.file && line ? codeToggle(rid, String(n.file), line, null) : null);
    }));
  }

  function block(title, wide) {
    return h("section", { class: "v-block" + (wide ? " wide" : "") }, h("h3", null, title),
      Array.prototype.slice.call(arguments, 2));
  }

  function verdictDetail(rid, v, loc) {
    const lead = v.lead || {};
    const facts = [];
    if (present(lead.index)) facts.push("lead " + lead.index);
    if (lead.shape) facts.push("shape " + lead.shape);
    if (lead.confidence) facts.push("confidence " + lead.confidence);
    if (isNum(v.sink_centrality) && v.sink_centrality > 0) facts.push("sink centrality " + v.sink_centrality.toFixed(3));
    return h("div", { class: "v-body" },
      block("Reason", false, v.reason ? h("p", { class: "prose" }, v.reason) : h("p", { class: "muted" }, "No reason was recorded.")),
      block("Verifier evidence", false, longText(v.evidence, "The verifier recorded no evidence.")),
      block("Lead", false, h("p", { class: "prose" }, lead.text || ""),
        facts.length ? h("p", { class: "facts" }, facts.join(DOT)) : null),
      block("Lead evidence", false, longText(lead.evidence, "The lead cites no evidence.")),
      Array.isArray(lead.path) && lead.path.length ? block("Path", true, pathList(rid, lead.path)) : null,
      block("Code", true, loc.file && loc.line_start
        ? codeToggle(rid, loc.file, loc.line_start, loc.line_end)
        : h("p", { class: "muted" }, "No file and line are cited, so there is no code to show.")));
  }

  function verdictItem(rid, v) {
    const loc = effectiveLocation(v);
    const lead = v.lead || {};
    const detailId = "verdict-" + nextUid();
    const it = { decision: v.decision, open: false, row: null, detail: null };
    const toggle = h("button", {
      type: "button", class: "disclosure", "aria-expanded": "false", "aria-controls": detailId,
      "aria-label": "Details for lead " + (present(lead.index) ? lead.index : "?"),
    });
    const cell = h("td", { colspan: "8" });
    it.detail = h("tr", { class: "v-detail", id: detailId, hidden: true }, cell);
    const sev = SEVERITIES.indexOf(v.severity) >= 0 ? v.severity : null;
    it.row = h("tr", { class: "v-row" },
      h("td", { class: "c-toggle" }, toggle),
      h("td", null, decisionBadge(v.decision)),
      h("td", null, sev ? h("span", { class: "sev sev-" + sev.toLowerCase() }, sev) : (v.severity ? String(v.severity) : dash())),
      h("td", { class: "mono nowrap" }, loc.cwe || dash()),
      h("td", { class: "mono c-loc" }, whereText(loc) ? pathText(whereText(loc)) : dash()),
      h("td", { class: "mono c-fn" }, loc.function || dash()),
      h("td", { class: "nowrap" }, v.route || "full"),
      h("td", { class: "c-lead" }, h("span", { class: "clamp" }, lead.text || "")));
    let built = false;
    function setOpen(open) {
      it.open = open;
      toggle.setAttribute("aria-expanded", String(open));
      it.row.classList.toggle("is-open", open);
      it.detail.hidden = !open || it.row.hidden;
      if (open && !built) { built = true; cell.appendChild(verdictDetail(rid, v, loc)); }
    }
    toggle.addEventListener("click", function () { setOpen(!it.open); });
    it.row.addEventListener("click", function (e) {
      if (e.target && e.target.closest && e.target.closest("button, a, input, select, summary")) return;
      const sel = window.getSelection ? String(window.getSelection()) : "";
      if (sel) return;   // selecting a path to copy it must not fold the row
      setOpen(!it.open);
    });
    return it;
  }

  async function loadEvents(rid, seq) {
    const tally = newTally();
    let tail = [], offset = 0, partial = false;
    for (let page = 0; ; page++) {
      if (page >= EVENT_PAGES_MAX) { partial = true; break; }
      const d = await api("GET", "/api/runs/" + enc(rid) + "/events?offset=" + offset);
      if (!isLive(seq)) return null;
      const evs = d && Array.isArray(d.events) ? d.events : [];
      for (const e of evs) { tallyEvent(tally, e); tail.push(e); }
      if (tail.length > 2 * MAX_LOG_LINES) tail = tail.slice(-MAX_LOG_LINES);
      if (!d || !isNum(d.offset) || d.offset <= offset) break;
      offset = d.offset;
    }
    return { tally: tally, tail: tail.slice(-MAX_LOG_LINES), partial: partial };
  }

  function showReport(id, d, seq, extra) {
    setTitle("Report: " + runName(d));
    const job = (extra && extra.job) || (app.state && app.state.job) || null;
    const ownJob = job && job.run_id === id ? job : null;
    const verdicts = sortVerdicts(Array.isArray(d.verdicts) ? d.verdicts : []);
    const counts = countDecisions(verdicts);
    const leads = Array.isArray(d.leads) ? d.leads : [];

    const tokensDd = h("dd", null, "counting" + ELLIPSIS);
    const costDd = h("dd", null, "counting" + ELLIPSIS);
    const meta = h("dl", { class: "meta" },
      h("div", null, h("dt", null, "State"), h("dd", null, stateBadge(d.state))),
      h("div", null, h("dt", null, "Engine"), h("dd", null, engineLabel(d.engine))),
      h("div", null, h("dt", null, "Model"), h("dd", null, d.model
        ? h("span", { class: "mono", title: d.model }, d.engine === "lm" ? basename(d.model) : d.model)
        : h("span", { class: "muted" }, "not recorded"))),
      h("div", null, h("dt", null, "Started"), h("dd", null, timeEl(d.started))),
      h("div", null, h("dt", null, "Leads"), h("dd", null, isNum(d.n_leads) ? fmtInt(d.n_leads) : h("span", { class: "muted" }, "none saved"))),
      h("div", null, h("dt", null, "Tokens"), tokensDd),
      h("div", null, h("dt", null, "Cost"), costDd),
      ownJob && ownJob.state === "exited"
        ? h("div", null, h("dt", null, "Scan exit"), h("dd", null, ownJob.exit_note || ("exited with " + ownJob.returncode)))
        : null);

    const notices = [];
    if (d.problem) notices.push(notice("warn", "Some run files could not be read: " + sentence(d.problem)));
    if (d.elsewhere) {
      notices.push(notice("info", "A scan outside this UI (in a terminal?) is writing this run right now. Reload to see how far it has got."));
    } else if (d.state === "incomplete" && !leads.length) {
      notices.push(notice("warn", "No leads were saved: the scan stopped or crashed before discovery finished. The event log shows how far it got."));
    } else if (d.state === "incomplete") {
      notices.push(notice("warn", "This run did not finish: " + fmtInt(verdicts.length) + " of " + fmtInt(leads.length) +
        " leads have a verdict. The event log shows how far it got."));
    } else if (d.state === "stopped") {
      notices.push(notice("warn", "This scan stopped before it finished: " + fmtInt(verdicts.length) + " of " +
        fmtInt(leads.length) + " leads have a verdict. " + (d.resumable
          ? "Resume verifies the rest without repeating discovery."
          : "An Orion-LM run cannot be resumed; its verdicts so far are below.")));
    } else if (d.state === "done" && !verdicts.length) {
      notices.push(notice("info", "Discovery found no leads, so there was nothing to verify."));
    }

    const busy = jobActive();
    const actions = [];
    if (d.resumable) {
      const resumeBtn = h("button", {
        type: "button", class: "btn btn-primary", disabled: busy,
        title: busy ? "A scan is running. Resume this one when it ends." : null,
        on: { click: function (e) { resumeRun(id, e.currentTarget); } },
      }, "Resume");
      actions.push(resumeBtn);
      screen.stateHook = function () {        // the running scan may end while this report is open
        if (!isLive(seq)) return;
        const now = jobActive();
        resumeBtn.disabled = now;
        if (now) resumeBtn.setAttribute("title", "A scan is running. Resume this one when it ends.");
        else resumeBtn.removeAttribute("title");
      };
    }

    const files = (Array.isArray(d.files) ? d.files : []).filter(function (f) { return DOWNLOADS.indexOf(f) >= 0; });
    const filesEl = files.length ? h("ul", { class: "files", "aria-label": "Download the run's files" }, files.map(function (f) {
      return h("li", null, h("a", {
        class: "btn btn-small", download: f,
        href: "/api/runs/" + enc(id) + "/files/" + enc(f) + "?t=" + encodeURIComponent(app.token || ""),
      }, DOWNLOAD_LABELS[f], " ", h("span", { class: "mono muted" }, f)));
    })) : null;

    // Verdict table with per-decision filter chips (all shown at first; a chip hides or shows its decision).
    const shown = new Set(DECISIONS);
    const items = [];
    const emptyRow = h("tr", { class: "empty-row", hidden: true },
      h("td", { colspan: "8" }, "No verdict matches the decisions picked above."));
    function applyFilter() {
      let visible = 0;
      for (const it of items) {
        const on = shown.has(it.decision) || DECISIONS.indexOf(it.decision) < 0;
        it.row.hidden = !on;
        it.detail.hidden = !on || !it.open;
        if (on) visible += 1;
      }
      emptyRow.hidden = visible > 0 || !items.length;
    }
    const chips = h("div", { class: "chips", role: "group", "aria-label": "Show decisions" }, DECISIONS.map(function (dec) {
      const chip = h("button", {
        type: "button", class: "chip", "data-d": dec, "aria-pressed": "true", disabled: !counts[dec],
        on: {
          click: function () {
            if (shown.has(dec)) shown.delete(dec); else shown.add(dec);
            chip.setAttribute("aria-pressed", String(shown.has(dec)));
            applyFilter();
          },
        },
      }, h("span", { class: "chip-label" }, dec), h("span", { class: "chip-n" }, fmtInt(counts[dec])));
      return chip;
    }));
    const tbody = h("tbody");
    for (const v of verdicts) {
      const it = verdictItem(id, v);
      items.push(it);
      tbody.appendChild(it.row);
      tbody.appendChild(it.detail);
    }
    tbody.appendChild(emptyRow);
    const table = verdicts.length ? h("div", { class: "table-wrap" }, h("table", { class: "verdicts" },
      h("caption", { class: "sr-only" }, "Verdicts, confirmed first. Each row opens its reason, evidence and code."),
      h("thead", null, h("tr", null,
        h("th", { scope: "col", class: "c-toggle" }, h("span", { class: "sr-only" }, "Details")),
        ["Decision", "Severity", "CWE", "Location", "Function", "Route", "Lead"].map(function (t) { return h("th", { scope: "col" }, t); }))),
      tbody)) : null;

    const withVerdict = new Set(verdicts.map(function (v) { return v.lead && v.lead.index; }));
    const unverified = leads.filter(function (l) { return l && !withVerdict.has(l.index); });
    const unverifiedEl = unverified.length ? h("details", { class: "panel" },
      h("summary", null, "Leads without a verdict (" + fmtInt(unverified.length) + ")"),
      h("div", { class: "panel-body" }, h("ul", { class: "lead-list" }, unverified.map(function (l) {
        const where = whereText(effectiveLocation({ lead: l }));
        return h("li", null, h("span", { class: "mono" }, "lead " + l.index), l.shape ? DOT + "shape " + l.shape : null,
          where ? [DOT, monoPath(where)] : null, h("div", { class: "prose" }, l.text || ""));
      })))) : null;

    const reportEl = typeof d.report_text === "string" && d.report_text ? h("details", { class: "panel" },
      h("summary", null, "Plain-text report (report.txt)"),
      h("div", { class: "panel-body" }, h("pre", { class: "report-text", tabindex: "0" }, d.report_text))) : null;

    const logNote = h("p", { class: "muted" }, "Reading the event log" + ELLIPSIS);
    const logEl = h("div", { class: "log log-static", role: "log", "aria-live": "off", "aria-label": "Event log", tabindex: "0" });
    const logPanel = h("details", { class: "panel", open: d.state === "incomplete" },
      h("summary", null, "Event log"), h("div", { class: "panel-body" }, logNote, logEl));

    let outPanel = null;
    if (ownJob) {
      const pre = h("pre", { class: "output", tabindex: "0", "aria-label": "Scan output" });
      outPanel = h("details", { class: "panel", open: d.state !== "done" }, h("summary", null, "Scan output"),
        h("div", { class: "panel-body" }, pre));
      api("GET", "/api/job").then(function (r) {
        if (!isLive(seq) || !r || !r.job || r.job.run_id !== id) return;
        appendJobOutput(pre, r.output, r.next, 0);
      }).catch(function () { /* the panel stays empty */ });
    }

    show(h("section", { class: "report" },
      pageHead(runName(d), d.repo ? monoPath(d.repo) : null, actions, "Report"),
      meta, notices, filesEl,
      verdicts.length ? h("section", { class: "report-section" },
        h("div", { class: "section-head" }, h("h2", null, "Verdicts"), chips), table) : null,
      unverifiedEl, reportEl, logPanel, outPanel));
    applyFilter();

    loadEvents(id, seq).then(function (res) {
      if (!res || !isLive(seq)) return;
      tokensDd.textContent = tokensText(res.tally.usage);
      costDd.textContent = costText(res.tally.usage);
      if (!res.tally.events) { logNote.textContent = "This run logged no events."; return; }
      logNote.textContent = (res.tail.length < res.tally.events
        ? "The last " + fmtInt(res.tail.length) + " of " + fmtInt(res.tally.events) + " events"
        : fmtInt(res.tally.events) + " events") + (res.partial ? ", from the start of a log too long to read in full." : ".");
      const frag = document.createDocumentFragment();
      for (const e of res.tail) frag.appendChild(logLine(e));
      logEl.appendChild(frag);
    }).catch(function (err) {
      if (!isLive(seq)) return;
      tokensDd.textContent = "unknown";
      costDd.textContent = "unknown";
      logNote.textContent = "Cannot read the event log: " + err.message;
    });
  }

  // Compare ──────────────────────────────────────────────────────────────────────────────────────────
  function runCard(title, s, rid) {
    const bits = [engineLabel(s.engine), RUN_STATES[s.state] || s.state, s.started ? "started " + fmtTime(s.started) : null];
    return h("section", { class: "card run-card" },
      h("h2", null, title),
      h("p", { class: "repo-name" }, runName(s)),
      s.repo ? h("p", { class: "sub" }, monoPath(s.repo)) : null,
      h("p", { class: "sub" }, bits.filter(Boolean).join(DOT)),
      h("div", { class: "counts" }, countChips(s.counts)),
      h("p", { class: "run-card-link" }, h("a", { href: runHref(rid) }, "Open " + title.charAt(0).toLowerCase() + title.slice(1))));
  }

  function rowWhere(r) { return whereText({ file: r.file, line_start: r.line }); }

  function compareTable(rows, caption) {
    return h("div", { class: "table-wrap" }, h("table", null,
      h("caption", { class: "sr-only" }, caption),
      h("thead", null, h("tr", null, ["Decision", "Severity", "CWE", "Location", "Function", "Lead"].map(function (t) {
        return h("th", { scope: "col" }, t);
      }))),
      h("tbody", null, rows.map(function (r) {
        return h("tr", null,
          h("td", null, decisionBadge(r.decision)),
          h("td", null, r.severity ? String(r.severity) : dash()),
          h("td", { class: "mono nowrap" }, r.cwe || dash()),
          h("td", { class: "mono c-loc" }, rowWhere(r) ? pathText(rowWhere(r)) : dash()),
          h("td", { class: "mono c-fn" }, r["function"] || dash()),
          h("td", { class: "c-lead" }, h("span", { class: "clamp" }, r.text || "")));
      }))));
  }

  function changedTable(rows) {
    return h("div", { class: "table-wrap" }, h("table", null,
      h("caption", { class: "sr-only" }, "Verdicts whose decision changed from run A to run B"),
      h("thead", null, h("tr", null, ["Decision, A to B", "CWE", "Location", "Function", "Lead"].map(function (t) {
        return h("th", { scope: "col" }, t);
      }))),
      h("tbody", null, rows.map(function (c) {
        const a = c.a || {}, b = c.b || {};
        const r = b.file || b.text ? b : a;
        return h("tr", null,
          h("td", { class: "nowrap" }, decisionBadge(a.decision), h("span", { class: "arrow", "aria-hidden": "true" }, " → "),
            h("span", { class: "sr-only" }, " changed to "), decisionBadge(b.decision)),
          h("td", { class: "mono nowrap" }, r.cwe || dash()),
          h("td", { class: "mono c-loc" }, rowWhere(r) ? pathText(rowWhere(r)) : dash()),
          h("td", { class: "mono c-fn" }, r["function"] || dash()),
          h("td", { class: "c-lead" }, h("span", { class: "clamp" }, r.text || "")));
      }))));
  }

  async function showCompare(p) {
    const seq = screen.seq;
    setTitle("Compare runs");
    showLoading("Compare runs", "Matching the two runs' verdicts" + ELLIPSIS);
    let d;
    try {
      d = await api("GET", "/api/compare?a=" + enc(p.a) + "&b=" + enc(p.b));
    } catch (e) {
      if (isLive(seq)) show(errorView("Compare runs", e));
      return;
    }
    if (!isLive(seq)) return;
    const appeared = Array.isArray(d.appeared) ? d.appeared : [];
    const vanished = Array.isArray(d.vanished) ? d.vanished : [];
    const changed = Array.isArray(d.changed) ? d.changed : [];
    const unchanged = isNum(d.unchanged) ? d.unchanged : 0;
    const section = function (title, n, body) {
      return h("section", { class: "report-section" }, h("h2", null, title + " (" + fmtInt(n) + ")"), body);
    };
    show(h("section", { class: "compare" },
      pageHead("Compare runs", "Verdicts are matched by file, CWE and function or line; one with no file or CWE is matched by its lead text.",
        [h("a", { class: "btn", href: compareHref(p.b, p.a) }, "Swap A and B")]),
      h("div", { class: "compare-cards" }, runCard("Run A", d.a || {}, p.a), runCard("Run B", d.b || {}, p.b)),
      d.same_repo === false ? notice("warn", "These runs scanned different repos, so few of their verdicts can match.") : null,
      h("p", { class: "compare-summary" },
        [fmtInt(appeared.length) + " appeared", fmtInt(vanished.length) + " vanished", fmtInt(changed.length) + " changed",
          fmtInt(unchanged) + " unchanged"].join(DOT)),
      section("Only in run B (appeared)", appeared.length, appeared.length ? compareTable(appeared, "Verdicts only in run B")
        : h("p", { class: "muted" }, "Nothing appeared: every verdict in run B is also in run A.")),
      section("Only in run A (vanished)", vanished.length, vanished.length ? compareTable(vanished, "Verdicts only in run A")
        : h("p", { class: "muted" }, "Nothing vanished: every verdict in run A is also in run B.")),
      section("Changed decision", changed.length, changed.length ? changedTable(changed)
        : h("p", { class: "muted" }, "No verdict changed its decision.")),
      h("p", { class: "muted" }, fmtInt(unchanged) + " unchanged")));
  }

  // Settings ─────────────────────────────────────────────────────────────────────────────────────────
  async function showSettings() {
    const seq = screen.seq;
    setTitle("Settings");
    showLoading("Settings", "Reading the model access settings" + ELLIPSIS);
    let access;
    try { access = await api("GET", "/api/access"); } catch (e) { if (isLive(seq)) show(errorView("Settings", e)); return; }
    if (isLive(seq)) renderSettings(access || {}, seq, false);
  }

  function accessStatus(a) {
    const rows = [
      ["Mode", modeLabel(a.mode)],
      ["Source", a.source === "env" ? "From environment" : a.source === "ui" ? "Set on this page" : "Default"],
    ];
    if (a.mode === "anthropic_key") rows.push(["Key", hintText(a.key_set, a.key_hint)]);
    if (a.mode === "endpoint" && a.endpoint) {
      const ep = a.endpoint;
      rows.push(["Base URL", ep.base_url || ""], ["Model", ep.model || ""],
        ["Token", hintText(ep.token_set, ep.token_hint, "none")],
        ["Sent as", ep.auth === "x-api-key" ? "x-api-key header" : "Authorization: Bearer header"]);
    }
    rows.push(["Kept", keptText(a)]);
    const notes = Array.isArray(a.notes) ? a.notes.filter(function (n) { return typeof n === "string" && n; }) : [];
    return [
      h("dl", { class: "kv" }, rows.map(function (r) { return [h("dt", null, r[0]), h("dd", null, r[1])]; })),
      notes.length ? h("ul", { class: "notes" }, notes.map(function (n) { return h("li", null, n); })) : null,
    ];
  }

  function renderSettings(access, seq, focusSave) {
    let mode = MODES.indexOf(access.mode) >= 0 ? access.mode : "login";
    const ep = access.endpoint || {};
    const field = function (id, label, input, help) {
      return h("div", { class: "field" }, h("label", { for: id }, label), input,
        help ? h("p", { class: "help", id: id + "-help" }, help) : null, fieldErrorEl(id));
    };
    const keyInput = h("input", {
      type: "password", id: "f-key", autocomplete: "off", spellcheck: "false", "aria-describedby": "f-key-help",
      placeholder: access.mode === "anthropic_key" && access.key_set ? "Type a new key to replace the current one" : "sk-ant-" + ELLIPSIS,
    });
    const urlInput = h("input", {
      type: "text", id: "f-base-url", value: ep.base_url || "", inputmode: "url", autocomplete: "off",
      spellcheck: "false", "aria-describedby": "f-base-url-help",
    });
    const tokenInput = h("input", {
      type: "password", id: "f-token", autocomplete: "off", spellcheck: "false", "aria-describedby": "f-token-help",
      placeholder: ep.token_set ? "Type a new token to replace the current one" : "",
    });
    const modelInput = h("input", {
      type: "text", id: "f-ep-model", value: ep.model || "", autocomplete: "off", spellcheck: "false",
      "aria-describedby": "f-ep-model-help",
    });
    const authSelect = h("select", { id: "f-auth" },
      h("option", { value: "bearer" }, "Bearer token (Authorization header)"),
      h("option", { value: "x-api-key" }, "API key (x-api-key header)"));
    authSelect.value = ep.auth === "x-api-key" ? "x-api-key" : "bearer";

    const keyBox = h("div", { class: "mode-fields" },
      field("f-key", "API key", keyInput, "Put in each scan's environment as ANTHROPIC_API_KEY, never on a command line, in a log or in a run folder. This page shows only its last four characters."));
    const endpointBox = h("div", { class: "mode-fields" },
      notice("warn", "Experimental. Your code is sent to this endpoint, and no scan through one has passed Orion's NodeGoat check yet, so treat its results as a trial."),
      field("f-base-url", "Base URL", urlInput, "An http or https address, for a gateway that speaks the Anthropic API or a local Ollama. No user name or password in it."),
      field("f-token", "Token (optional)", tokenInput, "A local Ollama needs none. Saving replaces the stored token, so leave this empty only when the endpoint takes no token."),
      field("f-ep-model", "Model", modelInput, "The model name as the endpoint knows it. Discovery, the verifier and its sub-agents all use it."),
      h("div", { class: "field field-narrow" }, h("label", { for: "f-auth" }, "Send the token as"), authSelect));

    function sync() {
      keyBox.hidden = mode !== "anthropic_key";
      endpointBox.hidden = mode !== "endpoint";
    }

    const saveBtn = h("button", { type: "submit", class: "btn btn-primary" }, "Save");
    const forgetBtn = h("button", { type: "button", class: "btn", disabled: access.source !== "ui", on: { click: forget } }, "Forget key");

    async function save(e) {
      e.preventDefault();
      clearFieldErrors(form);
      let body;
      if (mode === "login") {
        body = { mode: "login" };
      } else if (mode === "anthropic_key") {
        const key = keyInput.value.trim();
        if (!key) {
          setFieldError("f-key", access.mode === "anthropic_key" && access.key_set
            ? "Type a new key to replace the current one." : "Type your Anthropic API key.");
          keyInput.focus();
          return;
        }
        body = { mode: "anthropic_key", key: key };
      } else {
        const base = urlInput.value.trim(), model = modelInput.value.trim();
        let firstBad = null;
        if (!base) { setFieldError("f-base-url", "Enter the endpoint's base URL."); firstBad = urlInput; }
        if (!model) { setFieldError("f-ep-model", "Enter the model name the endpoint serves."); firstBad = firstBad || modelInput; }
        if (firstBad) { firstBad.focus(); return; }
        body = { mode: "endpoint", base_url: base, token: tokenInput.value.trim(), model: model, auth: authSelect.value };
      }
      saveBtn.disabled = true;
      try {
        const pub = await api("POST", "/api/access", body);
        keyInput.value = "";
        tokenInput.value = "";
        if (!isLive(seq)) return;
        if (app.state) app.state.access = pub;
        renderHeader();
        say("Saved. Scans you start from now on use " + modeLabel(pub && pub.mode) + ".", "ok", true);
        renderSettings(pub || {}, seq, true);
      } catch (err) {
        if (isLive(seq)) say(err.message, "error");
      } finally {
        body = null;
        saveBtn.disabled = false;
      }
    }

    async function forget() {
      forgetBtn.disabled = true;
      try {
        const pub = await api("DELETE", "/api/access");
        if (!isLive(seq)) return;
        if (app.state) app.state.access = pub;
        renderHeader();
        say("Forgot the saved key and settings. Scans now use " + modeLabel(pub && pub.mode) +
          (pub && pub.source === "env" ? " from the server's environment." : "."), "ok", true);
        renderSettings(pub || {}, seq, true);
      } catch (err) {
        if (isLive(seq)) { say(err.message, "error"); forgetBtn.disabled = false; }
      }
    }

    const form = h("form", { class: "form", novalidate: true, on: { submit: save } },
      pageHead("Settings", "Model access for the scans you start here. Discovery and the verifier always share one model."),
      h("fieldset", null, h("legend", null, "Model access"), MODES.map(function (m) {
        const id = "f-mode-" + m;
        return h("div", { class: "choice" },
          h("input", {
            type: "radio", name: "mode", id: id, value: m, checked: m === mode, "aria-describedby": id + "-help",
            on: { change: function () { mode = m; sync(); } },
          }),
          h("label", { for: id }, MODE_LABELS[m]),
          h("p", { class: "help", id: id + "-help" }, MODE_HELP[m]));
      })),
      keyBox, endpointBox,
      h("div", { class: "form-actions" }, saveBtn, forgetBtn,
        h("span", { class: "help" }, "Forget key deletes the saved key, token and endpoint settings from the keychain and from memory.")));

    show(h("div", { class: "settings" }, form,
      h("section", { class: "card" }, h("h2", null, "In use now"), accessStatus(access)),
      defaultModelCard()));
    sync();
    if (focusSave) saveBtn.focus();
  }

  // The model New scan preselects for a Claude scan (design §3: Settings holds a default model).
  function defaultModelCard() {
    const models = app.state && Array.isArray(app.state.models) && app.state.models.length
      ? app.state.models.map(String) : ["sonnet", "opus", "haiku"];
    const current = readDefaultModel();
    const isCustom = !!current && models.indexOf(current) < 0;
    const customInput = h("input", {
      type: "text", id: "f-default-custom", value: isCustom ? current : "", autocomplete: "off", spellcheck: "false",
      placeholder: "a model id the claude CLI accepts",
    });
    const customWrap = h("div", { class: "sub-field", hidden: !isCustom },
      h("label", { for: "f-default-custom" }, "Model name"), customInput, fieldErrorEl("f-default-custom"));
    const select = h("select", {
      id: "f-default-model", "aria-describedby": "f-default-model-help",
      on: {
        change: function (e) {
          customWrap.hidden = e.target.value !== CUSTOM_MODEL;
          if (!customWrap.hidden) customInput.focus();
        },
      },
    }, models.map(function (m) { return h("option", { value: m }, m); }), h("option", { value: CUSTOM_MODEL }, "Custom" + ELLIPSIS));
    select.value = isCustom ? CUSTOM_MODEL : (current || models[0]);

    function save(e) {
      e.preventDefault();
      clearFieldErrors(form);
      const m = select.value === CUSTOM_MODEL ? customInput.value.trim() : select.value;
      if (!validModelName(m)) {
        setFieldError("f-default-custom", "Enter a model name: up to 120 letters, digits or . _ : / @ [ ] -, not starting with -.");
        customInput.focus();
        return;
      }
      if (!storeDefaultModel(m)) {
        say("This browser does not let the page store settings, so the default model was not saved.", "error");
        return;
      }
      app.form.model = null;                  // New scan picks the new default the next time it opens
      app.form.custom = "";
      say("Saved. New Claude scans start with " + m + " selected.", "ok");
    }

    const form = h("form", { class: "form", novalidate: true, on: { submit: save } },
      h("h2", null, "Default model"),
      h("div", { class: "field field-narrow" },
        h("label", { for: "f-default-model" }, "Model for new Claude scans"), select,
        h("p", { class: "help", id: "f-default-model-help" },
          "Preselected on New scan when model access is the claude login or an Anthropic key; a scan through an endpoint always uses the endpoint's model. Kept in this browser only."),
        customWrap),
      h("div", { class: "form-actions" }, h("button", { type: "submit", class: "btn" }, "Save default")));
    return h("section", { class: "card" }, form);
  }

  // ── Boot ──────────────────────────────────────────────────────────────────────────────────────────
  function boot() {
    app.token = initToken();
    const skip = document.querySelector(".skip");
    if (skip) {
      skip.addEventListener("click", function (e) {
        e.preventDefault();   // "#main" is not a route: move focus without touching the hash
        const main = byId("main");
        if (main) main.focus();
      });
    }
    window.addEventListener("hashchange", route);
    if (app.token) ensureState().catch(function () { /* the screen shows the error */ });
    route();
    stateLoop();
  }

  const exported = {
    formatEvent: formatEvent, usageOf: usageOf, sumUsage: sumUsage, tokensText: tokensText, costText: costText,
    newTally: newTally, tallyEvent: tallyEvent, phaseList: phaseList, phaseStates: phaseStates,
    phaseStateText: phaseStateText, verifyCounts: verifyCounts, verifyProgress: verifyProgress,
    progressText: progressText, effectiveLocation: effectiveLocation, whereText: whereText,
    sortVerdicts: sortVerdicts, countDecisions: countDecisions, parseRoute: parseRoute, runHref: runHref,
    compareHref: compareHref, appliesTo: appliesTo, choiceDefault: choiceDefault, collectOptions: collectOptions,
    accessSummary: accessSummary, accessLine: accessLine, keptText: keptText, hintText: hintText,
    fmtInt: fmtInt, fmtElapsed: fmtElapsed, fmtTime: fmtTime, basename: basename, safeHref: safeHref, h: h,
    pathParts: pathParts, validModelName: validModelName,
    DECISIONS: DECISIONS, PHASES: PHASES, RUNTIME_WARNING: RUNTIME_WARNING,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = exported;
  if (typeof window !== "undefined" && typeof document !== "undefined" && document.getElementById("view")) {
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
    else boot();
  }
})();
