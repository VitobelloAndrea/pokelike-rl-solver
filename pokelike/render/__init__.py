"""Local UI layer -- a thin viewer over `pokelike.engine`'s `RunState`.

Per CLAUDE.md: "the basic image to simulate the main modes" local UI,
starting simple (text/console) and polished later; `js/ui.js` is reference-
only and does NOT influence this package's data shapes or design.
`pokelike.engine` has no dependency on this package (or vice versa beyond
reading `RunState`'s public fields) -- keeping the state machine UI-agnostic
per CLAUDE.md's Phase 3 note.
"""
