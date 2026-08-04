// MUST FAIL: the destructuring half of computed_key_reference.js, ISOLATED.
// `const { [fetch]: pulled } = registry` binds a NAME, but the computed key is
// still an expression that is evaluated, so the global is read.
//
// The independently-moving twin of computed_object_key_reference.js: the two
// forms live in different walk paths and must each be able to regress alone.
const registry = {};

const { [fetch]: pulled } = registry;

module.exports = pulled;
