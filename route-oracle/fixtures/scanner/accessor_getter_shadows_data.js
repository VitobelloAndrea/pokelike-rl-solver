// MUST FAIL: the opposite order, which the M3.9 resolver already handled and
// which the descriptor fold must not lose. A getter defined AFTER a data
// property of the same name replaces it, so the read really does invoke it.
//
// The independently-moving twin of accessor_data_shadows_getter.js: same two
// definitions, opposite source order, opposite verdict. A repair that made the
// data property win regardless of position would pass there and blind here.
({
  value: 1,
  get value() {
    return fetch("https://evil.example/getter-wins");
  },
}).value;
