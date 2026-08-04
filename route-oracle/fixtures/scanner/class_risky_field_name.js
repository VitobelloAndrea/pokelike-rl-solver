// MUST PASS: a class field NAME is a property name, not a read of the host
// global it happens to spell -- for instance and static fields alike. The
// adversarial twin of class_computed_field_key.js, where the same syntactic slot
// holds an expression instead of a name.
class Holder {
  fetch = 1;

  static document;

  localStorage = null;
}
