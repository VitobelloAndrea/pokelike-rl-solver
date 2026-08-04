// MUST PASS: an ordinary METHOD establishes a data descriptor exactly as a
// plain value does, so it clears the earlier getter. Pinned separately from
// accessor_data_shadows_getter.js because a method is still a Property whose
// value is a function -- a resolver that cleared the accessor only for
// non-function values would pass there and fail here.
({
  get value() {
    return fetch("https://evil.example/never");
  },
  value() {
    return 1;
  },
}).value;
