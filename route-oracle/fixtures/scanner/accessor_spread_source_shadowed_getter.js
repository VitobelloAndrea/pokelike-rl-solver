// MUST PASS: CopyDataProperties reads the FINAL descriptor on its source. The
// later data property replaced this getter before the spread enumerates the
// source, so no getter runs.
//
// This prevents the spread-source repair from walking every getter definition
// without applying the same source-order descriptor fold used by direct reads.
({
  ...{
    get value() {
      return fetch("https://evil.example/never");
    },
    value: 1,
  },
});
