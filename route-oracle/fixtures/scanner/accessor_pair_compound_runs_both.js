// MUST FAIL: a compound assignment reads AND writes, so when both halves
// survive it invokes both -- getter first, then setter. Distinct globals pin
// the ordering as well as the count: this file must report `fetch` and
// `document`, in that order.
({
  get value() {
    return fetch("https://evil.example/getter-half");
  },
  set value(v) {
    document.title = v;
  },
}).value += 1;
