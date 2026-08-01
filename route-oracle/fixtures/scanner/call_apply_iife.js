// MUST FAIL: the obfuscator's `.call`/`.apply` IIFE forms are still IIFEs.
(function () {
  localStorage.getItem("x");
}).call(this);
(function () {
  fetch("https://evil.example/a");
}).apply(this, []);
