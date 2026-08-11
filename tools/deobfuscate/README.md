# deobfuscate

`pokelike_forked/js/bundle.*.js` is a single ~2.5MB bundle run through
javascript-obfuscator.io's string-array encoding (see [docs/logic-notes.md](../../docs/logic-notes.md)
for full context on why the mirror is one file instead of the separate
`battle.js`/`map.js`/etc. the project originally assumed).

`decode-strings.js` reverses just the string-array encoding — it does not
rename minified variables (`Bm9`, `B2O`, `iu`, ...) and does not unflatten
control flow, it only resolves `alias(0xHEX)` calls back to their literal
string values. That alone is enough to grep/read the source meaningfully.

## Usage

```
node decode-strings.js <input-bundle.js> <output-file.js>
npx prettier --write <output-file.js>   # optional, but much easier to read
```

Regenerate `pokelike_forked/js/bundle.deobfuscated.js` from a newer
`bundle.<hash>.js` with:

```
node tools/deobfuscate/decode-strings.js pokelike_forked/js/bundle.<newhash>.js pokelike_forked/js/bundle.deobfuscated.js
```

## The one bug worth knowing about

The decoder alias is not always a single `const X = k;` — it's a **chain**
(`const Bm9 = k;` at file scope, then `const Bn0 = Bm9;` inside some deeply
nested function, etc.). An early version of this script only collected
direct `= k` aliases and silently left ~19,000 calls un-decoded — no error,
because looking up *some* wrong string array index never throws, it just
returns a plausible-looking wrong value. The script now computes the
transitive closure of alias assignments before substituting; if you ever see
suspiciously-generic hex placeholders survive a re-run (or values that don't
quite make sense in context), suspect this class of bug first and check
`aliases.size` in the script's output — it should be in the thousands, not
tens.
