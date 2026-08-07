# Pokelike Python Port

This repository is an experimental, deterministic Python port of a captured
Pokelike browser-game build. Its main purposes are:

- studying and documenting the behavior of the JavaScript game;
- reproducing selected game and battle rules in a testable Python engine;
- checking the port against the captured JavaScript with fixed-seed
  equivalence fixtures; and
- providing groundwork for console, local web, and reinforcement-learning
  experiments.

This is not a complete or authoritative recreation of Pokelike. Story and
Nuzlocke behavior across Generations 1–4 is only partially implemented, parity
coverage is deliberately narrow, and several modes and pieces of content are
still absent or approximate. Do not use the Python engine as a general oracle
for claims about the original game beyond behavior covered by the JavaScript
comparison fixtures.


## Repository layout

- `pokelike/` — deterministic Python engine, battle logic, data, tests,
  console renderer, and local web UI.
- `pokelike_forked/` — captured browser build and deobfuscated JavaScript used
  as the local source of truth for parity work.
- `tools/battle-oracle/` — fixed-seed JavaScript-versus-Python battle
  comparison harness and fixtures.
- `tools/extract-data/` — JavaScript data-extraction tooling.
- `tools/deobfuscate/` — helper used to decode the captured bundle.
- `tools/fetch-sprites/` — sprite-fetching utility.

## Requirements

- Python 3.11 or newer. The runtime uses only the Python standard library.
- Node.js and npm for the extraction tools and JavaScript battle oracle.

Commands below assume the repository root is the current directory.

## Run the Python port

Run an automatic console episode:

```console
python -m pokelike.render.play --seed 42 --gen3
```

Play interactively:

```console
python -m pokelike.render.play --interactive
```

Start the local web UI:

```console
python -m pokelike.webui.server
```

Then open <http://127.0.0.1:8000/>. The server has no authentication or HTTPS
and is intended only for local, single-user use. Do not expose it directly to
an untrusted network.

## Tests

Run the Python test suite:

```console
python -m unittest discover -s .
```

The cross-language battle oracle additionally needs the extraction tool's
Node dependency:

```console
npm ci --prefix tools/extract-data
python tools/battle-oracle/compare.py --all
```

The oracle checks that its executable JavaScript prefix is fresh relative to
`pokelike_forked/js/bundle.deobfuscated.js`, runs the real captured
`runBattle`, and compares selected final state, rounds, and RNG behavior with
the Python port.

## Development approach

Treat `pokelike_forked/js/bundle.deobfuscated.js` as the source of truth for
porting work. For a parity change:

1. isolate the behavior in a fixed-seed oracle fixture;
2. verify that the fixture executes the real JavaScript and fails against the
   incorrect Python behavior;
3. make the narrow Python correction;
4. run the fixture, the full oracle, and the Python tests; and
5. document only the behavior actually demonstrated.

Passing unit tests or a small fixture set does not establish complete parity.

## Project status and contributions

The project is suitable for source study, deterministic experiments, and
incremental porting. It is not yet a complete game, production service, or
validated reinforcement-learning environment.

Contributions should keep changes narrowly scoped, preserve deterministic RNG
behavior, and include source citations and regression coverage where
practical.

## Legal

This is an independent research project and is not affiliated with or endorsed
by Pokelike, Nintendo, Game Freak, or The Pokémon Company. Pokémon and related
names and assets belong to their respective owners.

No license has been added to this repository. Do not assume permission to
redistribute or reuse its contents until the repository owner adds an
appropriate license and confirms the status of captured third-party code and
assets.
