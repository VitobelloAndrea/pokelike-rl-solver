// MUST PASS: `null` is not `undefined`, so it does NOT trigger the default.
// Kept separate from iife_default_supplied.js because a classifier that treated
// "falsy" or "nullish" as absent would pass that fixture and fail this one.
(function (value = fetch("https://evil.example/default")) {
  return value;
})(null);
