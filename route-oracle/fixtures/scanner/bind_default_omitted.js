// MUST FAIL: `.bind(receiver)` fixes only the receiver, so the parameter is
// still missing and its default runs. The adversarial twin of
// bind_default_supplied.js: an implementation that treated any bind as
// "arguments were supplied" would pass that one and silence this.
(function (value = fetch("https://evil.example/default")) {
  return value;
}).bind(null)();
