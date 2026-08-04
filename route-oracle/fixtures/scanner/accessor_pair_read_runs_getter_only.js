// MUST FAIL: when BOTH halves of an accessor descriptor survive, a read invokes
// the getter and only the getter. The two bodies name DIFFERENT globals so the
// report proves which half ran: this file must report `fetch`, never `document`.
//
// The independently-moving twin of accessor_pair_write_runs_setter_only.js:
// identical literal, read instead of write, and the opposite half must fire.
({
  get value() {
    return fetch("https://evil.example/getter-half");
  },
  set value(v) {
    document.title = v;
  },
}).value;
