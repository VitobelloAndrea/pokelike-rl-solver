// MUST PASS: defining a getter does not invoke it. The accessor body is only
// load-reachable if something touches the property at load, and nothing here
// does -- the literal is stored in a binding and never read.
//
// The adversarial twin of accessor_getter_read.js, which is the same literal
// with `.value` appended.
const holder = {
  get value() {
    return fetch("https://evil.example/not-read");
  },
};

module.exports = holder;
