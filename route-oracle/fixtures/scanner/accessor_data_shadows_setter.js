// MUST PASS: the setter direction of accessor_data_shadows_getter.js. A later
// data property clears the setter half too, so assigning stores a value instead
// of invoking anything. M3.10 defect 1 reported this as a load-time fetch.
({
  set value(v) {
    fetch("https://evil.example/never");
  },
  value: 1,
}).value = 2;
