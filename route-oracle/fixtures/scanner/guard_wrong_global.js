// MUST FAIL: the guard names a DIFFERENT global than the risky call. Proving
// `navigator` is absent says nothing about `fetch`. A guard analysis that
// matched "is there a typeof test nearby" rather than "does it name THIS
// identifier" would wave this through.
(function () {
  if (typeof navigator === "undefined") return;
  fetch("https://evil.example/wrong-guard");
})();
