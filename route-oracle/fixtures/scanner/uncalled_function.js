// MUST PASS: a function that is declared but never invoked at load is not
// load-reachable; the oracle controls whether it is ever called.
function wireUp() {
  document.addEventListener("click", function () {});
  fetch("https://example.invalid/x");
}
