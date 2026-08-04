// MUST FAIL: the same property reached by its string spelling. A numeric
// definition key and a string access must canonicalise to one key.
({
  get 0() {
    return fetch("https://evil.example/numeric-string");
  },
})["0"];
