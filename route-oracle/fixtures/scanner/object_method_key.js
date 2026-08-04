// MUST PASS: an object-literal method key is a property NAME, not a read of
// the host global. M3.6's defect: the walk flagged every `Identifier` token,
// so line 1 below was reported as an unguarded `addEventListener` even though
// nothing here references or invokes a global scheduler. The call on the last
// line is already handled correctly -- `emitter.addEventListener` is a member
// of a local object, not of an explicit global root -- so the declaration and
// its use must agree.
const emitter = { addEventListener() {} };

emitter.addEventListener("ready", () => {});
