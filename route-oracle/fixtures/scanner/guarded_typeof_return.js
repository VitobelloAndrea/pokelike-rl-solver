// MUST PASS: dominated by a real `typeof ... === "undefined"` early return.
(function () {
  if (typeof localStorage === "undefined") return;
  localStorage.setItem("k", "v");
  localStorage.removeItem("k");
})();
