// MUST FAIL: a computed field KEY is evaluated while the class is being
// defined, even though the instance initialiser it names is deferred. The two
// halves of the same member have different reach, which is the distinction a
// blanket "class bodies are deferred" repair would destroy.
//
// The MUST PASS twin is class_risky_field_name.js: a non-computed field name is
// just a name.
class Named {
  [fetch("https://evil.example/key")] = 1;
}
