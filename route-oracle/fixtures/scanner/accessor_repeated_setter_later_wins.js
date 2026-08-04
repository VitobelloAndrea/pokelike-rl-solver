// MUST PASS: the later setter replaces the earlier setter of the same name.
// Assigning invokes only the inert later body.
//
// M3.12's matching "first setter wins" mutation also survived until this
// source-order case existed.
({
  set value(v) {
    fetch("https://evil.example/never");
  },
  set value(v) {
    this.saved = v;
  },
}).value = 2;
