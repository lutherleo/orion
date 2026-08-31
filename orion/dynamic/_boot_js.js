// In-subprocess Node bootstrap: run a driver under the V8 CPU profiler, emit the ObservedTrace wire
// JSON. The Node analogue of _boot_py.py — invoked as `node _boot_js.js <driver> <root> <out>` inside
// the sandboxed child (Orion never runs target JS in its own process).
//
// Why the CPU profiler and not per-call instrumentation: Node has no cheap, dependency-free hook for
// "every caller->callee with file:line". The built-in inspector CPU profile gives exactly that — a
// call tree whose nodes carry callFrame {functionName, url, lineNumber} and whose parent->child edges
// are real observed calls. It is SAMPLED, so the JS trace is a lower bound (matching the delta's
// stated semantics); a function exercised in a loop is reliably captured. Attribution (url+line) is
// exact for whatever is sampled. Dispatch detection is left to a later CDP upgrade (design §5).
//
// Scope: only callFrames whose url resolves under `root` are emitted, so stdlib/node_internal frames
// are dropped — the same target-scoping the Python tracer applies.

'use strict';
const inspector = require('inspector');
const path = require('path');
const fs = require('fs');

const [, , driver, rootArg, outPath] = process.argv;
const root = path.resolve(rootArg);

function urlToPath(url) {
  if (!url) return null;
  if (url.startsWith('file://')) {
    try { return require('url').fileURLToPath(url); } catch (e) { return null; }
  }
  // Some frames carry a bare path or a non-file scheme (e.g. "node:internal/...") — keep only real paths.
  if (url.startsWith('node:') || url.indexOf('://') !== -1) return null;
  return url;
}

function underRoot(file) {
  if (!file) return false;
  const p = path.resolve(file);
  return p === root || p.startsWith(root + path.sep);
}

function fname(cf) {
  return (cf.functionName && cf.functionName.length) ? cf.functionName : '<anonymous>';
}

const session = new inspector.Session();
session.connect();

function post(method, params) {
  return new Promise((resolve, reject) =>
    session.post(method, params || {}, (err, res) => (err ? reject(err) : resolve(res))));
}

function convert(profile) {
  // profile.nodes: [{id, callFrame:{functionName,url,lineNumber(0-based),columnNumber}, children:[id]}]
  const byId = new Map();
  for (const n of profile.nodes) byId.set(n.id, n);

  const calls = new Map();    // key -> row (dedup)
  const methods = new Map();  // key -> [name,file,line]

  const loc = (n) => {
    const cf = n.callFrame;
    const file = urlToPath(cf.url);
    if (!underRoot(file)) return null;
    const relFile = path.relative(root, file).split(path.sep).join('/');
    return { name: fname(cf), file: relFile, line: (cf.lineNumber | 0) + 1 };  // 0-based -> 1-based
  };

  for (const n of profile.nodes) {
    const parent = loc(n);
    if (parent) methods.set(`${parent.file}:${parent.line}:${parent.name}`, [parent.name, parent.file, parent.line]);
    if (!n.children) continue;
    for (const cid of n.children) {
      const child = byId.get(cid);
      if (!child) continue;
      const c = loc(child);
      if (!c) continue;
      methods.set(`${c.file}:${c.line}:${c.name}`, [c.name, c.file, c.line]);
      if (parent) {
        const key = `${parent.file}|${parent.line}|${parent.name}|${c.file}|${c.line}|${c.name}`;
        if (!calls.has(key)) calls.set(key, [parent.file, parent.line, parent.name, c.file, c.line, c.name]);
      }
    }
  }
  return { calls: [...calls.values()], dispatches: [], methods: [...methods.values()] };
}

async function main() {
  let wire = { calls: [], dispatches: [], methods: [] };
  try {
    await post('Profiler.enable');
    await post('Profiler.setSamplingInterval', { interval: 20 });  // microseconds — fine-grained
    await post('Profiler.start');
    try {
      require(path.resolve(driver));            // run the driver (synchronous portion)
    } catch (e) {
      // driver failures are expected; we still emit whatever was profiled
    }
    const { profile } = await post('Profiler.stop');
    wire = convert(profile);
  } catch (e) {
    // any inspector failure -> empty-but-valid trace, never a crash
  } finally {
    try { fs.writeFileSync(outPath, JSON.stringify(wire)); } catch (e) { /* ignore */ }
    session.disconnect();
  }
}

main();
