// MUST FAIL: an unguarded localStorage write inside a top-level IIFE.
(function () {
  localStorage.setItem("k", "v");
})();
