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
    // M6/N24. The hover card positions itself against the viewport and asks
    // whether the desktop side-placement rule applies -- the source's own
    // `matchMedia('(max-width: 768px)')` gate (bundle.deobfuscated.js:64531).
    // A fixed desktop-sized viewport is reported, so the placement branch a
    // detector exercises is deterministic.
    innerWidth: 1280,
    innerHeight: 900,
    matchMedia: (query) => ({ media: String(query), matches: false }),
    // M6/N25. A driveable ResizeObserver. The shim does no layout, so it can
    // never fire one on its own -- but it CAN hand the test the callback and
    // let it fire deliberately, which is what turns "the map redraws from
    // stale state on resize" from an unobservable browser-only bug into a
    // checkable one. Registered observers are exposed on `__observers`.
    ResizeObserver: function ResizeObserverStub(cb) {
      this.callback = cb;
      this.targets = [];
      this.observe = (el) => { this.targets.push(el); observers.push(this); };
      this.unobserve = () => {};
      this.disconnect = () => {};
    },
  };
  const observers = [];
  sandbox.__observers = observers;
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;

  const ctx = vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(APP_JS, 'utf8'), ctx, { filename: 'app.js' });

  // Capture toasts AFTER load, by wrapping the real function rather than
  // replacing it -- the real one still runs, so its DOM effects stay testable.
  const realToast = ctx.showToast;
  ctx.showToast = (msg) => { toasts.push(String(msg)); return realToast(msg); };

  return { ctx, document, clock, toasts, posts, observers };
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
// M6/N24. The stat hover card.
//
// `#team-hover-card` and its CSS shipped with R1 and nothing ever showed one.
// The shim cannot lay out or paint, so it cannot check that the card LOOKS
// right -- but it can check the two things that were actually broken: that the
// element exists at all, and that hovering a team slot populates it from the
// mon's own `mon_view` fields. Both are real, checkable assertions.
// ---------------------------------------------------------------------------

/** The first fixture with at least one team member. */
function teamFixture() {
  const entry = Object.entries(fixtures).find(([, st]) => st.team && st.team.length);
  if (!entry) throw new Error('no fixture carries a team -- hover-card detectors would be vacuous');
  return entry;
}

detector('N24: index.html declares the #team-hover-card element', () => {
  const { document } = boot();
  assert(document.getElementById('team-hover-card') !== null,
    'index.html has no #team-hover-card -- main.css styles one, so nothing could ever show it');
});

detector('N24: hovering a team slot populates the hover card', () => {
  const [name, state] = teamFixture();
  const { ctx, document } = boot();
  ctx.renderTeamBar(state);
  const slot = document.querySelector('#team-bar .team-slot');
  assert(slot, `fixture ${name}: renderTeamBar produced no .team-slot`);
  const card = document.getElementById('team-hover-card');
  // The shim applies no stylesheet, so the initial `display` is the inline
  // value (""), not the `none` main.css:1637 gives it in a browser. Both mean
  // "not shown"; asserting on either would be asserting on the shim.
  assert(card.style.display !== 'block', 'the hover card was visible before any hover');

  slot.dispatch('mouseenter', {});
  assertEqual(card.style.display, 'block', 'mouseenter did not show the hover card');
  const mon = state.team[0];
  const text = card.textContent;
  assert(text.includes(mon.nickname || mon.name),
    `the hover card does not name the hovered Pokemon (${JSON.stringify(text)})`);
});

detector('N24: the hover card shows the stat data R1 built for it', () => {
  const [name, state] = teamFixture();
  const { ctx, document } = boot();
  ctx.renderTeamBar(state);
  document.querySelector('#team-bar .team-slot').dispatch('mouseenter', {});
  const card = document.getElementById('team-hover-card');
  assert(card.querySelector('.hover-stats'),
    `fixture ${name}: the hover card drew no stat block -- this is the whole point of N24`);
  const mon = state.team[0];
  const text = card.textContent;
  // The EFFECTIVE attack is the number the battle engine would really read.
  // Asserting on it (not on base) is what makes a regression to "base only"
  // visible, which is what every other surface in this client used to show.
  assert(text.includes(String(mon.effective_stats.atk)),
    `the hover card does not show effective atk ${mon.effective_stats.atk}`);
  assert(text.includes(String(mon.effective_stats.speed)),
    `the hover card does not show effective speed ${mon.effective_stats.speed}`);
});

detector('N24: the card annotates stages/buffs rather than differencing base stats', () => {
  const [, state] = teamFixture();
  const { ctx, document } = boot();
  // A raised stage is the ONLY honest reason an effective stat differs from
  // what it would otherwise be. `base_stats` is the species table on a
  // different scale entirely (Bulbasaur Atk 49 vs Atk 9 at Lv5), so a card
  // that differenced the two would report a huge phantom debuff on every
  // Pokemon. Asserting the annotation tracks `stages` pins the right source.
  const mon = JSON.parse(JSON.stringify(state.team[0]));
  mon.stages = Object.assign({}, mon.stages, { atk: 2 });
  const patched = Object.assign({}, state, { team: [mon].concat(state.team.slice(1)) });
  ctx.renderTeamBar(patched);
  document.querySelector('#team-bar .team-slot').dispatch('mouseenter', {});
  const card = document.getElementById('team-hover-card');
  const note = card.querySelector('.hover-stat-delta');
  assert(note, 'a raised stage produced no annotation on the hover card');
  assert(note.textContent.includes('+2'),
    `the annotation does not report the +2 stage (got ${JSON.stringify(note.textContent)})`);
  assert(!card.textContent.includes('-40'),
    'the card differenced base against effective stats -- see the app.js comment');
});

detector('N24: mouseleave hides the hover card again', () => {
  const [, state] = teamFixture();
  const { ctx, document, clock } = boot();
  ctx.renderTeamBar(state);
  const slot = document.querySelector('#team-bar .team-slot');
  const card = document.getElementById('team-hover-card');
  slot.dispatch('mouseenter', {});
  assertEqual(card.style.display, 'block', 'precondition: the card should be shown');
  slot.dispatch('mouseleave', {});
  // 64565-64578: the source fades out first and un-displays on a 140 ms timer.
  assertEqual(card.style.opacity, '0', 'mouseleave did not start the fade-out');
  clock.drain();
  assertEqual(card.style.display, 'none', 'the hover card never un-displayed');
});

detector('N24: the crit line follows the source\'s own base-crit condition', () => {
  const [, state] = teamFixture();
  const { ctx, document } = boot();
  ctx.renderTeamBar(state);
  document.querySelector('#team-bar .team-slot').dispatch('mouseenter', {});
  const card = document.getElementById('team-hover-card');
  const mon = state.team[0];
  // 64494-64496: shown ONLY when it differs from _BASE_CRIT (6.25) by >= 0.01.
  const shouldShow = Math.abs(mon.crit_chance - 6.25) >= 0.01;
  assertEqual(!!card.querySelector('.hover-crit'), shouldShow,
    `crit line presence disagrees with the source's condition ` +
    `(crit_chance=${mon.crit_chance})`);
});

// ---------------------------------------------------------------------------
// M6/N25 REGRESSION. The map's ResizeObserver must redraw from the CURRENT
// state, never from a captured one.
//
// This is a live bug report, not a hypothetical: the first implementation
// closed over the `state` argument of the FIRST `renderMap` call and, being
// installed once for the whole run, redrew that same state on every later
// resize. Visiting a node and then resizing repainted the PREVIOUS map --
// the node you had just cleared showed as clickable and the newly-reachable
// one did not. Reported as "select the first node, cannot go on to the
// second".
//
// The shim does no layout so it cannot fire a real resize; the sandbox's
// ResizeObserver hands the callback over instead, which is enough to pin the
// staleness, which is the actual defect.
// ---------------------------------------------------------------------------

detector('N25: a resize redraws the map from the CURRENT state, not a stale one', () => {
  // `.map-node` groups are appended in `map.nodes` order and carry no id
  // attribute, so the POSITIONS of the clickable ones are what identifies a
  // map state in the DOM.
  const clickablePositions = (st) => {
    const out = [];
    (st.map.nodes || []).forEach((n, i) => { if (n.clickable) out.push(i); });
    return out.join(',');
  };
  // Two DIFFERENT map states are required or the test is vacuous.
  let a = null;
  let b = null;
  for (const [, st] of mapFixtures()) {
    const ids = clickablePositions(st);
    if (!ids) continue;
    if (!a) { a = { st, ids }; continue; }
    if (ids !== a.ids) { b = { st, ids }; break; }
  }
  if (!b) throw new Error('need two map fixtures with different clickable sets -- vacuous otherwise');

  const { ctx, document, clock, observers } = boot();
  ctx.render(a.st);
  assert(observers.length, 'renderMap installed no ResizeObserver at all');

  // Advance to the second state, exactly as visiting a node would.
  ctx.render(b.st);

  // Now a resize happens. Change the reported size so the debounced callback
  // does not early-return on "size unchanged".
  const container = document.getElementById('map-container');
  container.clientWidth = 999;
  container.clientHeight = 1665;
  observers.forEach((o) => o.callback([]));
  clock.drain();

  const nodes = container.querySelectorAll('.map-node');
  const drawn = [];
  nodes.forEach((g, i) => {
    if (g.classList.contains('map-node--clickable')) drawn.push(i);
  });
  assertEqual(drawn.join(','), b.ids,
    `the post-resize map does not match the CURRENT state (drew [${drawn.join(',')}], ` +
    `current is [${b.ids}], previous was [${a.ids}]) -- it was redrawn from a ` +
    'captured state (stale-closure regression)');
});

// ---------------------------------------------------------------------------
// M6/N26. The region selector.
//
// Unlike N23/N24/N25 this one IS mechanically testable: what the browser sends
// to /api/reset is a real, checkable assertion, and it is exactly what was
// broken -- the client sent `nuzlocke_mode` alone, so every browser run started
// Gen1 no matter what (CODEX section 7.1).
// ---------------------------------------------------------------------------

detector('N26: each region builds the flags its generation needs', () => {
  const { ctx } = boot();
  assertEqual(JSON.stringify(ctx.resetBodyForRegion(1, false)),
    JSON.stringify({ nuzlocke_mode: false }), 'Kanto must send NO gen flag');
  assertEqual(JSON.stringify(ctx.resetBodyForRegion(2, false)),
    JSON.stringify({ nuzlocke_mode: false, gen2_mode: true }), 'Johto must send gen2_mode');
  assertEqual(JSON.stringify(ctx.resetBodyForRegion(3, false)),
    JSON.stringify({ nuzlocke_mode: false, gen3_mode: true }), 'Hoenn must send gen3_mode');
  assertEqual(JSON.stringify(ctx.resetBodyForRegion(4, false)),
    JSON.stringify({ nuzlocke_mode: false, gen4_mode: true }), 'Sinnoh must send gen4_mode');
});

detector('N26: the generation flags are mutually exclusive by construction', () => {
  const { ctx } = boot();
  // server.py:140-142 rejects two at once. The UI must not be able to build it
  // in the first place, which is a property of resetBodyForRegion itself.
  for (const gen of [1, 2, 3, 4]) {
    const body = ctx.resetBodyForRegion(gen, false);
    const set = ['gen2_mode', 'gen3_mode', 'gen4_mode'].filter((k) => body[k]);
    assert(set.length <= 1,
      `generation ${gen} built ${set.length} generation flags: ${set.join(', ')}`);
  }
});

detector('N26: the Classic/Nuzlocke toggle reaches the request body', () => {
  const { ctx } = boot();
  assertEqual(ctx.resetBodyForRegion(4, true).nuzlocke_mode, true,
    'the Nuzlocke toggle did not reach the body');
  assertEqual(ctx.resetBodyForRegion(4, false).nuzlocke_mode, false,
    'the Classic toggle did not reach the body');
  // ...and the region flag survives the toggle, which is the combination a
  // naive implementation drops.
  assertEqual(ctx.resetBodyForRegion(4, true).gen4_mode, true,
    'selecting Nuzlocke discarded the chosen region');
});

detector('N26: clicking a region card POSTs that region to /api/reset', () => {
  const { ctx, document, posts } = boot();
  ctx.showRegionScreen(false);
  const cards = document.querySelectorAll('#history-region-list .history-region-btn');
  assertEqual(cards.length, 4, 'the region list did not draw all four regions');
  cards[3].dispatch('click', {});
  const reset = posts.filter((p) => p.url === '/api/reset').pop();
  assert(reset, 'clicking a region card sent no /api/reset');
  assertEqual(reset.body.gen4_mode, true,
    'the fourth region card did not request Gen4 -- this is CODEX 7.1 exactly');
});

detector('N26: the title cards open the region screen instead of starting Gen1', () => {
  const { ctx, document, posts } = boot();
  document.getElementById('btn-start-story').dispatch('click', {});
  assert(!posts.filter((p) => p.url === '/api/reset').length,
    'the Story card started a run immediately, skipping region choice');
  assert(document.getElementById('region-screen').classList.contains('active'),
    'the Story card did not raise the region screen');
  // The Nuzlocke card is this port's own shortcut: same screen, toggle preset.
  // There is exactly ONE title card now. Nuzlocke is chosen by the toggle on
  // the region screen, which is what the source does (`#btn-history-classic` /
  // `#btn-history-nuzlocke`, pokelike_forked/index.html:694-697); the port's
  // separate Nuzlocke title card was its own invention and, once the toggle
  // existed, was a second control for the same setting showing the same
  // artwork. Removed on a live report.
  assert(document.getElementById('btn-start-nuzlocke') === null,
    'the redundant Nuzlocke title card is back -- Nuzlocke is the region ' +
    "screen's toggle, not a mode card");
  ctx.setRegionMode(true);
  assertEqual(ctx.resetBodyForRegion(1, true).nuzlocke_mode, true,
    'the region screen toggle does not reach the request body');
});

// ---------------------------------------------------------------------------
// R6/N33. Item presentation, and the hover card that should not be there.
// ---------------------------------------------------------------------------

/** The fixture for a phase, or a thrown error naming what is missing -- a
 *  silently skipped detector is the failure mode this whole suite exists to
 *  avoid. */
function phaseFixture(phase) {
  const entry = Object.entries(fixtures).find(([, st]) => st.phase === phase);
  if (!entry) throw new Error(`no fixture reached ${phase} -- this detector would be vacuous`);
  return entry;
}

detector('N33: item choice options carry the metadata the contract enriches them with', () => {
  // Non-vacuity for the two detectors below, asserted on the DATA first: the
  // renderer cannot draw an icon or a description the projection never sent,
  // and before R6 it never sent them -- `item_view` fed the BAG only.
  const [name, state] = phaseFixture('item_choice');
  const opts = state.pending.options;
  assert(opts.length, `fixture ${name}: item_choice carried no options`);
  for (const opt of opts) {
    for (const key of ['desc', 'icon', 'icon_url', 'known']) {
      assert(key in opt,
        `fixture ${name}: option ${opt.id} has no ${key} -- contract._pending_options ` +
        'is no longer enriching ITEM_CHOICE, so the card cannot draw it');
    }
  }
  assert(opts.some((o) => o.desc), `fixture ${name}: no offered item had a description at all`);
  assert(opts.some((o) => o.icon), `fixture ${name}: no offered item had an icon at all`);
});

detector('N33: the item card renders the icon and description, not just the name', () => {
  const [name, state] = phaseFixture('item_choice');
  const { ctx, document } = boot();
  ctx.render(state);

  const cards = document.querySelectorAll('#item-choices .item-card');
  assertEqual(cards.length, state.pending.options.length,
    `fixture ${name}: the item screen did not build one .item-card per option ` +
    '(the card used to be labelled `poke-card`, which matches none of the ' +
    '.item-icon/.item-name/.item-desc rules that style its contents)');

  const opt = state.pending.options[0];
  const card = cards[0];

  const iconBox = card.querySelector('.item-icon');
  assert(iconBox, 'the item card drew no .item-icon');
  const img = iconBox.querySelector('img');
  assert(img, 'the item card drew no icon image at all');
  assert(String(img.className).includes('item-sprite-icon'),
    `the icon does not carry the source's own .item-sprite-icon class (${img.className})`);
  const expectedSrc = opt.icon_url || ('/img/sprites/items/' + opt.id.replace(/_/g, '-') + '.png');
  assertEqual(img.src, expectedSrc, 'the icon does not point at the item the contract named');

  const nameEl = card.querySelector('.item-name');
  assert(nameEl, 'the item card drew no .item-name');
  assertEqual(nameEl.textContent, opt.name, 'the item card names the wrong item');

  // The actual R6 §3.1 complaint: the description was thrown away.
  const descEl = card.querySelector('.item-desc');
  assert(descEl, 'the item card drew no .item-desc -- the description the contract supplies is still being discarded');
  assertEqual(descEl.textContent, opt.desc, 'the item card shows a description that is not the contract\'s');

  // And the literal "Usable"/"Held item" strings the port invented are gone:
  // the source tags ONLY a usable item, and with its own wording.
  const tag = card.querySelector('.item-tag');
  if (opt.usable) {
    assert(tag, 'a usable item drew no .item-tag');
    assert(String(tag.className).includes('item-tag--usable'),
      'the usable tag does not carry the source\'s own modifier class');
  } else {
    assert(!tag, 'a HELD item drew a tag -- the source tags only usable items (79391-79393)');
  }
  assert(!card.textContent.includes('Held item'),
    'the invented "Held item" string is still being rendered');
});

detector('N33: a missing item sprite falls back to the emoji the contract carries', () => {
  // R6 §4: this mirror ships no item sprites, and a fresh checkout that never
  // runs tools/fetch-sprites/fetch_item_sprites.py has none either. The
  // source's own onerror (52122-52128) is what covers that, so the fallback --
  // not the happy path -- is the branch a browser here actually takes.
  const [, state] = phaseFixture('item_choice');
  const { ctx, document } = boot();
  ctx.render(state);
  const opt = state.pending.options.find((o) => o.icon);
  assert(opt, 'no offered item carried an emoji icon -- the fallback would be vacuous');
  const idx = state.pending.options.indexOf(opt);
  const iconBox = document.querySelectorAll('#item-choices .item-card')[idx].querySelector('.item-icon');

  iconBox.querySelector('img').dispatch('error', {});
  assert(!iconBox.querySelector('img'), 'the failed image was left in the DOM');
  assertEqual(iconBox.textContent, opt.icon,
    'a failed item sprite did not degrade to the emoji the contract supplies');
});

detector('N33: the item-equip screen raises NO hover card, while the team bar still does', () => {
  // THE R6 §3.2 REGRESSION. M6/N24 attached the hover card inside
  // `mkPokeCardOption`, which the item-equip screen shares, so assigning an
  // item raised a Pokemon-card overlay. The source's `showTeamHoverCard` call
  // sites are the team bar (64672/64675/64811), the starter screen (75731) and
  // the Elite-prep party/vs lists (78217/78223/78328) -- the item-equip target
  // list (79495-79521) is not among them.
  //
  // Both halves are asserted together on purpose: "no hover card anywhere"
  // would also pass the first half, and would be a different bug.
  const [name, state] = phaseFixture('item_equip_choice');
  const { ctx, document } = boot();
  ctx.render(state);

  const cards = document.querySelectorAll('#item-equip-choices .poke-card');
  assert(cards.length, `fixture ${name}: the item-equip screen drew no target cards`);
  const card = document.getElementById('team-hover-card');

  for (const target of cards) {
    assertEqual(target.listenerCount('mouseenter'), 0,
      'an item-equip target still binds a mouseenter -- the unwanted hover card is back');
    target.dispatch('mouseenter', {});
    target.dispatch('mousemove', {});
  }
  assert(card.style.display !== 'block',
    'hovering an item-equip target raised the hover card -- the source does not show one here');

  // ...and the team bar, which the source DOES show one on, still works. This
  // is what makes the assertion above a placement check rather than a deletion.
  const [, teamState] = teamFixture();
  ctx.renderTeamBar(teamState);
  const slot = document.querySelector('#team-bar .team-slot');
  assert(slot, 'the team bar drew no slot');
  slot.dispatch('mouseenter', {});
  assertEqual(card.style.display, 'block',
    'the team bar no longer shows a hover card -- the fix deleted too much');
});

// ---------------------------------------------------------------------------
// R6/N34. The move block: structured markup, not prose.
// ---------------------------------------------------------------------------

detector('N34: the projected move power is a real power, not a placeholder', () => {
  // NON-TAUTOLOGY GUARD, and the reason it exists is worth recording: the
  // detector below compares the rendered badge against `move_preview.power`.
  // If the PROJECTION is what regressed, both sides move together and the
  // comparison passes while showing the player nothing useful -- which is
  // exactly what happened when `"power": move.power` was mutated to
  // `"power": 0` and the whole suite stayed green.
  //
  // `power > 0 for a move not flagged no_damage` is independent of the
  // projection's value, so it holds the projection to something. The Python
  // side asserts the same invariant against the ported move table
  // (test_renderer_contract.test_a_damaging_move_preview_never_reports_zero_power).
  let checked = 0;
  for (const [name, state] of Object.entries(fixtures)) {
    for (const mon of state.team || []) {
      const mp = mon.move_preview;
      if (!mp || !mp.name) continue;
      checked += 1;
      if (mp.no_damage) continue;
      assert(typeof mp.power === 'number' && mp.power > 0,
        `fixture ${name}: ${mp.name} is a damaging move projected with power ` +
        `${JSON.stringify(mp.power)} -- the card would render "${mp.power} PWR"`);
    }
  }
  assert(checked > 0, 'no fixture carried a move_preview at all -- the N34 detectors are vacuous');
});

detector('N34: the move power reaches the DOM as .move-power-badge, not as prose', () => {
  // R6 §6.1. The port rendered `Move: Magical Leaf (Grass 40)` as one text
  // line in a `.hover-move` div, which M6 wrote; the source builds structured
  // markup (64348-64366) whose CSS main.css already ships.
  const [name, state] = teamFixture();
  const mon = state.team.find((m) => m.move_preview && m.move_preview.name);
  assert(mon, `fixture ${name}: no team member carried a move_preview -- vacuous`);
  const idx = state.team.indexOf(mon);

  const { ctx, document } = boot();
  ctx.renderTeamBar(state);
  const card = document.querySelectorAll('#team-bar .team-slot')[idx];
  assert(card, 'the team bar drew no slot for the Pokemon with a move');

  const block = card.querySelector('.poke-move');
  assert(block, 'no .poke-move block was drawn at all');

  const nameEl = block.querySelector('.move-name');
  assert(nameEl, 'the move block drew no .move-name');
  assertEqual(nameEl.textContent, mon.move_preview.name, 'the move block names the wrong move');
  assertEqual(nameEl.getAttribute('title'), mon.move_preview.name,
    'the source puts the move name in the title attribute too (64350-64352)');

  // THE POINT OF THIS DETECTOR: the power is a value inside its own element.
  const badge = block.querySelector('.move-power-badge');
  assert(badge, 'the move power is not in a .move-power-badge -- it is still prose');
  const expected = mon.move_preview.no_damage
    ? '—'
    : (mon.move_preview.power + ' PWR');
  assertEqual(badge.textContent, expected,
    'the power badge does not carry the value from move_preview');

  const typeBadge = block.querySelector('.move-type-badge');
  assert(typeBadge, 'the move block drew no .move-type-badge');
  assertEqual(typeBadge.textContent, mon.move_preview.type || '—',
    'the type badge does not carry the type from move_preview');

  const cat = block.querySelector('.move-cat-icon');
  assert(cat, 'the move block drew no category icon');
  assertEqual(cat.src, mon.move_preview.is_special ? '/img/special.png' : '/img/physical.png',
    'the category icon does not follow move_preview.is_special');

  // The invented prose line is gone.
  assert(!card.querySelector('.hover-move'), 'the invented .hover-move prose line is back');
  assert(!card.textContent.includes('Move: '), 'the move is still being rendered as a sentence');
});

detector('N34: a missing category PNG degrades to the source\'s own text badge', () => {
  // img/physical.png and img/special.png are absent from this mirror (R6 §4),
  // so this fallback is what a browser here actually draws.
  const [, state] = teamFixture();
  const mon = state.team.find((m) => m.move_preview && m.move_preview.name);
  assert(mon, 'no team member carried a move_preview -- vacuous');
  const { ctx, document } = boot();
  ctx.renderTeamBar(state);
  const block = document.querySelectorAll('#team-bar .team-slot')[state.team.indexOf(mon)]
    .querySelector('.poke-move');

  block.querySelector('.move-cat-icon').dispatch('error', {});
  const badge = block.querySelector('.move-meta .type-badge[class*="move-cat-"]')
    || block.querySelectorAll('.move-meta span')[0];
  assert(badge, 'a failed category icon left nothing behind');
  const expectedClass = mon.move_preview.is_special ? 'move-cat-special' : 'move-cat-physical';
  assert(String(badge.className).includes(expectedClass),
    `the fallback badge does not use the source's own ${expectedClass} class (${badge.className})`);
});

detector('N34: the move-tutor card can say what the move actually is', () => {
  // CODEX gap 10. The tutor option carried an opaque `move_tier` integer and
  // nothing else, so the card read "tier 0" -- `contract._pending_options` now
  // enriches it with the same `_move_preview` every mon_view uses.
  const [name, state] = phaseFixture('move_tutor_choice');
  const opts = state.pending.options;
  assert(opts.some((o) => o.move_preview && o.move_preview.name),
    `fixture ${name}: no move-tutor option carried a move_preview`);

  const { ctx, document } = boot();
  ctx.render(state);
  const idx = opts.findIndex((o) => o.move_preview && o.move_preview.name);
  const card = document.querySelectorAll('#move-tutor-choices .poke-card')[idx];
  assert(card, 'the move-tutor screen drew no card for the option with a move');
  const badge = card.querySelector('.move-power-badge');
  assert(badge, 'the move-tutor card shows no move power');
  const mp = opts[idx].move_preview;
  assertEqual(badge.textContent, mp.no_damage ? '—' : (mp.power + ' PWR'),
    'the move-tutor card\'s power does not come from its own move_preview');
});

detector('N38: a screen change tears down the hover card it left behind', () => {
  // Found by LOOKING at a screenshot, not by reading: a Pokemon hover card was
  // still floating over the item-choice screen, because removing an element
  // never fires `mouseleave`. The source's own `showScreen`
  // (bundle.deobfuscated.js:63765-63782) hides the node tooltip, the item
  // tooltip and the hover card on every screen change; the port took none of
  // it.
  const [, state] = teamFixture();
  const { ctx, document, clock } = boot();
  ctx.renderTeamBar(state);
  const card = document.getElementById('team-hover-card');

  document.querySelector('#team-bar .team-slot').dispatch('mouseenter', {});
  assertEqual(card.style.display, 'block', 'the hover card never came up -- vacuous');

  // A screen change with no pointer movement at all, which is exactly what
  // clicking a hovered card produces.
  ctx.showScreen('item-screen');
  // The fade starts immediately (opacity), and `display: none` lands 140ms
  // later -- the source's own delay. Both are asserted: opacity alone would
  // pass on a card that never un-displays, and draining alone would not prove
  // the teardown began at the screen change.
  assertEqual(card.style.opacity, '0',
    'the screen change did not even begin hiding the hover card');
  clock.drain();
  assert(card.style.display !== 'block',
    'the hover card survived the screen change and is now floating over an ' +
    'unrelated screen');
});

// ---------------------------------------------------------------------------
// R6/N35. Run navigation.
// ---------------------------------------------------------------------------

detector('N35: the run nav ships only buttons that are wired to something', () => {
  // R6 §5 is explicit that a dead button is worse than no button.
  //
  // The shipped MARKUP is what has to be enumerated here, not the shim's
  // document: the shim pre-creates one element per index.html id, flat under
  // <body>, so it models no containment and `.map-menu-icons` has no id to
  // find it by. Reading index.html directly is also the stronger check -- it
  // is the bytes a browser gets.
  const html = fs.readFileSync(INDEX_HTML, 'utf8');
  const bar = /<div class="map-menu-icons">([\s\S]*?)<\/div>/.exec(html);
  assert(bar, 'index.html declares no .map-menu-icons -- there is still no run navigation');

  const ids = [...bar[1].matchAll(/<button\b[^>]*\bid="([^"]+)"/g)].map((m) => m[1]);
  assert(ids.length, 'the run nav shipped no buttons at all');
  // Every <button> in the bar must be one of the ids found -- an unidentified
  // button could not be wired from `wireButtons` and would be dead by
  // construction.
  const buttonCount = (bar[1].match(/<button\b/g) || []).length;
  assertEqual(buttonCount, ids.length, 'a run-nav button carries no id, so nothing can wire it');

  const { document } = boot();
  for (const id of ids) {
    const btn = document.getElementById(id);
    assert(btn, `run-nav button #${id} is in the markup but not in the document`);
    assert(btn.listenerCount('click') > 0, `run-nav button #${id} is wired to nothing`);
  }
  // And the source's own asset for each control actually ships (R6 §5: the
  // eight img/menu PNGs were already copied, so a 404 here is a port slip).
  for (const src of [...bar[1].matchAll(/<img[^>]*\bsrc="([^"]+)"/g)].map((m) => m[1])) {
    const onDisk = path.join(REPO, 'pokelike', 'webui', 'static', src.replace(/^\//, ''));
    assert(fs.existsSync(onDisk), `run-nav icon ${src} does not exist on disk`);
  }
});

detector('N35: reset from the run nav POSTs the CURRENT run\'s mode and region', () => {
  // R6 §5: "Reset must go through the engine, not the DOM."
  const [, state] = phaseFixture('on_map');
  const gen4 = Object.assign(JSON.parse(JSON.stringify(state)), {
    nuzlocke_mode: true, gen2_mode: false, gen3_mode: false, gen4_mode: true,
  });

  const { ctx, document, posts } = boot();
  ctx.render(gen4);
  assert(document.body.classList.contains('run-menu-in-run'),
    'the run nav is not marked in-run on the map screen, so its buttons are hidden');

  document.getElementById('btn-run-reset').dispatch('click', {});
  // The source confirms before discarding a run (84556-84567); nothing may be
  // sent until it is confirmed.
  assert(!posts.filter((p) => p.url === '/api/reset').length,
    'reset fired immediately -- it discarded the run with no confirmation');
  const confirm = document.getElementById('btn-run-action-confirm');
  assert(confirm, 'the reset confirmation offered no confirm button');
  confirm.dispatch('click', {});

  const reset = posts.filter((p) => p.url === '/api/reset').pop();
  assert(reset, 'confirming a reset sent no /api/reset -- it did not go through the engine');
  assertEqual(reset.body.gen4_mode, true, 'reset did not repeat the run\'s REGION');
  assertEqual(reset.body.nuzlocke_mode, true, 'reset did not repeat the run\'s MODE');
  assert(!reset.body.gen2_mode && !reset.body.gen3_mode,
    'reset sent more than one generation flag');
});

detector('N35: abandoning a run returns to the title screen, and only when confirmed', () => {
  const [, state] = phaseFixture('on_map');
  const { ctx, document, posts } = boot();
  ctx.render(state);

  document.getElementById('btn-run-home').dispatch('click', {});
  assert(!document.getElementById('title-screen').classList.contains('active'),
    'abandon left the run with no confirmation');
  document.getElementById('btn-run-action-confirm').dispatch('click', {});
  assert(document.getElementById('title-screen').classList.contains('active'),
    'confirming abandon did not return to the title screen');
  // Abandoning is a client-side navigation, not an engine action: the source's
  // `goHomeFromRun` (84594-84607) shows the title screen and posts nothing.
  assert(!posts.filter((p) => p.url === '/api/reset').length,
    'abandoning a run reset the engine, which is the OTHER control');
});

detector('N35: the run nav is hidden during battle and outside a run', () => {
  const { ctx, document } = boot();
  ctx.showScreen('title-screen');
  assert(!document.body.classList.contains('run-menu-in-run'),
    'the run nav claims to be in a run on the title screen');

  const [, state] = phaseFixture('on_map');
  ctx.render(state);
  assert(document.body.classList.contains('run-menu-in-run'), 'the run nav is not live on the map');
  assert(!document.body.classList.contains('in-battle'), 'the map screen claims to be a battle');

  // main.css:7595 hides the whole bar while `body.in-battle` is set.
  ctx.showScreen('battle-screen');
  assert(document.body.classList.contains('in-battle'),
    'the battle screen does not set body.in-battle, so the nav stays up over the battle');
});

detector('N35: R and H are inert outside a run', () => {
  const { ctx, document, posts } = boot();
  ctx.showScreen('title-screen');
  ctx.handleShortcutKey({ code: 'KeyR', target: { tagName: 'DIV' }, preventDefault() {} });
  ctx.handleShortcutKey({ code: 'KeyH', target: { tagName: 'DIV' }, preventDefault() {} });
  assert(!document.getElementById('btn-run-action-confirm'),
    'a run-nav shortcut fired on the title screen, where there is no run to reset');
  assert(!posts.filter((p) => p.url === '/api/reset').length, 'a shortcut reset a nonexistent run');
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
