// Pokelike web UI -- talks to the local JSON API (pokelike/webui/server.py)
// wrapping pokelike/engine.py. NOT a port of pokelike_forked/js/bundle.js --
// a brand-new client for a brand-new (Python) game engine. Reuses the real
// site's CSS classes (main.css, copied verbatim) for visual consistency.
//
// Known limitation (see pokelike/webui/__init__.py's module docstring):
// battle_loop.run_battle resolves a whole battle synchronously with no
// per-turn event feed, so the battle screen here shows the pre-battle
// matchup then jumps straight to the final result on "Continue" -- not an
// animated turn-by-turn exchange like the real site.

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

// A battle that just happened (state.log's newest entry) gets shown as its
// own screen with a "Continue" button before whatever screen the new phase
// implies -- see this file's header note on why it isn't animated.
function applyWithBattleInterstitial(state) {
  const hasNew = state.log_total > lastSeenLogTotal && state.log.length > 0;
  const last = state.log[state.log.length - 1];
  lastSeenLogTotal = state.log_total;
  if (hasNew && last && last.type === 'battle') {
    renderBattle(last, () => render(state));
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
    fill.style.background = clamped > 50 ? '#3cc24a' : clamped > 20 ? '#f08c10' : '#e22a18';
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
// Map rendering -- a fixed per-layer grid layout, NOT a port of the
// source's own (untraced) node-positioning algorithm. See
// pokelike/webui/__init__.py's module docstring.
// ---------------------------------------------------------------------

const VIEW_W = 1600;
const VIEW_H = 2668;
const NODE_R = 60;

const NODE_SYMBOLS = {
  start: '●', battle: 'B', catch: 'C', item: 'I', question: '?', boss: 'S',
  pokecenter: 'P', trainer: 'T', legendary: 'L', move_tutor: 'M', trade: 'R',
  silver: 'Y', magma: 'G', aqua: 'A', underground: 'U', distortion: 'D',
  reward: 'W', subexit: 'X',
};

function nodeSymbol(node, questionCache) {
  if (node.type === 'question' && questionCache && questionCache[node.id]) {
    const resolved = questionCache[node.id];
    if (resolved === 'shiny') return '*';
    if (resolved === 'mega') return '+';
    return NODE_SYMBOLS[resolved] || '?';
  }
  return NODE_SYMBOLS[node.type] || '?';
}

function nodeFillColor(node) {
  if (!node.revealed) return '#222';
  if (node.accessible) return '#ffd700';
  if (node.visited) return '#4a5a88';
  return '#888';
}

function renderMap(state) {
  const container = document.getElementById('map-container');
  container.innerHTML = '';
  const mapData = state.map;
  if (!mapData) return;

  const nodesById = {};
  mapData.nodes.forEach((n) => { nodesById[n.id] = n; });
  const byLayer = {};
  mapData.nodes.forEach((n) => { (byLayer[n.layer] = byLayer[n.layer] || []).push(n); });
  const layerIndices = Object.keys(byLayer).map(Number).sort((a, b) => a - b);
  const layerCount = layerIndices.length;

  function pos(node) {
    const layerPos = layerIndices.indexOf(node.layer);
    const y = 140 + (layerCount <= 1 ? 0 : layerPos / (layerCount - 1)) * (VIEW_H - 280);
    const width = byLayer[node.layer].length;
    const x = width <= 1 ? VIEW_W / 2 : 200 + (node.col / (width - 1)) * (VIEW_W - 400);
    return { x, y };
  }

  const svgNS = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(svgNS, 'svg');
  svg.setAttribute('viewBox', `0 0 ${VIEW_W} ${VIEW_H}`);
  svg.setAttribute('preserveAspectRatio', 'xMidYMid meet');

  mapData.edges.forEach(([srcId, dstId]) => {
    const src = nodesById[srcId];
    const dst = nodesById[dstId];
    if (!src || !dst) return;
    const p1 = pos(src);
    const p2 = pos(dst);
    const line = document.createElementNS(svgNS, 'line');
    line.setAttribute('x1', p1.x);
    line.setAttribute('y1', p1.y);
    line.setAttribute('x2', p2.x);
    line.setAttribute('y2', p2.y);
    line.setAttribute('stroke', src.visited ? '#ffd700' : '#555');
    line.setAttribute('stroke-width', '6');
    svg.appendChild(line);
  });

  mapData.nodes.forEach((node) => {
    const p = pos(node);
    const g = document.createElementNS(svgNS, 'g');
    g.classList.add('map-node');
    if (node.accessible) g.classList.add('map-node--clickable');
    g.style.setProperty('--node-tx', p.x + 'px');
    g.style.setProperty('--node-ty', p.y + 'px');
    g.setAttribute('transform', `translate(${p.x}, ${p.y})`);

    const circle = document.createElementNS(svgNS, 'circle');
    circle.setAttribute('r', String(NODE_R));
    circle.setAttribute('fill', nodeFillColor(node));
    const isCurrent = node.id === mapData.current_node_id;
    circle.setAttribute('stroke', isCurrent ? '#ffd700' : '#000');
    circle.setAttribute('stroke-width', isCurrent ? '8' : '4');
    g.appendChild(circle);

    const label = document.createElementNS(svgNS, 'text');
    label.setAttribute('text-anchor', 'middle');
    label.setAttribute('dominant-baseline', 'central');
    label.setAttribute('font-size', '40');
    label.setAttribute('fill', '#000');
    label.textContent = nodeSymbol(node, mapData.question_cache);
    g.appendChild(label);

    if (node.accessible) {
      g.style.cursor = 'pointer';
      g.addEventListener('click', () => doAction({ type: 'VisitNode', node_id: node.id }));
    }
    svg.appendChild(g);
  });

  container.appendChild(svg);
}

function renderTeamBar(state) {
  const el = document.getElementById('team-bar');
  el.innerHTML = '';
  state.team.forEach((mon) => el.appendChild(makePokeCard(mon)));
}

function renderItemBar(state) {
  const el = document.getElementById('item-bar');
  el.innerHTML = '';
  if (!state.items.length) {
    el.textContent = '(none)';
    return;
  }
  const counts = {};
  state.items.forEach((id) => { counts[id] = (counts[id] || 0) + 1; });
  Object.entries(counts).forEach(([id, count]) => {
    const span = document.createElement('span');
    span.className = 'type-badge';
    span.style.marginRight = '6px';
    span.style.display = 'inline-block';
    span.style.marginBottom = '4px';
    span.textContent = id + (count > 1 ? ' x' + count : '');
    el.appendChild(span);
  });
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

function renderBattle(logEntry, onContinue) {
  document.getElementById('battle-title').textContent = logEntry.won ? 'Victory!' : 'Defeat...';
  document.getElementById('battle-subtitle').textContent = logEntry.rounds + ' rounds';
  const p = document.getElementById('player-side');
  p.innerHTML = '';
  logEntry.player_team.forEach((m) => p.appendChild(makePokeCard(m)));
  const e = document.getElementById('enemy-side');
  e.innerHTML = '';
  logEntry.enemy_team.forEach((m) => e.appendChild(makePokeCard(m)));
  showScreen('battle-screen');
  document.getElementById('btn-continue-battle').onclick = onContinue;
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
      renderChoiceScreen(
        'evolution-screen',
        'evolution-choices',
        (opt, idx) =>
          makePokeCard({ species_id: opt.into, name: opt.name }, {
            onClick: () => doAction({ type: 'SelectOption', index: idx }),
          }),
        null
      );
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
}

wireButtons();
