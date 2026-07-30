// fps-meter.js — opt-in on-device performance overlay for diagnosing lag.
//
// NOT part of the game bundle; a separate static script so it can run without
// touching/obfuscating game code. Disabled by default — zero cost unless on.
//
// Turn ON:  visit /fps (e.g. https://…/fps) OR add ?fps to any URL. Persists
//           via localStorage across in-app navigation.
// Turn OFF: visit with ?fps=0  (or clear site data).
//
// Hunting: random single frames ~100ms on some hits — a STALL, not low FPS.
// GC was ruled out (no heap drop on the stalls). So this build adds the Long
// Animation Frames API to break each long frame into category:
//   • LoAF <dur>  — total frame time the browser reports as long (>=50ms)
//   • sl=<n>      — style + LAYOUT time (a forced reflow / big style recalc)
//   • js=<n>      — total SCRIPT time in that frame
//   • fsl=<n>     — worst forced-style/layout caused BY a script (layout thrash)
//   • paint≈dur-sl-js — whatever's left is paint / GPU raster / compositing
//   • └ invoker   — which callback drove the longest script in that frame
// Read the breakdown when a stall hits: sl dominates → reflow; paint≈ dominates
// → GPU raster (the hit filter re-rasterizing the bg-image card); js dominates
// → script.
(function fpsMeter() {
  try {
    const q = location.search || '';
    const path = location.pathname || '';
    if (/[?&]fps=0\b/.test(q)) { try { localStorage.removeItem('poke_fps'); } catch (_) {} return; }
    // Enable via the /fps route (SPA fallback serves index.html) or ?fps on any URL.
    if (/[?&]fps\b/.test(q) || /\/fps\/?$/i.test(path)) { try { localStorage.setItem('poke_fps', '1'); } catch (_) {} }
    let on = false;
    try { on = localStorage.getItem('poke_fps') === '1'; } catch (_) {}
    if (!on) return;
  } catch (_) { return; }

  const box = document.createElement('div');
  box.style.cssText =
    'position:fixed;top:6px;right:6px;z-index:2147483647;pointer-events:none;' +
    'font:11px/1.3 ui-monospace,Menlo,Consolas,monospace;white-space:pre;' +
    'background:rgba(8,12,24,.85);color:#9fe;padding:6px 8px;border-radius:8px;' +
    'border:1px solid #2b3b5c;text-align:right;min-width:172px';
  const add = () => (document.body ? document.body.appendChild(box) : requestAnimationFrame(add));
  add();

  function activeScreen() {
    const els = document.querySelectorAll('[id$="-screen"]');
    for (const e of els) if (e.classList.contains('active')) return e.id.replace('-screen', '');
    return '?';
  }
  function hitActive() {
    return !!document.querySelector(
      '.battle-pokemon.attacking, .battle-pokemon[class*="hit-"], .battle-pokemon.crit-flash'
    );
  }

  // ── Long Animation Frames: the category breakdown of each >=50ms frame ──
  let loaf = null, loafCount = 0;
  const loafSupported = !!(window.PerformanceObserver && PerformanceObserver.supportedEntryTypes
    && PerformanceObserver.supportedEntryTypes.indexOf('long-animation-frame') >= 0);
  const memSupported = !!(window.performance && performance.memory);
  if (loafSupported) {
    try {
      const po = new PerformanceObserver((list) => {
        for (const e of list.getEntries()) {
          loafCount++;
          let jsSum = 0, fsl = 0, topJs = 0, inv = '';
          for (const s of (e.scripts || [])) {
            jsSum += s.duration || 0;
            fsl = Math.max(fsl, s.forcedStyleAndLayoutDuration || 0);
            if ((s.duration || 0) > topJs) { topJs = s.duration; inv = s.invoker || s.sourceURL || s.name || '?'; }
          }
          loaf = {
            dur: Math.round(e.duration),
            sl: Math.round(e.styleAndLayoutDuration || 0),
            js: Math.round(jsSum),
            fsl: Math.round(fsl),
            paint: Math.max(0, Math.round(e.duration - (e.styleAndLayoutDuration || 0) - jsSum)),
            inv: String(inv).slice(-42),
            hit: hitActive(),
          };
        }
      });
      po.observe({ type: 'long-animation-frame', buffered: true });
    } catch (_) {}
  }

  let last = performance.now();
  let stamps = [];
  let longCount = 0, lastLong = 0, hudT = 0;

  function frame(now) {
    const dt = now - last; last = now;
    stamps.push(now);
    while (stamps.length && now - stamps[0] > 1000) stamps.shift();
    if (dt > 50) { longCount++; lastLong = dt; }

    if (now - hudT > 250) {
      hudT = now;
      const fps = stamps.length;
      let worst = 0;
      for (let i = 1; i < stamps.length; i++) { const d = stamps[i] - stamps[i - 1]; if (d > worst) worst = d; }
      box.style.color = fps >= 55 ? '#7fffa8' : fps >= 45 ? '#ffd86b' : '#ff7a7a';
      let txt =
        'cap LoAF:' + (loafSupported ? 'y' : 'n') + ' mem:' + (memSupported ? 'y' : 'n') +
        '\nFPS ' + fps + '   worst ' + worst.toFixed(0) + 'ms' +
        '\nlong>50: ' + longCount + (lastLong ? '  last ' + lastLong.toFixed(0) + 'ms' : '');
      if (loaf) {
        txt += '\nLoAF ' + loaf.dur + 'ms' + (loaf.hit ? ' ⚔' : '') +
               '\n sl=' + loaf.sl + ' js=' + loaf.js + ' paint≈' + loaf.paint + (loaf.fsl ? ' fsl=' + loaf.fsl : '') +
               '\n └ ' + (loaf.inv || '—');
      } else if (!loafSupported) {
        txt += '\nno LoAF on this browser';
      }
      txt += '\n' + activeScreen() + (hitActive() ? '  ⚔HIT' : '');
      box.textContent = txt;
    }
    requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);
})();
