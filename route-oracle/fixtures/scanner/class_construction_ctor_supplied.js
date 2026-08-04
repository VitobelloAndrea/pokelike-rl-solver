// MUST PASS: the same constructor default, with an argument that proves it
// dead. The MUST PASS twin of class_construction_ctor_default.js -- the
// suppression rule has to reach the class path too, not only the IIFE path.
new (class {
  constructor(value = fetch("https://evil.example/ctor-default")) {
    this.value = value;
  }
})(1);
