// MUST PASS: a later DATA property replaces the whole accessor descriptor, so
// the earlier getter is gone before anything reads it. M3.10 defect 1 -- the
// resolver applied "a later definition wins" only WITHIN one accessor kind, so
// this data property never cleared the getter and the file was reported as a
// load-time fetch that real JavaScript never performs (`.value` is 1).
({
  get value() {
    return fetch("https://evil.example/never");
  },
  value: 1,
}).value;
