// MUST FAIL: the setter direction of accessor_getter_shadows_data.js. A setter
// defined after a data property of the same name replaces it, so the assignment
// invokes it.
({
  value: 1,
  set value(v) {
    fetch("https://evil.example/setter-wins");
  },
}).value = 2;
