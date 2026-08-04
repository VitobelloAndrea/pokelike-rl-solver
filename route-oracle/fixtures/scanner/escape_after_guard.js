// MUST FAIL: the first access is correctly guarded, the second is not. Guard
// domination is per-identifier AND positional: a `typeof` test inside an `if`
// consequent guards that consequent only, and does not dominate the statements
// that follow it. Reporting this file as clean would mean one legitimate guard
// launders every later access to the same global.
(function () {
  if (typeof localStorage !== "undefined") {
    localStorage.getItem("guarded-and-fine");
  }
  localStorage.setItem("not-guarded-at-all", "1");
})();
