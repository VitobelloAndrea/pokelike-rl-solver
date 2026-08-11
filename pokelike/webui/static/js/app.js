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

// ---------------------------------------------------------------------
// M6/N26. Region select.
//
// `engine.reset` has taken gen2/gen3/gen4 since before the renderer track, and
// `server.py:140-142` already decodes and mutual-exclusion-checks all three --
// but the browser sent only `nuzlocke_mode`, so every run in the web UI
// started Gen1 regardless. CODEX section 7.1's finding, never closed.
//
// The table is the source's own `HISTORY_REGIONS`
// (bundle.deobfuscated.js:83084-83112), flag for flag. The `lock` field
// records the source's gate (83223-83226) even though this port does not
// enforce it -- see index.html's #region-screen comment for why.
// ---------------------------------------------------------------------
// `bg` is `STAGE_REGION_BG` (bundle.deobfuscated.js:83070-83078). NONE of
// those files exists in this mirror -- `pokelike_forked/img/` has no
// `regions/` directory at all, the same class of gap as the 33 absent
// gym-leader sprites. The paths are still carried, and the card layers them
// OVER the source's own gradient fallback (83226-83228), so a 404 simply falls
// through to the gradient and the artwork appears by itself if the files are
// ever added.
const HISTORY_REGIONS = [
  { gen: 1, label: 'Kanto', gens: 'Gen 1', flags: {},
    bg: '/img/regions/kanto.jpg', lock: null },
  { gen: 2, label: 'Johto', gens: 'Gen 2', flags: { gen2_mode: true },
    bg: '/img/regions/johto.jpg', lock: 'Beat Kanto to unlock Johto' },
  { gen: 3, label: 'Hoenn', gens: 'Gen 3', flags: { gen3_mode: true },
    bg: '/img/regions/hoenn.jpg', lock: 'Beat the story once to unlock Hoenn' },
  { gen: 4, label: 'Sinnoh', gens: 'Gen 4', flags: { gen4_mode: true },
    bg: '/img/regions/sinnoh.jpg', lock: 'Beat the story once to unlock Sinnoh' },
];

// 83226-83228, the source's own fallback when a region has no background.
const REGION_BG_GRADIENT = 'linear-gradient(135deg,#1a0a3e,#3a0a6e)';

let _regionNuzlocke = false;

/** The exact body `/api/reset` is sent for a region. Split out from the click
 *  handler so it is directly testable: the mutual exclusion is a property of
 *  THIS function, not of a DOM event. */
function resetBodyForRegion(gen, nuzlocke) {
  const region = HISTORY_REGIONS.find((r) => r.gen === gen);
  if (!region) throw new Error('no such region generation: ' + gen);
  // Built from the region's OWN flag set, so two generations can never both be
  // true -- the UI cannot construct the invalid combination the server would
  // reject. Gen 1 is the absence of all three, exactly as HISTORY_REGIONS
  // spells it.
  return Object.assign({ nuzlocke_mode: !!nuzlocke }, region.flags);
}

function renderRegionList() {
  const list = document.getElementById('history-region-list');
  if (!list) return;
  list.innerHTML = '';
  HISTORY_REGIONS.forEach((region) => {
    // `createHistoryRegionCard` (83133-83145) builds a DIV with role=button and
    // tabindex, NOT a <button>, and the class is `history-region-btn`. An
    // earlier pass invented `history-region-card`, which matches nothing in
    // main.css -- so the cards rendered completely unstyled. The
    // `--no-challenge` variant is the source's own two-column layout; this port
    // narrows it further (see index.html) because it has neither the
    // achievements nor the run-history columns the other two carry.
    const card = document.createElement('div');
    card.className = 'history-region-btn history-region-btn--no-challenge';
    card.setAttribute('role', 'button');
    card.setAttribute('tabindex', '0');
    card.setAttribute('data-gen', String(region.gen));

    const visual = document.createElement('div');
    visual.className = 'history-region-visual';
    const bg = document.createElement('div');
    bg.className = 'history-region-bg';
    // The region art layered over the gradient: a missing file falls through
    // to the gradient instead of leaving the card blank.
    bg.style.backgroundImage = `url('${region.bg}'), ${REGION_BG_GRADIENT}`;
    visual.appendChild(bg);

    const drop = document.createElement('div');
    drop.className = 'history-region-label-drop';
    const name = document.createElement('div');
    name.className = 'history-region-name';
    name.textContent = region.label;
    const gens = document.createElement('div');
    gens.className = 'history-region-gens';
    gens.textContent = region.gens;
    drop.appendChild(name);
    drop.appendChild(gens);
    visual.appendChild(drop);
    card.appendChild(visual);

    // `bindHistoryRegionCardActivate` (83146+): a role=button div needs the
    // keyboard half wired explicitly, which a real <button> would give free.
    const activate = () => resetRun(resetBodyForRegion(region.gen, _regionNuzlocke));
    card.addEventListener('click', activate);
    card.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') activate();
    });
    list.appendChild(card);
  });
}

function setRegionMode(nuzlocke) {
  _regionNuzlocke = !!nuzlocke;
  const classic = document.getElementById('btn-history-classic');
  const nuz = document.getElementById('btn-history-nuzlocke');
  // The source's own selected-state class (index.html:695-696).
  if (classic) classic.classList.toggle('history-mode-btn--selected', !_regionNuzlocke);
  if (nuz) nuz.classList.toggle('history-mode-btn--selected', _regionNuzlocke);
}

/** The body that repeats the CURRENT run's mode and region. Reads the
 *  observation's own top-level flags, so it cannot drift from what the engine
 *  actually started. */
function repeatRunBody() {
  const st = currentState;
  if (!st) return { nuzlocke_mode: false };
  const body = { nuzlocke_mode: !!st.nuzlocke_mode };
  if (st.gen2_mode) body.gen2_mode = true;
  else if (st.gen3_mode) body.gen3_mode = true;
  else if (st.gen4_mode) body.gen4_mode = true;
  return body;
}

function showRegionScreen(nuzlocke) {
  setRegionMode(nuzlocke);
  renderRegionList();
  showScreen('region-screen');
}

// ---------------------------------------------------------------------
// R6/N35. Run navigation: reset, and abandon.
//
// RESET GOES THROUGH THE ENGINE, NOT THE DOM. `confirmResetRun`
// (bundle.deobfuscated.js:84556-84568) gates on a confirmation and then calls
// `executeResetRun` (84245); the port's equivalent of "execute" is the
// `/api/reset` call `resetRun` already makes, and the body is `repeatRunBody()`
// -- the same helper M6 built to repeat the CURRENT run's mode and region,
// read off the observation's own top-level flags so it cannot drift from what
// the engine actually started. Nothing here reaches into the DOM to fake a
// state change.
//
// The confirmation is the source's own behaviour, not added caution: 84559-
// 84566 skips the modal only when the run is empty or a setting says to, and
// shows `showResetRunConfirmModal` otherwise. This port has neither an
// is-empty test nor a settings store, so it always confirms -- the safe
// direction of that simplification, since the action discards a run.
//
// WHAT "BACK" MEANS, which R6 §5 asks to be justified. The engine's `Phase` is
// not a navigation stack and almost nothing in it is reversible: a caught
// Pokemon, a chosen reward, a resolved battle and a consumed item are all
// committed by the time the next observation exists, and `engine.py` exposes
// no inverse for any of them. A per-phase "back" would therefore have to be
// faked in the browser, against a server-held state that had already moved --
// exactly the DOM-not-engine reset the brief forbids.
//
// So "back" is ported as the one back the SOURCE actually has: `goHomeFromMenu`
// (84582-84594) abandons the run and returns to the title screen. It is
// labelled "Abandon Run" rather than "Back" because that is what it does, and
// it confirms for the same reason reset does.
//
// `isMidFight` (84569-84581) is ported with it: the source refuses to leave
// while a battle is unresolved and its Continue button is not yet showing.
// This client's equivalent condition is simply that the battle screen is up,
// since its battle replay owns the screen until the player continues.
// ---------------------------------------------------------------------

function isMidFight() {
  return activeScreenId() === 'battle-screen';
}

/** The confirm-then-act modal both run-nav controls share. */
function confirmRunAction(titleText, descText, confirmLabel, onConfirm) {
  const { box, list } = buildModal(titleText, descText);
  const confirm = document.createElement('button');
  confirm.type = 'button';
  confirm.id = 'btn-run-action-confirm';
  confirm.className = 'btn-primary btn-md btn-block';
  confirm.textContent = confirmLabel;
  confirm.addEventListener('click', () => { closeModal(); onConfirm(); });
  list.appendChild(confirm);
  modalCancelButton(box, 'Cancel');
}

function confirmResetRun() {
  if (isMidFight()) return;
  confirmRunAction(
    'Reset Run',
    'Start this region and mode over from the beginning. The current run is lost.',
    'Reset Run',
    () => resetRun(repeatRunBody()));
}

function confirmAbandonRun() {
  if (isMidFight()) return;
  confirmRunAction(
    'Abandon Run',
    'Leave this run and return to the title screen. The current run is lost.',
    'Abandon Run',
    () => {
      // `goHomeFromRun` (84594-84607) ends with `showScreen("title-screen")`.
      // This port models no save/resume (P1.9), so there is no run to persist
      // on the way out -- the engine simply keeps holding the abandoned run
      // until the next /api/reset replaces it.
      currentState = null;
      showScreen('title-screen');
    });
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

// R6/N35. The two body classes the source's quick-nav is driven by.
//
// `in-battle` hides the whole bar (main.css:7595). The source sets it from a
// MutationObserver watching `#battle-screen`'s `active` class
// (pokelike_forked/index.html:777-783); this client has a single funnel for
// that -- `showScreen` -- so it sets it there instead. Same condition, one
// fewer moving part.
//
// `run-menu-in-run` gates the `.nav-in-run` buttons (7597). The source's own
// condition is `state.starterSpeciesId` present AND the screen is not
// gameover/win (`updateRunMenuBar`, 63894-63899): a run exists once a starter
// has been chosen, and stops being abandonable once it is already over. The
// port's equivalent of "a starter has been chosen" is simply having an
// observation whose phase is past `choose_starter`.
const PRE_RUN_SCREENS = new Set(['title-screen', 'region-screen', 'starter-screen']);
const RUN_OVER_SCREENS = new Set(['gameover-screen', 'win-screen']);

function updateRunNav(screenId) {
  const inRun = !!currentState
    && !PRE_RUN_SCREENS.has(screenId)
    && !RUN_OVER_SCREENS.has(screenId);
  document.body.classList.toggle('run-menu-in-run', inRun);
  document.body.classList.toggle('in-battle', screenId === 'battle-screen');
}

function showScreen(id) {
  document.querySelectorAll('.screen').forEach((s) => s.classList.remove('active'));
  const el = document.getElementById(id);
  if (el) el.classList.add('active');
  // R6/N38. The source's own `showScreen` (bundle.deobfuscated.js:63765-63782)
  // tears down the transient overlays on every screen change -- the node
  // tooltip (63772-63773), the item tooltip, and the hover card (63775) --
  // and only then updates the run menu (63777).
  //
  // The port took none of that, and it is a real defect, not tidiness:
  // removing an element does NOT fire `mouseleave`, so a hover card raised
  // over a card that is then clicked away survives its own screen. It was
  // found by looking at a screenshot -- a Pokemon hover card was still
  // floating over the item-choice screen, which is the very complaint R6 §3.2
  // exists for, arriving by a second route.
  mapTooltip.hide();
  hideTeamHoverCard();
  updateRunNav(id);
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
// ---------------------------------------------------------------------
// M6/N24. The stat hover card.
//
// `#team-hover-card` and its CSS were copied verbatim into main.css with R1
// and sat unused ever since: R1 built exactly the data it needs
// (`base_stats`, `effective_stats`, `stat_buffs`, `status_flags` on every
// `mon_view`) so it could be built later, and no milestone from R2 to R5
// claimed it.
//
// Ported from `showTeamHoverCard` (bundle.deobfuscated.js:64506-64564) and
// `hideTeamHoverCard` (64565-64578). Content is the source's own:
// `renderPokemonCard(mon, ...) + hoverCritLine(mon) + hoverAugmentLine(mon)`
// (64523-64524). This client substitutes its own `makePokeCard`, which is the
// same substitution R4 made for the battle field.
// ---------------------------------------------------------------------

// 64494: the crit line appears only when the value differs from the base by
// at least 0.01, so an ordinary Pokemon shows no line at all.
const HOVER_BASE_CRIT = 6.25;

function hoverExtraLines(mon) {
  const out = [];
  if (mon.crit_chance !== undefined && mon.crit_chance !== null
      && Math.abs(mon.crit_chance - HOVER_BASE_CRIT) >= 0.01) {
    const line = document.createElement('div');
    line.className = 'hover-crit';
    // 64496: the source rounds to a whole percent.
    line.textContent = 'Crit chance: ' + Math.round(mon.crit_chance) + '%';
    out.push(line);
  }
  if (mon.augment_pct) {
    const line = document.createElement('div');
    line.className = 'hover-augment';
    line.textContent = '🧬 Augment: +' + mon.augment_pct + '% all stats';
    out.push(line);
  }
  return out;
}

let _hoverHideTimer = null;

function showTeamHoverCard(mon, anchorEl, placement) {
  const el = document.getElementById('team-hover-card');
  if (!el || !mon || !anchorEl) return;
  if (_hoverHideTimer) { clearTimeout(_hoverHideTimer); _hoverHideTimer = null; }
  const wasHidden = el.style.display === 'none' || el.style.display === '' || el._fadingOut;
  el._fadingOut = false;

  el.innerHTML = '';
  el.appendChild(makePokeCard(mon, { hover: true }));
  hoverExtraLines(mon).forEach((line) => el.appendChild(line));
  el.style.display = 'block';
  if (wasHidden) el.style.opacity = '0';

  // 64527-64555. Placement, viewport-clamped with the source's own 8px margin
  // and 6px anchor gap. Side placement is desktop-only: the source gates it on
  // `!matchMedia('(max-width: 768px)').matches` (64531).
  const rect = anchorEl.getBoundingClientRect();
  const w = el.offsetWidth || 200;   // 0xc8
  const h = el.offsetHeight || 300;  // 0x12c
  const side = (!window.matchMedia('(max-width: 768px)').matches
    && (placement === 'left' || placement === 'right')) ? placement : null;
  let top;
  let left;
  if (side) {
    top = rect.top + rect.height / 2 - h / 2;
    if (top + h > window.innerHeight - 8) top = window.innerHeight - h - 8;
    if (top < 8) top = 8;
    if (side === 'left') {
      left = rect.left - w - 8;
      if (left < 8) left = rect.right + 8;
    } else {
      left = rect.right + 8;
      if (left + w > window.innerWidth - 8) left = rect.left - w - 8;
    }
    if (left < 8) left = 8;
    if (left + w > window.innerWidth - 8) left = window.innerWidth - w - 8;
  } else {
    top = rect.bottom + 6;
    if (top + h > window.innerHeight - 8) top = rect.top - h - 6;
    left = rect.left + rect.width / 2 - w / 2;
    if (left + w > window.innerWidth - 8) left = window.innerWidth - w - 8;
    if (left < 8) left = 8;
  }
  el.style.left = left + 'px';
  el.style.top = top + 'px';
  if (wasHidden) {
    requestAnimationFrame(() => { if (!el._fadingOut) el.style.opacity = '1'; });
  } else {
    el.style.opacity = '1';
  }
}

function hideTeamHoverCard() {
  const el = document.getElementById('team-hover-card');
  if (!el) return;
  if (_hoverHideTimer) clearTimeout(_hoverHideTimer);
  el._fadingOut = true;
  el.style.opacity = '0';
  // 0x8c = 140 ms, the source's own fade-out delay before it un-displays.
  _hoverHideTimer = setTimeout(() => {
    el.style.display = 'none';
    el._fadingOut = false;
    _hoverHideTimer = null;
  }, 140);
}

// 64671-64677: mouseenter AND mousemove both (re)show, mouseleave hides.
// `makePokeCard` is shared across the team bar, reward picks, swap screens and
// the battle field, so attaching here is what gives every one of them the same
// card -- which is what the source does, since every one of those surfaces
// calls `showTeamHoverCard` too (64672, 75731, 78217, 78328).
function attachHoverCard(cardEl, mon, placement) {
  cardEl.addEventListener('mouseenter', () => showTeamHoverCard(mon, cardEl, placement));
  cardEl.addEventListener('mousemove', () => showTeamHoverCard(mon, cardEl, placement));
  cardEl.addEventListener('mouseleave', () => hideTeamHoverCard());
  // Deliberately NO `pointerdown` listener here. The source does not attach
  // one either -- it calls `hideTeamHoverCard()` from the handlers that
  // already exist (the held-item badge at 64701, the drag gesture at 64811) --
  // and adding one would put a SECOND pointerdown handler on a team slot that
  // already has the drag gesture's. That is exactly the double-dispatch class
  // R3 found by executing app.js, and R5's standing detector catches it.
}

// R6/N34. `t("common.pwr")` in English (bundle.deobfuscated.js:29233), and
// `t("move.physical")`/`t("move.special")` (29318). Spelled out rather than
// invented, so the badge reads exactly as the site's does.
const MOVE_PWR_LABEL = 'PWR';
const MOVE_CAT_LABEL = { physical: 'Physical', special: 'Special' };

/** The source's `.poke-move` block (bundle.deobfuscated.js:64348-64366).
 *  Appends nothing when the contract supplied no `move_preview` -- the port
 *  reports absence honestly rather than drawing a placeholder move. */
function appendMoveBlock(card, mp) {
  if (!mp || !mp.name) return;
  const block = document.createElement('div');
  block.className = 'poke-move';

  const nameEl = document.createElement('div');
  nameEl.className = 'move-name';
  // 64350-64352: the name is both the text and the title attribute, because
  // `.move-name` clips long names (main.css:1229-1240).
  nameEl.setAttribute('title', mp.name);
  nameEl.textContent = mp.name;
  block.appendChild(nameEl);

  const meta = document.createElement('div');
  meta.className = 'move-meta';

  // 64353-64357. `img/physical.png` and `img/special.png` are among the files
  // GENUINELY ABSENT from this mirror (R6 §4) -- the same class of gap as the
  // region art and the 33 gym-leader sprites. The source has no fallback here
  // because on the real site the files exist.
  //
  // The fallback below is therefore this port's own, but the thing it falls
  // back TO is not invented: `.move-cat-physical` / `.move-cat-special`
  // (main.css:1287-1288) are the source's own badge classes for exactly this
  // label, already carrying its red/purple palette. A missing PNG degrades to
  // the source's own coloured text badge instead of a broken-image icon.
  const catKey = mp.is_special ? 'special' : 'physical';
  const catLabel = MOVE_CAT_LABEL[catKey];
  const icon = document.createElement('img');
  icon.className = 'move-cat-icon';
  icon.src = '/img/' + catKey + '.png';
  icon.alt = catLabel;
  icon.setAttribute('title', catLabel);
  icon.addEventListener('error', () => {
    const badge = document.createElement('span');
    badge.className = 'type-badge move-cat-' + catKey;
    badge.setAttribute('title', catLabel);
    // The badge row is 16px tall and very narrow; the initial is what fits.
    badge.textContent = catLabel.charAt(0);
    icon.replaceWith(badge);
  }, { once: true });
  meta.appendChild(icon);

  // 64358-64361. `type-` + lowercase type is the source's own class rule, and
  // the em dash is its own placeholder for a typeless move.
  const typeBadge = document.createElement('span');
  typeBadge.className = 'type-badge move-type-badge'
    + (mp.type ? ' type-' + String(mp.type).toLowerCase() : '');
  typeBadge.textContent = mp.type ? mp.type : '—';
  meta.appendChild(typeBadge);

  // 64362-64363. `noDamage` shows the em dash; everything else shows the real
  // power. This is the field R6 §6.1 exists for: it reaches the DOM as a value
  // in `.move-power-badge`, not as prose inside a sentence.
  const power = document.createElement('span');
  power.className = 'move-power-badge';
  power.textContent = mp.no_damage ? '—' : (mp.power + ' ' + MOVE_PWR_LABEL);
  meta.appendChild(power);

  block.appendChild(meta);
  card.appendChild(block);
}

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

  // R6/N34. The move, as STRUCTURED markup on every card.
  //
  // M6 rendered this as the prose line `Move: Magical Leaf (Grass 40)` in a
  // `.hover-move` div, and only on the hover card. Both halves of that were
  // this port's invention. The source builds
  // `renderPokemonCard`'s `.poke-move` block (bundle.deobfuscated.js:64348-
  // 64366) on EVERY card it draws, not on a hover overlay:
  //
  //   .poke-move
  //     .move-name          (also the title attribute)
  //     .move-meta
  //       img.move-cat-icon        img/physical.png | img/special.png
  //       span.type-badge.move-type-badge
  //       span.move-power-badge    power + " PWR", or "—" when noDamage
  //
  // main.css already styles all of it verbatim -- `.poke-move` (1176),
  // `.move-meta` (1191), `.move-name` (1229), `.move-power-badge` (1270),
  // `.move-cat-icon` (1198-1209) -- exactly the situation M6 found for
  // `#team-hover-card` itself: the rules shipped and no markup ever used them.
  //
  // ON EVERY CARD, not hover-only, because that is where the source puts it and
  // because it is the answer to the other half of R6/N33: with the hover card
  // now correctly confined to the two screens the source shows it on (see
  // `mkPokeCardOption`), a hover-only move would leave the catch, swap, trade
  // and move-tutor screens with no move information at all -- and the move is
  // the single most decision-relevant fact on a move-tutor card.
  appendMoveBlock(card, data.move_preview);

  // M6/N24. The stat block, drawn only on the hover card. This is the data R1
  // put on every `mon_view` specifically so this could be built later:
  // `effective_stats` is what the battle engine would actually read right now
  // (stages, buffs and the mon's own held item folded in via the same
  // `get_effective_stat` the damage formula calls), `base_stats` is the
  // unmodified table, and `stat_buffs`/`stages` are why they differ.
  //
  // Showing the effective number ALONE would be misleading mid-battle, and
  // showing the base alone is what the port did everywhere before this, so
  // both are shown and the delta is called out.
  if (opts.hover && data.effective_stats) {
    const table = document.createElement('div');
    table.className = 'hover-stats';
    // The number shown is the EFFECTIVE stat -- what the battle engine would
    // really read right now.
    //
    // `base_stats` is deliberately NOT shown beside it as a delta. The two are
    // different quantities on different scales, not a before/after pair:
    // `base_stats` is the species table (Bulbasaur Atk 49) and
    // `effective_stats` is the computed battle stat at this level (Atk 9 at
    // Lv5). Differencing them yields "-40", which reads as a crippling debuff
    // and means nothing. What actually moves the effective number is `stages`
    // and `stat_buffs`, so those are what get annotated.
    //
    // Note also the key asymmetry: `base_stats` spells the defence stat
    // `defense` and carries `hp`; `effective_stats`, `stages` and `stat_buffs`
    // all spell it `def` and have no `hp` (HP is `current_hp`/`max_hp`).
    const stages = data.stages || {};
    const buffs = data.stat_buffs || {};
    const rows = [
      { label: 'Atk', key: 'atk' },
      { label: 'Def', key: 'def' },
      { label: 'SpA', key: 'special' },
      { label: 'SpD', key: 'spdef' },
      { label: 'Spe', key: 'speed' },
    ];
    rows.forEach((spec) => {
      const value = data.effective_stats[spec.key];
      if (value === undefined) return;
      const row = document.createElement('div');
      row.className = 'hover-stat-row';
      const nameEl = document.createElement('span');
      nameEl.className = 'hover-stat-name';
      nameEl.textContent = spec.label;
      const valEl = document.createElement('span');
      valEl.className = 'hover-stat-value';
      valEl.textContent = String(value);
      row.appendChild(nameEl);
      row.appendChild(valEl);
      const stage = stages[spec.key] || 0;
      const buff = buffs[spec.key] || 0;
      if (stage || buff) {
        const note = document.createElement('span');
        note.className = 'hover-stat-delta';
        const total = stage + buff;
        note.style.color = total > 0 ? 'var(--green, #3cc24a)' : 'var(--red, #e22a18)';
        const bits = [];
        if (stage) bits.push((stage > 0 ? '+' : '') + stage + ' stg');
        if (buff) bits.push((buff > 0 ? '+' : '') + buff + ' buf');
        note.textContent = ' ' + bits.join(' ');
        row.appendChild(note);
      }
      table.appendChild(row);
    });
    if (table.children.length) card.appendChild(table);

    // `status` alone is not the whole story: it only ever holds freeze/sleep,
    // while burn, paralysis and poison live in separate fields. R1 collapsed
    // all of them into `status_flags` for exactly this reason.
    const sf = data.status_flags || {};
    const flags = [];
    if (sf.sleep_or_freeze) flags.push(String(sf.sleep_or_freeze));
    if (sf.burned) flags.push('burned');
    if (sf.paralyzed) flags.push('paralyzed');
    if (sf.poison_stacks) flags.push('poison x' + sf.poison_stacks);
    if (flags.length) {
      const row = document.createElement('div');
      row.className = 'hover-status-flags';
      row.style.color = 'var(--red, #e22a18)';
      row.textContent = flags.join(' · ');
      card.appendChild(row);
    }

    if (data.ability) {
      const row = document.createElement('div');
      row.className = 'hover-ability';
      row.textContent = 'Ability: ' + data.ability;
      card.appendChild(row);
    }
  }
  return card;
}

// ---------------------------------------------------------------------
// R6/N33. Item cards.
//
// `makeItemCard` rendered a bare name plus the literal string "Usable" /
// "Held item" and ignored `icon`, `icon_url` and `desc` -- all three of which
// `contract.item_view` (render/contract.py:419-441) has carried on every
// option since R3, which added them precisely so the browser would stop being
// handed bare string ids (CODEX gap 6). The data was there for three
// milestones and the renderer never read it.
//
// Ported from the source's OWN item-offer card
// (bundle.deobfuscated.js:79378-79394):
//
//   .item-card
//     .item-icon                 itemIconHtml(item, 0x24)   -> 36px
//     .item-name                 tItemName(id, name)
//     .item-desc                 tItemDesc(id, desc)
//     .item-tag.item-tag--usable "USABLE ITEM"   (only when usable)
//
// THE CLASS WAS WRONG, and this is the R6 §3.1 question answered from the
// source rather than by taste: the card was labelled `poke-card`, the same
// class Pokemon cards use. `.item-card` is a real, distinct rule in the
// stylesheet (main.css:1989-2003) with its own border, hover lift and
// `.locked` state, and `.item-icon`/`.item-name`/`.item-desc`/`.item-tag`/
// `.item-tag--usable` (2015-2029) only ever apply INSIDE it. Under
// `poke-card` every one of those child rules was inert, which is the direct
// reason the screen looked like bare text. `poke-card` is also wrong on its
// own terms -- it is a 180px flex column built around a sprite (1072-1080),
// and an item has no sprite slot. The same pairing appears again at
// 84906-84914 for the Challenge passive picker, so `.item-card` is the
// source's settled item-card class, not a one-off.
// ---------------------------------------------------------------------

/** The source's `itemIconHtml` (bundle.deobfuscated.js:52113-52141), as DOM.
 *
 *  One deliberate substitution. The source falls back to
 *  `raw.githubusercontent.com/PokeAPI/sprites/.../items/<id>.png` -- a REMOTE
 *  URL fetched at render time. This client is local-only and offline, and its
 *  species sprites are already served from a local cache that
 *  `tools/fetch-sprites/` fills from that same public dataset, so the item
 *  icon points at the local mirror of it on the same path shape. The id
 *  transform (`_` -> `-`) is the source's own (52116).
 *
 *  The `onerror` behaviour is the source's own too (52122-52128): a failed
 *  load is replaced by a span carrying the item's emoji at 80% of the icon
 *  box, floored at 10px. That is why a missing PNG is not a defect here --
 *  every ported item carries an emoji `icon`, so the card always shows
 *  something. */
function appendItemIcon(parent, opt, size) {
  const px = size || 36;
  const img = document.createElement('img');
  img.className = 'item-sprite-icon pixel-art';
  img.src = opt.icon_url || ('/img/sprites/items/' + String(opt.id).replace(/_/g, '-') + '.png');
  img.alt = opt.name || '';
  img.setAttribute('title', opt.name || '');
  img.style.width = px + 'px';
  img.style.height = px + 'px';
  img.style.verticalAlign = 'middle';
  img.addEventListener('error', () => {
    const span = document.createElement('span');
    span.textContent = opt.icon || '';
    span.style.fontSize = Math.max(10, Math.round(px * 0.8)) + 'px';
    span.style.lineHeight = '1';
    span.style.verticalAlign = 'middle';
    img.replaceWith(span);
  }, { once: true });
  parent.appendChild(img);
}

function makeItemCard(opt, onClick) {
  const card = document.createElement('div');
  card.className = 'item-card';
  card.setAttribute('role', 'button');
  card.setAttribute('tabindex', '0');
  card.addEventListener('click', onClick);
  // The source sets `cursor: pointer` inline (79395) and routes the card
  // through `_kbCard` for keyboard activation; this client wires the keyboard
  // half directly, as it already does for the region cards.
  card.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') onClick(e);
  });

  const icon = document.createElement('div');
  icon.className = 'item-icon';
  appendItemIcon(icon, opt, 36);
  card.appendChild(icon);

  const name = document.createElement('div');
  name.className = 'item-name';
  name.textContent = opt.name;
  card.appendChild(name);

  // The description the contract has been carrying unread since R3. Omitted
  // rather than blanked when the item is one `item_view` reports `known:
  // false` for (Mega Stones -- a separate source table with a different
  // shape), so an unknown item degrades to icon + name instead of an empty row.
  if (opt.desc) {
    const desc = document.createElement('div');
    desc.className = 'item-desc';
    desc.textContent = opt.desc;
    card.appendChild(desc);
  }

  // 79391-79393: the tag is drawn ONLY for a usable item. A held item gets no
  // tag at all -- the port's old unconditional "Held item" line was its own
  // invention.
  if (opt.usable) {
    const tag = document.createElement('div');
    tag.className = 'item-tag item-tag--usable';
    tag.textContent = 'USABLE ITEM';
    card.appendChild(tag);
  }
  return card;
}

// ---------------------------------------------------------------------
// R6/N33. The hover card is attached PER SCREEN, not inside this helper.
//
// M6/N24 called `attachHoverCard` from inside `mkPokeCardOption`, on the
// stated belief that the source shows a hover card on every screen where the
// player chooses between Pokemon. That belief was wrong, and the concrete
// symptom the owner reported is that assigning an item to a Pokemon raised an
// unwanted Pokemon-card overlay on the item-equip screen.
//
// The `showTeamHoverCard` call sites, re-derived here rather than taken on
// trust, are exhaustively:
//
//   64672, 64675, 64811   the team bar (`renderTeamBar`; 64811 is the same
//                         function's pointer-drag release path)
//   75731                 the starter screen
//   78217, 78223          the Elite-prep party list
//   78328                 the Elite-prep "vs" comparison
//
// That is the whole list. The item-equip target list (79495-79521) is NOT on
// it, and neither are the catch, swap, trade, move-tutor or reward screens.
// Ports of the two Elite-prep sites do not arise: this client has no
// Elite-prep screen.
//
// So the attachment now happens at exactly two places -- `renderTeamBar` and
// `renderStarterScreen` -- and `mkPokeCardOption`, which every other choice
// screen shares, attaches nothing. Being a shared helper is what made the
// regression reach a screen nobody intended it to; keeping the attachment at
// the call site is what stops the next change from doing the same.
//
// What the other choice screens lose is not lost information: the source does
// not need a hover card there because its own `renderPokemonCard` draws the
// move block on the card itself, which `makePokeCard` now does too (N34). The
// stat block remains hover-only, which is this port's own reduction and is
// recorded as an open finding rather than silently widened here.
// ---------------------------------------------------------------------
// The two surfaces that DO get a hover card already attach their own, on the
// elements they build themselves: `renderTeamBar` (app.js, on each
// `.team-slot`) and `renderStarterScreen`. Neither goes through this helper,
// so no hover-attaching variant of it is needed -- and adding one "for
// symmetry" would just re-create the shared attachment point this removes.
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
// The source's no-sprite branch (bundle.deobfuscated.js:54314-54348). This is
// the source's REAL behaviour on an image-load error, not an invention -- but
// because no node sprite files ship with this mirror (R6 §4), it is the branch
// this port draws for EVERY node, all the time, rather than the rare fallback
// it is on the live site. That is why R6 asks for it to be made more legible.
//
// Everything the source decides is still the source's: `circle_radius` (22 for
// a boss, 18 otherwise, 54315), the per-type fill from `getNodeColor`, the
// unexplored `#2a2a3a`/`#aaa` pair, the stroke colours and widths, the
// clickable pulse, and the glyph from `getNodeIcon`. The contract already
// carries all of them.
//
// R6/N37 — the two legibility changes, both labelled as this port's own:
//
//  1. The glyph gets a black outline via `paint-order: stroke`. The source
//     draws it as bare `fill: #fff` over per-type fills that range from
//     `#6a4a1a` to `#333`, and a white glyph on a light fill washes out. The
//     technique is NOT invented: it is the source's own, used 20 lines above
//     on the boss-preview level text (54294-54299, `paint-order: stroke` /
//     `stroke: #000` / `stroke-width: 2`). Applied here to the node glyph.
//  2. The glyph scales with the circle instead of being a fixed 14px. The
//     source hard-codes 14 for both radii, so a boss's larger 22px circle
//     carries the same size glyph as an ordinary 18px one and reads as
//     emptier, not bigger. Scaling keeps 14 at r=18 exactly, so the ordinary
//     node is unchanged from the source and only the boss grows.
function appendNodeCircle(g, node) {
  const radius = node.sprite_size.circle_radius;
  const circle = svgEl('circle', {
    r: radius,
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
    // 14 at the source's ordinary radius of 18, by construction.
    'font-size': Math.round((14 * radius) / 18),
    fill: node.unexplored ? '#aaa' : '#fff',
    'paint-order': 'stroke',
    stroke: '#000',
    'stroke-width': 2,
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

// M6/N25. The map is drawn at whatever size the container happens to be when
// `renderMap` runs, and `renderMap` only runs on a state update -- so before
// this, resizing the window or rotating a phone left every node at its old
// pixel position until the player next clicked something. There was no
// `resize` handler of any kind.
//
// The container's size no longer depends on sibling content (see index.html's
// #map-container override, which is the actual N25 fix), so this observer now
// fires only on a REAL viewport change rather than every time the team grew.
// It is still debounced: a drag-resize emits a continuous stream, and
// re-rendering the whole SVG per frame is wasted work.
let _mapResizeObserver = null;
let _mapResizeTimer = null;
let _mapLastSize = '';

function observeMapContainer(container) {
  if (typeof ResizeObserver !== 'function' || _mapResizeObserver) return;
  _mapResizeObserver = new ResizeObserver(() => {
    if (_mapResizeTimer) clearTimeout(_mapResizeTimer);
    _mapResizeTimer = setTimeout(() => {
      _mapResizeTimer = null;
      const size = Math.round(container.clientWidth) + 'x' + Math.round(container.clientHeight);
      // Only redraw once the size has actually SETTLED on something new --
      // the observer also fires for the render we just did.
      if (size === _mapLastSize) return;
      // MUST read the app's CURRENT state, never a captured one. The observer
      // is installed once and lives for the whole run, so an earlier version
      // of this closed over the `state` argument of the FIRST renderMap call
      // and redrew that forever: after visiting a node, any later resize
      // repainted the PREVIOUS map, showing the node you had just cleared as
      // still clickable and the newly-reachable one as not. That is a live bug
      // report -- "select the first node, cannot go on to the second".
      if (currentState && currentState.map) renderMap(currentState);
    }, 120);
  });
  _mapResizeObserver.observe(container);
}

function renderMap(state) {
  const container = document.getElementById('map-container');
  container.innerHTML = '';
  const mapData = state.map;
  if (!mapData) return;
  observeMapContainer(container);

  // The source sizes its SVG from the live container, with the same fallbacks
  // (bundle.deobfuscated.js:54113-54125).
  const w = Math.round(container.clientWidth) || MAP_DEFAULT_W;
  const h = Math.round(container.clientHeight) || MAP_DEFAULT_H;
  // What the observer above compares against, so it can tell "the size really
  // changed" from "we just re-rendered at the same size".
  _mapLastSize = w + 'x' + h;

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
    // M6/N24. 64671-64677 attaches the hover card to every team slot.
    attachHoverCard(card, mon);
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
        // 64701: the source dismisses the hover card before opening the
        // overlay, or it would sit on top of the modal it just raised.
        hideTeamHoverCard();
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
// from a held-item badge (64702-64709 on the team bar, 78203 on the party
// screen).
//
// M6 completes this. R3 built the shell but could draw neither of the source's
// two remaining exits, because the engine had no action for either; both now
// exist (engine.UnequipItem / engine.HandOffItem), so the modal is now the
// source's own layout:
//
//   * one row per member, labelled exactly as the source labels it (79470):
//     "Holding" for the member the overlay was opened from, "Swap" for a
//     member already holding something, "Equip" otherwise;
//   * "⬇ Unequip (return to bag)" -- `#btn-equip-to-bag` with
//     `fromPokemonIdx >= 0` (79518, 79549-79553);
//   * "Cancel" -- `#btn-equip-cancel` (79563-79569), whose whole body is
//     `overlay.remove()`, which is why it closes locally and sends nothing.
//
// The member rows send HandOffItem, NOT EquipItem: the source's hand-off gives
// the target's old item to the SOURCE member (79544-79545), where an
// unequip-then-equip would send it to the bag. See engine.HandOffItem.
function openHeldItemModal(state, teamIdx) {
  const mon = state.team[teamIdx];
  const info = mon.held_item_info || null;
  const itemLabel = (info && info.name) || mon.held_item;
  const { box, list } = buildModal(
    (mon.nickname || mon.name) + ' is holding ' + itemLabel,
    (info && info.desc) || ''
  );

  const legal = state.legal_actions || {};
  const canHandOff = !!legal.hand_off_item
    && (legal.hand_off_item.from_indices || []).includes(teamIdx);

  state.team.forEach((other, otherIdx) => {
    if (otherIdx === teamIdx) {
      // The source still draws this row, disabled, labelled "Holding".
      list.appendChild(equipRow(other, 'Holding', false, null));
      return;
    }
    list.appendChild(equipRow(other, other.held_item ? 'Swap' : 'Equip', canHandOff, () => {
      closeModal();
      doAction({ type: 'HandOffItem', from_index: teamIdx, to_index: otherIdx });
    }));
  });

  const canUnequip = !!legal.unequip_item
    && (legal.unequip_item.team_indices || []).includes(teamIdx);
  const toBag = document.createElement('button');
  toBag.type = 'button';
  toBag.id = 'btn-equip-to-bag';
  toBag.className = 'btn-primary btn-md btn-block';
  toBag.textContent = '⬇ Unequip (return to bag)';
  toBag.disabled = !canUnequip;
  toBag.addEventListener('click', () => {
    closeModal();
    doAction({ type: 'UnequipItem', team_index: teamIdx });
  });
  box.appendChild(toBag);

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
    // teams.
    //
    // M6/N10 closed the biggest reason this mattered: held-item recoil and
    // healing now emit their own `effect` records, so the replay accounts for
    // those HP changes rather than ending on a number the roster contradicts.
    // The redraw stays -- it is what the source does, and the families still
    // unrecorded (send-outs, transforms) can move a roster the replay never
    // mentions.
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
    // M6/N10. The source's `effect` branch (bundle.deobfuscated.js:69352-69375)
    // does two things beyond the HP bar every step already gets: it spawns a
    // damage popup whenever `hpChange` is truthy, keyed "heal" when positive
    // and "normal" otherwise (69364-69368), and it clears the `fainted` class
    // when the new HP is above zero (69369) -- a heal can bring a card back.
    if (step.kind === 'effect' && card) {
      if (step.hp_after > 0) card.classList.remove('fainted');
      if (step.damage) {
        spawnBattlePopup(card, (step.damage > 0 ? '+' : '') + step.damage + ' HP');
      }
    }

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
    const card = makePokeCard(
      opt, { onClick: () => doAction({ type: 'ChooseStarter', species_id: opt.species_id }) });
    // M6/N24. The source shows the hover card here too (75731).
    attachHoverCard(card, opt);
    container.appendChild(card);
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
  // M6/N26. Both title cards now open the region screen instead of starting a
  // Gen1 run immediately -- which is what the source does (its Story card
  // opens `#history-region-select`, index.html:302-312 -> 682). The Nuzlocke
  // card is this port's own extra entry point and simply pre-sets the toggle
  // the source puts on that screen.
  document.getElementById('btn-start-story').onclick = () => showRegionScreen(false);
  document.getElementById('btn-region-back').onclick = () => showScreen('title-screen');
  document.getElementById('btn-history-classic').onclick = () => setRegionMode(false);
  document.getElementById('btn-history-nuzlocke').onclick = () => setRegionMode(true);
  document.getElementById('btn-next-map').onclick = () => doAction({ type: 'AdvanceMap' });
  // M6/N26. These used to send `nuzlocke_mode` ALONE, so retrying or replaying
  // after a Johto/Hoenn/Sinnoh run silently restarted it in Kanto. The
  // observation has carried `gen2_mode`/`gen3_mode`/`gen4_mode` at top level
  // since R1, so the current run's own region is what gets repeated.
  document.getElementById('btn-retry').onclick = () => resetRun(repeatRunBody());
  document.getElementById('btn-play-again').onclick = () => resetRun(repeatRunBody());
  // R6/N35. The floating quick-nav. Both go through the engine or through
  // `showScreen`; neither fabricates state in the DOM. The source binds these
  // with inline `onclick="confirmResetRun()"` / `goHomeFromMenu()`
  // (pokelike_forked/index.html:445-446); this client keeps its own convention
  // of wiring every button from one place.
  document.getElementById('btn-run-reset').onclick = () => confirmResetRun();
  document.getElementById('btn-run-home').onclick = () => confirmAbandonRun();
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

  // R6/N35. The quick-nav's own shortcuts, which its markup already declares
  // as `data-shortcut="R"` / `"H"` -- the same letters the source binds
  // (pokelike_forked/index.html:445-446) and the same badges main.css draws
  // for them (8476). Both are inert outside a run, because `.nav-in-run` hides
  // the buttons there and `pressButton` is the same path a click takes.
  if (ev.code === 'KeyR' || ev.code === 'KeyH') {
    if (!document.body.classList.contains('run-menu-in-run')) return;
    const id = ev.code === 'KeyR' ? 'btn-run-reset' : 'btn-run-home';
    if (pressButton(id)) ev.preventDefault();
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
