// MUST FAIL: `try { … } catch {}` swallows the ReferenceError, but it does not
// make the access inert -- on a runtime where the global EXISTS the side effect
// happens in full. This is the real bundle's own shape at prefix lines
// 47541/47548, which an earlier scanner's success message wrongly described as
// `typeof`-guarded. An exception boundary is an audited exception: it has to be
// declared in the allow-list with a reason, never inferred.
(function () {
  try {
    localStorage.setItem("swallowed", "1");
  } catch (e) {}
})();
