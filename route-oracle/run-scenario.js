// The JavaScript side of the M3 short-full-run route oracle.
//
// Executes a deterministic Story/Nuzlocke route through the REAL source
// functions in the audited route prefix (`out/route-prefix.js`, produced by
// extract-prefix.js -- see that file's header for the cut point and its
// safety reasoning) and emits a normalized checkpoint stream on stdout.
//
//   node run-scenario.js <scenario.json>        -> one JSON object on stdout
//
// ---------------------------------------------------------------------------
// WHAT IS REAL AND WHAT IS STUBBED
// ---------------------------------------------------------------------------
//
// Real (never reimplemented here): startNewRun, showStarterSelect,
// selectStarter, startMap, generateMap, generateSubMap, onNodeClick,
// advanceFromNode, doBattleNode, doBossNode, doTrainerNode, doSilverNode,
// doAdminNode, doCatchNode, doItemNode, doPokeCenterNode, doLegendaryNode,
// doMoveTutorNode, doTradeNode, doShinyNode, enterSubMap, doSubMapBoss,
// doSubMapReward, returnFromSubMap, showSwapScreen, catchPokemon,
// runBattleScreen, runBattle, applyLevelGain, checkAndEvolveTeam,
// grantPickupItem, incrementStoryCounter, showBadgeScreen (its "next map"
// handler IS the map transition), rollShiny, createInstance,
// fetchPokemonById, weightedRandom, rng/seedRng/getRngSeed.
//
// Stubbed, and ONLY these categories:
//   * presentation  - showScreen (also used as the source's own "which
//                     screen is the player sitting on" signal), renderMap,
//                     showMapScreen, renderBattleField, renderTeamBar,
//                     renderItemBadges, renderTrainerIcons, renderPokemonCard,
//                     animateBattleVisually, animateLevelUp, showToast,
//                     showMapNotification, showElitePrepScreen,
//                     showTrainerSelect, makeTraitOverlay,
//                     renderTraitPreview/renderTraitDeltaRows,
//                     showTeamHoverCard/hideTeamHoverCard, makeMaxedStarsEl,
//                     showGameOver (pure end-of-run screen + account stats).
//   * persistence   - saveRun, and the `localStorage` object itself (an
//                     in-memory store installed AFTER load; see below).
//                     `captureMapStart`, `isSpeciesOwned`, `ownershipBadges`,
//                     `loadPersistentBuffs`, `markPokedexCaught` and friends
//                     are REAL and correctly behave as a brand-new account
//                     against that empty store.
//                     `recordMonOrigin` is REAL and deliberately NOT stubbed:
//                     despite the name, its body (bundle.deobfuscated.js:
//                     79047-79063) sets `state.usedBallCatch` /
//                     `state.gotViaQuestion`, which are RUN state this schema
//                     compares. Only the story counter it then increments is
//                     absorbed by the in-memory store. Stubbing it SUPPRESSED
//                     gameplay state, which is as wrong as inventing some.
//                     (M3.3: this header previously still listed it as
//                     stubbed, contradicting driver.js.)
//   * timers/audio  - setTimeout/setInterval/requestAnimationFrame are routed
//                     through a deterministic virtual queue; there is no audio.
//   * network       - none is reachable on the routes used; `fetch` is left
//                     undefined so any attempt is a hard ReferenceError rather
//                     than a silent fallback (see NETWORK GUARD below).
//
// No stub generates gameplay state and no stub calls rng(). The RNG counter
// wraps the source's own `rng` binding, so any draw a stub accidentally made
// would show up in the checkpoint stream rather than hide.
//
// ---------------------------------------------------------------------------
// WHY `localStorage` IS DEFINED ONLY AFTER THE PREFIX HAS LOADED
// ---------------------------------------------------------------------------
//
// Defining `localStorage` on the sandbox BEFORE running the prefix makes the
// prefix hang at load (>25s, reproduced this session; tools/battle-oracle hit
// the same wall). `scan-toplevel-danger.js` (extended this session to descend
// into immediately-invoked function expressions, which the battle-oracle
// scanner did not) shows why: 10 load-reachable `localStorage` references
// live inside two top-level IIFEs -- the storage-migration block at
// bundle.deobfuscated.js:38813-38863 and the progress-backup block at
// 38864+, each opening with `if (typeof localStorage == "undefined") return;`,
// the second of which sits behind the obfuscator's self-defending wrapper.
// Leaving the global undefined satisfies both guards, so both IIFEs return
// immediately. The driver then installs a plain in-memory stub for the
// FUNCTION bodies that need one (`getHallOfFame`, `getSettings`,
// `unlockAchievement`, ... all called later, by us). Empty storage is the
// correct model here anyway: it is a brand-new account with no Hall of Fame,
// no persistent stat buffs and no saved settings, which is what the Python
// port represents.
//
// ---------------------------------------------------------------------------
// DRIVER SHAPE
// ---------------------------------------------------------------------------
//
// As in tools/battle-oracle/run-fixture.js, the driver is concatenated onto
// the prefix and executed as ONE `vm.runInContext` call: `state` is declared
// `let state = BOg;` (bundle.deobfuscated.js:74982) and Node's vm does not
// reflect top-level `let`/`const` bindings onto the sandbox object, so a
// separate second call could not see it. Communication is `__SCENARIO__` in,
// `__RESULT__` out.
//
// The route is driven the way a player drives it: by invoking the source's
// OWN handlers (`onNodeClick`, the swap screen's card click listeners, the
// catch screen's card listeners, `btn-continue-battle`, `btn-next-map`),
// never by calling gameplay internals directly.

'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const SCHEMA_VERSION = 2;

const [, , scenarioPath] = process.argv;
if (!scenarioPath) {
  console.error('Usage: node run-scenario.js <scenario.json>');
  process.exit(1);
}

const scenario = JSON.parse(fs.readFileSync(scenarioPath, 'utf8'));
if (scenario.schema_version !== SCHEMA_VERSION) {
  console.error(
    `scenario ${scenarioPath} declares schema_version ${scenario.schema_version}, runner speaks ${SCHEMA_VERSION}`,
  );
  process.exit(1);
}

const prefixPath = path.join(__dirname, 'out', 'route-prefix.js');
if (!fs.existsSync(prefixPath)) {
  console.error(
    `missing ${prefixPath} -- run: node route-oracle/extract-prefix.js ` +
      'pokelike_forked/js/bundle.deobfuscated.js route-oracle/out/route-prefix.js',
  );
  process.exit(1);
}
const rawPrefix = fs.readFileSync(prefixPath, 'utf8');

// Round-counter instrumentation, identical to tools/battle-oracle/
// run-fixture.js: an exact, assertion-guarded source edit. If the needle
// stops matching exactly once the runner fails loudly rather than silently
// reporting `rounds: 0` for every battle.
//
// M3.3b workstream 5 extends the SAME site with a turn-boundary mark. The
// source's `detailedLog` is a flat stream with no per-round delimiter -- the
// only `overtime_start` marker is pushed once, at the overtime threshold
// (bundle.deobfuscated.js:55418-55422) -- so an ordered PER-TURN projection
// needs to know where each round begins. `BI4++` is the round counter's own
// increment at the very top of the round loop (55415-55418), before any of
// that round's events exist, and `BcM` (the detailed log) is in scope there.
// Recording `BcM.length` at that instant yields the exact turn boundaries
// without touching the log, the loop, any source state, or the RNG.
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

// Same two decoder shims tools/extract-data and tools/battle-oracle install:
// the obfuscator's string-decoder helpers are already inlined in the
// deobfuscated bundle, but a handful of `const Bxx = k;` / `K()` residues
// remain as no-ops.
const DECODER_STUBS = 'var k = function(){}, K = function(){ return []; };\n';

const DRIVER = fs.readFileSync(path.join(__dirname, 'driver.js'), 'utf8');

// ---------------------------------------------------------------------------
// Sandbox
// ---------------------------------------------------------------------------

// A recording DOM stub. Unlike tools/battle-oracle's fully inert Proxy, this
// one has to REMEMBER handlers: the source attaches the player's real choices
// with `addEventListener('click', ...)` / `.onclick = ...`, and the driver's
// only honest way to make a choice is to invoke those exact handlers. It
// stores nothing gameplay-bearing itself -- every field it keeps is a
// handler, a child list, or an inert presentation attribute.
function makeDom() {
  const byId = new Map();
  const bySelector = new Map();

  // M4 repair item 1: the real button-attribute-selector shape used by the
  // three showScreen-less overlays (`openItemEquipModal`/`doMoveTutorNode`,
  // which share the literal id `item-equip-modal`, and
  // `showTeamPickerModal`'s `submap-pick-modal`) -- `button[data-idx]`,
  // `button[data-tutor]`, `[data-unequip]`. All three build their rows via a
  // single raw `innerHTML =` assignment (not `createElement`+`appendChild`),
  // so the existing `.__children` tracking never sees the buttons at all --
  // WITHOUT this, the source's own `el.querySelectorAll(sel).forEach(fn =>
  // el.addEventListener('click', fn))` wiring (bundle.deobfuscated.js:
  // 79522, 79534, 80535, 76876) attaches ZERO listeners, so these buttons
  // are inert in this harness regardless of what the driver does.
  const ATTR_SELECTOR_RE = /^([a-zA-Z][a-zA-Z0-9]*)?\[data-([a-zA-Z0-9_-]+)\]$/;

  function scanAttrMatches(html, tag, attr) {
    const tagPart = tag ? tag : '[a-zA-Z][a-zA-Z0-9]*';
    const tagRe = new RegExp('<(' + tagPart + ')\\b([^>]*)>', 'gi');
    const attrRe = new RegExp('data-' + attr + '\\s*=\\s*"([^"]*)"', 'i');
    const values = [];
    let m;
    while ((m = tagRe.exec(html)) !== null) {
      const am = attrRe.exec(m[2]);
      if (am) values.push(am[1]);
    }
    return values;
  }

  function makeEl(tag) {
    const el = {
      tagName: String(tag || 'div').toUpperCase(),
      __children: [],
      __listeners: Object.create(null),
      __attrs: Object.create(null),
      __selectorCache: new Map(),
      __selectorAllCache: new Map(),
      onclick: null,
      onkeydown: null,
      disabled: false,
      value: '',
      textContent: '',
      className: '',
      src: '',
      title: '',
      dataset: Object.create(null),
      style: new Proxy({ cssText: '' }, { get: (t, p) => (p in t ? t[p] : ''), set: (t, p, v) => ((t[p] = v), true) }),
      classList: {
        add() {}, remove() {}, toggle() {}, contains: () => false,
      },
      addEventListener(type, fn) {
        (el.__listeners[type] || (el.__listeners[type] = [])).push(fn);
      },
      removeEventListener(type, fn) {
        const l = el.__listeners[type];
        if (l) el.__listeners[type] = l.filter((f) => f !== fn);
      },
      appendChild(child) {
        if (child) { el.__children.push(child); child.parentElement = el; }
        return child;
      },
      // `Element.append(...nodes)` -- unlike `appendChild`, takes any number
      // of arguments. `showBranchingChoice` (bundle.deobfuscated.js:70604:
      // `B2e["append"](B2D, B2P, B2a)`) is the only M4-reachable caller; a
      // real DOM also accepts plain strings as text, which nothing here
      // needs. Missing entirely until the M4 branching-evolution route
      // actually exercised this overlay for the first time.
      append(...children) {
        for (const child of children) el.appendChild(child);
      },
      insertBefore(child) { return el.appendChild(child); },
      insertAdjacentHTML() {},
      removeChild(child) { el.__children = el.__children.filter((c) => c !== child); return child; },
      remove() {
        if (el.parentElement) el.parentElement.__children = el.parentElement.__children.filter((c) => c !== el);
      },
      setAttribute(k, v) { el.__attrs[k] = v; },
      getAttribute(k) { return k in el.__attrs ? el.__attrs[k] : null; },
      removeAttribute(k) { delete el.__attrs[k]; },
      hasAttribute(k) { return k in el.__attrs; },
      // Any selector resolves to a stable synthetic descendant. The source
      // only ever uses the result to attach or read handlers/styles, so a
      // stable per-(element, selector) child is behaviourally sufficient and
      // keeps `querySelector('.poke-card')` clickable.
      querySelector(sel) {
        if (!el.__selectorCache.has(sel)) {
          const child = makeEl('div');
          child.parentElement = el;
          el.__selectorCache.set(sel, child);
        }
        return el.__selectorCache.get(sel);
      },
      // Scoped NARROWLY to the two dynamically-built overlay containers that
      // actually need it (by their literal, fixed id) -- every other
      // `querySelectorAll` call site in the whole bundle (battle rendering,
      // settings, dex, team-bar drag/drop, ...) keeps returning `[]`
      // UNCHANGED, exactly as before this fix. Widening it to every element
      // would have risked silently activating some other, already-exercised
      // `.forEach` that previously ran zero times across the 11 existing
      // scenarios -- unaudited and unrelated to this repair.
      querySelectorAll(sel) {
        if (el.id !== 'item-equip-modal' && el.id !== 'submap-pick-modal') return [];
        const key = String(sel);
        if (!el.__selectorAllCache.has(key)) {
          const parsed = ATTR_SELECTOR_RE.exec(key.trim());
          let made = [];
          if (parsed) {
            const tag = parsed[1] ? parsed[1].toLowerCase() : null;
            const attr = parsed[2];
            made = scanAttrMatches(html, tag, attr).map((value) => {
              const child = makeEl(tag || 'div');
              child.parentElement = el;
              child.setAttribute('data-' + attr, value);
              child.dataset[attr] = value;
              return child;
            });
          }
          el.__selectorAllCache.set(key, made);
        }
        return el.__selectorAllCache.get(key);
      },
      getBoundingClientRect() { return { top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0 }; },
      focus() {}, blur() {}, scrollIntoView() {}, click() {},
    };
    // `innerHTML = ...` REPLACES an element's subtree in a real DOM. The stub
    // therefore has to drop the old children AND the synthetic descendants
    // handed out by `querySelector`, or a screen that is rebuilt keeps its
    // previous generation's click listeners alive. That is not cosmetic:
    // `showSwapScreen` rewrites `#swap-incoming`'s innerHTML and then
    // `addEventListener('click', ...)`s the accept handler onto
    // `#swap-incoming .poke-card` (bundle.deobfuscated.js:79152-79201), and
    // `doCatchNode` rewrites `#catch-choices` before appending its cards
    // (78435+). Without this, the SECOND such screen in a route fires the
    // first screen's stale closure as well -- so any route that ACCEPTS a
    // card (as opposed to declining, which the original four scenarios all
    // did) would have driven a decision the source never offered.
    let html = '';
    Object.defineProperty(el, 'innerHTML', {
      get() { return html; },
      set(v) {
        html = String(v === undefined || v === null ? '' : v);
        el.__children = [];
        el.__selectorCache = new Map();
        el.__selectorAllCache = new Map();
      },
      enumerable: true,
      configurable: true,
    });
    // The source walks upward (`el.parentElement.querySelectorAll(...)`,
    // `el.parentElement.insertBefore(...)`) on elements it only ever fetched
    // by id, so a detached root still needs a parent. Created lazily so an
    // element that IS appended somewhere keeps its real parent.
    let parent = null;
    Object.defineProperty(el, 'parentElement', {
      get() {
        if (!parent) { parent = makeEl('div'); parent.__children.push(el); }
        return parent;
      },
      set(v) { parent = v; },
      enumerable: true,
      configurable: true,
    });
    // M4 repair item 1: `openItemEquipModal`/`doMoveTutorNode` (both reuse
    // the literal id `item-equip-modal`) and `showTeamPickerModal`'s
    // `submap-pick-modal` are `document.createElement`d and only get their
    // id assigned AFTERWARD (`B2O.id = "item-equip-modal"`), never fetched
    // by id themselves. Without this, `document.getElementById(...)` -- the
    // ONLY way the driver can ever get a handle on them -- would keep
    // returning the unrelated auto-vivified stub `getElementById` fabricates
    // for any never-before-seen id, forever disconnected from the real
    // element the source built and attached listeners to.
    let idValue = '';
    Object.defineProperty(el, 'id', {
      get() { return idValue; },
      set(v) {
        idValue = String(v === undefined || v === null ? '' : v);
        if (idValue) byId.set(idValue, el);
      },
      enumerable: true,
      configurable: true,
    });
    return el;
  }

  const body = makeEl('body');
  const documentElement = makeEl('html');

  const document = {
    body,
    documentElement,
    head: makeEl('head'),
    readyState: 'complete',
    createElement: (tag) => makeEl(tag),
    createTextNode: () => makeEl('text'),
    createDocumentFragment: () => makeEl('fragment'),
    getElementById(id) {
      if (!byId.has(id)) { const el = makeEl('div'); el.id = id; byId.set(id, el); }
      return byId.get(id);
    },
    // A document-level `#id descendant` lookup must resolve THROUGH the
    // element, not through a flat document cache, so that rewriting that
    // element's innerHTML invalidates the descendant (see the innerHTML
    // setter above). `showSwapScreen` depends on exactly this: it rewrites
    // `#swap-incoming` and then looks up `#swap-incoming .poke-card`.
    querySelector(sel) {
      const nested = /^#([A-Za-z0-9_-]+)\s+(.+)$/.exec(String(sel));
      if (nested) return document.getElementById(nested[1]).querySelector(nested[2]);
      if (!bySelector.has(sel)) bySelector.set(sel, makeEl('div'));
      return bySelector.get(sel);
    },
    querySelectorAll: () => [],
    addEventListener() {},
    removeEventListener() {},
    getElementsByClassName: () => [],
    getElementsByTagName: () => [],
  };
  return document;
}

const sandbox = { console };
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
sandbox.location = { hostname: 'localhost', search: '', href: 'http://localhost/' };
// Load-time timer stubs: inert, so the obfuscator's anti-tamper
// setTimeout/setInterval registrations at bundle.deobfuscated.js:38810-38811
// never fire. The driver replaces them with the virtual queue after load.
sandbox.setTimeout = () => 0;
sandbox.setInterval = () => 0;
sandbox.clearTimeout = () => {};
sandbox.clearInterval = () => {};
sandbox.requestAnimationFrame = () => 0;
sandbox.cancelAnimationFrame = () => {};
sandbox.document = makeDom();
sandbox.performance = { now: () => 0 };
sandbox.atob = (s) => Buffer.from(String(s), 'base64').toString('binary');
sandbox.btoa = (s) => Buffer.from(String(s), 'binary').toString('base64');
// NOT defined, deliberately: localStorage/sessionStorage (see header),
// fetch/XMLHttpRequest/WebSocket (network guard), navigator, Audio.
sandbox.__SCENARIO__ = scenario;

vm.createContext(sandbox);
vm.runInContext(DECODER_STUBS + prefix + '\n' + DRIVER, sandbox, { timeout: 300000 });

(async () => {
  // The driver is async; give its microtasks a chance to complete.
  for (let i = 0; i < 200000 && !sandbox.__DONE__; i++) {
    await new Promise((r) => setImmediate(r));
  }
  if (!sandbox.__DONE__) {
    console.error('route oracle did not finish (driver never signalled completion)');
    process.exit(1);
  }
  if (sandbox.__FATAL__) {
    console.error('route oracle fatal error:\n' + sandbox.__FATAL__);
    process.exit(1);
  }
  process.stdout.write(sandbox.__RESULT__);
})();
