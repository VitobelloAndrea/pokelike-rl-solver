// Pokelike web UI -- talks to the local JSON API (pokelike/webui/server.py)
// wrapping pokelike/engine.py. NOT a port of pokelike_forked/js/bundle.js --
// a brand-new client for a brand-new (Python) game engine. Reuses the real
// site's CSS classes (main.css, copied verbatim) for visual consistency.
//
// Battle presentation (R4): the battle screen replays the fight turn by turn.
// The old note here -- that run_battle resolved a whole battle synchronously
// and exposed no turn-level event stream, so this screen could only jump to
// the final result -- was false from R1 onward: the feed has existed since R1
// (battle_loop's battle_events/status_events, carried out through
// engine._run_battle onto RunState.last_battle) and has been fully rostered
// since R2/N2. R4 is the milestone that consumes it, via
// contract.battle_view's `replay`.
//
// Synchronous resolution was never the obstacle it was described as -- it is
// the SOURCE's own model too. runBattleScreen resolves the entire battle first
// (bundle.deobfuscated.js:81208-81222) and then replays the finished log
// through animateBattleVisually (81272). What is genuinely approximated here,
// deliberately and documented in docs/renderer-contract.md section 11, is the
// browser-native half: the source's per-move particle canvases
// (playAttackAnimation, 66698+) and its requestAnimationFrame HP tween
// (65035-65064) have no discrete algorithm to port.

let currentState = null;
let lastSeenLogTotal = 0;

// ---------------------------------------------------------------------
// API + top-level flow
// ---------------------------------------------------------------------

async function apiPost(path, body) {
  const res = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
  const data = await res.json();
  if (!res.ok) {
    showToast(data.error || ('HTTP ' + res.status));
    throw new Error(data.error || String(res.status));
  }
  return data;
}

async function resetRun(opts) {
  lastSeenLogTotal = 0;
  const state = await apiPost('/api/reset', opts);
  render(state);
}

async function doAction(action) {
  let state;
  try {
    state = await apiPost('/api/action', action);
  } catch (e) {
    return; // error already surfaced via showToast; leave the current screen up
  }
  applyWithBattleInterstitial(state);
}

// R4 (CODEX section 7.7). This used to inspect ONLY state.log's newest entry,
// so a battle immediately followed by anything else in the same Engine.step --
// an evolve, a badge, an item grant, a victory -- showed no battle screen at
// all. Engine.step routinely appends several entries at once (engine.py's
// _after_battle logs "battle", then the caller logs whatever came next), and
// log_total advances by more than one, which the old code computed and then
// ignored.
//
// Now every entry appended since the last poll is scanned. Two bounds matter:
// log_total is the authoritative count of what the engine appended, but the
// server trims state.log to its trailing few (state_json.encode_state's
// recent_log, default 5). So the batch is the last `appended` entries CLAMPED
// to what actually arrived -- if more entries landed than were sent, the
// oldest of them are simply not here to inspect.
//
// Decision on a batch that holds a battle AND other entries: show the
// interstitial FIRST, then continue to the screen the newest entry implies.
// That is both the source's own order (runBattleScreen awaits its battle
// screen before its success callback runs the next node's screen) and the only
// option that skips neither -- returning to render(state) on Continue means
// the post-battle evolve/badge/victory screen still appears, just after the
// battle it followed rather than instead of it.
function applyWithBattleInterstitial(state) {
  const appended = Math.max(0, state.log_total - lastSeenLogTotal);
  const batch = appended > 0 ? state.log.slice(Math.max(0, state.log.length - appended)) : [];
  lastSeenLogTotal = state.log_total;
  const battleEntry = batch.find((e) => e && e.type === 'battle');
  if (battleEntry) {
    renderBattle(battleEntry, state, () => render(state));
  } else {
    render(state);
  }
}

function showScreen(id) {
  document.querySelectorAll('.screen').forEach((s) => s.classList.remove('active'));
  const el = document.getElementById(id);
  if (el) el.classList.add('active');
}

function showToast(msg) {
  const el = document.createElement('div');
  el.className = 'map-notification';
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => {
    el.style.opacity = '0';
    setTimeout(() => el.remove(), 500);
  }, 2500);
}

// ---------------------------------------------------------------------
// Sprites + generic card builders
// ---------------------------------------------------------------------

function spriteUrl(speciesId, shiny) {
  return shiny ? `/img/sprites/pokemon/shiny/${speciesId}.png` : `/img/sprites/pokemon/${speciesId}.png`;
}

// `data` is any of: a pending-choice option, a team-member summary
// (engine.py's _mon_summary), or a battle log entry's player/enemy_team
// item -- all share enough fields (species_id/name, optionally level/
// types/current_hp/max_hp/status/held_item/is_shiny) that one builder
// covers every screen that shows a Pokemon.
function makePokeCard(data, opts) {
  opts = opts || {};
  const card = document.createElement('div');
  card.className = 'poke-card';
  if (opts.onClick) {
    card.setAttribute('role', 'button');
    card.setAttribute('tabindex', '0');
    card.addEventListener('click', opts.onClick);
    card.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') opts.onClick(e);
    });
  }

  const spriteWrap = document.createElement('div');
  spriteWrap.className = 'poke-sprite-wrap';
  const img = document.createElement('img');
  img.className = 'poke-sprite' + (data.is_shiny ? ' shiny' : '');
  img.src = spriteUrl(data.species_id, data.is_shiny);
  img.alt = data.name || '';
  img.onerror = () => { img.style.visibility = 'hidden'; };
  spriteWrap.appendChild(img);
  if (data.is_shiny) {
    const badge = document.createElement('span');
    badge.className = 'shiny-badge';
    badge.textContent = '✨';
    spriteWrap.appendChild(badge);
  }
  card.appendChild(spriteWrap);

  const name = document.createElement('div');
  name.className = 'poke-name';
  name.textContent = data.name || ('#' + data.species_id);
  card.appendChild(name);

  if (data.level !== undefined) {
    const lvl = document.createElement('div');
    lvl.className = 'poke-level';
    lvl.textContent = 'Lv' + data.level;
    card.appendChild(lvl);
  }

  if (data.types && data.types.length) {
    const row = document.createElement('div');
    row.className = 'poke-types';
    data.types.forEach((t) => {
      const badge = document.createElement('span');
      badge.className = 'type-badge type-' + String(t).toLowerCase();
      badge.textContent = t;
      row.appendChild(badge);
    });
    card.appendChild(row);
  }

  if (data.max_hp !== undefined && data.max_hp !== null) {
    const wrap = document.createElement('div');
    wrap.className = 'poke-hp';
    const bg = document.createElement('div');
    bg.className = 'hp-bar-bg';
    const fill = document.createElement('div');
    fill.className = 'hp-bar-fill';
    const pct = data.hp_pct !== undefined ? data.hp_pct : (100 * data.current_hp) / Math.max(1, data.max_hp);
    const clamped = Math.max(0, Math.min(100, pct));
    fill.style.width = clamped + '%';
    // R5/N12: the source's hpBarColor, same as the battle field. This used to
    // be `clamped > 50 ? '#3cc24a' : clamped > 20 ? '#f08c10' : '#e22a18'` --
    // different thresholds AND different hexes from the one function the source
    // routes every HP bar through, so team/reward/game-over cards disagreed with
    // the battle screen about what "low HP" looks like.
    fill.style.background = hpBarColor(clamped / 100);
    bg.appendChild(fill);
    wrap.appendChild(bg);
    const text = document.createElement('div');
    text.className = 'hp-text';
    text.textContent = data.current_hp + '/' + data.max_hp;
    wrap.appendChild(text);
    card.appendChild(wrap);
  }

  if (data.held_item) {
    const held = document.createElement('div');
    held.className = 'team-slot-item';
    held.textContent = '@' + data.held_item;
    card.appendChild(held);
  }
  if (data.status) {
    const status = document.createElement('div');
    status.style.color = 'var(--red)';
    status.textContent = data.status;
    card.appendChild(status);
  }
  return card;
}

function makeItemCard(opt, onClick) {
  const card = document.createElement('div');
  card.className = 'poke-card';
  card.setAttribute('role', 'button');
  card.setAttribute('tabindex', '0');
  card.addEventListener('click', onClick);
  const name = document.createElement('div');
  name.className = 'poke-name';
  name.textContent = opt.name;
  card.appendChild(name);
  const kind = document.createElement('div');
  kind.className = 'poke-level';
  kind.textContent = opt.usable ? 'Usable' : 'Held item';
  card.appendChild(kind);
  return card;
}

function mkPokeCardOption(opt, idx) {
  return makePokeCard(opt, { onClick: () => doAction({ type: 'SelectOption', index: idx }) });
}

function renderChoiceScreen(screenId, containerId, cardBuilderFn, skipBtnId) {
  const container = document.getElementById(containerId);
  container.innerHTML = '';
  currentState.pending.options.forEach((opt, idx) => container.appendChild(cardBuilderFn(opt, idx)));
  if (skipBtnId) {
    document.getElementById(skipBtnId).style.display = currentState.pending.optional ? '' : 'none';
  }
  showScreen(screenId);
}

// ---------------------------------------------------------------------
// Map rendering -- R2. This IS a port of the source's own node layout and
// presentation, replacing the invented grid that stood here before.
//
// Every layout, colour, glyph, sprite-path and hover-text DECISION is made
// once, in Python, by pokelike/render/contract.py (which cites
// bundle.deobfuscated.js line by line). This file only draws what the
// contract hands it, so it and render/console.py cannot drift about what a
// node is. What remains here is genuinely browser-side:
//
//   * turning `node.pos`'s viewport-free fractions into pixels, using the
//     container's live clientWidth/clientHeight exactly as the source does
//     (renderMap, bundle.deobfuscated.js:54113-54114, 54132-54138);
//   * the SVG element construction of renderMap's own two branches --
//     sprite (54184-54314) and circle+icon fallback (54315-54348);
//   * the hover tooltip element (_mapTooltip, 54026-54051).
//
// Deliberate approximation, NOT a trace: the source only ever takes the
// circle branch when getNodeSprite returns null, but this mirror ships no
// node sprite files (pokelike_forked/img/sprites/ holds only pokemon/), so
// an image that fails to load falls back to the same circle+icon
// presentation rather than rendering a broken image. See contract.UNSUPPLIED.
// ---------------------------------------------------------------------

const SVG_NS = 'http://www.w3.org/2000/svg';

// renderMap's `|| 0x258` / `|| 0x1f4` fallbacks, for a container that has not
// been laid out yet (bundle.deobfuscated.js:54113-54114).
const MAP_DEFAULT_W = 600;
const MAP_DEFAULT_H = 500;

function svgEl(name, attrs) {
  const el = document.createElementNS(SVG_NS, name);
  if (attrs) Object.entries(attrs).forEach(([k, v]) => el.setAttribute(k, String(v)));
  return el;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

// The inverse of contract._node_positions, and the ONLY place pixels are
// computed. Mirrors contract.node_pixel_position; `edge_margin` travels on the
// map view so the source's 28 is not hard-coded independently here.
function nodePixelPos(pos, mapData, w, h) {
  const margin = mapData.edge_margin;
  const x = pos.x_frac * w;
  const y = pos.y_frac === null ? h / 2 : margin + pos.y_frac * (h - 2 * margin);
  return { x, y };
}

// _mapTooltip (bundle.deobfuscated.js:54026-54051): one reused fixed-position
// element that follows the cursor, shown only while the map screen is up.
const mapTooltip = (() => {
  let el = null;
  const node = () => (el = el || document.getElementById('map-node-tooltip'));
  return {
    show(html, x, y) {
      const screen = document.getElementById('map-screen');
      if (!screen || !screen.classList.contains('active')) return;
      const t = node();
      if (!t) return;
      t.innerHTML = html;
      t.style.left = x + 'px';
      t.style.top = y + 'px';
      t.classList.add('visible');
    },
    hide() {
      const t = node();
      if (t) t.classList.remove('visible');
    },
  };
})();

// contract._node_tooltip carries the source's hover CONTENT as structure
// ({title, notes, team}) rather than getNodeLabel's inline-styled HTML string,
// so render/console.py can use it too. This is where the browser turns it back
// into markup, mirroring getNodeLabel's own styling (54698-54704, 54721-54725,
// 54762-54768).
function tooltipHtml(tip) {
  if (!tip) return '';
  let html = `<div style="font-weight:bold;margin-bottom:3px;">${escapeHtml(tip.title)}</div>`;
  tip.notes.forEach((note) => {
    html += `<div style="color:#ccc;font-size:9px;">${escapeHtml(note)}</div>`;
  });
  tip.team.forEach((mon) => {
    html += `<div style="color:#ccc;font-size:9px;">${escapeHtml(mon.name)}`
      + ` <span style="color:#aaa;">Lv${escapeHtml(String(mon.level))}</span></div>`;
  });
  return html;
}

// renderMap's circle+icon branch (bundle.deobfuscated.js:54315-54348): fill is
// #2a2a3a when unexplored, else getNodeColor; the stroke and its pulse mark a
// clickable node; the glyph is ✓ for the node you are standing on.
function appendNodeCircle(g, node) {
  const circle = svgEl('circle', {
    r: node.sprite_size.circle_radius,
    fill: node.unexplored ? '#2a2a3a' : node.color,
    stroke: node.clickable ? '#fff' : node.unexplored ? '#444' : '#555',
    'stroke-width': node.clickable ? 3 : 1,
  });
  if (node.clickable) {
    circle.appendChild(svgEl('animate', {
      attributeName: 'stroke-opacity', values: '1;0.3;1',
      dur: '1.5s', repeatCount: 'indefinite',
    }));
  }
  g.appendChild(circle);
  const text = svgEl('text', {
    'text-anchor': 'middle', 'dominant-baseline': 'central',
    'font-size': 14, fill: node.unexplored ? '#aaa' : '#fff',
  });
  text.textContent = node.is_current ? '✓' : node.icon;
  g.appendChild(text);
}

// renderMap's sprite branch (bundle.deobfuscated.js:54184-54252): the sprite is
// drawn centred in its own per-type box, dimmed when visited, with a pulsing
// white marker above a clickable node and a ✓ over the current one.
function appendNodeSprite(g, node) {
  const w = node.sprite_size.w;
  const h = node.sprite_size.h;
  const img = svgEl('image', {
    href: node.sprite_url.replace(/ /g, '%20'),
    x: -(w / 2), y: -(h / 2), width: w, height: h,
    preserveAspectRatio: 'xMidYMid meet',
  });
  img.classList.add('map-node-sprite', 'pixel-art');
  img.style.pointerEvents = 'none';
  img.style.imageRendering = 'pixelated';
  if (node.dimmed) img.style.filter = 'brightness(0.72)';
  // Not in the source -- see this section's header: no node sprite files ship
  // with this mirror, so a failed load degrades to the circle branch.
  img.addEventListener('error', () => { img.remove(); appendNodeCircle(g, node); }, { once: true });
  g.appendChild(img);

  if (node.clickable) {
    const marker = svgEl('g', { fill: '#fff' });
    marker.appendChild(svgEl('animate', {
      attributeName: 'opacity', values: '0.55;0.1;0.55',
      dur: '1.5s', repeatCount: 'indefinite',
    }));
    const baseY = h / 2 - 2;
    [0.35, 0.55, 0.35].forEach((frac, i) => {
      const barW = Math.round((w * frac) / 4) * 4;
      marker.appendChild(svgEl('rect', {
        x: -(barW / 2), y: baseY + (i - 1) * 4 - 2, width: barW, height: 4,
      }));
    });
    g.insertBefore(marker, img);
  }

  if (node.is_current) {
    const check = svgEl('text', {
      'text-anchor': 'middle', 'dominant-baseline': 'central',
      'font-size': 16, fill: '#fff',
    });
    check.textContent = '✓';
    g.appendChild(check);
  }

  // appendMapNodeHitArea (54096-54108): a transparent rect so the whole box is
  // hoverable/clickable, not just the sprite's opaque pixels.
  g.appendChild(svgEl('rect', {
    x: -(w / 2), y: -(h / 2), width: w, height: h, fill: 'transparent',
  }));
}

function renderMap(state) {
  const container = document.getElementById('map-container');
  container.innerHTML = '';
  const mapData = state.map;
  if (!mapData) return;

  // The source sizes its SVG from the live container, with the same fallbacks
  // (bundle.deobfuscated.js:54113-54125).
  const w = Math.round(container.clientWidth) || MAP_DEFAULT_W;
  const h = Math.round(container.clientHeight) || MAP_DEFAULT_H;

  const svg = svgEl('svg', {
    id: 'map-svg', width: w, height: h,
    viewBox: `0 0 ${w} ${h}`, overflow: 'visible',
    'shape-rendering': 'crispEdges',
  });
  svg.style.display = 'block';
  svg.style.width = '100%';
  svg.style.height = '100%';
  svg.style.imageRendering = 'pixelated';

  const posById = {};
  mapData.nodes.forEach((n) => { posById[n.id] = nodePixelPos(n.pos, mapData, w, h); });
  // The source keeps the same id -> <g> map (`B2d`, 54163/54471) so its
  // shortcut-badge pass can find a node's group after the fact.
  const groupsById = {};

  // Edges first, so nodes draw over them (54143-54162). Every stroke decision
  // was already taken by contract.edge_view.
  mapData.edges.forEach((edge) => {
    const p1 = posById[edge.from];
    const p2 = posById[edge.to];
    if (!p1 || !p2) return;
    const line = svgEl('line', {
      x1: p1.x, y1: p1.y, x2: p2.x, y2: p2.y,
      stroke: edge.color, 'stroke-width': edge.width,
    });
    if (edge.dashed) line.setAttribute('stroke-dasharray', '4,5');
    svg.appendChild(line);
  });

  mapData.nodes.forEach((node) => {
    const p = posById[node.id];
    const g = svgEl('g', { transform: `translate(${p.x},${p.y})` });
    g.classList.add('map-node');
    if (node.clickable) g.classList.add('map-node--clickable');
    g.style.setProperty('--node-tx', p.x + 'px');
    g.style.setProperty('--node-ty', p.y + 'px');
    g.style.cursor = node.clickable ? 'pointer' : 'default';
    if (node.unexplored) g.style.opacity = '0.75';
    // R5/N5. The source dims a visited node at the GROUP (renderMap:54181,
    // `BcY.visited && (BcA.style.filter = "brightness(0.72)")`), which is what
    // makes a visited CIRCLE-branch node (START, on every reachable route) look
    // visited at all. This client only ever dimmed the sprite <image>, so the
    // circle branch rendered undimmed. Note the source really does dim twice on
    // the sprite branch -- appendMapNodeSprite:54067 applies the same filter to
    // the <image> as well, so the two compound there. Both are kept: this is a
    // trace, not a tidy-up.
    if (node.dimmed) g.style.filter = 'brightness(0.72)';

    if (node.sprite_url) appendNodeSprite(g, node);
    else appendNodeCircle(g, node);

    const html = tooltipHtml(node.tooltip);
    g.addEventListener('mouseenter', (ev) => mapTooltip.show(html, ev.clientX, ev.clientY));
    g.addEventListener('mousemove', (ev) => mapTooltip.show(html, ev.clientX, ev.clientY));
    g.addEventListener('mouseleave', () => mapTooltip.hide());
    attachNodeTouchGesture(g, node, html);

    if (node.clickable) {
      g.addEventListener('click', (ev) => {
        // The source suppresses the synthetic click that follows a handled
        // touchend, so a tap does not fire the action twice (54462-54470).
        if (g._suppressNextClick) { g._suppressNextClick = false; return; }
        void ev;
        mapTooltip.hide();
        doAction({ type: 'VisitNode', node_id: node.id });
      });
    }
    svg.appendChild(g);
    groupsById[node.id] = g;
  });

  appendNodeShortcutBadges(mapData, groupsById);
  container.appendChild(svg);
}

// R5. The source's touch long-press tooltip for a map node
// (bundle.deobfuscated.js:54400-54470), deferred here by R2 as R5 interaction
// scope and re-traced this session rather than taken on trust.
//
// The gesture, exactly as the source defines it:
//   * 400 ms (`Bcb = 0x190`, 54400) held without moving opens the tooltip;
//   * a move of more than 12 px (`BcR = 0xc`, 54401) CANCELS the pending
//     long-press -- compared as squared distance, `dx*dx + dy*dy > 12*12`
//     (54442), so it is a true radius and not a per-axis box;
//   * once the tooltip is open, further movement just moves it (54436-54439);
//   * touchend either DISMISSES (if the long-press fired, or the touch lasted
//     >= 400 ms anyway) or acts on the node (54447-54457) -- never both;
//   * touchcancel clears everything (54458-54461).
//
// `mapTooltip.show` is reused for `move`: this client has one show() that also
// repositions, where the source splits show/move (54026-54051).
const NODE_LONG_PRESS_MS = 400;   // 0x190
const NODE_TOUCH_SLOP_PX = 12;    // 0xc

function attachNodeTouchGesture(g, node, html) {
  let timer = null;
  let longPressFired = false;
  let startedAt = 0;
  let startX = 0;
  let startY = 0;

  g.addEventListener('touchstart', (ev) => {
    const t = ev.touches && ev.touches[0];
    if (!t) return;
    longPressFired = false;
    startedAt = Date.now();
    startX = t.clientX;
    startY = t.clientY;
    clearTimeout(timer);
    timer = setTimeout(() => {
      longPressFired = true;
      mapTooltip.show(html, t.clientX, t.clientY);
    }, NODE_LONG_PRESS_MS);
  }, { passive: true });

  g.addEventListener('touchmove', (ev) => {
    const t = ev.touches && ev.touches[0];
    if (!t) return;
    if (longPressFired) {
      mapTooltip.show(html, t.clientX, t.clientY);
      return;
    }
    const dx = t.clientX - startX;
    const dy = t.clientY - startY;
    if (dx * dx + dy * dy > NODE_TOUCH_SLOP_PX * NODE_TOUCH_SLOP_PX) {
      clearTimeout(timer);
      timer = null;
    }
  }, { passive: true });

  g.addEventListener('touchend', (ev) => {
    clearTimeout(timer);
    g._suppressNextClick = true;
    if (longPressFired || Date.now() - startedAt >= NODE_LONG_PRESS_MS) {
      mapTooltip.hide();
      if (ev.preventDefault) ev.preventDefault();
      return;
    }
    if (node.clickable) {
      mapTooltip.hide();
      doAction({ type: 'VisitNode', node_id: node.id });
    }
    if (ev.preventDefault) ev.preventDefault();
  });

  g.addEventListener('touchcancel', () => {
    clearTimeout(timer);
    timer = null;
    longPressFired = false;
    mapTooltip.hide();
  });
}

// R5. The map's own keyboard-shortcut badges (renderMap, 54474-54510): the
// first TWO accessible-and-unvisited nodes, ordered by (layer, col), get a
// numbered SVG badge -- and Digit1/Digit2 on the map screen visit exactly those
// two (the global handler, 88130-88144, re-derives the same ordered list).
// Hidden unless `body.show-shortcuts` is on, which is main.css:8482-8483.
const MAP_SHORTCUT_NODES = 2;

function shortcutOrderedNodes(mapData) {
  return mapData.nodes
    .filter((n) => n.clickable)
    .sort((a, b) => (a.layer !== b.layer ? a.layer - b.layer : a.col - b.col))
    .slice(0, MAP_SHORTCUT_NODES);
}

function appendNodeShortcutBadges(mapData, groupsById) {
  shortcutOrderedNodes(mapData).forEach((node, i) => {
    const g = groupsById[node.id];
    if (!g) return;
    const badge = svgEl('g', { class: 'map-node-shortcut', transform: 'translate(18,-20)' });
    badge.appendChild(svgEl('rect', {
      x: -9, y: -9, width: 18, height: 18, rx: 4,
      fill: '#6b57a6', stroke: '#0e0b16', 'stroke-width': 1.5,
    }));
    const text = svgEl('text', {
      'text-anchor': 'middle', y: 4, fill: '#fff',
      'font-size': 12, 'font-family': 'monospace', 'font-weight': 'bold',
    });
    text.textContent = String(i + 1);
    badge.appendChild(text);
    g.appendChild(badge);
  });
}

// ---------------------------------------------------------------------
// R3: party and bag interaction.
//
// This IS a port of the source's own interaction model, traced in
// bundle.deobfuscated.js rather than invented:
//
//   * renderTeamBar (64593-64817) makes each `.team-slot` a pointer-drag
//     handle. The drop handler `BcV` (64790-64812) resolves the slot under
//     the pointer and performs a straight TWO-ELEMENT SWAP --
//     `[team[dragIdx], team[dropIdx]] = [team[dropIdx], team[dragIdx]]`
//     (64805) -- then re-renders. It is a transposition, never a general
//     reinsertion, which is why the permutation built below is the identity
//     with exactly two positions exchanged. engine.ReorderTeam accepts any
//     permutation; the source only ever produces this one.
//   * A slot's held-item badge owns its own gesture: pointerdown on
//     `.team-slot-item` aborts the drag (64723-64725) and its pointerup opens
//     the equip modal for that held item (64695-64712).
//   * renderItemBadges (64820-64955) renders ONE badge PER BAG INDEX
//     (64834) -- not an aggregated count -- because every downstream call
//     is by index. A tap opens a modal (64936-64939): `openUsableItemModal`
//     for a usable item, `openItemEquipModal` for a held item. A drag onto a
//     team slot short-circuits the modal and applies directly (64940-64950),
//     routing on `it.usable` exactly as engine.EquipItem's docstring cites.
//
// Everything here is gated on `state.legal_actions`, never re-derived: the
// engine already computed which bag indices are equippable and which team
// members each usable item may target (engine.legal_actions, 533-555).
// ---------------------------------------------------------------------

// The source's own 6px movement threshold separating a tap from a drag
// (bundle.deobfuscated.js:64757-64759 for team slots, 64883-64885 for items).
const DRAG_THRESHOLD_PX = 6;

function legalReorder(state) {
  return (state.legal_actions && state.legal_actions.reorder_team) || null;
}

function legalUseItem(state, bagIdx) {
  const entries = (state.legal_actions && state.legal_actions.use_item) || [];
  return entries.find((e) => e.item_index === bagIdx) || null;
}

function legalEquip(state, bagIdx) {
  const eq = state.legal_actions && state.legal_actions.equip_item;
  if (!eq || !eq.bag_indices.includes(bagIdx)) return null;
  return eq;
}

// The transposition the source performs, expressed as the full permutation
// engine.ReorderTeam takes: new_team[i] = old_team[order[i]].
function swapPermutation(size, i, j) {
  const order = [];
  for (let k = 0; k < size; k++) order.push(k);
  order[i] = j;
  order[j] = i;
  return order;
}

function clearDragOver() {
  document.querySelectorAll('.team-slot-dragover')
    .forEach((el) => el.classList.remove('team-slot-dragover'));
}

// Ports the ghost-element drag of renderTeamBar's pointerdown handler
// (64719-64815) and renderItemBadges' (64866-64951). `onDropSlot(idx)` is
// called with the team index under the pointer at pointerup, or the gesture
// is treated as a tap and `onTap()` is called instead.
function attachDragGesture(el, { onDropSlot, onTap }) {
  el.style.touchAction = 'none';
  el.addEventListener('contextmenu', (e) => e.preventDefault());
  el.addEventListener('pointerdown', (e) => {
    if (e.button !== undefined && e.button !== 0) return;
    // 64723-64725: a pointerdown inside the held-item badge is that badge's,
    // not the slot's -- the drag must not start.
    if (e.target.closest && e.target.closest('.team-slot-item')) return;
    e.preventDefault();
    el.setPointerCapture(e.pointerId);
    const box = el.getBoundingClientRect();
    const offX = e.clientX - box.left;
    const offY = e.clientY - box.top;
    const startX = e.clientX;
    const startY = e.clientY;
    let ghost = null;
    let dragging = false;

    const onMove = (ev) => {
      if (!dragging
          && (Math.abs(ev.clientX - startX) > DRAG_THRESHOLD_PX
              || Math.abs(ev.clientY - startY) > DRAG_THRESHOLD_PX)) {
        dragging = true;
        ghost = el.cloneNode(true);
        ghost.classList.add('team-drag-ghost');
        ghost.style.cssText =
          'position:fixed;pointer-events:none;z-index:9999;width:'
          + box.width + 'px;opacity:0.85;transition:none;';
        document.body.appendChild(ghost);
        el.style.opacity = '0.3';
      }
      if (!ghost) return;
      ghost.style.left = (ev.clientX - offX) + 'px';
      ghost.style.top = (ev.clientY - offY) + 'px';
      clearDragOver();
      const over = document.elementFromPoint(ev.clientX, ev.clientY);
      const slot = over && over.closest('#team-bar .team-slot');
      if (slot && slot !== el) slot.classList.add('team-slot-dragover');
    };

    const cleanup = () => {
      if (ghost) ghost.remove();
      ghost = null;
      el.style.opacity = '';
      clearDragOver();
      el.removeEventListener('pointermove', onMove);
      el.removeEventListener('pointerup', onUp);
      el.removeEventListener('pointercancel', cleanup);
    };

    const onUp = (ev) => {
      const wasDragging = dragging;
      const over = wasDragging ? document.elementFromPoint(ev.clientX, ev.clientY) : null;
      const slot = over && over.closest('#team-bar .team-slot');
      cleanup();
      if (!wasDragging) {
        if (onTap) onTap();
        return;
      }
      if (!slot || slot === el) return;
      const slots = [...document.querySelectorAll('#team-bar .team-slot')];
      const idx = slots.indexOf(slot);
      if (idx !== -1 && onDropSlot) onDropSlot(idx);
    };

    el.addEventListener('pointermove', onMove);
    el.addEventListener('pointerup', onUp);
    el.addEventListener('pointercancel', cleanup);
  });
}

function renderTeamBar(state) {
  const el = document.getElementById('team-bar');
  el.innerHTML = '';
  const reorder = legalReorder(state);
  state.team.forEach((mon, idx) => {
    const card = makePokeCard(mon);
    // The source's own class pair (64625-64627): `team-slot` always,
    // `team-slot-reorder` plus a grab cursor only when reordering is live.
    card.classList.add('team-slot');
    if (reorder) {
      card.classList.add('team-slot-reorder');
      card.style.cursor = 'grab';
      card.title = 'Drag onto another team member to swap places';
      // R5. The source's keyboard badge (64629-64632): only while a reorder is
      // live, only on indices 1..5, and labelled with the digit you press --
      // "⇧" + (idx + 1), so slot 1 reads ⇧2. Shift+DigitN then swaps the LEAD
      // with slot N-1 (swapPartyLeadWith, 88145-88177). Index 0 gets no badge
      // because it is already the lead and swapping it with itself is a no-op.
      if (idx >= 1 && idx <= 5) card.setAttribute('data-shortcut', '⇧' + (idx + 1));
    }

    // 64695-64712: the held-item badge opens the equip modal for that item.
    const heldBadge = card.querySelector('.team-slot-item');
    if (heldBadge && mon.held_item) {
      heldBadge.style.cursor = 'pointer';
      heldBadge.title = 'Held: ' + mon.held_item + ' -- click for options';
      heldBadge.addEventListener('pointerdown', (e) => e.stopPropagation());
      heldBadge.addEventListener('click', (e) => {
        e.stopPropagation();
        openHeldItemModal(state, idx);
      });
    }

    if (reorder) {
      attachDragGesture(card, {
        onDropSlot: (dropIdx) => {
          if (dropIdx === idx) return;
          doAction({
            type: 'ReorderTeam',
            order: swapPermutation(reorder.team_size, idx, dropIdx),
          });
        },
      });
    }
    el.appendChild(card);
  });
}

function renderItemBar(state) {
  const el = document.getElementById('item-bar');
  el.innerHTML = '';
  if (!state.items.length) {
    el.textContent = '(none)';
    return;
  }
  // One badge PER BAG INDEX, matching renderItemBadges (64834) -- the
  // aggregated-by-count rendering this replaced could not address an item,
  // and UseItem/EquipItem are both index-addressed.
  state.items.forEach((id, bagIdx) => {
    const info = (state.items_info && state.items_info[bagIdx]) || { id: id, name: id, usable: false };
    const span = document.createElement('span');
    span.className = 'type-badge item-badge';
    span.style.marginRight = '6px';
    span.style.display = 'inline-block';
    span.style.marginBottom = '4px';
    span.textContent = info.name || id;

    const useEntry = legalUseItem(state, bagIdx);
    const equipEntry = legalEquip(state, bagIdx);
    if (useEntry) {
      span.style.cursor = 'pointer';
      span.title = (info.desc || info.name || id) + ' -- click to use';
      // ONE gesture handler, not a `click` listener alongside it. The source
      // installs only the pointer sequence (bundle.deobfuscated.js:64866-64951)
      // and discriminates tap from drag by its own 6px threshold; adding a
      // `click` listener too would fire BOTH on every real tap, because the
      // browser synthesises `click` after `pointerup`.
      attachDragGesture(span, {
        // 64943-64947: a drag straight onto a slot applies without the modal,
        // but only onto a target the eligibility rule allows.
        onDropSlot: (teamIdx) => {
          if (!useEntry.target_indices.includes(teamIdx)) return;
          doAction({ type: 'UseItem', item_index: bagIdx, target_index: teamIdx });
        },
        onTap: () => openUsableItemModal(state, bagIdx, useEntry),
      });
    } else if (equipEntry) {
      span.style.cursor = 'pointer';
      span.title = (info.desc || info.name || id) + ' -- click to equip';
      attachDragGesture(span, {
        // 64948: the non-usable branch, equipItemFromBag(bagIdx, slotIdx).
        onDropSlot: (teamIdx) => {
          doAction({ type: 'EquipItem', bag_index: bagIdx, team_index: teamIdx });
        },
        onTap: () => openEquipFromBagModal(state, bagIdx),
      });
    } else {
      // Rendered but inert: a bag item the engine reports as neither usable
      // nor equippable right now (no eligible target, or an id this port
      // recognises in neither table -- e.g. escape_rope, which is consumed by
      // the loss path, not from the bag). Shown rather than hidden so the bag
      // stays an honest picture of what is carried.
      span.style.opacity = '0.55';
      span.title = (info.desc || info.name || id) + ' -- no action available right now';
    }
    el.appendChild(span);
  });
}

// ---------------------------------------------------------------------
// R3: the two bag modals (openItemEquipModal 79442-79570,
// openUsableItemModal 79671-79782). Built as overlays appended to <body>,
// exactly as the source builds them, rather than as .screen divs -- they sit
// over the map screen and dismiss without a phase change.
// ---------------------------------------------------------------------

function closeModal() {
  const existing = document.getElementById('pokelike-modal');
  if (existing) existing.remove();
}

function buildModal(titleText, descText) {
  closeModal();
  const overlay = document.createElement('div');
  overlay.id = 'pokelike-modal';
  overlay.className = 'item-equip-overlay';
  const box = document.createElement('div');
  box.className = 'item-equip-box';
  const header = document.createElement('div');
  header.className = 'equip-item-header';
  const title = document.createElement('div');
  title.className = 'equip-item-name';
  title.textContent = titleText;
  header.appendChild(title);
  if (descText) {
    const desc = document.createElement('div');
    desc.className = 'equip-item-desc';
    desc.textContent = descText;
    header.appendChild(desc);
  }
  box.appendChild(header);
  const list = document.createElement('div');
  list.className = 'equip-pokemon-list';
  box.appendChild(list);
  overlay.appendChild(box);
  document.body.appendChild(overlay);
  return { overlay, box, list };
}

// One team row, shaped like the source's `.equip-pokemon-row`
// (79466-79505 / 79714-79752). `enabled=false` reproduces the source's own
// greying of an ineligible target (79683-79685) rather than letting the click
// through to a server-side rejection.
function equipRow(mon, rightHtmlText, enabled, onClick) {
  const row = document.createElement('div');
  row.className = 'equip-pokemon-row';
  if (!enabled) {
    row.style.opacity = '0.45';
    row.style.pointerEvents = 'none';
  }
  const img = document.createElement('img');
  img.className = 'equip-poke-sprite';
  img.src = spriteUrl(mon.species_id, mon.is_shiny);
  img.onerror = () => { img.style.display = 'none'; };
  row.appendChild(img);

  const info = document.createElement('div');
  info.className = 'equip-poke-info';
  const name = document.createElement('div');
  name.className = 'equip-poke-name';
  name.textContent = mon.nickname || mon.name;
  info.appendChild(name);
  const lv = document.createElement('div');
  lv.className = 'equip-poke-lv';
  lv.textContent = 'Lv' + mon.level
    + (mon.max_hp != null ? ' — ' + (mon.fainted ? 'Fainted' : mon.current_hp + '/' + mon.max_hp + ' HP') : '');
  info.appendChild(lv);
  row.appendChild(info);

  const held = document.createElement('div');
  held.className = 'equip-held-slot';
  held.textContent = mon.held_item ? '@' + mon.held_item : '— empty —';
  row.appendChild(held);

  const group = document.createElement('div');
  group.className = 'equip-btn-group';
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'btn-primary btn-sm';
  btn.textContent = rightHtmlText;
  btn.disabled = !enabled;
  if (enabled && onClick) btn.addEventListener('click', onClick);
  group.appendChild(btn);
  row.appendChild(group);
  if (enabled && onClick) {
    row.style.cursor = 'pointer';
    row.addEventListener('click', (e) => { if (e.target !== btn) onClick(); });
  }
  return row;
}

function modalCancelButton(box, label) {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.id = 'btn-modal-cancel';
  btn.className = 'btn-secondary btn-md btn-block';
  btn.textContent = label || 'Cancel';
  btn.addEventListener('click', closeModal);
  box.appendChild(btn);
  return btn;
}

// openUsableItemModal (79671-79782): pick a target for a consumable. The
// eligibility rule is `usableItemCanTarget` (79571-79583), which the engine
// already applied -- `entry.target_indices` IS that answer, so the greying
// below is the engine's decision, not a second implementation of it.
function openUsableItemModal(state, bagIdx, entry) {
  const info = (state.items_info && state.items_info[bagIdx]) || { name: state.items[bagIdx] };
  const { box, list } = buildModal('Use ' + (info.name || state.items[bagIdx]), info.desc || '');
  state.team.forEach((mon, teamIdx) => {
    const eligible = entry.target_indices.includes(teamIdx);
    list.appendChild(equipRow(mon, eligible ? 'Use' : 'Not eligible', eligible, () => {
      closeModal();
      doAction({ type: 'UseItem', item_index: bagIdx, target_index: teamIdx });
    }));
  });
  modalCancelButton(box, 'Cancel');
}

// openItemEquipModal's bag branch, `fromBagIdx >= 0` (79442-79570): equip a
// bag item onto a member. Equipping over an existing held item returns that
// item to the bag -- engine._apply_equip_item does exactly that (1853-1858),
// mirroring the source's `items.push(prevHeld)` (79538-79540).
function openEquipFromBagModal(state, bagIdx) {
  const info = (state.items_info && state.items_info[bagIdx]) || { name: state.items[bagIdx] };
  const { box, list } = buildModal('Equip ' + (info.name || state.items[bagIdx]), info.desc || '');
  state.team.forEach((mon, teamIdx) => {
    list.appendChild(equipRow(mon, mon.held_item ? 'Swap' : 'Equip', true, () => {
      closeModal();
      doAction({ type: 'EquipItem', bag_index: bagIdx, team_index: teamIdx });
    }));
  });
  modalCancelButton(box, 'Keep in Bag');
}

// openItemEquipModal's `fromPokemonIdx >= 0` branch (79442-79570), reached
// from a held-item badge (64702-64709).
//
// PARTIAL, and deliberately so -- see docs/audits/R3-implementation.md. The
// source offers "Unequip (return to bag)" (79530) and a direct hand-off to
// another member (79544-79545). Neither is expressible: engine.EquipItem
// moves BAG -> member only (engine.py:301-321, 1833-1860) and the port has no
// unequip/hand-off action at all. Inventing one would be new engine surface,
// which R3 is explicitly not scoped to add. What IS reachable is shown: the
// item can be displaced by equipping a different BAG item onto that member,
// which returns the held one to the bag.
function openHeldItemModal(state, teamIdx) {
  const mon = state.team[teamIdx];
  const { box, list } = buildModal(
    (mon.nickname || mon.name) + ' is holding ' + mon.held_item,
    'Equip a different bag item onto this Pokemon to send ' + mon.held_item + ' back to the bag.'
  );
  const eq = (state.legal_actions && state.legal_actions.equip_item) || null;
  const candidates = eq ? eq.bag_indices : [];
  if (!candidates.length) {
    const note = document.createElement('div');
    note.className = 'equip-poke-lv';
    note.style.padding = '8px';
    note.textContent = 'No equippable item in the bag right now.';
    list.appendChild(note);
  }
  candidates.forEach((bagIdx) => {
    const info = (state.items_info && state.items_info[bagIdx]) || { name: state.items[bagIdx] };
    const row = document.createElement('div');
    row.className = 'equip-pokemon-row';
    const label = document.createElement('div');
    label.className = 'equip-poke-info';
    const nm = document.createElement('div');
    nm.className = 'equip-poke-name';
    nm.textContent = info.name || state.items[bagIdx];
    label.appendChild(nm);
    const ds = document.createElement('div');
    ds.className = 'equip-poke-lv';
    ds.textContent = info.desc || '';
    label.appendChild(ds);
    row.appendChild(label);
    const group = document.createElement('div');
    group.className = 'equip-btn-group';
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'btn-primary btn-sm';
    btn.textContent = 'Swap in';
    btn.addEventListener('click', () => {
      closeModal();
      doAction({ type: 'EquipItem', bag_index: bagIdx, team_index: teamIdx });
    });
    group.appendChild(btn);
    row.appendChild(group);
    list.appendChild(row);
  });
  modalCancelButton(box, 'Cancel');
}

function renderBadgeCount(state) {
  const el = document.getElementById('badge-count-panel');
  el.innerHTML = '';
  for (let i = 0; i < 8; i++) {
    const span = document.createElement('span');
    const earned = i < state.badges;
    span.className = earned ? 'badge-icon-img' : 'badge-icon-empty';
    span.style.display = 'inline-block';
    span.style.width = '16px';
    span.style.height = '16px';
    span.style.borderRadius = '50%';
    span.style.margin = '2px';
    span.style.background = earned ? 'var(--gold)' : 'transparent';
    span.style.border = earned ? '1px solid #000' : '1px dashed #666';
    el.appendChild(span);
  }
}

// ---------------------------------------------------------------------
// Battle / badge / game-over / win screens
// ---------------------------------------------------------------------

// The source's own HP-bar colour function, verbatim (hpBarColor,
// bundle.deobfuscated.js:64134-64137). R4 introduced it for the battle field
// only and left makePokeCard on this client's pre-contract invention
// (>50 green / >20 orange / red); R5/N12 unifies every HP bar on it, because
// the source has exactly ONE hpBarColor and every bar it draws goes through it
// (renderHpBar 64138-64150, renderTeamBar 64622, renderBattleField 64959+).
// Kept here rather than moved up to makePokeCard: this is a function
// DECLARATION, so it is hoisted and both call sites resolve it regardless of
// order, and leaving it put keeps the R5 diff to the thresholds themselves.
function hpBarColor(fraction) {
  return fraction > 0.5 ? '#00FF4A' : fraction > 0.1 ? '#EAFF00' : '#FF0000';
}

// One combatant on the battle field, mirroring renderBattleField's structure
// (bundle.deobfuscated.js:64959-65030): a name+level line, an HP bar with its
// own text, the sprite, and data-idx so a replay step can find it by
// side+index exactly the way animateBattleVisually's querySelector does.
function makeBattleCard(mon, idx) {
  const el = document.createElement('div');
  el.className = 'battle-pokemon' + (mon.current_hp <= 0 ? ' fainted' : '');
  el.setAttribute('data-idx', String(idx));

  const name = document.createElement('div');
  name.className = 'battle-poke-name';
  name.textContent = (mon.nickname || mon.name) + ' Lv' + mon.level;
  el.appendChild(name);

  const hp = document.createElement('div');
  hp.className = 'poke-hp';
  const bg = document.createElement('div');
  bg.className = 'hp-bar-bg';
  const fill = document.createElement('div');
  fill.className = 'hp-bar-fill';
  const frac = Math.max(0, Math.min(1, mon.current_hp / Math.max(1, mon.max_hp)));
  fill.style.width = Math.floor(frac * 100) + '%';
  fill.style.background = hpBarColor(frac);
  bg.appendChild(fill);
  hp.appendChild(bg);
  const text = document.createElement('span');
  text.className = 'hp-text';
  text.textContent = Math.max(0, mon.current_hp) + '/' + mon.max_hp;
  hp.appendChild(text);
  el.appendChild(hp);

  const img = document.createElement('img');
  img.className = 'battle-sprite';
  img.src = spriteUrl(mon.species_id, mon.is_shiny);
  img.alt = mon.name || '';
  img.onerror = () => { img.style.visibility = 'hidden'; };
  el.appendChild(img);
  return el;
}

// Moves one card's HP bar to `hpAfter`. The source tweens this over 250 ms via
// requestAnimationFrame (animateHpBarFull, bundle.deobfuscated.js:65035-65064);
// a CSS transition over the same duration is the deliberate approximation --
// there is no discrete algorithm to port, only a per-frame interpolation, and
// the browser already owns that. Documented as an approximation in
// docs/renderer-contract.md section 11.
function setBattleHp(card, hpAfter, hpMax, durationMs) {
  if (!card || !hpMax) return;
  const fill = card.querySelector('.hp-bar-fill');
  const text = card.querySelector('.hp-text');
  const frac = Math.max(0, Math.min(1, hpAfter / hpMax));
  if (fill) {
    fill.style.transition = 'width ' + durationMs + 'ms linear, background ' + durationMs + 'ms linear';
    fill.style.width = Math.floor(frac * 100) + '%';
    fill.style.background = hpBarColor(frac);
  }
  if (text) text.textContent = Math.max(0, hpAfter) + '/' + hpMax;
  if (hpAfter <= 0) card.classList.add('fainted');
}

// The source's crit/status callout: a short-lived .crit-popup child removed on
// a timer (bundle.deobfuscated.js:69268-69274 for "Critical!", and the same
// shape for Thawed!/Shattered!/Woke up!/Asleep at 69610-69676).
function spawnBattlePopup(card, label) {
  if (!card || !label) return;
  const popup = document.createElement('div');
  popup.className = 'crit-popup';
  popup.textContent = label;
  card.appendChild(popup);
  setTimeout(() => popup.remove(), 800);
}

// R4's replay driver. Cancels any replay still running -- a player who clicks
// through fast can start a second battle before the first finished draining.
let battleReplayToken = 0;

// The source's own replay-speed constants (bundle.deobfuscated.js:63640-63641,
// `const SKIP_SPEED = 0x3, OVERTIME_SPEED = 0x5`) and the overtime timeout's
// own literal (`0x7530` = 30 000 ms, 81270). R4 ported the default of 1 and
// SKIP_SPEED; R5 adds OVERTIME_SPEED and the timeout that arms it.
const SKIP_SPEED = 3;
const OVERTIME_SPEED = 5;
const OVERTIME_MS = 30000;

// The turn-by-turn battle replay. This is what R4 exists for: contract.py's
// battle_view has carried this feed since R1 (fully rostered since R2/N2) and
// until now NOTHING read it -- this screen showed the coarse post-battle log
// entry and jumped straight to the result.
//
// The model is the source's own. runBattleScreen resolves the WHOLE battle
// first (runBattle, bundle.deobfuscated.js:81208-81222) and only then replays
// the finished log through animateBattleVisually (81272). So this is a pure
// client-side drain of an already-fixed sequence: it touches no engine state,
// issues no request, and Engine.step timing is completely unaffected
// (CODEX section 7.7 point 3).
function renderBattle(logEntry, state, onContinue) {
  const view = state && state.battle;
  const token = ++battleReplayToken;

  document.getElementById('battle-title').textContent = logEntry.won ? 'Victory!' : 'Defeat...';
  const subtitle = document.getElementById('battle-subtitle');
  const continueBtn = document.getElementById('btn-continue-battle');
  const p = document.getElementById('player-side');
  const e = document.getElementById('enemy-side');
  const logHost = document.getElementById('battle-log');
  if (logHost) logHost.innerHTML = ''; // one pane per battle, not per run
  showScreen('battle-screen');
  continueBtn.onclick = onContinue;

  // No replay feed (an older server, or a battle that produced no events):
  // fall back to exactly what this screen did before R4 rather than blanking.
  if (!view || !view.replay || !view.replay.length) {
    subtitle.textContent = logEntry.rounds + ' rounds';
    p.innerHTML = '';
    logEntry.player_team.forEach((m) => p.appendChild(makePokeCard(m)));
    e.innerHTML = '';
    logEntry.enemy_team.forEach((m) => e.appendChild(makePokeCard(m)));
    return;
  }

  // First frame: the PRE-battle rosters (contract version 4). player_team /
  // enemy_team are the POST-battle state and are the replay's LAST frame --
  // opening on them would spoil the outcome before a single hit is drawn.
  const playerStart = view.player_team_start.length ? view.player_team_start : view.player_team;
  const enemyStart = view.enemy_team_start.length ? view.enemy_team_start : view.enemy_team;
  p.innerHTML = '';
  playerStart.forEach((m, i) => p.appendChild(makeBattleCard(m, i)));
  e.innerHTML = '';
  enemyStart.forEach((m, i) => e.appendChild(makeBattleCard(m, i)));

  const cardFor = (side, idx) => {
    const host = side === 'player' ? p : e;
    return host.querySelector('.battle-pokemon[data-idx="' + idx + '"]');
  };

  // The Skip control, ported in intent from the source's #btn-auto-battle:
  // it sets battleSpeedMultiplier = SKIP_SPEED (3) and every pause is
  // `ms / battleSpeedMultiplier` (bundle.deobfuscated.js:63640, 69109-69111,
  // 81251-81260). Same divisor, same default of 1.
  let speed = 1;
  continueBtn.textContent = 'Skip';
  continueBtn.onclick = () => { speed = SKIP_SPEED; };

  // R5. The 30-second overtime speed bump (81267-81270), which R4 ported
  // neither half of. The source arms a `setTimeout` for 0x7530 = 30 000 ms as
  // the replay starts; if the replay is still running when it fires, the
  // multiplier is raised to OVERTIME_SPEED (5) -- but only ever RAISED
  // (`battleSpeedMultiplier < OVERTIME_SPEED && ...`), so a player who already
  // pressed Skip is never slowed back down to 5 from a higher speed. The timer
  // is cleared the moment the replay finishes (81273), which is what stops it
  // leaking into the next battle.
  //
  // NOT ported, and deliberately: the `overtime-banner` the source removes on
  // the next line (81274-81275) belongs to a DIFFERENT mechanic that happens to
  // share the name -- an `overtime_start` battle-log record worth 3x damage,
  // built at 69377-69382. That is gameplay, it is Endless-only, and its record
  // would have to come from `battle_loop.py`, which is the oracle's compared
  // surface. This is the presentation speed bump only.
  let overtimeTimer = setTimeout(() => {
    if (speed < OVERTIME_SPEED) speed = OVERTIME_SPEED;
  }, OVERTIME_MS);

  let i = 0;
  const finish = () => {
    clearTimeout(overtimeTimer);
    overtimeTimer = null;
    if (token !== battleReplayToken) return;
    // The source does exactly this at the end of the replay:
    // renderBattleField(Bch, BcL) (81278) redraws from the real post-battle
    // teams. It is not cosmetic -- HP changes that no event records (held-item
    // recoil and healing, battle_loop.py:726-739) are invisible to the replay,
    // so the last replayed HP and the true final HP can legitimately differ.
    p.innerHTML = '';
    view.player_team.forEach((m, idx) => p.appendChild(makeBattleCard(m, idx)));
    e.innerHTML = '';
    view.enemy_team.forEach((m, idx) => e.appendChild(makeBattleCard(m, idx)));
    subtitle.textContent = (view.player_won ? 'Victory' : 'Defeat') + ' -- ' + view.rounds + ' rounds';
    continueBtn.textContent = 'Continue';
    continueBtn.onclick = onContinue;
  };

  const drain = () => {
    if (token !== battleReplayToken) {
      // Superseded by a newer battle. Disarm the overtime timer too, or a
      // stale one would raise the NEW replay's speed 30 s in.
      clearTimeout(overtimeTimer);
      overtimeTimer = null;
      return;
    }
    if (i >= view.replay.length) {
      finish();
      return;
    }
    const step = view.replay[i++];
    subtitle.textContent = step.turn !== null ? ('Turn ' + step.turn) : 'After the turn';

    const card = cardFor(step.side, step.idx);
    const hpMs = Math.round(250 / speed);
    if (step.hp_after !== null && step.hp_max) {
      setBattleHp(card, step.hp_after, step.hp_max, hpMs);
    }
    if (step.crit) {
      if (card) card.classList.add('crit-flash');
      spawnBattlePopup(card, 'Critical!');
      setTimeout(() => card && card.classList.remove('crit-flash'), 800);
    }
    if (step.kind === 'faint' && card) card.classList.remove('active-pokemon');

    // The log line. The source builds this exact string
    // (bundle.deobfuscated.js:69309-69322) but never shows it -- its log
    // container is `const B2V = null` (69084) and its appender returns
    // immediately (69102), so all ~12 of its log calls are dead code in this
    // mirror. This client DOES show it: it is the clearest per-attack
    // presentation available without the source's particle canvases, and the
    // strings are the source's own. Declared as a deviation, not a port.
    appendBattleLogLine(step);

    // Pacing is the source's own per-kind pause (contract._REPLAY_DELAY_MS,
    // carried on the step) plus the HP tween it has to outlast, divided by the
    // same speed multiplier the Skip control moves.
    const wait = Math.round(step.delay_ms / speed) + (step.hp_after !== null ? hpMs : 0);
    setTimeout(drain, Math.max(16, wait));
  };
  drain();
}

// The battle log pane. Cleared per battle, appended per step, kept scrolled to
// the newest line -- the structure the source's own (disabled) appender uses,
// `div.log-entry` children with its log-* class (bundle.deobfuscated.js:
// 69100-69108).
function appendBattleLogLine(step) {
  const host = document.getElementById('battle-log');
  if (!host) return;
  const line = document.createElement('div');
  line.className = 'log-entry ' + (step.cls || '');
  line.textContent = step.text;
  host.appendChild(line);
  host.scrollTop = host.scrollHeight;
}

function renderBadge(state) {
  document.getElementById('badge-count-display').textContent = state.badges + ' / 8 badges';
  showScreen('badge-screen');
}

function renderGameOver(state) {
  document.getElementById('gameover-badges').textContent = 'Badges earned: ' + state.badges;
  const el = document.getElementById('gameover-team');
  el.innerHTML = '';
  state.team.forEach((m) => el.appendChild(makePokeCard(m)));
  showScreen('gameover-screen');
}

function renderWin(state) {
  const el = document.getElementById('win-team');
  el.innerHTML = '';
  state.team.forEach((m) => el.appendChild(makePokeCard(m)));
  showScreen('win-screen');
}

// ---------------------------------------------------------------------
// R3: the two phases that previously fell through to "Unhandled phase".
// ---------------------------------------------------------------------

// Phase.REWARD_TEAM_PICK -- the Sinnoh submap "sacrifice"/"stat10" reward
// (engine.py:3225-3249, resolved by _resolve_reward_team_pick at 3279).
// Options are `_mon_summary` dicts, so mkPokeCardOption fits; the TITLE is
// what carries the meaning, because the two branches present an identical
// team list for opposite outcomes -- releasing a member versus buffing one.
// Both strings come from pending.context (the source's own, showTeamPickerModal
// call sites bundle.deobfuscated.js:77022-77024 and 77041-77044).
function renderRewardPickScreen(state) {
  const ctx = (state.pending && state.pending.context) || {};
  document.getElementById('reward-pick-title').textContent = ctx.title || 'Choose a Pokemon';
  document.getElementById('reward-pick-desc').textContent = ctx.desc || '';
  renderChoiceScreen('reward-pick-screen', 'reward-pick-choices', mkPokeCardOption, null);
}

// Phase.ESCAPE_ROPE_CHOICE -- the P0.6 recovery offered after an ordinary
// boss/legendary loss when a rope is in the bag (engine.py:1543-1554).
// `optional` is always true here: index 0 consumes the rope, null declines
// into GAME_OVER -- the source's two #btn-continue-battle handlers
// (bundle.deobfuscated.js:81409-81421 accept, 81426-81428 decline).
function renderEscapeRopeScreen(state) {
  const ctx = (state.pending && state.pending.context) || {};
  document.getElementById('escape-rope-title').textContent = ctx.title || 'Defeat...';
  document.getElementById('escape-rope-desc').textContent = ctx.desc || '';
  const container = document.getElementById('escape-rope-choices');
  container.innerHTML = '';
  state.pending.options.forEach((opt, idx) => {
    const card = document.createElement('div');
    card.className = 'poke-card';
    card.setAttribute('role', 'button');
    card.setAttribute('tabindex', '0');
    const label = document.createElement('div');
    label.className = 'poke-name';
    label.textContent = opt.label || opt.action || 'Use item';
    card.appendChild(label);
    const sub = document.createElement('div');
    sub.className = 'poke-level';
    sub.textContent = 'Bag slot ' + opt.item_index;
    card.appendChild(sub);
    const act = () => doAction({ type: 'SelectOption', index: idx });
    card.addEventListener('click', act);
    card.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') act();
    });
    container.appendChild(card);
  });
  document.getElementById('btn-decline-rope').style.display =
    state.pending.optional ? '' : 'none';
  showScreen('escape-rope-screen');
}

// Phase.EVOLUTION_CHOICE. R3 rebuild: showBranchingChoice
// (bundle.deobfuscated.js:70560-70612) titles the screen with WHO is evolving
// and renders each branch as sprite + name + types. The engine's option is
// `{into, name}` only; `species_id`, `types` and `is_shiny` are the contract's
// read-side enrichment (contract._pending_options).
function renderEvolutionScreen(state) {
  const ctx = (state.pending && state.pending.context) || {};
  const header = document.querySelector('#evolution-screen .screen-intro h2');
  if (header) header.textContent = ctx.title || 'Evolving!';
  let desc = document.getElementById('evolution-desc');
  if (!desc) {
    desc = document.createElement('p');
    desc.id = 'evolution-desc';
    desc.className = 'screen-desc';
    const intro = document.querySelector('#evolution-screen .screen-intro');
    if (intro) intro.appendChild(desc);
  }
  desc.textContent = ctx.desc || '';
  renderChoiceScreen('evolution-screen', 'evolution-choices', mkPokeCardOption, null);
}

function renderStarterScreen(state) {
  const container = document.getElementById('starter-choices');
  container.innerHTML = '';
  state.pending.options.forEach((opt) => {
    container.appendChild(
      makePokeCard(opt, { onClick: () => doAction({ type: 'ChooseStarter', species_id: opt.species_id }) })
    );
  });
  showScreen('starter-screen');
}

// ---------------------------------------------------------------------
// Master dispatcher
// ---------------------------------------------------------------------

function render(state) {
  currentState = state;
  switch (state.phase) {
    case 'choose_starter':
      renderStarterScreen(state);
      break;
    case 'on_map':
      renderTeamBar(state);
      renderItemBar(state);
      renderBadgeCount(state);
      renderMap(state);
      showScreen('map-screen');
      break;
    case 'catch_choice':
      renderChoiceScreen('catch-screen', 'catch-choices', mkPokeCardOption, 'btn-skip-catch');
      break;
    case 'swap_choice':
      renderChoiceScreen('swap-screen', 'swap-choices', mkPokeCardOption, 'btn-cancel-swap');
      break;
    case 'evolution_choice':
      renderEvolutionScreen(state);
      break;
    case 'move_tutor_choice':
      renderChoiceScreen('move-tutor-screen', 'move-tutor-choices', mkPokeCardOption, 'btn-skip-tutor');
      break;
    case 'item_choice':
      renderChoiceScreen(
        'item-screen',
        'item-choices',
        (opt, idx) => makeItemCard(opt, () => doAction({ type: 'SelectOption', index: idx })),
        'btn-skip-item'
      );
      break;
    case 'item_equip_choice':
      renderChoiceScreen('item-equip-screen', 'item-equip-choices', mkPokeCardOption, null);
      break;
    case 'trade_choice':
      renderChoiceScreen('trade-screen', 'trade-choices', mkPokeCardOption, 'btn-skip-trade');
      break;
    case 'reward_team_pick':
      renderRewardPickScreen(state);
      break;
    case 'escape_rope_choice':
      renderEscapeRopeScreen(state);
      break;
    case 'next_map_ready':
      renderBadge(state);
      break;
    case 'game_over':
      renderGameOver(state);
      break;
    case 'victory':
      renderWin(state);
      break;
    default:
      showToast('Unhandled phase: ' + state.phase);
  }
}

// ---------------------------------------------------------------------
// Static button wiring
// ---------------------------------------------------------------------

function wireButtons() {
  document.getElementById('btn-start-story').onclick = () => resetRun({ nuzlocke_mode: false });
  document.getElementById('btn-start-nuzlocke').onclick = () => resetRun({ nuzlocke_mode: true });
  document.getElementById('btn-next-map').onclick = () => doAction({ type: 'AdvanceMap' });
  document.getElementById('btn-retry').onclick = () =>
    resetRun({ nuzlocke_mode: currentState ? currentState.nuzlocke_mode : false });
  document.getElementById('btn-play-again').onclick = () =>
    resetRun({ nuzlocke_mode: currentState ? currentState.nuzlocke_mode : false });
  document.getElementById('btn-skip-catch').onclick = () => doAction({ type: 'SelectOption', index: null });
  document.getElementById('btn-skip-item').onclick = () => doAction({ type: 'SelectOption', index: null });
  document.getElementById('btn-skip-tutor').onclick = () => doAction({ type: 'SelectOption', index: null });
  document.getElementById('btn-cancel-swap').onclick = () => doAction({ type: 'SelectOption', index: null });
  document.getElementById('btn-skip-trade').onclick = () => doAction({ type: 'SelectOption', index: null });
  // R3. The escape-rope decline: the ordinary "Continue..." that ends the run
  // (bundle.deobfuscated.js:81426-81428).
  document.getElementById('btn-decline-rope').onclick = () => doAction({ type: 'SelectOption', index: null });
  // R3. The item-equip overlay's OTHER two exits, neither of which had a
  // control before. `index: null` is #btn-equip-to-bag -- bank the item and
  // advance (bundle.deobfuscated.js:79552-79562). `cancel: true` is
  // #btn-equip-cancel, whose whole body is `overlay.remove()` (79563-79569):
  // no equip, no bank, no advance, node left unvisited with its offer still
  // pinned. M5 proved those are genuinely different exits and modelled the
  // third as SelectOption.cancel (engine.py:250-261); this is the first
  // browser control that can send it.
  document.getElementById('btn-equip-to-bag').onclick = () => doAction({ type: 'SelectOption', index: null });
  document.getElementById('btn-equip-cancel').onclick = () => doAction({ type: 'SelectOption', cancel: true });
}

// ---------------------------------------------------------------------
// R5: keyboard shortcuts. A port of the source's single global keydown
// handler (bundle.deobfuscated.js:87896-88156), restricted to the screens this
// client actually has -- the source's handler also covers Endless, challenges,
// the Pokedex/achievements/settings/credits modals and the league/mart nav,
// none of which exist here (see webui/__init__.py's scope note).
//
// The shape is the source's own, in its own order:
//   * typing into an INPUT/TEXTAREA/contenteditable suppresses everything
//     (87899-87904);
//   * Escape cancels the item-equip overlay via its own Cancel button
//     (87921-87928);
//   * Enter presses the screen's primary continue-style button (87998-88006);
//   * Space presses the screen's skip/cancel button (88056-88078);
//   * Digit1..N select the Nth card on a choice screen (88079-88129);
//   * unmodified Digit1/Digit2 on the map visit the first two accessible,
//     unvisited nodes ordered by (layer, col) -- the same list the badges are
//     drawn on (88130-88144);
//   * Shift+Digit2..6 swap the party LEAD with slot 1..5 (88145-88154).
// ---------------------------------------------------------------------

// 87965-87971, narrowed to this client's screens.
const ENTER_BUTTON_BY_SCREEN = {
  'battle-screen': 'btn-continue-battle',
  'badge-screen': 'btn-next-map',
  'gameover-screen': 'btn-retry',
  'win-screen': 'btn-play-again',
};

// 88066-88072, same narrowing.
const SPACE_BUTTON_BY_SCREEN = {
  'catch-screen': 'btn-skip-catch',
  'item-screen': 'btn-skip-item',
  'swap-screen': 'btn-cancel-swap',
  'trade-screen': 'btn-skip-trade',
  'move-tutor-screen': 'btn-skip-tutor',
  'escape-rope-screen': 'btn-decline-rope',
};

// The choice screens whose Nth card is selectable by DigitN, with the source's
// own per-screen digit count: catch/item are 3 (88080, 88096), swap is 6
// (88118). The others follow the same rule at their own option count.
const DIGIT_CHOICE_SCREENS = {
  'catch-screen': ['catch-choices', 3],
  'item-screen': ['item-choices', 3],
  'swap-screen': ['swap-choices', 6],
  'trade-screen': ['trade-choices', 3],
  'move-tutor-screen': ['move-tutor-choices', 3],
  'item-equip-screen': ['item-equip-choices', 6],
};

function activeScreenId() {
  const el = document.querySelector('.screen.active');
  return el ? el.id : null;
}

/** `B2Q` (87973-87979): click a button only if it is present and enabled. */
function pressButton(id) {
  const btn = id && document.getElementById(id);
  if (!btn || btn.disabled) return false;
  if (typeof btn.click === 'function') btn.click();
  else if (typeof btn.onclick === 'function') btn.onclick();
  else return false;
  return true;
}

function handleShortcutKey(ev) {
  const tag = ev.target && ev.target.tagName;
  if (tag === 'INPUT' || tag === 'TEXTAREA' || (ev.target && ev.target.isContentEditable)) return;
  if (ev.ctrlKey || ev.metaKey || ev.altKey) return;

  const screen = activeScreenId();
  const digit = ['Digit1', 'Digit2', 'Digit3', 'Digit4', 'Digit5', 'Digit6'].indexOf(ev.code);

  // Escape: the equip overlay's own Cancel, which is NOT a skip -- it banks
  // nothing and leaves the node unvisited (79563-79569, M5's finding).
  if (ev.code === 'Escape' && !ev.shiftKey) {
    if (screen === 'item-equip-screen' && pressButton('btn-equip-cancel')) ev.preventDefault();
    return;
  }

  if (ev.shiftKey) {
    // 88145-88154: Shift+Digit2..6 -> swapPartyLeadWith(1..5).
    if (screen === 'map-screen' && digit >= 1) {
      if (swapPartyLeadWith(digit)) ev.preventDefault();
    }
    return;
  }

  if (ev.code === 'Enter') {
    if (pressButton(ENTER_BUTTON_BY_SCREEN[screen])) ev.preventDefault();
    return;
  }

  if (ev.code === 'Space') {
    if (pressButton(SPACE_BUTTON_BY_SCREEN[screen])
        || pressButton(ENTER_BUTTON_BY_SCREEN[screen])) ev.preventDefault();
    return;
  }

  if (digit < 0) return;

  if (screen === 'map-screen') {
    // 88130-88144. Re-derived from the same ordering the badges use, so the
    // badge and the key can never disagree about which node is "1".
    if (digit >= MAP_SHORTCUT_NODES) return;
    const state = currentState;
    if (!state || !state.map) return;
    const node = shortcutOrderedNodes(state.map)[digit];
    if (!node) return;
    ev.preventDefault();
    mapTooltip.hide();
    doAction({ type: 'VisitNode', node_id: node.id });
    return;
  }

  if (screen === 'starter-screen') {
    const card = document.getElementById('starter-choices').children[digit];
    if (card) { ev.preventDefault(); card.click(); }
    return;
  }

  const choice = DIGIT_CHOICE_SCREENS[screen];
  if (!choice) return;
  const [containerId, limit] = choice;
  if (digit >= limit) return;
  const container = document.getElementById(containerId);
  const card = container && container.children[digit];
  if (card) { ev.preventDefault(); card.click(); }
}

/** `swapPartyLeadWith` (88157-88177): swap team[0] with team[i], guarded to a
 *  real in-range non-lead slot. Expressed here as the engine's own
 *  `ReorderTeam` permutation, which R3 established is a two-element swap
 *  (`bundle.deobfuscated.js:64805`) -- the same action the drag produces. */
function swapPartyLeadWith(i) {
  const state = currentState;
  if (!state || !state.team) return false;
  if (i <= 0 || i >= state.team.length) return false;
  const reorder = legalReorder(state);
  if (!reorder) return false;
  doAction({ type: 'ReorderTeam', order: swapPermutation(reorder.team_size, 0, i) });
  return true;
}

// `applyShortcutsClass` (70823-70828) gates the badges on a desktop pointer AND
// a "show keyboard shortcuts" SETTING. This client has no settings system (see
// webui/__init__.py), so only the pointer half is ported; the badges are on by
// default on a hover-capable pointer, and the CSS that hides them otherwise is
// the mirrored main.css:8482-8483 / 8464-8474, unmodified.
function applyShortcutsClass() {
  let desktop = true;
  try {
    desktop = window.matchMedia('(hover: hover) and (pointer: fine)').matches;
  } catch (e) {
    desktop = false;
  }
  document.body.classList.toggle('show-shortcuts', desktop);
}

wireButtons();
document.addEventListener('keydown', handleShortcutKey);
applyShortcutsClass();
