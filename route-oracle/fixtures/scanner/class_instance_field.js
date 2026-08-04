// MUST PASS: defining a class does NOT run an instance field initialiser --
// that happens once per `new`, and nothing here constructs a Deferred. M3.8
// defect 2: class bodies kept load-time reach and every PropertyDefinition
// initialiser was walked without checking `static`, so this was reported as a
// load-time fetch.
//
// Three adversarial twins pin the other side: class_static_field.js (a static
// field DOES run), class_direct_construction.js (a directly constructed class
// DOES run its instance fields) and class_computed_field_key.js (a computed key
// runs even when the initialiser is deferred).
class Deferred {
  value = fetch("https://evil.example/later");
}
