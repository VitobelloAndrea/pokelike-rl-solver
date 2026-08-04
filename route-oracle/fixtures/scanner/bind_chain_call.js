// MUST FAIL: `.bind(…)` chained, then invoked through `.call`. Peeling the
// binds has to happen before the `.call`/`.apply` recognition, or this reaches
// neither detector.
(function () {
  localStorage.getItem("x");
}).bind(this).bind(null).call(null);
