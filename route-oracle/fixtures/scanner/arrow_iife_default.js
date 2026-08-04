// MUST FAIL: an arrow IIFE has parameter defaults too. arrow_iife.js pins the
// BODY of an arrow IIFE; this pins its PARAMETERS, which reach the walk through
// a different path.
((title = document.title) => {
  return title;
})();
