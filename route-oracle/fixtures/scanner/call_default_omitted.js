// MUST FAIL: `.call(receiver)` supplies a RECEIVER, not an argument, so the
// parameter is still missing and its default still runs. An implementation that
// forgot to drop argument 0 would count the receiver as the first parameter and
// silence this.
(function (value = fetch("https://evil.example/default")) {
  return value;
}).call(null);
