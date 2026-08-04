// MUST PASS: `delete` removes a property descriptor. It invokes NEITHER
// accessor half, so an accessor's body is not load-reachable through it.
// M3.10 defect 2 -- `walk` modelled only read/write/readwrite, so a `delete`
// fell through the generic child walk and reached its member as an ordinary
// READ, reporting a fetch that real JavaScript never performs (verified in
// node: the getter does not run and `delete` returns true).
//
// The independently-moving twin of accessor_getter_read.js: identical literal
// and identical property, and the only difference is `delete` instead of a read.
delete ({
  get value() {
    return fetch("https://evil.example/never");
  },
}).value;
