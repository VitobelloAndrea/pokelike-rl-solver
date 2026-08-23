// ============================================================================
// M7 -- the JavaScript side of the interactive cross-runtime sweep.
//
//   node sweep-host.js
//
// Speaks line-delimited JSON on stdin/stdout. One request object per line in,
// one response object per line out, in order. This is what lets `sweep.py`
// hold BOTH runtimes at the same step and compare their normalized legal
// action sets BEFORE either side commits to an action -- something the
// pre-M7 `run-scenario.js` cannot do, because it replays a pinned action list
// and runs to completion in one shot.
//
// Requests:
//   {"op":"reset","config":{...}}   start a fresh run (see `configToScenario`)
//   {"op":"legal"}                  -> {screen, actions:[normalized...]}
//   {"op":"state","battles_seen":N} -> {checkpoint, battles, rng_draws_total}
//   {"op":"apply","action":{...}}   execute through the real source handler
//   {"op":"quit"}
//
// EVERYTHING about the sandbox, the prefix, the stubs and the normalization is
// the existing, already-audited machinery:
//   * the sandbox comes from `sandbox.js`, extracted verbatim from
//     `run-scenario.js` (see that file's header);
//   * the prefix and its round-counter instrumentation are read and applied
//     exactly as `run-scenario.js` does;
//   * `driver.js` is loaded unchanged and enters its additive `SC.sweep`
//     branch, handing `sweep-adapter.js` its own audited helpers.
//
// There is deliberately NO second source-prefix mechanism: this host reads the
// same `out/route-prefix.js` that `compare.py` re-extracts and hash-checks.
// ============================================================================

'use strict';

const fs = require('fs');
const path = require('path');
const readline = require('readline');
const vm = require('vm');
const { makeSandbox } = require('./sandbox.js');

const SWEEP_PROTOCOL_VERSION = 1;
const SCHEMA_VERSION = 2;

const prefixPath = path.join(__dirname, 'out', 'route-prefix.js');
if (!fs.existsSync(prefixPath)) {
  console.error(
    `missing ${prefixPath} -- run: node route-oracle/extract-prefix.js ` +
      'pokelike_forked/js/bundle.deobfuscated.js route-oracle/out/route-prefix.js',
  );
  process.exit(1);
}
const rawPrefix = fs.readFileSync(prefixPath, 'utf8');

// Identical assertion-guarded round-counter edit to `run-scenario.js`'s. Kept
// literally the same needle so the two runners instrument the same site; if
// the source moves, BOTH fail loudly rather than one silently reporting zero.
const ROUND_COUNTER_NEEDLE = '(BI4++,\n      BI4 === BI6 + 0x1 &&';
const roundNeedleCount = rawPrefix.split(ROUND_COUNTER_NEEDLE).length - 1;
if (roundNeedleCount !== 1) {
  console.error(`round-counter instrumentation expected one source match, found ${roundNeedleCount}`);
  process.exit(1);
}
const prefix = rawPrefix.replace(
  ROUND_COUNTER_NEEDLE,
  '(BI4++, globalThis.__ROUND_COUNT__ = BI4, globalThis.__ROUND_MARKS__.push(BcM.length),\n      BI4 === BI6 + 0x1 &&',
);

const DECODER_STUBS = 'var k = function(){}, K = function(){ return []; };\n';
const DRIVER = fs.readFileSync(path.join(__dirname, 'driver.js'), 'utf8');
const ADAPTER = fs.readFileSync(path.join(__dirname, 'sweep-adapter.js'), 'utf8');

// ---------------------------------------------------------------------------
// One episode = one fresh sandbox
// ---------------------------------------------------------------------------
// A run cannot be reset in place: `startNewRun` does not undo the module-level
// state the prefix builds at load, and reusing a context across episodes would
// let one episode's leftovers (localStorage, the Pokedex cache, the RNG
// binding) reach the next. A fresh context per episode is the only way an
// episode digest can be independent of batch order, which is an explicit M7
// acceptance gate.
let sandbox = null;

function configToScenario(config) {
  // The sweep's episode config is deliberately the SAME shape the frozen
  // scenarios use for everything the source reads (`seed`, `mode`), so the two
  // corpora are configured identically and no second config dialect exists.
  // `actions`/`starter_index` are absent by construction: in sweep mode the
  // driver never reaches the fixed-scenario loop.
  return {
    schema_version: SCHEMA_VERSION,
    scenario: config.scenario || 'sweep',
    seed: config.seed >>> 0,
    mode: {
      nuzlocke: !!(config.mode && config.mode.nuzlocke),
      gen2: !!(config.mode && config.mode.gen2),
      gen3: !!(config.mode && config.mode.gen3),
      gen4: !!(config.mode && config.mode.gen4),
    },
    sweep: true,
    actions: [],
  };
}

function resetSandbox(config) {
  sandbox = makeSandbox();
  sandbox.__SCENARIO__ = configToScenario(config);
  // The sweep service loop has to yield with a real MACROTASK so this host's
  // own `setImmediate` pump gets to run; a microtask spin inside the sandbox
  // deadlocks the protocol. `setImmediate` is deliberately not part of the
  // shared sandbox (nothing in the source uses it, and adding it there would
  // change `run-scenario.js`'s environment), so it is injected here, for the
  // sweep host only. It is not reachable from any source code path -- only
  // `sweep-adapter.js`'s service loop names it.
  sandbox.__SWEEP_YIELD__ = () => new Promise((r) => setImmediate(r));
  vm.createContext(sandbox);
  vm.runInContext(DECODER_STUBS + prefix + '\n' + DRIVER + '\n' + ADAPTER, sandbox, {
    timeout: 300000,
  });
}

async function pumpUntil(predicate, what) {
  for (let i = 0; i < 2000000; i++) {
    if (predicate()) return;
    if (sandbox.__FATAL__) throw new Error('sandbox fatal:\n' + sandbox.__FATAL__);
    await new Promise((r) => setImmediate(r));
  }
  throw new Error('sweep host timed out waiting for ' + what);
}

async function call(req) {
  sandbox.__SWEEP_RESP__ = null;
  sandbox.__SWEEP_REQ__ = req;
  await pumpUntil(() => sandbox.__SWEEP_RESP__ !== null && sandbox.__SWEEP_RESP__ !== undefined, req.op);
  const resp = sandbox.__SWEEP_RESP__;
  sandbox.__SWEEP_RESP__ = null;
  if (!resp.ok) throw new Error(resp.error);
  return resp.value;
}

// ---------------------------------------------------------------------------
// stdio service
// ---------------------------------------------------------------------------
const rl = readline.createInterface({ input: process.stdin, terminal: false });
const queue = [];
let draining = false;

function reply(obj) {
  process.stdout.write(JSON.stringify(obj) + '\n');
}

async function handleLine(line) {
  let req;
  try {
    req = JSON.parse(line);
  } catch (e) {
    reply({ ok: false, error: 'bad JSON: ' + String(e) });
    return;
  }
  try {
    if (req.op === 'hello') {
      reply({ ok: true, value: { protocol: SWEEP_PROTOCOL_VERSION, schema_version: SCHEMA_VERSION } });
      return;
    }
    if (req.op === 'reset') {
      resetSandbox(req.config || {});
      // The driver runs `startNewRun` + the real starter offer and only THEN
      // enters the sweep service, so readiness is the signal that the source
      // has genuinely reached the starter screen.
      await pumpUntil(() => sandbox.__SWEEP_READY__ || sandbox.__DONE__ || sandbox.__FATAL__, 'reset');
      if (sandbox.__FATAL__) throw new Error('sandbox fatal:\n' + sandbox.__FATAL__);
      if (!sandbox.__SWEEP_READY__) {
        // The driver's own try/catch caught something before the service
        // started; surface its recorded error rather than a bare timeout.
        const out = sandbox.__RESULT__ ? JSON.parse(sandbox.__RESULT__) : null;
        throw new Error('driver never reached the sweep service: ' + (out && out.error));
      }
      reply({ ok: true, value: { ready: true } });
      return;
    }
    if (!sandbox) throw new Error('no episode: send reset first');
    if (req.op === 'quit') {
      await call({ op: 'quit' });
      reply({ ok: true, value: { bye: true } });
      rl.close();
      return;
    }
    reply({ ok: true, value: await call(req) });
  } catch (e) {
    reply({ ok: false, error: (e && e.stack) || String(e) });
  }
}

let stdinEnded = false;

async function drain() {
  if (draining) return;
  draining = true;
  while (queue.length) {
    await handleLine(queue.shift());
  }
  draining = false;
  // Only NOW may the process end. Exiting straight out of the `close` event
  // kills any request still in flight: a piped `printf ... | node
  // sweep-host.js` closes stdin the instant the last line is written, which
  // silently dropped every response after the first await (observed while
  // bringing the protocol up -- `legal` never answered).
  if (stdinEnded) process.exit(0);
}

rl.on('line', (line) => {
  if (!line.trim()) return;
  queue.push(line);
  drain();
});

rl.on('close', () => {
  stdinEnded = true;
  if (!draining && !queue.length) process.exit(0);
});
