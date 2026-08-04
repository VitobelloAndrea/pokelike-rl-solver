// MUST PASS: the later getter replaces the earlier getter of the same name.
// The live getter is inert, so the earlier risky body must not be walked.
//
// M3.12's "first getter wins" mutation survived the 91-fixture corpus because
// no fixture repeated one accessor kind.
({
  get value() {
    return fetch("https://evil.example/never");
  },
  get value() {
    return 1;
  },
}).value;
