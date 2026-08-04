// MUST FAIL: a scheduler reached through an EXPLICIT global root is the global
// scheduler. `window.addEventListener(…)` and `self.queueMicrotask(…)` wire up
// load-time work exactly as the bare calls in toplevel_timers.js do; only the
// spelling differs.
//
// This is the boundary the object/class fixtures test from the other side:
// `emitter.addEventListener` is NOT flagged because `emitter` is not a global
// root, so the rule has to turn on the root's identity and nothing else.
window.addEventListener("load", function () {
  fetch("https://evil.example/on-load");
});

self.queueMicrotask(function () {
  localStorage.setItem("k", "v");
});
