// MUST PASS: spreading a setter-only property performs a Get, which yields
// undefined; it does not invoke the setter. Only a live getter body is
// load-reachable through CopyDataProperties.
({
  ...{
    set value(v) {
      fetch("https://evil.example/never");
    },
  },
});
