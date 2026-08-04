// MUST FAIL: `.call(…)` alone. call_apply_iife.js exercises both spellings
// together; this one exists so that deleting `.call` recognition specifically
// flips a fixture instead of hiding behind the surviving `.apply` branch.
(function () {
  localStorage.getItem("x");
}).call(this);
