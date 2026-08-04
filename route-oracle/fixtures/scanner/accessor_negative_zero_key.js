// MUST FAIL: ToString(-0) is "0", so a `get 0()` accessor is invoked by a
// computed `[-0]` access. The unary spelling must not fall out of static key
// resolution.
({
  get 0() {
    return fetch("https://evil.example/negative-zero-key");
  },
})[-0];
