'use strict';
// A minimal, hand-rolled DOM good enough to EXECUTE pokelike/webui/static/js/
// app.js under Node's built-in `vm`. No jsdom/puppeteer/playwright dependency:
// R2, R3 and R4 each built a throwaway version of exactly this in scratch
// space, found a real bug with it, and discarded it. This is that shim, kept.
//
// It implements only what app.js actually calls -- measured, not guessed:
//   appendChild, createElement(NS), getElementById, addEventListener,
//   classList, remove, setAttribute, querySelector(All), closest, body,
//   removeEventListener, elementFromPoint, stopPropagation/preventDefault,
//   scrollTop/scrollHeight, clientWidth/clientHeight, getBoundingClientRect,
//   insertBefore, plus setTimeout/clearTimeout/requestAnimationFrame/console.
//
// Two properties make it useful as a DETECTOR rather than just a host:
//   * every addEventListener call is RECORDED on the element, so a duplicate
//     registration (R3's double-dispatch bug) is directly observable;
//   * setTimeout is VIRTUAL -- callbacks queue against a fake clock the test
//     drives, so a battle replay drains deterministically and instantly
//     instead of taking its real wall-clock duration.

class ClassList {
  constructor(el) { this._el = el; this._set = []; }
  add(...names) {
    for (const n of names) if (n && !this._set.includes(n)) this._set.push(n);
  }
  remove(...names) {
    for (const n of names) {
      const i = this._set.indexOf(n);
      if (i >= 0) this._set.splice(i, 1);
    }
  }
  contains(n) { return this._set.includes(n); }
  toggle(n, force) {
    const has = this.contains(n);
    const want = force === undefined ? !has : !!force;
    if (want) this.add(n); else this.remove(n);
    return want;
  }
  get length() { return this._set.length; }
  item(i) { return this._set[i]; }
  toString() { return this._set.join(' '); }
  [Symbol.iterator]() { return this._set[Symbol.iterator](); }
}

// `style` records every property written, including via setProperty, so a
// detector can assert on a CSS filter/opacity the way a browser would show it.
function makeStyle() {
  const store = {};
  return new Proxy(store, {
    get(t, k) {
      if (k === 'setProperty') return (name, value) => { t[name] = String(value); };
      if (k === 'getPropertyValue') return (name) => (name in t ? t[name] : '');
      if (k === 'removeProperty') return (name) => { delete t[name]; };
      if (k === '_store') return t;
      const v = t[k];
      return v === undefined ? '' : v;
    },
    set(t, k, v) { t[k] = String(v); return true; },
  });
}

let _uid = 0;

class Element {
  constructor(tagName, ns) {
    this.tagName = String(tagName).toUpperCase();
    this.namespaceURI = ns || null;
    this.childNodes = [];
    this.parentNode = null;
    this.attributes = {};
    this.classList = new ClassList(this);
    this.style = makeStyle();
    this.listeners = {};          // type -> [handler, ...]   (the detector hook)
    this._text = '';
    this._uid = ++_uid;
    this.scrollTop = 0;
    this.scrollHeight = 0;
    this.disabled = false;
    this.onclick = null;
  }

  // --- identity / attributes -------------------------------------------
  get id() { return this.attributes.id || ''; }
  set id(v) { this.attributes.id = String(v); }
  get className() { return this.classList.toString(); }
  set className(v) {
    this.classList = new ClassList(this);
    String(v).split(/\s+/).filter(Boolean).forEach((c) => this.classList.add(c));
  }
  setAttribute(name, value) {
    if (name === 'class') { this.className = value; return; }
    this.attributes[name] = String(value);
  }
  getAttribute(name) {
    if (name === 'class') return this.className;
    return name in this.attributes ? this.attributes[name] : null;
  }
  hasAttribute(name) { return this.getAttribute(name) !== null; }
  removeAttribute(name) { delete this.attributes[name]; }
  setAttributeNS(_ns, name, value) { this.setAttribute(name.replace(/^xlink:/, ''), value); }

  // --- tree -------------------------------------------------------------
  appendChild(child) {
    if (child.parentNode) child.parentNode.removeChild(child);
    child.parentNode = this;
    this.childNodes.push(child);
    return child;
  }
  insertBefore(child, ref) {
    if (child.parentNode) child.parentNode.removeChild(child);
    const i = ref ? this.childNodes.indexOf(ref) : -1;
    child.parentNode = this;
    if (i < 0) this.childNodes.push(child); else this.childNodes.splice(i, 0, child);
    return child;
  }
  removeChild(child) {
    const i = this.childNodes.indexOf(child);
    if (i >= 0) this.childNodes.splice(i, 1);
    child.parentNode = null;
    return child;
  }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  get children() { return this.childNodes.slice(); }
  get firstChild() { return this.childNodes[0] || null; }

  // --- content ----------------------------------------------------------
  get textContent() {
    if (this.childNodes.length) return this.childNodes.map((c) => c.textContent).join('');
    return this._text;
  }
  set textContent(v) { this.childNodes = []; this._text = String(v); }
  // app.js only ever ASSIGNS innerHTML, and only ever to '' (to clear) or to a
  // small literal string. Parsing is deliberately not implemented: a shim that
  // silently half-parsed markup would make detectors lie about what got built.
  // Anything non-empty is stored verbatim and reported by `innerHTMLRaw`.
  get innerHTML() { return this._innerHTML || ''; }
  set innerHTML(v) {
    this.childNodes = [];
    this._innerHTML = String(v);
    if (this._innerHTML) this._text = '';
  }
  get innerHTMLRaw() { return this._innerHTML || ''; }

  // --- events -----------------------------------------------------------
  addEventListener(type, handler, opts) {
    (this.listeners[type] = this.listeners[type] || []).push(handler);
    void opts;
  }
  removeEventListener(type, handler) {
    const arr = this.listeners[type];
    if (!arr) return;
    const i = arr.indexOf(handler);
    if (i >= 0) arr.splice(i, 1);
  }
  /** Fires every registered handler for `type`; returns how many ran. */
  dispatch(type, event) {
    const ev = Object.assign(
      { type, target: this, preventDefault() {}, stopPropagation() {} },
      event || {},
    );
    const handlers = (this.listeners[type] || []).slice();
    handlers.forEach((h) => h.call(this, ev));
    if (type === 'click' && typeof this.onclick === 'function') {
      this.onclick.call(this, ev);
      return handlers.length + 1;
    }
    return handlers.length;
  }
  click() { return this.dispatch('click', {}); }
  /** How many handlers are bound for `type` -- the double-dispatch detector. */
  listenerCount(type) {
    let n = (this.listeners[type] || []).length;
    if (type === 'click' && typeof this.onclick === 'function') n += 1;
    return n;
  }

  // --- layout (fixed, so renderMap's `|| 0x258` fallbacks are exercisable) --
  get clientWidth() { return this._clientWidth === undefined ? 600 : this._clientWidth; }
  set clientWidth(v) { this._clientWidth = v; }
  get clientHeight() { return this._clientHeight === undefined ? 500 : this._clientHeight; }
  set clientHeight(v) { this._clientHeight = v; }
  getBoundingClientRect() {
    return { x: 0, y: 0, top: 0, left: 0, right: this.clientWidth, bottom: this.clientHeight,
             width: this.clientWidth, height: this.clientHeight };
  }

  // --- selectors --------------------------------------------------------
  closest(sel) {
    let node = this;
    while (node) {
      if (matches(node, sel)) return node;
      node = node.parentNode;
    }
    return null;
  }
  matches(sel) { return matches(this, sel); }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  querySelectorAll(sel) {
    const out = [];
    for (const part of String(sel).split(',')) {
      collect(this, part.trim(), out);
    }
    return out;
  }
  descendants() {
    const out = [];
    const walk = (n) => n.childNodes.forEach((c) => { out.push(c); walk(c); });
    walk(this);
    return out;
  }
}

// Supports the selector forms app.js actually uses: `#id`, `.cls`, `tag`,
// `[attr="value"]`, `[attr]`, and descendant chains of those. Anything else
// throws rather than silently matching nothing -- a shim that quietly returns
// [] for a selector it does not understand turns a real bug into a pass.
function matchesSimple(el, token) {
  const re = /^([a-zA-Z][\w-]*)?((?:[.#][\w-]+|\[[^\]]+\])*)$/;
  const m = re.exec(token);
  if (!m) throw new Error(`dom-shim: unsupported selector token ${JSON.stringify(token)}`);
  if (m[1] && el.tagName !== m[1].toUpperCase()) return false;
  const rest = m[2] || '';
  const parts = rest.match(/[.#][\w-]+|\[[^\]]+\]/g) || [];
  for (const p of parts) {
    if (p[0] === '.') { if (!el.classList.contains(p.slice(1))) return false; }
    else if (p[0] === '#') { if (el.id !== p.slice(1)) return false; }
    else {
      const inner = p.slice(1, -1);
      const eq = inner.indexOf('=');
      if (eq < 0) { if (!el.hasAttribute(inner)) return false; }
      else {
        const name = inner.slice(0, eq);
        const want = inner.slice(eq + 1).replace(/^["']|["']$/g, '');
        if (el.getAttribute(name) !== want) return false;
      }
    }
  }
  return true;
}

function matches(el, sel) {
  const tokens = String(sel).trim().split(/\s+/);
  // Only the DESCENDANT combinator (whitespace) is implemented. A child/
  // sibling combinator must not be silently downgraded to a descendant step --
  // that would quietly widen a selector and let a detector pass on the wrong
  // element. app.js uses none of them.
  for (const t of tokens) {
    if (t === '>' || t === '+' || t === '~') {
      throw new Error(`dom-shim: unsupported combinator ${JSON.stringify(t)} in ${JSON.stringify(sel)}`);
    }
  }
  if (tokens.length === 1) return matchesSimple(el, tokens[0]);
  if (!matchesSimple(el, tokens[tokens.length - 1])) return false;
  let node = el.parentNode;
  let i = tokens.length - 2;
  while (node && i >= 0) {
    if (matchesSimple(node, tokens[i])) i -= 1;
    node = node.parentNode;
  }
  return i < 0;
}

function collect(root, sel, out) {
  for (const el of root.descendants()) {
    if (matches(el, sel) && !out.includes(el)) out.push(el);
  }
}

// --- the virtual clock ---------------------------------------------------
class Clock {
  constructor() { this.queue = []; this.now = 0; this.seq = 0; }
  setTimeout(fn, ms) {
    const id = ++this.seq;
    this.queue.push({ id, at: this.now + (Number(ms) || 0), fn });
    return id;
  }
  clearTimeout(id) {
    const i = this.queue.findIndex((t) => t.id === id);
    if (i >= 0) this.queue.splice(i, 1);
  }
  /** Runs queued callbacks in time order. `limit` bounds runaway loops. */
  drain(limit = 20000) {
    let ran = 0;
    while (this.queue.length && ran < limit) {
      this.queue.sort((a, b) => (a.at - b.at) || (a.id - b.id));
      const t = this.queue.shift();
      this.now = Math.max(this.now, t.at);
      t.fn();
      ran += 1;
    }
    if (ran >= limit) throw new Error('dom-shim: timer queue did not settle');
    return ran;
  }
}

function createDocument(knownIds) {
  const byId = new Map();
  const doc = {
    _byId: byId,
    createElement(tag) { return new Element(tag, null); },
    createElementNS(ns, tag) { return new Element(tag, ns); },
    getElementById(id) { return byId.get(id) || null; },
    querySelector(sel) { return doc.body.querySelector(sel); },
    querySelectorAll(sel) { return doc.body.querySelectorAll(sel); },
    elementFromPoint() { return null; },
    addEventListener(type, handler) { doc.body.addEventListener(type, handler); },
    removeEventListener(type, handler) { doc.body.removeEventListener(type, handler); },
    dispatch(type, ev) { return doc.body.dispatch(type, ev); },
  };
  doc.body = new Element('body', null);
  doc.documentElement = new Element('html', null);
  // Every id the real index.html declares is pre-created, so app.js sees the
  // document it actually ships against. An id NOT in index.html returns null,
  // exactly as in a browser -- so a typo'd getElementById is still a bug here.
  for (const id of knownIds) {
    const el = new Element('div', null);
    el.id = id;
    if (id.endsWith('-screen') || id === 'title-screen') el.classList.add('screen');
    byId.set(id, el);
    doc.body.appendChild(el);
  }
  return doc;
}

module.exports = { Element, ClassList, Clock, createDocument, matches };
