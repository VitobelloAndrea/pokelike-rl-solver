// MUST PASS: the setter direction of delete_accessor_getter.js. `delete` does
// not invoke a setter either -- it is not an assignment.
delete ({
  set value(v) {
    fetch("https://evil.example/never");
  },
}).value;
