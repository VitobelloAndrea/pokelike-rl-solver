// Shared VM sandbox for the route-oracle's JavaScript runners.
//
// EXTRACTED VERBATIM from `run-scenario.js` by M7, with no behavioral change,
// so `run-scenario.js` (the frozen 29-scenario route runner) and
// `sweep-host.js` (M7's interactive cross-runtime sweep host) build the SAME
// sandbox from ONE definition instead of two that can drift apart. The frozen
// parity signature and the 29/29 strict result are the proof that the move
// changed nothing: both are re-run and unchanged in
// `docs/audits/M7-implementation.md`.
//
// The `makeDom()` body, every `sandbox.*` assignment, and every explanatory
// comment below are byte-identical to the pre-M7 `run-scenario.js` text. The
// only additions are this header, the `module.exports` at the bottom, and the
// wrapping of the sandbox construction in a `makeSandbox()` function so each
// host can build its own.

'use strict';


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

function makeSandbox() {
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
  return sandbox;
}

module.exports = { makeDom, makeSandbox };
