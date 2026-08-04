// MUST FAIL: the existing host-global policy is unchanged by the new `delete`
// mode. `delete window.fetch` still NAMES the risky global in a reference
// position off an explicit global root, so it stays exactly as visible as it
// was before `delete` was modelled at all.
delete window.fetch;
