// MUST FAIL: an IIFE evaluates a parameter default whose argument is MISSING,
// before its body starts. M3.8 defect 1 -- the IIFE branch walked the body and
// the supplied arguments but never the parameter patterns, so this scored
// `load-reachable risky references: 0`.
//
// The MUST PASS twin is iife_default_supplied.js: the same default, with an
// argument that proves it dead. Neither verdict may move without the other
// staying put.
(function (value = fetch("https://evil.example/default")) {
  return value;
})();
