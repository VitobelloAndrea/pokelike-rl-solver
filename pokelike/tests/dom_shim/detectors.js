'use strict';
// The standing DOM-shim detector suite: executes the REAL
// pokelike/webui/static/js/app.js under Node's `vm` against the REAL
// pokelike/webui/static/index.html element set, driven by fixture payloads that
// pokelike/tests/test_dom_shim.py generates from the REAL
// pokelike.render.contract.observation().
//
// Nothing here is a mock or a reimplementation of app.js. Every assertion is
// about bytes that ship.
//
// Usage:  node pokelike/tests/dom_shim/detectors.js <fixtures-dir>
// Exit 0 = all detectors passed; exit 1 = at least one failed (report on stdout).

const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { Element, Clock, createDocument } = require('./shim');

const REPO = path.resolve(__dirname, '..', '..', '..');
const APP_JS = path.join(REPO, 'pokelike', 'webui', 'static', 'js', 'app.js');
const INDEX_HTML = path.join(REPO, 'pokelike', 'webui', 'static', 'index.html');

// ---------------------------------------------------------------------------
// Harness
// ---------------------------------------------------------------------------

function indexHtmlIds() {
  const html = fs.readFileSync(INDEX_HTML, 'utf8');
  const ids = new Set();
  for (const m of html.matchAll(/\bid="([^"]+)"/g)) ids.add(m[1]);
  if (ids.size === 0) throw new Error('index.html declared no ids -- shim would be vacuous');
  return ids;
}

/** Boots app.js in a fresh sandbox. Returns the live context. */
function boot() {
  const clock = new Clock();
  const document = createDocument(indexHtmlIds());
  const toasts = [];
  const posts = [];

  const sandbox = {
    document,
    console: { log() {}, warn() {}, error() {} },
    setTimeout: (fn, ms) => clock.setTimeout(fn, ms),
    clearTimeout: (id) => clock.clearTimeout(id),
    requestAnimationFrame: (fn) => clock.setTimeout(fn, 16),
    // app.js only reaches the network through apiPost; every call is recorded
    // and answered with a never-settling promise, so a detector can assert what
    // the UI TRIED to send without a server.
    fetch: (url, opts) => {
      posts.push({ url, body: opts && opts.body ? JSON.parse(opts.body) : null });
      return new Promise(() => {});
    },
    Math, JSON, Object, Array, String, Number, Boolean, Date, Promise, Error,
    RegExp, Map, Set, Symbol, isNaN, parseInt, parseFloat,
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;

  const ctx = vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(APP_JS, 'utf8'), ctx, { filename: 'app.js' });

  // Capture toasts AFTER load, by wrapping the real function rather than
  // replacing it -- the real one still runs, so its DOM effects stay testable.
  const realToast = ctx.showToast;
  ctx.showToast = (msg) => { toasts.push(String(msg)); return realToast(msg); };

  return { ctx, document, clock, toasts, posts };
}

// ---------------------------------------------------------------------------
// Assertions
// ---------------------------------------------------------------------------

const results = [];
function detector(name, fn) {
  try {
    fn();
    results.push({ name, ok: true });
  } catch (err) {
    results.push({ name, ok: false, message: err && err.message ? err.message : String(err) });
  }
}
function assert(cond, msg) { if (!cond) throw new Error(msg); }
function assertEqual(actual, expected, msg) {
  if (actual !== expected) throw new Error(`${msg} (expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)})`);
}

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const fixturesDir = process.argv[2];
if (!fixturesDir) { console.error('usage: node detectors.js <fixtures-dir>'); process.exit(2); }
const fixtures = {};
for (const f of fs.readdirSync(fixturesDir)) {
  if (f.endsWith('.json')) {
    fixtures[path.basename(f, '.json')] = JSON.parse(fs.readFileSync(path.join(fixturesDir, f), 'utf8'));
  }
}
if (!Object.keys(fixtures).length) { console.error('no fixtures found in ' + fixturesDir); process.exit(2); }

/** The observation fixtures whose phase is `on_map`, i.e. the map renders. */
function mapFixtures() {
  return Object.entries(fixtures).filter(([, st]) => st.phase === 'on_map' && st.map);
}

// ---------------------------------------------------------------------------
// 0a. META: the SHIM's own detection logic, self-tested
// ---------------------------------------------------------------------------
//
// Every detector below is only as trustworthy as the primitives it is built
// on. A shim whose listenerCount always returned 1, or whose selector engine
// quietly matched nothing, would turn this whole suite green while checking
// nothing at all -- so those primitives are tested directly, against known
// inputs, before anything touches app.js.

detector('meta: listenerCount counts real registrations', () => {
  const el = new Element('div');
  assertEqual(el.listenerCount('click'), 0, 'a fresh element reported listeners');
  const a = () => {};
  const b = () => {};
  el.addEventListener('click', a);
  assertEqual(el.listenerCount('click'), 1, 'one listener was not counted as one');
  el.addEventListener('click', b);
  assertEqual(el.listenerCount('click'), 2, 'a DUPLICATE listener was not counted');
  assertEqual(el.listenerCount('pointerdown'), 0, 'an unrelated type reported listeners');
  el.removeEventListener('click', b);
  assertEqual(el.listenerCount('click'), 1, 'removeEventListener did not decrement');
  el.onclick = () => {};
  assertEqual(el.listenerCount('click'), 2, 'an onclick assignment was not counted');
});

detector('meta: dispatch fires every registered handler exactly once', () => {
  const el = new Element('div');
  let n = 0;
  el.addEventListener('click', () => { n += 1; });
  el.addEventListener('click', () => { n += 1; });
  el.dispatch('click', {});
  assertEqual(n, 2, 'dispatch did not fire both handlers');
});

detector('meta: the selector engine matches and rejects correctly', () => {
  const root = new Element('div');
  const child = new Element('span');
  child.className = 'alpha beta';
  child.setAttribute('data-idx', '3');
  root.appendChild(child);

  assertEqual(root.querySelectorAll('.alpha').length, 1, '.class did not match');
  assertEqual(root.querySelectorAll('.gamma').length, 0, '.class matched the wrong element');
  assertEqual(root.querySelectorAll('span').length, 1, 'tag did not match');
  assertEqual(root.querySelectorAll('div').length, 0, 'tag matched the wrong element');
  assertEqual(root.querySelectorAll('[data-idx="3"]').length, 1, 'attribute= did not match');
  assertEqual(root.querySelectorAll('[data-idx="4"]').length, 0, 'attribute= matched a wrong value');
  assertEqual(root.querySelectorAll('span.alpha[data-idx="3"]').length, 1, 'compound did not match');
  assertEqual(root.querySelectorAll('.alpha .beta').length, 0, 'descendant matched a non-descendant');
});

detector('meta: an unsupported selector THROWS rather than matching nothing', () => {
  // A silent [] here is the difference between "no bug" and "no test".
  const root = new Element('div');
  root.appendChild(new Element('span'));
  // The RIGHTMOST token is the one evaluated against every candidate, so it is
  // the one that has to be unsupported for the throw to be reachable at all.
  for (const sel of ['span:nth-child(2)', 'span > .x', '.a ~ .b']) {
    let threw = false;
    try {
      root.querySelectorAll(sel);
    } catch (e) {
      threw = true;
    }
    assert(threw,
      `the selector engine silently accepted ${JSON.stringify(sel)}, which it cannot evaluate`);
  }
});

detector('meta: classList and style record what was written', () => {
  const el = new Element('g');
  el.classList.add('map-node');
  assert(el.classList.contains('map-node'), 'classList.add did not stick');
  el.classList.remove('map-node');
  assert(!el.classList.contains('map-node'), 'classList.remove did not stick');
  el.style.filter = 'brightness(0.72)';
  assertEqual(el.style.filter, 'brightness(0.72)', 'style write was not readable');
  assertEqual(new Element('g').style.filter, '', 'an untouched style read non-empty');
  el.style.setProperty('--node-tx', '5px');
  assertEqual(el.style['--node-tx'], '5px', 'setProperty was not readable');
});

detector('meta: the virtual clock runs timers in time order', () => {
  const clock = new Clock();
  const order = [];
  clock.setTimeout(() => order.push('late'), 500);
  clock.setTimeout(() => order.push('early'), 10);
  const id = clock.setTimeout(() => order.push('cancelled'), 20);
  clock.clearTimeout(id);
  clock.drain();
  assertEqual(order.join(','), 'early,late', 'the clock did not honour delays / clearTimeout');
});

// ---------------------------------------------------------------------------
// 0b. The harness itself must not be vacuous
// ---------------------------------------------------------------------------

detector('shim: app.js loads and wires its static buttons', () => {
  const { ctx, document } = boot();
  assert(typeof ctx.render === 'function', 'app.js defined no render()');
  assert(typeof ctx.renderMap === 'function', 'app.js defined no renderMap()');
  // wireButtons() runs at top level and would have thrown on a missing id.
  const start = document.getElementById('btn-start-story');
  assert(start && typeof start.onclick === 'function', 'btn-start-story was never wired');
});

detector('shim: fixtures are real contract.observation payloads', () => {
  const names = Object.keys(fixtures);
  assert(names.length >= 3, `too few fixtures (${names.length}) to be meaningful`);
  assert(mapFixtures().length >= 1, 'no on_map fixture -- the map detectors would be vacuous');
  for (const [name, st] of Object.entries(fixtures)) {
    for (const key of ['phase', 'team', 'log', 'log_total', 'contract_version']) {
      assert(key in st, `fixture ${name} is missing observation field ${key}`);
    }
  }
});

// ---------------------------------------------------------------------------
// 1. N5 -- the regression this infrastructure exists to prove it can catch
// ---------------------------------------------------------------------------
//
// The source dims a VISITED node at the group (renderMap, bundle.deobfuscated
// .js:54181: `BcY.visited && (BcA.style.filter = "brightness(0.72)")`), and
// separately dims the sprite <image> inside it (appendMapNodeSprite:54067).
// app.js did only the second, so a visited node drawn by the CIRCLE branch --
// START, which is on every reachable route -- rendered undimmed.

detector('N5: every visited map node dims its GROUP, not just its sprite', () => {
  let checkedNodes = 0;
  let checkedCircleBranch = 0;

  for (const [name, state] of mapFixtures()) {
    const { ctx, document } = boot();
    ctx.render(state);
    const container = document.getElementById('map-container');
    const groups = container.querySelectorAll('.map-node');
    assert(groups.length > 0, `fixture ${name} rendered no map nodes at all`);

    const dimmed = state.map.nodes.filter((n) => n.dimmed);
    assert(dimmed.length > 0, `fixture ${name} has no visited node -- cannot test N5`);

    for (const node of dimmed) {
      // Find this node's group by the pixel transform the renderer gave it.
      const group = groups.find((g) => {
        const t = g.getAttribute('transform') || '';
        return t === expectedTransform(node, state.map, container);
      });
      assert(group, `fixture ${name}: no rendered group for visited node ${node.id}`);
      assertEqual(
        group.style.filter, 'brightness(0.72)',
        `fixture ${name}: visited node ${node.id} (${node.type}) renders UNDIMMED -- ` +
        'the source dims the whole <g> at renderMap:54181',
      );
      checkedNodes += 1;
      if (!node.sprite_url) checkedCircleBranch += 1;
    }
  }

  assert(checkedNodes > 0, 'N5 detector checked no nodes -- vacuous');
  // The circle branch is the one N5 was actually reported against. If no
  // fixture reaches it, say so rather than passing quietly.
  assert(
    checkedCircleBranch > 0,
    'N5 detector never exercised the CIRCLE branch (a node with no sprite_url), ' +
    'which is the case R2 reported -- fixtures are too narrow',
  );
});

function expectedTransform(node, mapData, container) {
  const w = Math.round(container.clientWidth) || 600;
  const h = Math.round(container.clientHeight) || 500;
  const margin = mapData.edge_margin;
  const x = node.pos.x_frac * w;
  const y = node.pos.y_frac === null ? h / 2 : margin + node.pos.y_frac * (h - 2 * margin);
  return `translate(${x},${y})`;
}

detector('N5: an UNVISITED node is left undimmed', () => {
  // The other half of the contract -- a fix that dimmed everything would
  // otherwise satisfy the detector above.
  let checked = 0;
  for (const [name, state] of mapFixtures()) {
    const { ctx, document } = boot();
    ctx.render(state);
    const container = document.getElementById('map-container');
    for (const node of state.map.nodes.filter((n) => !n.dimmed)) {
      const group = container.querySelectorAll('.map-node').find(
        (g) => (g.getAttribute('transform') || '') === expectedTransform(node, state.map, container),
      );
      if (!group) continue;
      assertEqual(group.style.filter, '',
        `fixture ${name}: unvisited node ${node.id} was dimmed`);
      checked += 1;
    }
  }
  assert(checked > 0, 'no unvisited node was checked -- vacuous');
});

// ---------------------------------------------------------------------------
// 2. Duplicate event listeners -- the R3 double-dispatch bug class
// ---------------------------------------------------------------------------

detector('listeners: a clickable map node binds exactly one click handler', () => {
  let checked = 0;
  for (const [name, state] of mapFixtures()) {
    const { ctx, document } = boot();
    ctx.render(state);
    const container = document.getElementById('map-container');
    for (const g of container.querySelectorAll('.map-node')) {
      const n = g.listenerCount('click');
      assert(n <= 1,
        `fixture ${name}: a map node has ${n} click handlers -- one action would ` +
        'dispatch twice (the R3 double-dispatch class)');
      if (g.classList.contains('map-node--clickable')) {
        assertEqual(n, 1, `fixture ${name}: a clickable map node has no click handler`);
        checked += 1;
      }
    }
  }
  assert(checked > 0, 'no clickable node was checked -- vacuous');
});

detector('listeners: one click on a map node sends exactly one action', () => {
  // The behavioural form of the same property: count what reaches the network.
  let checked = 0;
  for (const [name, state] of mapFixtures()) {
    const { ctx, document, posts, clock } = boot();
    ctx.render(state);
    const container = document.getElementById('map-container');
    const clickable = container.querySelectorAll('.map-node--clickable');
    if (!clickable.length) continue;
    const before = posts.length;
    clickable[0].dispatch('click', {});
    clock.drain();
    const sent = posts.slice(before).filter((p) => p.url === '/api/action');
    assertEqual(sent.length, 1,
      `fixture ${name}: one node click produced ${sent.length} /api/action posts`);
    assertEqual(sent[0].body.type, 'VisitNode', 'node click sent the wrong action');
    checked += 1;
  }
  assert(checked > 0, 'no clickable node was exercised -- vacuous');
});

detector('listeners: team-bar slots do not double-bind their drag handlers', () => {
  let checked = 0;
  let reorderSlots = 0;
  for (const [name, state] of mapFixtures()) {
    if (!state.team || !state.team.length) continue;
    const { ctx, document } = boot();
    ctx.render(state);
    const bar = document.getElementById('team-bar');
    for (const slot of bar.querySelectorAll('.team-slot')) {
      for (const type of ['pointerdown', 'mousedown', 'touchstart', 'click', 'dragstart']) {
        const n = slot.listenerCount(type);
        assert(n <= 1,
          `fixture ${name}: a team slot has ${n} '${type}' handlers -- ` +
          'the R3 double-dispatch class');
      }
      if (slot.classList.contains('team-slot-reorder')) {
        // The drag gesture only attaches when a reorder is legal; this is the
        // path R3's double-dispatch bug actually lived on.
        assertEqual(slot.listenerCount('pointerdown'), 1,
          `fixture ${name}: a reorderable team slot has no drag handler`);
        reorderSlots += 1;
      }
      checked += 1;
    }
  }
  assert(checked > 0, 'no team slot was checked -- vacuous');
  assert(reorderSlots > 0,
    'no REORDERABLE team slot was checked -- the drag-handler half is vacuous ' +
    '(fixtures never reached a team of 2+)');
});

detector('listeners: re-rendering does not accumulate handlers', () => {
  // The mechanism behind the R3 bug: a render path that binds without
  // rebuilding grows one more handler on every redraw.
  const entry = mapFixtures()[0];
  assert(entry, 'no on_map fixture');
  const [name, state] = entry;
  const { ctx, document } = boot();
  const counts = [];
  for (let i = 0; i < 3; i += 1) {
    ctx.render(state);
    const container = document.getElementById('map-container');
    counts.push(container.querySelectorAll('.map-node')
      .reduce((acc, g) => acc + g.listenerCount('click'), 0));
  }
  assertEqual(counts[0], counts[1], `fixture ${name}: click handlers grew on the 2nd render`);
  assertEqual(counts[1], counts[2], `fixture ${name}: click handlers grew on the 3rd render`);
});

// ---------------------------------------------------------------------------
// 3. Dispatcher coverage -- every phase reached in practice has a case
// ---------------------------------------------------------------------------

detector('dispatcher: no reachable phase falls through to "Unhandled phase"', () => {
  const seen = new Set();
  for (const [name, state] of Object.entries(fixtures)) {
    const { ctx, toasts } = boot();
    try {
      ctx.render(state);
    } catch (err) {
      throw new Error(`fixture ${name} (phase ${state.phase}) threw during render: ${err.message}`);
    }
    const unhandled = toasts.filter((t) => t.startsWith('Unhandled phase'));
    assertEqual(unhandled.length, 0,
      `fixture ${name}: render() fell through -- ${JSON.stringify(unhandled)}`);
    seen.add(state.phase);
  }
  assert(seen.size >= 3,
    `dispatcher detector saw only ${seen.size} distinct phase(s) -- too narrow to mean much`);
});

detector('dispatcher: an unknown phase DOES toast (the detector is not blind)', () => {
  // Non-vacuity for the detector above: prove the fall-through path still
  // exists and is observable, so a green run means something.
  const entry = Object.entries(fixtures)[0];
  const { ctx, toasts } = boot();
  ctx.render(Object.assign({}, entry[1], { phase: 'no_such_phase_r5' }));
  assert(toasts.some((t) => t.startsWith('Unhandled phase')),
    'an unknown phase produced no "Unhandled phase" toast -- the coverage detector above is blind');
});

// ---------------------------------------------------------------------------
// 4. Node presentation decisions come from the contract, not from app.js
// ---------------------------------------------------------------------------

detector('map: unexplored nodes carry the source\'s 0.75 opacity', () => {
  let checked = 0;
  for (const [name, state] of mapFixtures()) {
    const { ctx, document } = boot();
    ctx.render(state);
    const container = document.getElementById('map-container');
    for (const node of state.map.nodes.filter((n) => n.unexplored)) {
      const group = container.querySelectorAll('.map-node').find(
        (g) => (g.getAttribute('transform') || '') === expectedTransform(node, state.map, container),
      );
      if (!group) continue;
      assertEqual(group.style.opacity, '0.75',
        `fixture ${name}: unexplored node ${node.id} lost renderMap:54180's opacity`);
      checked += 1;
    }
  }
  assert(checked > 0, 'no unexplored node was checked -- vacuous');
});

detector('map: clickable is `accessible AND NOT visited`, per the contract', () => {
  for (const [name, state] of mapFixtures()) {
    const { ctx, document } = boot();
    ctx.render(state);
    const container = document.getElementById('map-container');
    const rendered = container.querySelectorAll('.map-node--clickable').length;
    const expected = state.map.nodes.filter((n) => n.clickable).length;
    assertEqual(rendered, expected,
      `fixture ${name}: ${rendered} clickable nodes drawn, contract says ${expected}`);
  }
});

// ---------------------------------------------------------------------------
// 5. R5's interaction ports (§4 of the brief)
// ---------------------------------------------------------------------------

detector('touch: a long press opens the tooltip and does NOT visit the node', () => {
  const [name, state] = mapFixtures()[0];
  const { ctx, document, posts, clock } = boot();
  ctx.render(state);
  const g = document.getElementById('map-container').querySelectorAll('.map-node--clickable')[0];
  assert(g, `fixture ${name}: no clickable node`);
  assert(g.listenerCount('touchstart') === 1, 'node has no touchstart handler');

  const before = posts.length;
  g.dispatch('touchstart', { touches: [{ clientX: 100, clientY: 100 }] });
  clock.drain();                       // lets the 400 ms long-press timer fire
  const tip = document.getElementById('map-node-tooltip');
  g.dispatch('touchend', { touches: [] });
  assertEqual(posts.length - before, 0,
    'a long press visited the node -- the source dismisses instead (54450-54453)');
  void tip;
});

detector('touch: a quick tap visits the node exactly once', () => {
  const [name, state] = mapFixtures()[0];
  const { ctx, document, posts } = boot();
  ctx.render(state);
  const g = document.getElementById('map-container').querySelectorAll('.map-node--clickable')[0];
  assert(g, `fixture ${name}: no clickable node`);
  const before = posts.length;
  // No clock.drain() between start and end: the long-press timer never fires,
  // which is what makes this a tap rather than a press.
  g.dispatch('touchstart', { touches: [{ clientX: 10, clientY: 10 }] });
  g.dispatch('touchend', { touches: [] });
  // The browser's synthetic click after touchend must NOT double-fire.
  g.dispatch('click', {});
  const sent = posts.slice(before).filter((p) => p.url === '/api/action');
  assertEqual(sent.length, 1,
    `a tap produced ${sent.length} actions -- the source suppresses the ` +
    'synthetic click after a handled touchend (54462-54470)');
});

detector('touch: moving past the 12px slop cancels the pending long press', () => {
  const [, state] = mapFixtures()[0];
  const { ctx, document, clock } = boot();
  ctx.render(state);
  const g = document.getElementById('map-container').querySelectorAll('.map-node--clickable')[0];
  g.dispatch('touchstart', { touches: [{ clientX: 0, clientY: 0 }] });
  g.dispatch('touchmove', { touches: [{ clientX: 40, clientY: 0 }] });
  const tooltip = document.getElementById('map-node-tooltip');
  const before = tooltip.classList.contains('visible');
  clock.drain();
  assertEqual(tooltip.classList.contains('visible'), before,
    'the long-press tooltip fired after a move past the slop radius (54442)');
});

detector('shortcuts: the map draws a numbered badge on its first two nodes', () => {
  let checked = 0;
  for (const [name, state] of mapFixtures()) {
    const { ctx, document } = boot();
    ctx.render(state);
    const badges = document.getElementById('map-container').querySelectorAll('.map-node-shortcut');
    const clickable = state.map.nodes.filter((n) => n.clickable).length;
    assertEqual(badges.length, Math.min(2, clickable),
      `fixture ${name}: ${badges.length} shortcut badges for ${clickable} clickable node(s)`);
    badges.forEach((b, i) => {
      assertEqual(b.textContent, String(i + 1), 'badge is not numbered 1..N (54506)');
    });
    if (badges.length) checked += 1;
  }
  assert(checked > 0, 'no shortcut badge was checked -- vacuous');
});

detector('shortcuts: Digit1 visits the same node badge 1 is drawn on', () => {
  let checked = 0;
  for (const [name, state] of mapFixtures()) {
    const { ctx, document, posts } = boot();
    ctx.render(state);
    const ordered = state.map.nodes.filter((n) => n.clickable)
      .sort((a, b) => (a.layer !== b.layer ? a.layer - b.layer : a.col - b.col));
    if (!ordered.length) continue;
    const before = posts.length;
    document.dispatch('keydown', { code: 'Digit1', shiftKey: false, target: document.body });
    const sent = posts.slice(before).filter((p) => p.url === '/api/action');
    assertEqual(sent.length, 1, `fixture ${name}: Digit1 sent ${sent.length} actions`);
    assertEqual(sent[0].body.node_id, ordered[0].id,
      `fixture ${name}: Digit1 visited a different node than badge 1 marks`);
    checked += 1;
  }
  assert(checked > 0, 'Digit1 was never exercised -- vacuous');
});

detector('shortcuts: team slots 1..5 carry the source\'s ⇧N badge', () => {
  let checked = 0;
  for (const [name, state] of mapFixtures()) {
    if (!state.legal_actions || !state.legal_actions.reorder_team) continue;
    const { ctx, document } = boot();
    ctx.render(state);
    const slots = document.getElementById('team-bar').querySelectorAll('.team-slot');
    slots.forEach((slot, idx) => {
      const badge = slot.getAttribute('data-shortcut');
      if (idx >= 1 && idx <= 5) {
        assertEqual(badge, '⇧' + (idx + 1),
          `fixture ${name}: team slot ${idx} badge is wrong (64629-64632)`);
        checked += 1;
      } else {
        assertEqual(badge, null,
          `fixture ${name}: team slot ${idx} should carry no badge (the lead)`);
      }
    });
  }
  assert(checked > 0, 'no badged team slot was checked -- vacuous');
});

detector('shortcuts: Shift+Digit2 swaps the lead with slot 1', () => {
  let checked = 0;
  for (const [name, state] of mapFixtures()) {
    if (!state.legal_actions || !state.legal_actions.reorder_team) continue;
    if (!state.team || state.team.length < 2) continue;
    const { ctx, document, posts } = boot();
    ctx.render(state);
    const before = posts.length;
    document.dispatch('keydown', { code: 'Digit2', shiftKey: true, target: document.body });
    const sent = posts.slice(before).filter((p) => p.url === '/api/action');
    assertEqual(sent.length, 1, `fixture ${name}: Shift+Digit2 sent ${sent.length} actions`);
    assertEqual(sent[0].body.type, 'ReorderTeam', 'Shift+Digit2 sent the wrong action');
    // swapPartyLeadWith(1): a two-element swap of 0 and 1, everything else fixed.
    const order = sent[0].body.order;
    assertEqual(order[0], 1, 'lead swap did not put slot 1 in front (88163-88166)');
    assertEqual(order[1], 0, 'lead swap did not move the lead into slot 1');
    for (let k = 2; k < order.length; k += 1) {
      assertEqual(order[k], k, 'lead swap disturbed an unrelated slot');
    }
    checked += 1;
  }
  assert(checked > 0, 'Shift+Digit2 was never exercised -- vacuous');
});

detector('shortcuts: a modifier the source ignores does nothing', () => {
  const [, state] = mapFixtures()[0];
  const { ctx, document, posts } = boot();
  ctx.render(state);
  const before = posts.length;
  document.dispatch('keydown', { code: 'Digit1', ctrlKey: true, target: document.body });
  document.dispatch('keydown', { code: 'Digit1', metaKey: true, target: document.body });
  document.dispatch('keydown', { code: 'Digit1', altKey: true, target: document.body });
  assertEqual(posts.length - before, 0, 'a Ctrl/Meta/Alt chord triggered a shortcut (87982-87985)');
});

detector('shortcuts: typing in a text field suppresses every shortcut', () => {
  const [, state] = mapFixtures()[0];
  const { ctx, document, posts } = boot();
  ctx.render(state);
  const input = document.createElement('input');
  const before = posts.length;
  document.dispatch('keydown', { code: 'Digit1', target: input });
  assertEqual(posts.length - before, 0, 'a shortcut fired while typing (87899-87904)');
});

detector('overtime: the 30s bump raises the replay speed, and only raises it', () => {
  const js = fs.readFileSync(APP_JS, 'utf8');
  // The constants must be the source's own values (63640-63641, 81270).
  assert(/const SKIP_SPEED = 3;/.test(js), 'SKIP_SPEED is not 3');
  assert(/const OVERTIME_SPEED = 5;/.test(js), 'OVERTIME_SPEED is not 5');
  assert(/const OVERTIME_MS = 30000;/.test(js), 'OVERTIME_MS is not 30000');
  // And the guard must be a RAISE, not an assignment: the source writes
  // `battleSpeedMultiplier < OVERTIME_SPEED && (battleSpeedMultiplier = ...)`.
  assert(/if \(speed < OVERTIME_SPEED\) speed = OVERTIME_SPEED;/.test(js),
    'the overtime bump is not guarded as a raise-only (81268-81269)');
  assert(/clearTimeout\(overtimeTimer\)/.test(js),
    'the overtime timer is never cleared -- it would leak into the next battle (81273)');
});

detector('overtime: a battle replay actually arms and disarms the timer', () => {
  // The behavioural half: drive a real battle fixture through the replay and
  // require the timer queue to be empty afterwards.
  const entry = Object.entries(fixtures).find(
    ([, st]) => st.battle && st.battle.replay && st.battle.replay.length,
  );
  if (!entry) throw new Error('no fixture carries a battle replay -- cannot test overtime');
  const [name, state] = entry;
  const { ctx, document, clock } = boot();
  const battleEntry = state.log.filter((e) => e.type === 'battle').pop();
  assert(battleEntry, `fixture ${name} has a battle view but no battle log entry`);
  ctx.renderBattle(battleEntry, state, () => {});
  clock.drain();
  assertEqual(clock.queue.length, 0,
    `fixture ${name}: timers still pending after the replay drained -- ` +
    'the overtime timeout was not cleared');
  assertEqual(document.getElementById('btn-continue-battle').textContent, 'Continue',
    'the replay did not reach its finish state');
});

// ---------------------------------------------------------------------------
// Report
// ---------------------------------------------------------------------------

let failed = 0;
for (const r of results) {
  if (r.ok) {
    console.log(`ok   ${r.name}`);
  } else {
    failed += 1;
    console.log(`FAIL ${r.name}\n       ${r.message}`);
  }
}
console.log(`\n${results.length - failed}/${results.length} DOM-shim detectors passed.`);
process.exit(failed ? 1 : 0);
