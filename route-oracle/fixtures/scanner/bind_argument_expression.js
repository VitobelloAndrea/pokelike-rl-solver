// MUST FAIL: the arguments a `.bind(…)` fixes are EXPRESSIONS, and they are
// evaluated at load in the context of the CALLER. The pre-M3.9 bind path
// returned only the underlying function and discarded every bind argument, so
// this scored zero references -- a latent hole in the `.bind` support M3.5
// added, closed as a side effect of mapping arguments onto parameters.
//
// Note the reach: `fetch` here runs at `top`, in the caller, not inside the
// invoked body at `iife`.
(function (value) {
  return value;
}).bind(null, fetch("https://evil.example/bound-arg"))();
