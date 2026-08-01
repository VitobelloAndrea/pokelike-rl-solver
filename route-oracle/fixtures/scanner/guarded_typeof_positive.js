// MUST PASS: the mirror idiom -- only runs when the global IS defined.
(function () {
  if (typeof fetch !== "undefined") {
    fetch("https://example.invalid/x");
  }
  typeof document !== "undefined" && document.addEventListener("click", function () {});
})();
