// MUST FAIL: dotted access off a global root. This form was already handled
// before M3.5 and is pinned here so the computed-access repair cannot regress
// it -- the two spellings must stay symmetric in both directions.
(function () {
  globalThis.localStorage.getItem("x");
})();
