// MUST FAIL: `.bind(…)()` is an IIFE too. Binding fixes the receiver; it does
// not stop the function running at load. M3.4 Defect C probe 1 -- before the
// M3.5 repair the scanner reported `load-reachable risky references: 0` here.
//
// ONE form per fixture on purpose: a fixture that bundles several spellings
// keeps failing when the detection for any single one is deleted, so it cannot
// tell which detector is still alive. See bind_chain_call.js for the other.
(function () {
  fetch("https://evil.example/bound");
}).bind(this)();
