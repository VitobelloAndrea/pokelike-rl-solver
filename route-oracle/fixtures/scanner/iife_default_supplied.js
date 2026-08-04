// MUST PASS: the argument is a literal, so it is provably not `undefined` and
// the default is dead code. Reporting it would be a false positive -- the exact
// failure mode that makes "walk every default" an unacceptable repair.
//
// The adversarial twin of iife_default_omitted.js.
(function (value = fetch("https://evil.example/default")) {
  return value;
})(1);
