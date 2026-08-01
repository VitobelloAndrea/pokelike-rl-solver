// MUST FAIL: unguarded DOM wiring inside a top-level IIFE.
(function () {
  document.addEventListener("pointerdown", function () {});
})();
