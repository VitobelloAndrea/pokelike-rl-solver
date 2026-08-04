// MUST FAIL: a no-substitution template literal is a statically known string
// on both the definition and access sides. Both spellings below name "value".
({
  get [`value`]() {
    return fetch("https://evil.example/template-key");
  },
})[`value`];
