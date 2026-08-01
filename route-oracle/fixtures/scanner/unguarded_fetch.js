// MUST FAIL: an unguarded network call inside a top-level IIFE.
(function () {
  fetch("https://evil.example/telemetry");
})();
