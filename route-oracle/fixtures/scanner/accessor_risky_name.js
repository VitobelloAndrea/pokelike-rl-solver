// MUST PASS: an accessor NAME is a property name, and a non-computed one is
// never a read of the host global it spells. The body is inert too, because
// nothing touches the property.
//
// Two independent rules have to hold for this to pass -- the M3.7
// reference-position rule for the key, and the M3.9 rule that only a directly
// evaluated accessor is reachable -- so it is deliberately narrow: an inert
// body, so the verdict turns on the name alone.
({
  get document() {
    return 1;
  },
  set localStorage(v) {},
});
