// MUST FAIL: nesting an IIFE inside an IIFE does not make it unreachable.
(function () {
  (function () {
    document.addEventListener("click", function () {});
  })();
})();
