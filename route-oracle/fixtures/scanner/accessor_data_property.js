// MUST PASS: an ordinary data property has no body to run. Reading it is not an
// invocation of anything, so the file is inert.
//
// The adversarial twin of accessor_getter_read.js: identical access, and the
// only difference is that the property is a value rather than an accessor.
({ value: 1 }).value;
