// In-subprocess Node bootstrap for a harness SCRIPT: run it under the inspector and dump
//   <covDir>/orion-<pid>.json         -- PRECISE coverage, the same {result:[...]} shape
//                                        NODE_V8_COVERAGE writes (exact call + block counts)
//   <profDir>/orion-<pid>.cpuprofile  -- the CPU profile (caller->callee frames, sampled)
// so v8_tracer.collect parses a harness run and a live server with ONE code path.
//
// Invoked as `node _boot_js.js <driver> <root> <covDir> <profDir>` inside the sandboxed child
// (Orion never runs target JS in its own process). The driver's synchronous part runs inside
// require(); a returned/exported promise is awaited, then the loop gets ORION_JS_SETTLE_MS to drain
// timers/IO so async handlers are observed too. Dumps are written in `finally`, so a throwing
// driver still yields its partial trace.

'use strict';
const inspector = require('inspector');
const path = require('path');
const fs = require('fs');

const [, , driver, , covDir, profDir] = process.argv;
const settleMs = parseInt(process.env.ORION_JS_SETTLE_MS || '500', 10);

const session = new inspector.Session();
session.connect();
const post = (method, params) => new Promise((resolve, reject) =>
  session.post(method, params || {}, (err, res) => (err ? reject(err) : resolve(res))));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function main() {
  let coverage = { result: [] };
  let profile = null;
  try {
    await post('Profiler.enable');
    await post('Profiler.startPreciseCoverage', { callCount: true, detailed: true });
    await post('Profiler.setSamplingInterval', { interval: 50 });  // microseconds
    await post('Profiler.start');
    try {
      const exported = require(path.resolve(driver));
      if (exported && typeof exported.then === 'function') await exported;
    } catch (e) {
      // driver failures are expected; whatever ran is still observed
    }
    if (settleMs > 0) await sleep(settleMs);
    ({ profile } = await post('Profiler.stop'));
    coverage = await post('Profiler.takePreciseCoverage');
  } catch (e) {
    // an inspector failure yields an empty-but-valid trace, never a crash
  } finally {
    try { fs.writeFileSync(path.join(covDir, `orion-${process.pid}.json`), JSON.stringify(coverage)); } catch (e) { /* ignore */ }
    if (profile) {
      try { fs.writeFileSync(path.join(profDir, `orion-${process.pid}.cpuprofile`), JSON.stringify(profile)); } catch (e) { /* ignore */ }
    }
    session.disconnect();
    process.exit(0);
  }
}

main();
