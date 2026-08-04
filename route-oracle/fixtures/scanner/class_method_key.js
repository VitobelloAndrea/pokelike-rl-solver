// MUST PASS: a class method name is a property name, and the class's own
// binding name is a binding. Neither is a read of a host global. Separate from
// object_method_key.js because a `MethodDefinition` inside a `ClassBody` is a
// different node type from a `Property` inside an `ObjectExpression`.
//
// Note what is NOT claimed here: a class body is still walked at the enclosing
// reach, because a static block or a field initialiser really does run when the
// class is evaluated. Only the NAMES are excluded.
class Emitter {
  addEventListener() {}

  removeEventListener() {}

  static dispatchEvent() {}
}

const emitter = new Emitter();
emitter.addEventListener("ready", () => {});
