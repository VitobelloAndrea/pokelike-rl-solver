// MUST PASS: `.apply(receiver, [1])` maps the literal array onto parameter
// positions, so the default is provably dead. The MUST PASS side of the
// `.apply` mapping; call_default_omitted.js is the MUST FAIL side of `.call`.
(function (value = fetch("https://evil.example/default")) {
  return value;
}).apply(null, [1]);
