// MUST PASS: the data property already cleared the getter, and a spread cannot
// bring it back -- object spread is CopyDataProperties, which uses
// CreateDataProperty and therefore defines only DATA properties on the target.
// Whatever `source` holds, `.value` is a data property when this reads it, so
// no body runs.
//
// The independently-moving twin of accessor_spread_after_accessor.js: the same
// trailing spread, and the verdict turns entirely on whether the descriptor was
// already a data descriptor when the spread was reached.
const source = {};
({
  get value() {
    return fetch("https://evil.example/never");
  },
  value: 1,
  ...source,
}).value;
