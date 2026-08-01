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

const RISKY = new Set([
  'document', 'localStorage', 'sessionStorage', 'navigator', 'fetch',
  'XMLHttpRequest', 'Audio', 'WebSocket', 'indexedDB', 'Notification',
  'geolocation', 'Worker', 'EventSource',
]);

const FUNCTION_TYPES = new Set([
  'FunctionDeclaration', 'FunctionExpression', 'ArrowFunctionExpression',
]);

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

// An IIFE's own body executes at load, so it is load-reachable. Recognises
// `(function(){…})()`, `(()=>{…})()`, and the `.call(…)`/`.apply(…)` variants
// the obfuscator also emits.
function immediatelyInvokedBody(node) {
  if (!node || node.type !== 'CallExpression') return null;
  const callee = node.callee;
  if (callee && FUNCTION_TYPES.has(callee.type)) return callee.body;
  if (
    callee &&
    callee.type === 'MemberExpression' &&
    !callee.computed &&
    callee.property &&
    (callee.property.name === 'call' || callee.property.name === 'apply') &&
    callee.object &&
    FUNCTION_TYPES.has(callee.object.type)
  ) {
    return callee.object.body;
  }
  return null;
}

const hits = [];

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

function walk(node, ctx) {
  if (!node || typeof node.type !== 'string') return;

  // `typeof X` never throws on an undeclared X -- that is the whole point of
  // the guard idiom -- so the operand of a `typeof` is not itself a risky
  // reference. Counting it would report every guard as its own violation.
  if (node.type === 'UnaryExpression' && node.operator === 'typeof' &&
      node.argument && node.argument.type === 'Identifier') {
    return;
  }

  if (node.type === 'Identifier' && RISKY.has(node.name) && ctx.reach) {
    hits.push({
      name: node.name,
      line: node.loc.start.line,
      reach: ctx.reach,
      guarded: ctx.guarded.has(node.name),
      inTry: !!ctx.inTry,
    });
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

  const iifeBody = immediatelyInvokedBody(node);
  if (iifeBody && ctx.reach) {
    // The invoked body runs at load. Its guard set starts fresh from the
    // surrounding one -- a guard outside the IIFE still dominates inside it.
    walk(iifeBody, childCtx(ctx, { reach: 'iife' }));
    for (const arg of node.arguments || []) walk(arg, ctx);
    return;
  }

  if (node.type === 'BlockStatement' || node.type === 'Program') {
    walkStatements(node.body, ctx);
    return;
  }

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
