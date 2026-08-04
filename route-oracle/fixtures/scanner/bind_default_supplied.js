// MUST PASS: `.bind(receiver, 1)` fixes the first ARGUMENT, so the parameter is
// supplied by the time the bound function is invoked and its default is dead.
// The bind chain has to contribute its arguments to the position mapping for
// this to be provable -- and bind_default_omitted.js is the twin that fails when
// it contributes none.
(function (value = fetch("https://evil.example/default")) {
  return value;
}).bind(null, 1)();
