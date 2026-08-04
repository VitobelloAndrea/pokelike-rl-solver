// MUST FAIL: a spread destroys the position mapping -- nothing can be said
// about which parameter receives what -- so every default is assumed to run.
// Separate from iife_default_unprovable.js because the two are different
// branches: one classifies a single argument, the other abandons the mapping.
(function (value = fetch("https://evil.example/default")) {
  return value;
})(...supplied);
