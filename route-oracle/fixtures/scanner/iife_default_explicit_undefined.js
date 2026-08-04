// MUST FAIL: passing `undefined` explicitly triggers the default exactly as
// omitting the argument does. An implementation that only counted ARITY --
// "an argument was supplied, so the default is dead" -- would wave this through.
(function (value = fetch("https://evil.example/default")) {
  return value;
})(undefined);
