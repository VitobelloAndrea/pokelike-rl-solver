// MUST PASS: touching a DIFFERENT property does not invoke this accessor.
// Pinned so that "the object literal defines an accessor somewhere" cannot
// become the trigger; the resolved property NAME has to match.
({
  get value() {
    return fetch("https://evil.example/never");
  },
}).other;
