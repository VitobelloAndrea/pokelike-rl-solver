// MUST FAIL: a shorthand property READS the identifier. `{ setTimeout }` is
// `{ setTimeout: setTimeout }`, so the global is captured and can be invoked
// later from anywhere -- it is a reference, not a declaration-only key.
//
// This is the adversarial twin of object_property_key.js. acorn emits the
// shorthand key and value as two distinct `Identifier` nodes at the SAME source
// offset; the walk skips only the key and always walks the value, so this scores
// exactly one hit, at the right line, and a "skip every non-computed key" repair
// would silently let it through.
const aliases = { setTimeout, addEventListener };

module.exports = aliases;
