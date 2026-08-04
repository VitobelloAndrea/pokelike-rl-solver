// MUST PASS: the non-shorthand key of an object property is a name too, and
// stays a name whatever the value is. Kept separate from the method-definition
// form (object_method_key.js) because acorn models them as different `Property`
// shapes, so one can regress without the other.
//
// The distinction that matters is with shorthand_risky_reference.js: dropping
// the `shorthand`/`computed` distinction and skipping every key would make this
// fixture pass for the wrong reason and let that one through.
const emitter = { addEventListener: localHandler, setTimeout: localScheduler };

emitter.addEventListener("ready", () => {});
