// MUST FAIL: a computed property key is an EXPRESSION that is evaluated, so
// `{ [setTimeout]: handler }` reads the global exactly as `setTimeout` alone
// would. The second statement is the same trap inside a destructuring pattern,
// where the surrounding syntax binds names but the key still evaluates.
//
// The adversarial twin of object_method_key.js / class_method_key.js: a repair
// that excluded property keys wholesale, without testing `computed`, would make
// both of these vanish.
const aliases = { [setTimeout]: handler };

const { [fetch]: pulled } = registry;

module.exports = [aliases, pulled];
