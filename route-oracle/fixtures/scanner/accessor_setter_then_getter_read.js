// MUST FAIL: the read direction of accessor_setter_then_getter_write.js. The
// later getter is the live read half, so a read invokes it and must report
// `fetch`, never `document`. The two files share one literal and differ only in
// the touch, so they must move independently.
({
  set value(v) {
    document.title = v;
  },
  get value() {
    return fetch("https://evil.example/getter-half");
  },
}).value;
