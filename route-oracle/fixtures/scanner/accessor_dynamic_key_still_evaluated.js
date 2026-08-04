// MUST FAIL: a genuinely dynamic computed key stays OUTSIDE static resolution
// -- no accessor is resolved against it -- but the key expression itself is
// still evaluated at load, so a risky call inside it is still reported. The
// accessor body is inert, so the only thing this file can report is the key.
//
// Pins the boundary of the M3.11 key rule: canonicalisation covers static
// string and finite numeric literals only, and everything else falls back to
// the conservative path rather than to silence.
({
  get value() {
    return 1;
  },
})[fetch("https://evil.example/dynamic-key")];
