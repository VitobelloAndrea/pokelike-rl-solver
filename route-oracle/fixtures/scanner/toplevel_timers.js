// MUST FAIL: scheduling work at load is a load-time side effect. The callback
// body runs later -- outside this scanner's reach, possibly after the run, and
// in a sandbox that never advances timers, never at all. M3.4 Defect C probe
// 2: timers were not in the risky set, so this scored 0 references.
//
// `setTimeout`/`setInterval` only; the microtask/frame forms are in
// toplevel_microtask.js so each group flips this corpus on its own.
setTimeout(function () {
  fetch("https://evil.example/later");
}, 0);

setInterval(function () {
  localStorage.setItem("k", "v");
}, 1000);
