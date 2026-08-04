// MUST PASS: the case that separates a real descriptor fold from a pair of
// independent per-kind searches. The data property clears BOTH halves; the
// later setter then establishes an accessor descriptor whose getter half starts
// ABSENT. So a read invokes nothing at all -- verified against node, where
// `.value` is undefined and neither body runs.
//
// A resolver that remembered the getter across the intervening data property
// would report `fetch` here.
({
  get value() {
    return fetch("https://evil.example/never");
  },
  value: 1,
  set value(v) {
    document.title = v;
  },
}).value;
