// MUST FAIL: the DOCUMENTED CONSERVATIVE spread policy, over-report direction.
// A spread whose runtime value is unknown may carry `value` and replace this
// getter with a data property, or may not carry it at all and leave the getter
// live. The descriptor is therefore unresolvable, and a safety gate must assume
// the accessor still runs rather than claim certainty that it was replaced.
//
// This is deliberately conservative, not exact: object spread is
// CopyDataProperties, which can only ever DEFINE data properties, so it can
// clear an accessor but never introduce one this fold cannot see. Skipping it
// can over-report and can never blind the gate.
const source = {};
({
  get value() {
    return fetch("https://evil.example/maybe-replaced");
  },
  ...source,
}).value;
