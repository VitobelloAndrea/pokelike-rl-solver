// MUST FAIL: the object-literal half of computed_key_reference.js, ISOLATED.
// `{ [setTimeout]: handler }` evaluates the key, so the global is read.
//
// The original fixture bundles this form with a destructuring-pattern computed
// key, and the two are handled by different code paths -- the Property branch
// and `walkPattern`. An M3.11 mutation that disabled only the Property branch
// therefore survived: the pattern half kept the bundled fixture failing and hid
// the regression. One form per fixture is what makes the mutation observable.
const handler = 1;

const aliases = { [setTimeout]: handler };

module.exports = aliases;
