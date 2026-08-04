// MUST FAIL: the write direction of accessor_pair_read_runs_getter_only.js.
// Assigning invokes the setter and only the setter, so this file must report
// `document`, never `fetch`. Preserving one half while replacing the other is
// the descriptor rule a same-kind-only resolver cannot express.
({
  get value() {
    return fetch("https://evil.example/getter-half");
  },
  set value(v) {
    document.title = v;
  },
}).value = 2;
