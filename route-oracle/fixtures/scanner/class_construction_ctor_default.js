// MUST FAIL: constructing an anonymous class directly evaluates its constructor
// parameter defaults, exactly as invoking an IIFE does. The `new` supplies no
// argument, so the default runs.
//
// This is the one place the invoked-parameter analysis is reached through the
// class path rather than the IIFE path, so it can regress on its own.
new (class {
  constructor(value = fetch("https://evil.example/ctor-default")) {
    this.value = value;
  }
})();
