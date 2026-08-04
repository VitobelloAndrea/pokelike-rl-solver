// MUST PASS: a declaration identifier is the name being BOUND, not a read of
// the global it shadows. Covers the three binding forms the walk has to tell
// apart from references: a `const` declarator id, a destructured binding name,
// and a function declaration's own name and parameters.
const setTimeout = localScheduler;

const { fetch, addEventListener: onEvent } = localHost;

function document(navigator) {
  return [navigator, fetch, onEvent, setTimeout];
}
