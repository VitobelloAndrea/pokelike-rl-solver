// MUST FAIL: a default nested inside a destructuring pattern. The outer default
// `= {}` supplies an object with no `endpoint` in it, so the inner default runs.
//
// Nested defaults are walked whenever the function is invoked at load, whatever
// the argument is: deciding otherwise would mean destructuring a supplied value,
// i.e. walking the object graph, which the M3 convergence boundary places
// outside the declared contract of this scanner. The direction is deliberate --
// it can over-report, never miss.
(function ({ endpoint = fetch("https://evil.example/nested") } = {}) {
  return endpoint;
})();
