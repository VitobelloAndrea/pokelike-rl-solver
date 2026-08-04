// MUST FAIL: a static initialisation block runs at class evaluation, exactly as
// a static field does. Kept separate from class_static_field.js because a
// StaticBlock is a different node type and can regress on its own.
class Immediate {
  static {
    setTimeout(function () {}, 0);
  }
}
