// MUST PASS: a plain assignment WRITES, so it does not invoke the getter. The
// mirror of accessor_read_skips_setter.js, and the twin of
// accessor_getter_read.js -- same literal, assigned instead of read.
({
  get value() {
    return fetch("https://evil.example/never");
  },
}).value = 1;
