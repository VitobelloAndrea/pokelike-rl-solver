// Load-time side-effect audit for the route-prefix slice.
//
// Usage:
//   node route-oracle/scan-toplevel-danger.js <prefix.js> [--allowlist <file>] [--json]
//
// Exit codes:
//   0  every load-reachable risky reference is PROVED guarded, or is covered
//      by an exact allow-list entry pinned to this prefix's sha256;
//   1  at least one load-reachable risky reference is unknown or unguarded;
//   2  usage error / the parser dependency is missing.
//
// ---------------------------------------------------------------------------
// WHY THIS EXISTS, AND WHAT THE PREVIOUS VERSION GOT WRONG
// ---------------------------------------------------------------------------
//
// The previous version descended into immediately-invoked function expressions
// (correct, and an improvement over tools/battle-oracle's scanner, which stops
// at any function body) but then treated *every* IIFE hit as safe: its
// `unexpected` filter kept only `reach === 'top'`. That made the gate blind by
// construction. Reproduced in M3.3 against this very prefix and against
// adversarial fixtures:
//
//   * a bare `(function(){ fetch("https://…"); })();` was reported and the
//     scanner still exited 0;
//   * `document.addEventListener("pointerdown"/"pointerover", …)` at prefix
//     lines 74780 / 74799 is real, load-time, completely UNguarded DOM wiring
//     that sits OUTSIDE the audited 63600-63900 window and was waved through;
//   * the two `localStorage` references at prefix lines 47541 / 47548 are not
//     `typeof`-guarded at all -- they are inside `try { … } catch {}` -- so the
//     old scanner's own success message ("Remaining hits are inside top-level
//     IIFEs that are themselves gated on `typeof <global> == \"undefined\"`")
//     was factually false about this bundle.
//
// So an IIFE is no longer evidence of anything. Every load-reachable reference
// must now earn its pass in one of exactly two ways:
//
//   1. GUARD ANALYSIS (computed here, never asserted): the reference is
//      dominated by a real `typeof X === "undefined"` early return, or is
//      inside the consequent of a real `typeof X !== "undefined"` test. Both
//      make the code inert when the global is simply not defined on the
//      sandbox, which is exactly how run-scenario.js is configured.
//   2. An exact ALLOW-LIST entry (`toplevel-allowlist.json`) naming the
//      identifier, line and reach, carrying a written reason, and pinned to
//      the prefix sha256 it was audited against. A changed prefix invalidates
//      the whole allow-list rather than silently carrying stale approvals.
//
// Anything else exits nonzero.
//
// Dependency: `acorn`, pinned in route-oracle/package.json + package-lock.json.
// Bootstrap from a fresh checkout:  npm --prefix route-oracle ci
// (`route-oracle/node_modules/` is git-ignored, the lockfile is tracked.) The
// old fallback onto `tools/extract-data/node_modules` was REMOVED: `tools/` is
// git-ignored wholesale, so depending on it made this audit step unreproducible
// from a fresh checkout.

'use strict';

const crypto = require('crypto');
const fs = require('fs');
const path = require('path');

// ---------------------------------------------------------------------------
// dependency
// ---------------------------------------------------------------------------

let acorn;
for (const candidate of [path.join(__dirname, 'node_modules', 'acorn'), 'acorn']) {
  try {
    acorn = require(candidate);
    break;
  } catch {
    /* try the next candidate */
  }
}
if (!acorn) {
  console.error(
    'acorn is not available. Bootstrap it with:\n  npm --prefix route-oracle ci\n' +
      '(exact version pinned in route-oracle/package-lock.json)',
  );
  process.exit(2);
}

// ---------------------------------------------------------------------------
// arguments
// ---------------------------------------------------------------------------

const argv = process.argv.slice(2);
let inPath = null;
let allowPath = path.join(__dirname, 'toplevel-allowlist.json');
let asJson = false;
for (let i = 0; i < argv.length; i++) {
  if (argv[i] === '--allowlist') allowPath = argv[++i];
  else if (argv[i] === '--json') asJson = true;
  else if (argv[i] === '--no-allowlist') allowPath = null;
  else if (!inPath) inPath = argv[i];
  else {
    console.error(`unexpected argument ${argv[i]}`);
    process.exit(2);
  }
}
if (!inPath) {
  console.error('Usage: node scan-toplevel-danger.js <prefix.js> [--allowlist <file>] [--json]');
  process.exit(2);
}

const src = fs.readFileSync(inPath);
const srcSha = crypto.createHash('sha256').update(src).digest('hex');
const text = src.toString('utf8');
const ast = acorn.parse(text, { ecmaVersion: 'latest', sourceType: 'script', locations: true });

// ---------------------------------------------------------------------------
// what counts as risky
// ---------------------------------------------------------------------------

// Host objects whose mere use at load is a side effect or an environment
// dependency. Flagged wherever the NAME appears in a reference position: as a
// bare identifier (`fetch(…)`), as a dotted property (`globalThis.fetch`), or
// as a computed one (`window["fetch"]`) -- all three are the same reference.
const RISKY = new Set([
  'document', 'localStorage', 'sessionStorage', 'navigator', 'fetch',
  'XMLHttpRequest', 'Audio', 'WebSocket', 'indexedDB', 'Notification',
  'geolocation', 'Worker', 'EventSource',
]);

// Deferred execution and event wiring (M3.4 Defect C, probe 2). Scheduling
// work at load IS a load-time side effect even though the callback body runs
// later: it escapes this scanner's reach, it can outlive the run, and in a
// sandbox that never advances timers it silently does nothing at all -- which
// is worse, not better, because the divergence from a real browser becomes
// invisible. The audit brief named "timers, event wiring" explicitly; before
// M3.5 they were not in the risky set at all.
//
// These are kept SEPARATE from `RISKY` because they are only interesting as
// GLOBAL calls. `document.addEventListener(…)` is already fully accounted for
// by the `document` reference -- flagging its property as well would double-
// count it and, worse, report `typeof document !== "undefined" &&
// document.addEventListener(…)` as unguarded, since the guard proves
// `document` and not the method name. So these are flagged only as a bare
// identifier or as a property of an explicit global root.
const RISKY_SCHEDULERS = new Set([
  'setTimeout', 'setInterval', 'setImmediate', 'queueMicrotask',
  'requestAnimationFrame', 'requestIdleCallback',
  'addEventListener', 'removeEventListener', 'dispatchEvent',
  'postMessage', 'BroadcastChannel',
]);

const GLOBAL_ROOTS = new Set(['window', 'globalThis', 'self', 'top', 'parent', 'frames']);

// The canonical JavaScript property-key STRING for a statically known primitive
// key expression, or null when the key cannot be resolved without evaluating
// runtime state.
//
// M3.10 defect 3: this rule used to exist twice, in two different strengths.
// The definition side (`propertyKeyName`) accepted a string OR a number literal
// and stringified it; the access side (`staticPropertyName`) accepted only
// `typeof value === 'string'`. So `({ get 0() { return fetch("/x"); } })[0]`
// resolved to no accessor at all and the scanner reported nothing -- a blind
// gate, the same class of error as M3.8's defects 1 and 3. `ToPropertyKey(0)`
// is `"0"`, so `[0]` and `["0"]` and a `get 0()` and a `get "0"()` are all one
// single property, and one rule must decide that for both sides.
//
// `String(n)` is exactly ECMAScript `ToString(Number)`, which is what
// `ToPropertyKey` applies to a numeric key: `String(0) === "0"`,
// `String(1.5) === "1.5"`, `String(1e21) === "1e+21"`. The same conversion
// handles overflowed numeric literals (`1e999` -> `"Infinity"`) and BigInts
// (`1n` -> `"1"`). A unary numeric spelling is still static: `-0` becomes
// `"0"`. A no-substitution template literal is a static string too.
//
// Deliberately NOT broadened into general `ToPropertyKey` evaluation. A
// genuinely dynamic key (`o[k]`), a template with substitutions and every
// expression whose value depends on runtime state are still declined. Their key
// expressions are walked normally, but no accessor is resolved against them.
function literalKeyString(node) {
  if (!node) return null;
  if (node.type === 'Literal') {
    if (typeof node.value === 'string') return node.value;
    if (typeof node.value === 'number' || typeof node.value === 'bigint') {
      return String(node.value);
    }
    return null;
  }
  if (
    node.type === 'UnaryExpression' &&
    (node.operator === '-' || node.operator === '+') &&
    node.argument &&
    node.argument.type === 'Literal' &&
    typeof node.argument.value === 'number'
  ) {
    const value = node.operator === '-' ? -node.argument.value : +node.argument.value;
    return String(value);
  }
  if (
    node.type === 'TemplateLiteral' &&
    (node.expressions || []).length === 0 &&
    (node.quasis || []).length === 1 &&
    typeof node.quasis[0].value.cooked === 'string'
  ) {
    return node.quasis[0].value.cooked;
  }
  return null;
}

// The property a member expression names, for both `a.b` and `a["b"]` / `a[0]`.
// Returns null for a genuinely dynamic key (`a[k]`), which cannot be resolved
// statically and is left to the ordinary walk of the key expression.
function staticPropertyName(node) {
  if (!node.computed) {
    return node.property && node.property.type === 'Identifier' ? node.property.name : null;
  }
  return literalKeyString(node.property);
}

const FUNCTION_TYPES = new Set([
  'FunctionDeclaration', 'FunctionExpression', 'ArrowFunctionExpression',
]);

// The name a Property/PropertyDefinition/MethodDefinition DEFINES, for the two
// spellings that are statically known: a plain identifier key and a literal
// key. Returns null for a computed key whose value cannot be resolved without
// evaluating the expression.
//
// Shares `literalKeyString` with `staticPropertyName`, so the definition side
// and the access side canonicalise identically -- see M3.10 defect 3 there.
function propertyKeyName(node) {
  if (!node || !node.key) return null;
  if (node.computed) return literalKeyString(node.key);
  if (node.key.type === 'Identifier') return node.key.name;
  return literalKeyString(node.key);
}

// The property DESCRIPTOR a DIRECT object literal establishes for `name`:
// `{ get, set }`, each half a function node or null, or null when the literal
// defines no statically resolvable property of that name at all.
//
// M3.9 defect 3 established the connection at all: `({ get value() { return
// fetch("/x"); } }).value` invokes the getter at load, so its body is
// load-reachable, and before M3.9 the scanner marked every accessor body
// non-reachable and never linked the member access back to the literal.
//
// M3.10 defect 1: the M3.9 version implemented "a later definition wins" only
// WITHIN one accessor kind. It scanned for `prop.kind !== kind` and skipped
// everything else, so a later `value: 1` data property never cleared an earlier
// getter and `({ get value() { return fetch("/loses"); }, value: 1 }).value`
// was reported as a load-time fetch. In real JavaScript the data property
// replaces the whole accessor descriptor and the getter never runs. The
// function's own comment asserted the rule the code did not implement, and the
// M3.9 corpus contained no literal defining one name twice, so nothing caught
// it.
//
// The repair is a real source-order descriptor fold over the matching direct
// properties, which is what an object literal actually performs:
//
//   * a data property or an ordinary method establishes a DATA descriptor and
//     clears BOTH halves;
//   * a getter establishes/replaces the getter and PRESERVES any live setter;
//   * a setter establishes/replaces the setter and PRESERVES any live getter;
//   * going from a data descriptor to an accessor one starts the other half
//     absent -- which is why `({ get v(){…}, v: 1, set v(x){…} }).v` runs
//     nothing on a read: the data property cleared the getter and the later
//     setter does not bring it back;
//   * later definitions win, in source order.
//
// SPREAD POLICY. A spread element is skipped, and that is safe in one direction
// only -- which is the direction that matters for a gate. Object spread is
// CopyDataProperties, which uses CreateDataProperty: it can only ever define
// DATA properties on the target. So a spread can CLEAR an accessor established
// earlier in the literal (`{ get v(){…}, ...o }` yields a data `v` when `o` has
// a `v`), but it can never INTRODUCE one that this fold cannot see. Skipping it
// therefore risks reporting an accessor body that a spread would have replaced
// -- an over-report -- and never risks missing one. It is not certainty about
// the descriptor, it is the deliberately conservative reading of an
// unresolvable one, and it is pinned by fixtures in both directions.
//
// Only an ObjectExpression sitting literally in the member expression's OBJECT
// position is resolved. `const o = { get value() {…} }; o.value` would require
// following a value through a binding, which the M3 convergence boundary places
// outside this scanner's contract; it is a declared limit, not an oversight.
function literalDescriptor(objectNode, name) {
  if (!objectNode || objectNode.type !== 'ObjectExpression' || name === null) return null;
  let get = null;
  let set = null;
  let defined = false;
  for (const prop of objectNode.properties || []) {
    // A SpreadElement carries no static key -- see the SPREAD POLICY above.
    if (!prop || prop.type !== 'Property') continue;
    if (propertyKeyName(prop) !== name) continue;
    defined = true;
    const fn = prop.value && FUNCTION_TYPES.has(prop.value.type) ? prop.value : null;
    if (prop.kind === 'get') {
      get = fn; // the setter half, if any, survives
    } else if (prop.kind === 'set') {
      set = fn; // the getter half, if any, survives
    } else {
      // A data property or an ordinary method: a data descriptor has no
      // accessor half at all, in either direction.
      get = null;
      set = null;
    }
  }
  return defined ? { get, set } : null;
}

// Object spread does two separate things, both of which matter here:
//
//   1. evaluate the source expression;
//   2. CopyDataProperties reads every enumerable own property from that source
//      before defining a DATA property on the target.
//
// M3.12 found that the descriptor discussion above covered only the target
// side. A DIRECT source literal can itself finish with a live getter, and the
// spread's `Get` invokes it immediately:
//
//   ({ ...{ get value() { return fetch("/copy"); } } });
//
// Merely walking the inner ObjectExpression treats the getter body as deferred,
// so the pre-repair scanner reported zero hits. Resolve each statically named
// final descriptor and walk its live getter. A dynamic computed getter is
// walked conservatively: a later runtime-equal property might replace it, so
// this can over-report, but it cannot hide the getter invocation.
//
// This deliberately does not follow a source through a binding. `{ ...source }`
// would require value/alias analysis, outside the declared scanner boundary.
function walkDirectSpreadGetters(sourceNode, ctx) {
  if (!sourceNode || sourceNode.type !== 'ObjectExpression') return;
  const staticNames = new Set();
  const dynamicGetters = [];
  for (const prop of sourceNode.properties || []) {
    if (!prop || prop.type !== 'Property') continue;
    const name = propertyKeyName(prop);
    if (name !== null) {
      staticNames.add(name);
      continue;
    }
    const fn = prop.value && FUNCTION_TYPES.has(prop.value.type) ? prop.value : null;
    if (prop.kind === 'get' && fn) dynamicGetters.push(fn);
  }
  for (const name of staticNames) {
    const desc = literalDescriptor(sourceNode, name);
    if (desc && desc.get) walk(desc.get.body, ctx);
  }
  for (const getter of dynamicGetters) walk(getter.body, ctx);
}

// ---------------------------------------------------------------------------
// reference positions (M3.6 defect)
// ---------------------------------------------------------------------------
//
// A risky NAME is only interesting where it is actually READ. The walk used to
// flag every `Identifier` node it reached, with no notion of the syntactic slot
// the token sat in, so
//
//   const emitter = { addEventListener() {} };
//
// was reported as an unguarded global scheduler on line 1 -- even though the
// program neither references nor invokes any global. That contradicted this
// file's own contract (schedulers count "only as a bare identifier or as a
// property of an explicit global root"): an object-literal method key is a
// property NAME, exactly the case the `emitter.addEventListener(…)` rule
// already declined to flag.
//
// The fix is structural, not a name/line exception: the walk now visits only
// identifier positions that denote a read. Four slots are names or bindings,
// never reads --
//
//   * non-computed property keys      `{ addEventListener() {} }`, `{ a: 1 }`,
//                                     `class E { addEventListener() {} }`
//   * declaration binding identifiers `const setTimeout = …`, `function fetch(){}`,
//                                     `class document {}`, and parameter names
//   * destructuring binding names     `const { setTimeout } = …`
//   * statement labels                `setTimeout: for (…) …`
//
// -- and two easily-confused slots ARE reads, so they stay visible:
//
//   * a SHORTHAND property (`{ setTimeout }`) reads the identifier. acorn emits
//     the key and the value as two distinct Identifier nodes at the same source
//     offset; skipping only the key and always walking the value keeps exactly
//     one hit, at the right line.
//   * a COMPUTED property key (`{ [setTimeout]: h }`, `const { [fetch]: x } = o`)
//     evaluates the identifier, so it is walked as an ordinary reference.
//
// Binding positions are handled by `walkPattern`, which descends through
// destructuring so that the parts of a pattern that really do evaluate --
// computed keys and default-value expressions -- are still walked as
// references, while the names being bound are not.

// Node types whose `key` is a property NAME when `computed` is false.
//
// Since M3.9 the two class-member types are normally consumed by `walkClass`,
// which needs to split on `static` and on whether the class is constructed.
// They are kept in this set as a backstop: if a `PropertyDefinition` or
// `MethodDefinition` were ever reached through some other path, its
// non-computed key must still not be counted as a reference.
const KEYED_MEMBERS = new Set(['Property', 'PropertyDefinition', 'MethodDefinition']);

const CLASS_TYPES = new Set(['ClassDeclaration', 'ClassExpression']);

// ---------------------------------------------------------------------------
// guard recognition
// ---------------------------------------------------------------------------

// `typeof X` where X is a bare identifier -> "X", else null.
function typeofOperand(node) {
  if (node && node.type === 'UnaryExpression' && node.operator === 'typeof' &&
      node.argument && node.argument.type === 'Identifier') {
    return node.argument.name;
  }
  return null;
}

function isUndefinedLiteral(node) {
  return node && node.type === 'Literal' && node.value === 'undefined';
}

// Recognises `typeof X == "undefined"` / `===` (polarity true) and
// `typeof X != "undefined"` / `!==` (polarity false). Returns {name, equalsUndefined}.
function typeofTest(node) {
  if (!node || node.type !== 'BinaryExpression') return null;
  const eq = node.operator === '==' || node.operator === '===';
  const ne = node.operator === '!=' || node.operator === '!==';
  if (!eq && !ne) return null;
  let name = typeofOperand(node.left);
  let other = node.right;
  if (name === null) {
    name = typeofOperand(node.right);
    other = node.left;
  }
  if (name === null || !isUndefinedLiteral(other)) return null;
  return { name, equalsUndefined: eq };
}

// Does this statement unconditionally leave the enclosing function body?
function isAbrupt(node) {
  if (!node) return false;
  if (node.type === 'ReturnStatement' || node.type === 'ThrowStatement') return true;
  if (node.type === 'BlockStatement') {
    return node.body.some(isAbrupt);
  }
  return false;
}

// ---------------------------------------------------------------------------
// walk
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// invocation analysis (M3.9 defect 1)
// ---------------------------------------------------------------------------
//
// An IIFE does not only run its BODY at load: it also evaluates every parameter
// default whose argument is missing. `(function (v = fetch("/x")) {})()` calls
// fetch before the body starts, and the pre-M3.9 scanner walked the body and the
// supplied arguments but never the parameter patterns, so it reported zero
// references on exactly that program.
//
// The repair maps statically visible arguments onto parameter POSITIONS,
// precisely enough to tell four cases apart:
//
//   omitted               `(function (x = f()) {})()`            default RUNS
//   literal `undefined`   `(…)(undefined)`, `(…)(void 0)`, a hole default RUNS
//   proved non-undefined  `(…)(1)`, `(…)({})`, `(…)(() => {})`   default is DEAD
//   unprovable            `(…)(maybe)`, `(…)(...xs)`             see POLICY
//
// POLICY for an argument whose value cannot be proved: treat it as if the
// default runs. This is a safety gate, so an unproved value must never be the
// thing that silences a detection. The accepted cost is a false positive on
// `(function (x = fetch()) {})(someVariable)`; proving that argument would mean
// following a value through an arbitrary binding, which the M3 convergence
// boundary places outside this scanner's contract. The policy is pinned by a
// fixture pair so it cannot drift silently.
//
// NESTED defaults (`function ({x = fetch()} = {}) {}`) are walked whenever the
// function is invoked at load, whatever the argument is. Deciding whether an
// inner default runs would mean destructuring a supplied value, i.e. walking the
// object graph -- also outside the contract. This direction cannot miss, only
// over-report, and it too is pinned by a fixture.
//
// An UNCALLED function is untouched by all of this: its parameters are walked at
// `reach: null` by the ordinary FUNCTION_TYPES branch, so `function later(x =
// fetch()) {}` still reports nothing.

// Argument expressions that are provably NOT `undefined`, so the matching
// parameter's default cannot run. Deliberately syntactic: every one of these
// node types evaluates to a value that is never `undefined`. `Literal` covers
// `null` too, which is correct -- only `undefined` triggers a default.
const DEFINITE_VALUE_TYPES = new Set([
  'Literal', 'ObjectExpression', 'ArrayExpression', 'FunctionExpression',
  'ArrowFunctionExpression', 'ClassExpression', 'TemplateLiteral',
  'NewExpression',
]);

function isDefinitelySupplied(node) {
  return !!node && DEFINITE_VALUE_TYPES.has(node.type);
}

// An argument slot that is absent, an array hole, or a spelling of `undefined`.
// Kept distinct from `isUndefinedLiteral`, which recognises the STRING
// `"undefined"` on the right of a `typeof` test.
function isUndefinedValueExpression(node) {
  if (!node) return true;
  if (node.type === 'Identifier' && node.name === 'undefined') return true;
  return node.type === 'UnaryExpression' && node.operator === 'void';
}

function isBindCall(node) {
  return !!(
    node &&
    node.type === 'CallExpression' &&
    node.callee &&
    node.callee.type === 'MemberExpression' &&
    !node.callee.computed &&
    node.callee.property &&
    node.callee.property.name === 'bind'
  );
}

// Peel `.bind(…)` off an expression, however many times it was applied, and
// return the underlying function together with the arguments those binds fixed
// -- or null if the base is not a function literal. `(function(){…}).bind(this)`
// is still that function; binding only fixes its receiver and its leading
// arguments. M3.4 Defect C, probe 1: `.call`/`.apply` were handled and
// `.bind(…)()` was not, so a bound IIFE ran at load completely unseen.
function unwrapBound(node) {
  const chain = [];
  let current = node;
  while (isBindCall(current)) {
    chain.push(current);
    current = current.callee.object;
  }
  if (!current || !FUNCTION_TYPES.has(current.type)) return null;
  // `f.bind(t1, a).bind(t2, b)` calls `f(a, b, …)`: the INNERMOST bind supplies
  // the leading arguments, and `chain` was collected outermost-first.
  let bound = [];
  const callerExprs = [];
  for (const call of chain.reverse()) {
    const all = call.arguments || [];
    for (const arg of all) callerExprs.push(arg);
    bound = bound.concat(all.slice(1)); // [0] is the receiver, not an argument
  }
  return { fn: current, bound, callerExprs };
}

function invocation(fn, prefix, mapped, callerExprs, unprovable) {
  const args = prefix.concat(mapped);
  const spread = args.some((a) => a && a.type === 'SpreadElement');
  return { fn, args, argsKnown: !unprovable && !spread, callerExprs };
}

// An IIFE's own body executes at load, so it is load-reachable. Recognises
// `(function(){…})()`, `(()=>{…})()`, the `.call(…)`/`.apply(…)` variants the
// obfuscator also emits, and any of those reached through `.bind(…)`:
// `f.bind(x)()`, `f.bind(x).call(y)`, `f.bind(x).bind(z)()`.
//
// Returns null, or:
//   fn           the function literal that runs
//   args         arguments mapped onto parameter positions. Used ONLY to
//                classify each position; these nodes are not walked from here.
//   argsKnown    false when the mapping itself is unprovable -- a spread, or
//                `.apply` with something that is not an array literal
//   callerExprs  every argument expression syntactically present in the
//                invocation chain, walked once each in the CALLER's context.
//                Collecting the bind calls' own arguments here also closes a
//                latent hole: `(function(){}).bind(null, fetch("/x"))()`
//                evaluates `fetch` at load, and the previous version discarded
//                the bind arguments entirely.
function immediatelyInvoked(node) {
  if (!node || node.type !== 'CallExpression') return null;
  const callee = node.callee;
  const own = node.arguments || [];

  if (callee && FUNCTION_TYPES.has(callee.type)) {
    return invocation(callee, [], own, own, false);
  }

  // `(function(){…}).bind(this)()` -- the callee is itself the bind call.
  const bound = unwrapBound(callee);
  if (bound) {
    return invocation(bound.fn, bound.bound, own, bound.callerExprs.concat(own), false);
  }

  if (
    callee &&
    callee.type === 'MemberExpression' &&
    !callee.computed &&
    callee.property &&
    (callee.property.name === 'call' || callee.property.name === 'apply') &&
    callee.object
  ) {
    // `.call`/`.apply` directly on a function literal, or on a bound one.
    let fn = null;
    let prefix = [];
    let callerExprs = own.slice();
    if (FUNCTION_TYPES.has(callee.object.type)) {
      fn = callee.object;
    } else {
      const boundReceiver = unwrapBound(callee.object);
      if (boundReceiver) {
        fn = boundReceiver.fn;
        prefix = boundReceiver.bound;
        callerExprs = boundReceiver.callerExprs.concat(own);
      }
    }
    if (!fn) return null;

    // `.call(receiver, a, b)` -- argument 0 is the receiver, not a parameter.
    if (callee.property.name === 'call') {
      return invocation(fn, prefix, own.slice(1), callerExprs, false);
    }
    // `.apply(receiver, argsArray)` -- only a literal array maps to positions.
    const arr = own[1];
    if (arr === undefined || isUndefinedValueExpression(arr) ||
        (arr.type === 'Literal' && arr.value === null)) {
      return invocation(fn, prefix, [], callerExprs, false);
    }
    if (arr.type === 'ArrayExpression') {
      return invocation(fn, prefix, arr.elements || [], callerExprs, false);
    }
    return invocation(fn, prefix, [], callerExprs, true);
  }
  return null;
}

const hits = [];

function record(name, at, ctx) {
  hits.push({
    name,
    line: at.loc.start.line,
    reach: ctx.reach,
    guarded: ctx.guarded.has(name),
    inTry: !!ctx.inTry,
  });
}

// `ctx` carries the analysis state down the tree:
//   reach     'top' | 'iife' | null   (null = only runs if something calls it)
//   guarded   Set of identifier names proved inert-when-absent at this point
//   inTry     true when lexically inside a `try` block that has a handler
function childCtx(ctx, over) {
  return {
    reach: over && 'reach' in over ? over.reach : ctx.reach,
    guarded: over && over.guarded ? over.guarded : ctx.guarded,
    inTry: over && 'inTry' in over ? over.inTry : ctx.inTry,
  };
}

// Walk an ordered statement list, accumulating `typeof`-guard domination:
// `if (typeof X === "undefined") return;` guards X for every LATER statement.
function walkStatements(list, ctx) {
  let guarded = ctx.guarded;
  for (const stmt of list) {
    walk(stmt, childCtx(ctx, { guarded }));
    if (stmt && stmt.type === 'IfStatement' && !stmt.alternate && isAbrupt(stmt.consequent)) {
      const test = typeofTest(stmt.test);
      if (test && test.equalsUndefined) {
        guarded = new Set(guarded);
        guarded.add(test.name);
      }
    }
  }
}

// Walk a BINDING position (a declarator id, a parameter, a catch param). The
// names introduced here are not reads, but the surrounding pattern can still
// contain expressions that are evaluated -- computed keys and defaults -- and
// those are handed back to the ordinary reference walk.
function walkPattern(node, ctx) {
  if (!node || typeof node.type !== 'string') return;
  switch (node.type) {
    case 'Identifier':
      return; // the name being bound
    case 'ObjectPattern':
      for (const prop of node.properties || []) {
        if (prop.type === 'RestElement') {
          walkPattern(prop.argument, ctx);
          continue;
        }
        if (prop.computed) walk(prop.key, ctx); // `const { [fetch]: x } = o`
        walkPattern(prop.value, ctx);
      }
      return;
    case 'ArrayPattern':
      for (const element of node.elements || []) walkPattern(element, ctx);
      return;
    case 'AssignmentPattern':
      walkPattern(node.left, ctx);
      walk(node.right, ctx); // the default expression is evaluated
      return;
    case 'RestElement':
      walkPattern(node.argument, ctx);
      return;
    default:
      // e.g. a MemberExpression target in `[window.fetch] = xs` -- a real read.
      walk(node, ctx);
  }
}

// Member access, dotted or COMPUTED. M3.4 Defect C, probe 3: dotted access was
// already caught, because the property of a non-computed member is an Identifier
// and the bare-identifier rule saw it, while a computed property is a Literal
// and escaped entirely -- `globalThis.localStorage` was flagged and
// `window["fetch"]` on the very next line was not. Both spellings name the same
// reference and are treated as one.
//
// `mode` says how the property is TOUCHED, which is what decides whether a
// directly-defined accessor runs: 'read' (`o.p`), 'write' (`o.p = v`),
// 'readwrite' (`o.p += v`, `o.p++`) or 'delete' (`delete o.p`).
//
// M3.10 defect 2: 'delete' did not exist. `walk` modelled exactly three modes,
// so a `delete` fell through to the generic child walk, which reached its
// MemberExpression argument as an ordinary READ and ran the getter --
// `delete ({ get value() { return fetch("/deleted"); } }).value` was reported
// as a load-time fetch. Deleting an accessor property invokes NEITHER half; it
// removes the descriptor. Everything else about the member is unchanged: the
// base object is still evaluated, a computed key expression is still evaluated,
// and the host-global/scheduler checks below still apply, so `delete
// window.fetch` stays exactly as visible as it was.
function walkMember(node, ctx, mode) {
  const prop = staticPropertyName(node);
  const rootName = node.object && node.object.type === 'Identifier' ? node.object.name : null;
  if (prop && RISKY.has(prop)) {
    record(prop, node.property, ctx);
    walk(node.object, ctx);
    return;
  }
  // A scheduler counts only off an explicit global root: `window.setTimeout`
  // is the global timer, `emitter.addEventListener` is not.
  if (prop && RISKY_SCHEDULERS.has(prop) && rootName && GLOBAL_ROOTS.has(rootName)) {
    record(prop, node.property, ctx);
    return;
  }

  // M3.9 defect 3: touching a property of a DIRECT object literal invokes that
  // literal's accessor, at the reach of the access itself. Only an accessor
  // whose body really runs is walked -- a getter is not reached by a plain
  // write, a setter is not reached by a plain read, a `delete` reaches neither,
  // and a data property has no body at all. An accessor that is merely DEFINED
  // stays inert, because the ordinary Property branch walks its function value
  // at `reach: null`.
  //
  // Stated as an explicit allow-list per mode rather than as `mode !== 'write'`
  // / `mode !== 'read'`: with a fourth mode in play, a negative test would make
  // any unrecognised mode silently run BOTH accessors, which is how 'delete'
  // would have been mismodelled again.
  const runsGetter = mode === 'read' || mode === 'readwrite';
  const runsSetter = mode === 'write' || mode === 'readwrite';
  if (runsGetter || runsSetter) {
    const desc = literalDescriptor(node.object, prop);
    if (desc) {
      if (runsGetter && desc.get) walk(desc.get.body, ctx);
      if (runsSetter && desc.set) walk(desc.set.body, ctx);
    }
  }

  walk(node.object, ctx);
  if (node.computed) walk(node.property, ctx);
}

// Walk the parameter list of a function that IS invoked at load. Everything a
// parameter pattern evaluates -- computed keys and default expressions --
// evaluates in the callee's scope at call time, so for an IIFE that is load
// time. The one thing this suppresses is a TOP-LEVEL default whose argument was
// proved to be a value other than `undefined`: that default is dead code, and
// reporting it would be a false positive. See the POLICY note above.
function walkInvokedParams(fn, args, argsKnown, ctx) {
  const params = (fn && fn.params) || [];
  for (let i = 0; i < params.length; i++) {
    const param = params[i];
    if (!param) continue;
    if (param.type !== 'AssignmentPattern') {
      walkPattern(param, ctx);
      continue;
    }
    walkPattern(param.left, ctx);
    const supplied = argsKnown && i < args.length && isDefinitelySupplied(args[i]);
    if (!supplied) walk(param.right, ctx);
  }
}

// Walk a class. `construction` is null when the class is merely DEFINED, or
// `{ args, argsKnown }` describing a direct `new` that constructs it at load.
//
// M3.9 defect 2: defining a class evaluates its computed member keys, its static
// field initialisers and its static blocks -- but NOT an instance field
// initialiser and NOT any method body. The previous version walked every
// `PropertyDefinition` value at the enclosing reach without checking
// `node.static`, so `class Deferred { value = fetch("/later"); }` was reported
// as a load-time fetch even though nothing ever constructed a `Deferred`.
//
// Instance initialisers and the constructor become load-reachable only through
// the one form this scanner can evaluate directly: `new (class { … })()`, where
// the class expression is the `new` callee itself. Reaching a NAMED class
// through a binding (`new Deferred()`) would need a symbol table, which the M3
// convergence boundary places outside the contract -- and the audited prefix
// contains no top-level class of any kind.
function walkClass(node, ctx, construction) {
  if (node.superClass) walk(node.superClass, ctx);
  const members = (node.body && node.body.body) || [];
  for (const member of members) {
    if (!member || typeof member.type !== 'string') continue;

    if (member.type === 'StaticBlock') {
      walkStatements(member.body || [], ctx); // runs at class evaluation
      continue;
    }

    // A computed key is evaluated while the class is being DEFINED, for
    // instance and static members alike: `class C { [fetch("/k")] = 1; }`
    // fetches even though the field initialiser is deferred.
    if (member.computed && member.key) walk(member.key, ctx);

    if (member.type === 'PropertyDefinition') {
      const runs = member.static || !!construction;
      if (member.value) walk(member.value, runs ? ctx : childCtx(ctx, { reach: null }));
      continue;
    }

    if (construction && member.type === 'MethodDefinition' &&
        member.kind === 'constructor' && member.value) {
      walkInvokedParams(member.value, construction.args, construction.argsKnown, ctx);
      walk(member.value.body, ctx);
      continue;
    }

    // Any other member value is a function whose body only runs when called --
    // including a getter or setter, which is defined here and not invoked.
    if (member.value) walk(member.value, ctx);
  }
}

function walk(node, ctx) {
  if (!node || typeof node.type !== 'string') return;

  // `typeof X` never throws on an undeclared X -- that is the whole point of
  // the guard idiom -- so the operand of a `typeof` is not itself a risky
  // reference. Counting it would report every guard as its own violation.
  if (node.type === 'UnaryExpression' && node.operator === 'typeof' &&
      node.argument && node.argument.type === 'Identifier') {
    return;
  }

  if (node.type === 'Identifier' && ctx.reach &&
      (RISKY.has(node.name) || RISKY_SCHEDULERS.has(node.name))) {
    record(node.name, node, ctx);
    return;
  }

  if (node.type === 'MemberExpression' && ctx.reach) {
    walkMember(node, ctx, 'read');
    return;
  }

  // `o.p = v` WRITES the property: it invokes a setter, never a getter. A
  // compound `o.p += v` reads first and then writes, so it does both. Handled
  // here rather than in `walkMember` because the read/write distinction lives in
  // the PARENT node, and the generic child walk would otherwise reach the target
  // as an ordinary read.
  if (node.type === 'AssignmentExpression' && ctx.reach &&
      node.left && node.left.type === 'MemberExpression') {
    walk(node.right, ctx);
    walkMember(node.left, ctx, node.operator === '=' ? 'write' : 'readwrite');
    return;
  }
  // `o.p++` reads and writes.
  if (node.type === 'UpdateExpression' && ctx.reach &&
      node.argument && node.argument.type === 'MemberExpression') {
    walkMember(node.argument, ctx, 'readwrite');
    return;
  }
  // `delete o.p` touches the property WITHOUT invoking either accessor. Handled
  // here for the same reason as assignment: the mode lives in the parent node,
  // and without this branch the generic child walk reaches the member as an
  // ordinary read. `walkMember` still evaluates the base object, still
  // evaluates a computed key expression, and still applies the risky-name and
  // scheduler checks -- see M3.10 defect 2 there.
  if (node.type === 'UnaryExpression' && node.operator === 'delete' && ctx.reach &&
      node.argument && node.argument.type === 'MemberExpression') {
    walkMember(node.argument, ctx, 'delete');
    return;
  }

  // `if (typeof X !== "undefined") { …X… }` -- the consequent is inert when
  // X is absent, so X counts as guarded inside it (and inside the test).
  if (node.type === 'IfStatement') {
    const test = typeofTest(node.test);
    walk(node.test, ctx);
    if (test && !test.equalsUndefined) {
      const inner = new Set(ctx.guarded);
      inner.add(test.name);
      walk(node.consequent, childCtx(ctx, { guarded: inner }));
    } else {
      walk(node.consequent, ctx);
    }
    if (node.alternate) walk(node.alternate, ctx);
    return;
  }

  // The same shape as an expression: `typeof X !== "undefined" && X.foo()`.
  if (node.type === 'LogicalExpression' && node.operator === '&&') {
    const test = typeofTest(node.left);
    walk(node.left, ctx);
    if (test && !test.equalsUndefined) {
      const inner = new Set(ctx.guarded);
      inner.add(test.name);
      walk(node.right, childCtx(ctx, { guarded: inner }));
    } else {
      walk(node.right, ctx);
    }
    return;
  }
  // `typeof X === "undefined" || X.foo()` is the mirror image.
  if (node.type === 'LogicalExpression' && node.operator === '||') {
    const test = typeofTest(node.left);
    walk(node.left, ctx);
    if (test && test.equalsUndefined) {
      const inner = new Set(ctx.guarded);
      inner.add(test.name);
      walk(node.right, childCtx(ctx, { guarded: inner }));
    } else {
      walk(node.right, ctx);
    }
    return;
  }
  // `typeof X === "undefined" ? … : …X…`
  if (node.type === 'ConditionalExpression') {
    const test = typeofTest(node.test);
    walk(node.test, ctx);
    if (test) {
      const inner = new Set(ctx.guarded);
      inner.add(test.name);
      const guardedBranch = test.equalsUndefined ? 'alternate' : 'consequent';
      const plainBranch = test.equalsUndefined ? 'consequent' : 'alternate';
      walk(node[guardedBranch], childCtx(ctx, { guarded: inner }));
      walk(node[plainBranch], ctx);
    } else {
      walk(node.consequent, ctx);
      walk(node.alternate, ctx);
    }
    return;
  }

  if (node.type === 'TryStatement') {
    walk(node.block, childCtx(ctx, { inTry: !!node.handler }));
    if (node.handler) walk(node.handler, ctx);
    if (node.finalizer) walk(node.finalizer, ctx);
    return;
  }

  const invoked = immediatelyInvoked(node);
  if (invoked && ctx.reach) {
    // The invoked parameters and body run at load. Their guard set starts fresh
    // from the surrounding one -- a guard outside the IIFE still dominates
    // inside it. Parameters are walked first because they are evaluated first.
    const inner = childCtx(ctx, { reach: 'iife' });
    walkInvokedParams(invoked.fn, invoked.args, invoked.argsKnown, inner);
    walk(invoked.fn.body, inner);
    // Argument expressions evaluate in the CALLER's context, not the callee's.
    for (const expr of invoked.callerExprs) walk(expr, ctx);
    return;
  }

  // `new (class { … })()` -- a direct anonymous or named class EXPRESSION in the
  // callee position is constructed at load, which runs its instance field
  // initialisers and its constructor.
  if (node.type === 'NewExpression' && ctx.reach && node.callee &&
      CLASS_TYPES.has(node.callee.type)) {
    const own = node.arguments || [];
    const spread = own.some((a) => a && a.type === 'SpreadElement');
    walkClass(node.callee, ctx, { args: own, argsKnown: !spread });
    for (const arg of own) walk(arg, ctx);
    return;
  }

  if (node.type === 'BlockStatement' || node.type === 'Program') {
    walkStatements(node.body, ctx);
    return;
  }

  // -------------------------------------------------------------------------
  // reference-position filtering -- see the block comment above KEYED_MEMBERS.
  // These branches sit immediately before the generic child walk so that every
  // rule above them (guards, member access, IIFEs, try/catch) keeps its exact
  // previous precedence.
  // -------------------------------------------------------------------------

  if (node.type === 'ObjectExpression') {
    for (const prop of node.properties || []) {
      if (prop && prop.type === 'SpreadElement') {
        // Constructing the source can itself evaluate computed keys, values and
        // nested spreads. CopyDataProperties then reads its live getters.
        walk(prop.argument, ctx);
        if (ctx.reach) walkDirectSpreadGetters(prop.argument, ctx);
      } else {
        walk(prop, ctx);
      }
    }
    return;
  }

  if (KEYED_MEMBERS.has(node.type)) {
    // `{ [setTimeout]: h }` / `class E { [fetch]() {} }` evaluate the key; a
    // plain `addEventListener() {}` only names it. The value is always walked,
    // which is what keeps the shorthand read `{ setTimeout }` visible.
    if (node.computed) walk(node.key, ctx);
    if (node.value) walk(node.value, ctx);
    return;
  }

  if (node.type === 'VariableDeclarator') {
    walkPattern(node.id, ctx);
    if (node.init) walk(node.init, ctx);
    return;
  }

  if (FUNCTION_TYPES.has(node.type)) {
    // The body only runs if something calls it; the name and the parameters are
    // bindings either way.
    const fnCtx = childCtx(ctx, { reach: null });
    for (const param of node.params || []) walkPattern(param, fnCtx);
    walk(node.body, fnCtx);
    return;
  }

  if (CLASS_TYPES.has(node.type)) {
    // `id` is a binding. The body is NOT descended with reach cleared: a static
    // block, a static field initialiser or a computed member key really does run
    // when the class is evaluated, which for a top-level class is load time. An
    // INSTANCE field initialiser does not -- see `walkClass`.
    walkClass(node, ctx, null);
    return;
  }

  if (node.type === 'CatchClause') {
    if (node.param) walkPattern(node.param, ctx);
    walk(node.body, ctx);
    return;
  }

  // A label is not a value read.
  if (node.type === 'LabeledStatement') {
    walk(node.body, ctx);
    return;
  }
  if (node.type === 'BreakStatement' || node.type === 'ContinueStatement') return;

  const inner = FUNCTION_TYPES.has(node.type) ? childCtx(ctx, { reach: null }) : ctx;
  for (const key of Object.keys(node)) {
    if (key === 'loc' || key === 'start' || key === 'end' || key === 'range') continue;
    const value = node[key];
    if (Array.isArray(value)) {
      for (const item of value) if (item && typeof item.type === 'string') walk(item, inner);
    } else if (value && typeof value.type === 'string') {
      walk(value, inner);
    }
  }
}

walkStatements(ast.body, { reach: 'top', guarded: new Set(), inTry: false });

// ---------------------------------------------------------------------------
// allow-list
// ---------------------------------------------------------------------------

let allow = null;
let allowError = null;
if (allowPath) {
  if (!fs.existsSync(allowPath)) {
    allowError = `allow-list ${allowPath} does not exist`;
  } else {
    allow = JSON.parse(fs.readFileSync(allowPath, 'utf8'));
    if (allow.prefix_sha256 !== srcSha) {
      allowError =
        `allow-list was audited against prefix sha256 ${allow.prefix_sha256}, ` +
        `but this file hashes to ${srcSha}. Re-audit every entry before updating it.`;
    }
  }
}

const allowIndex = new Map();
if (allow && !allowError) {
  for (const entry of allow.entries || []) {
    if (!entry.reason || !String(entry.reason).trim()) {
      allowError = `allow-list entry ${JSON.stringify(entry)} has no reason`;
      break;
    }
    allowIndex.set(`${entry.name}@${entry.line}/${entry.reach}`, entry);
  }
}

// ---------------------------------------------------------------------------
// verdict
// ---------------------------------------------------------------------------

const classified = hits.map((hit) => {
  const key = `${hit.name}@${hit.line}/${hit.reach}`;
  const entry = allowIndex.get(key);
  let verdict;
  let reason;
  if (hit.guarded) {
    verdict = 'proved-guarded';
    reason = 'dominated by a real `typeof ' + hit.name + ' (=|!)== "undefined"` test';
  } else if (entry) {
    verdict = 'allow-listed';
    reason = entry.reason;
  } else {
    verdict = 'UNGUARDED';
    reason = hit.inTry
      ? 'inside a try/catch, but not allow-listed -- an audited exception boundary must be declared explicitly'
      : 'no typeof guard and no allow-list entry';
  }
  return { ...hit, verdict, reason, key };
});

const unexpected = classified.filter((h) => h.verdict === 'UNGUARDED');
const unusedAllow = [...allowIndex.keys()].filter(
  (key) => !classified.some((h) => h.key === key && h.verdict === 'allow-listed'),
);

const byTypeCounts = {};
let bareCalls = 0;
for (const stmt of ast.body) {
  byTypeCounts[stmt.type] = (byTypeCounts[stmt.type] || 0) + 1;
  if (stmt.type === 'ExpressionStatement') bareCalls++;
}

if (asJson) {
  console.log(JSON.stringify({
    file: inPath,
    sha256: srcSha,
    top_level_statements: ast.body.length,
    statement_types: byTypeCounts,
    bare_top_level_expression_statements: bareCalls,
    hits: classified.map(({ key, ...rest }) => rest),
    unexpected: unexpected.length,
    unused_allowlist_entries: unusedAllow,
    allowlist_error: allowError,
  }, null, 2));
} else {
  console.log(`file: ${inPath}`);
  console.log(`sha256: ${srcSha}`);
  console.log(`top-level statements: ${ast.body.length}`);
  for (const [type, count] of Object.entries(byTypeCounts).sort()) {
    console.log(`  ${type}: ${count}`);
  }
  console.log(`bare top-level expression statements: ${bareCalls}`);
  console.log(`load-reachable risky references: ${classified.length}`);
  const grouped = new Map();
  for (const hit of classified) {
    const key = `${hit.name}/${hit.reach}/${hit.verdict}`;
    if (!grouped.has(key)) grouped.set(key, []);
    grouped.get(key).push(hit.line);
  }
  for (const [key, lines] of [...grouped].sort()) {
    console.log(`  ${key}: ${lines.length}x  lines ${lines.slice(0, 12).join(', ')}${lines.length > 12 ? ' …' : ''}`);
  }
}

// `process.exitCode` rather than `process.exit()`: the latter can truncate a
// buffered stdout that is redirected to a file, which silently produced an
// empty --json report during M3.3.
if (allowError) {
  console.error(`\nFAIL: ${allowError}`);
  process.exitCode = 1;
} else if (unusedAllow.length) {
  console.error(
    `\nFAIL: ${unusedAllow.length} allow-list entr(y/ies) matched nothing in this prefix: ` +
      `${unusedAllow.join(', ')}. A stale approval must be removed, not left to rot.`,
  );
  process.exitCode = 1;
} else if (unexpected.length) {
  console.error(`\nFAIL: ${unexpected.length} load-reachable risky reference(s) are unknown or unguarded:`);
  for (const hit of unexpected) {
    console.error(`  line ${hit.line}: ${hit.name} (reach=${hit.reach}) -- ${hit.reason}`);
  }
  process.exitCode = 1;
} else {
  console.log(
    `\nOK: all ${classified.length} load-reachable risky reference(s) are accounted for ` +
      `(${classified.filter((h) => h.verdict === 'proved-guarded').length} proved guarded by ` +
      `\`typeof\` analysis, ${classified.filter((h) => h.verdict === 'allow-listed').length} ` +
      `allow-listed against prefix ${srcSha.slice(0, 16)}...).`,
  );
  process.exitCode = 0;
}
