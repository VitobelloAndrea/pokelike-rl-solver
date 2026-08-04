// MUST FAIL: the write direction of accessor_data_between_accessors_read.js.
// The trailing setter is the live half of the descriptor, so an assignment does
// invoke it. Same three definitions, same order, only the touch differs -- the
// two files must move independently.
({
  get value() {
    return fetch("https://evil.example/never");
  },
  value: 1,
  set value(v) {
    document.title = v;
  },
}).value = 2;
