// MUST FAIL: ToPropertyKey on a BigInt produces its decimal string. `1n` and
// the property name "1" therefore identify the same direct accessor.
({
  get 1n() {
    return fetch("https://evil.example/bigint-key");
  },
})[1n];
