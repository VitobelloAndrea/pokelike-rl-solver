// MUST FAIL: a definition placed AFTER a spread wins outright, so no
// conservatism is involved here -- the getter is exactly the live half and the
// read really does invoke it. Pinned beside accessor_spread_after_accessor.js
// so the spread policy cannot collapse into "ignore any literal containing a
// spread", which would blind this case.
const source = {};
({
  ...source,
  get value() {
    return fetch("https://evil.example/definitely-live");
  },
}).value;
