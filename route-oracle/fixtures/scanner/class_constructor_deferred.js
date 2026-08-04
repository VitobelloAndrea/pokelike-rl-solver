// MUST PASS: a constructor body is a method body -- it runs on `new`, and
// nothing here constructs anything. The MUST PASS twin of
// class_direct_construction_ctor.js, which is the same constructor with a `new`
// in front of it.
class Deferred {
  constructor() {
    fetch("https://evil.example/never");
  }
}
