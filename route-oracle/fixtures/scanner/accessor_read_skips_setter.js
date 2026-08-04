// MUST PASS: READING a property runs the getter, never the setter. There is no
// getter here, so nothing runs and the read yields undefined.
//
// The adversarial twin of accessor_setter_write.js: identical literal, and the
// only difference is that the property is read instead of assigned. A repair
// that walked every accessor body on every access would silence that
// distinction and fail here.
({
  set value(v) {
    fetch(v);
  },
}).value;
