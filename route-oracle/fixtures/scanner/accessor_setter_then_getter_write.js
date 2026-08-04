// MUST FAIL: adding a GETTER to an existing accessor descriptor must PRESERVE
// its setter. The setter is defined first here, so after the later getter joins
// the descriptor both halves are live and an assignment still invokes the
// setter -- this file must report `document`, never `fetch`.
//
// The mirror image of accessor_pair_write_runs_setter_only.js, whose definitions
// run getter-then-setter. Only this order can detect a fold that clears the
// setter when a getter is added: in the other order the setter is written last
// and survives such a bug by accident. Found by an M3.11 mutation that survived
// the corpus until this fixture existed.
({
  set value(v) {
    document.title = v;
  },
  get value() {
    return fetch("https://evil.example/getter-half");
  },
}).value = 2;
