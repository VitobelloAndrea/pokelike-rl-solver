// MUST PASS: canonicalising numeric keys must not make every numeric access
// match every numeric accessor. `[1]` is not `"0"`, so this accessor is not the
// one being touched and nothing runs.
//
// The independently-moving twin of accessor_numeric_key_numeric_access.js: a
// repair that resolved any numeric access to any numeric accessor -- or that
// compared keys loosely enough for `0` and `1` to collide -- would pass there
// and produce a false positive here.
({
  get 0() {
    return fetch("https://evil.example/never");
  },
})[1];
