"""Port of the game's RNG (docs/logic-notes.md section 6).

The bundle uses **two non-interchangeable Mulberry32-family streams**, not
one. Both share the exact same per-call update step, but differ in how the
seed is turned into initial generator state:

- Stream A, ``seeded_rng(seed)`` (JS ``seededRng``,
  bundle.deobfuscated.js:59917): an ephemeral, self-contained generator.
  The seed is XOR-mixed with the golden-ratio constant ``0x9E3779B9`` before
  the first step. The only call site in the JS is ``rollRegion``
  (bundle.deobfuscated.js:60586), which builds a fresh one per call from
  ``stageNum * 1000 + regionIndex`` (line 60607) for Endless/Challenge
  region-archetype selection and World-Tournament trainer trait rolls. It
  never reads or writes Stream B's state.
- Stream B, ``rng()`` / ``seed_rng()`` / ``get_rng_seed()`` (JS
  bundle.deobfuscated.js:74818-74833): a module-level global singleton with
  NO seed mixing (``seedRng`` sets the raw state directly). This is the
  stream used for essentially everything else: battle (crit rolls, damage
  variance), catching (Fisher-Yates shuffles), regular map/node generation,
  and endless/tournament type rolls.

Given the same integer seed, Stream A and Stream B produce **different**
sequences (confirmed by running both against the actual bundle code via
Node -- see the seed/sequence pairs in
pokelike/tests/test_rng.py). Do not unify them into one code path beyond
sharing the low-level Mulberry32 step function.

**What this module does NOT make reproducible.** Per docs/logic-notes.md
6.3-6.4, a handful of gameplay-affecting rolls bypass both streams entirely
via raw, non-seeded ``Math.random()`` in the JS, and have no representation
here:

- the legendary-egg shiny roll (``rollLegendaryEggShiny``, line 74959),
- egg hatching -- both species selection AND shininess (``hatchEgg``, line
  87509),
- a PC minigame's local puzzle seed (``pcReroll``, line 88906), and
- a mid-run reseed hook: the ``spacial_rend`` ability
  (``useSpacialRend``, line 84415) re-mixes fresh ``Math.random()`` entropy
  into the *running* Stream B state (``seedRng((getRngSeed() ^
  Math.floor(Math.random()*2**32)) >>> 0)``). It composes only out of this
  module's own ``seed_rng``/``get_rng_seed``, so it belongs in battle.py's
  trait/ability implementation rather than here -- but its existence means
  a run that uses this ability is **not** fully replayable from its initial
  seed alone. Any Phase 4 reproducibility/validation tooling must account
  for this rather than assuming a saved ``rngSeed`` fully determines a run.

**Phase 3 note (flagging, not resolving):** Stream B is a true process-wide
global in the JS (and is ported the same way here, for fidelity). A
vectorized/parallel Gym setup will want independent RNG state per
environment instance -- ``Mulberry32`` below is a plain class for exactly
that reason, but the module-level ``rng()``/``seed_rng()``/``get_rng_seed()``
functions faithfully mirror the JS's single shared instance. Revisit this
when Phase 3's env design is underway; not a decision to make speculatively
here.
"""

from __future__ import annotations

import secrets
import time
from typing import Callable, Mapping, TypeVar

_MASK32 = 0xFFFFFFFF
_GOLDEN_RATIO_XOR = 0x9E3779B9
_MULBERRY_INCREMENT = 0x6D2B79F5

K = TypeVar("K")


def _mulberry32_step(state: int) -> tuple[int, float]:
    """One Mulberry32 update: advance ``state`` and produce a float in
    [0, 1). Shared by both streams (docs/logic-notes.md 6.1) -- this is the
    part that is identical between them; only initial-state derivation
    differs.

    All intermediate values are kept as unsigned 32-bit ints (masked with
    ``_MASK32``) rather than mirroring JS's int32 ``Math.imul``/``|0``
    semantics directly: XOR/shift/OR bit patterns don't depend on signed
    vs. unsigned interpretation, and a mod-2**32 product is the same
    regardless of which operand is treated as signed, so the masked-unsigned
    arithmetic below reproduces the JS bit-for-bit (verified against the
    live bundle via Node -- see test_rng.py).
    """
    state = (state + _MULBERRY_INCREMENT) & _MASK32
    t1 = ((state ^ (state >> 15)) * (state | 1)) & _MASK32
    imul2 = ((t1 ^ (t1 >> 7)) * (t1 | 0x3D)) & _MASK32
    t2 = ((t1 + imul2) & _MASK32) ^ t1
    result = ((t2 ^ (t2 >> 14)) & _MASK32) / 4294967296.0
    return state, result


class Mulberry32:
    """A single Mulberry32 generator instance holding its own 32-bit state.

    Not exposed by name in the JS (which only has the two call patterns
    below) -- this class exists purely so ``seeded_rng`` and the Stream B
    singleton can share one implementation of the update step without
    duplicating it.
    """

    __slots__ = ("_state",)

    def __init__(self, initial_state: int) -> None:
        self._state = initial_state & _MASK32

    def __call__(self) -> float:
        self._state, result = _mulberry32_step(self._state)
        return result

    @property
    def state(self) -> int:
        return self._state

    def seed(self, value: int) -> None:
        self._state = value & _MASK32


def seeded_rng(seed: int) -> Callable[[], float]:
    """Port of JS ``seededRng(seed)`` (bundle.deobfuscated.js:59917-59923),
    Stream A.

    Mixes the seed with the golden-ratio constant before the first draw,
    then returns a callable that produces successive Mulberry32 floats in
    [0, 1). Each call to this factory is independent -- it never reads or
    writes the Stream B global below.

    The only real call site in the JS (``rollRegion``, line 60586-60607)
    constructs one of these per call from ``stage_num * 1000 +
    region_index``.
    """
    generator = Mulberry32((seed ^ _GOLDEN_RATIO_XOR) & _MASK32)
    return generator


_stream_b = Mulberry32(0)

# **Phase-3-motivated fidelity fix (CODEX.md issue 15).** The JS's Stream B
# really is one process-wide global forever -- faithfully mirrored above by
# `_stream_b`, a single module-level instance every function in this
# codebase reads through the three functions below. That is fine for the
# real single-page browser game, but it means two `engine.Engine` instances
# in the same Python process silently shared one RNG stream: resetting or
# stepping one engine advanced/reseeded the state every other engine's next
# `rng()` call would see, so interleaved engines (or a future vectorized Gym
# setup) could never be independently reproducible from their own seeds.
#
# `_active` is the stream every call below actually reads/writes --
# `_stream_b` by default, so every existing caller (tests that call
# `battle.py`/`map_gen.py` functions directly, no `Engine` involved) keeps
# working unchanged against the single default global exactly as before.
# `engine.Engine` owns a private `Mulberry32` per instance and swaps it in
# via `set_active_stream()` for the duration of its own `reset()`/`step()`
# calls (see `engine.py`), so each engine's battle/catch/map-gen RNG draws
# are isolated to that engine's own stream while it's the one running --
# Python's single-threaded, non-reentrant call model means two engines'
# `step()` bodies never actually interleave mid-call, so swapping a single
# "current" pointer around each top-level call is sufficient for real
# independence, without threading an RNG instance through every function
# signature in `battle.py`/`battle_loop.py`/`battle_abilities.py`/
# `battle_traits.py`/`map_gen.py` (a far larger, riskier rewrite this fix
# deliberately avoids per CLAUDE.md's "avoid broad rewrites" convention).
_active = _stream_b


def rng() -> float:
    """Port of the global JS ``rng()`` (bundle.deobfuscated.js:74819-74827),
    Stream B. Used for battle rolls, catching, map/node generation, and
    endless/tournament type rolls -- everything except the Stream A sites
    above. Shares its update step with ``seeded_rng`` but NOT its seed
    mixing: see module docstring. Reads whichever stream is currently
    "active" -- see ``set_active_stream()``.
    """
    return _active()


def seed_rng(seed: int) -> None:
    """Port of JS ``seedRng(seed)`` (bundle.deobfuscated.js:74828-74830).
    Sets the currently-active stream's raw state directly -- unlike
    ``seeded_rng``, there is NO golden-ratio mix here. This is what a saved
    run's ``rngSeed`` is restored through, and what a fresh run seeds via
    ``new_run_seed()`` below.
    """
    _active.seed(seed)


def get_rng_seed() -> int:
    """Port of JS ``getRngSeed()`` (bundle.deobfuscated.js:74831-74833).
    Returns the currently-active stream's raw state -- what gets persisted
    as ``rngSeed`` in a save (``saveRun()``, line 74983)."""
    return _active.state


def new_stream(seed: int = 0) -> Mulberry32:
    """Create a fresh, independent Stream-B-shaped ``Mulberry32`` instance
    (no golden-ratio mix, same update step) for a caller -- normally
    ``engine.Engine`` -- that wants its own private RNG state rather than
    sharing the module-level default. See ``set_active_stream()``.
    """
    return Mulberry32(seed)


def set_active_stream(stream: Mulberry32) -> Mulberry32:
    """Swap which ``Mulberry32`` instance ``rng()``/``seed_rng()``/
    ``get_rng_seed()`` read/write, returning the PREVIOUS active stream so
    the caller can restore it afterward (``engine.Engine`` does this around
    every ``reset()``/``step()`` call -- see that class's docstring).
    Defaults to the shared module-level ``_stream_b`` singleton when no
    ``Engine`` has activated its own stream, so every pre-existing caller of
    ``rng()`` (tests, `battle.py`/`map_gen.py` used directly) is unaffected.
    """
    global _active
    previous = _active
    _active = stream
    return previous


def get_active_stream() -> Mulberry32:
    """The ``Mulberry32`` instance ``rng()`` currently reads/writes."""
    return _active


def new_run_seed() -> int:
    """Port of the fresh-run seeding behavior in ``startNewRun`` (line
    75442-75457) and ``startEndlessRun`` (line 84106-84129), which use the
    identical expression: ``(Date.now() ^ ((Math.random()*2**32)|0)) >>> 0``.

    This mixes real wall-clock time with real (non-seeded, non-reproducible)
    entropy to produce a fresh Stream B seed for a brand-new run. It is
    deliberately NOT deterministic -- pass an explicit integer straight to
    ``seed_rng()`` instead when a reproducible/test run is wanted, which is
    exactly how the JS distinguishes "new run" (this function) from
    "resumed/shared run" (``seed_rng`` alone).
    """
    now_ms = int(time.time() * 1000) & _MASK32
    entropy = secrets.randbits(32)
    return (now_ms ^ entropy) & _MASK32


def weighted_random(bag: Mapping[K, float]) -> K:
    """Port of JS ``weightedRandom(bag)`` (bundle.deobfuscated.js:53206-
    53213) -- the one named, reusable RNG helper in the source; everything
    else in the JS is ad-hoc inline ``rng()`` math (docs/logic-notes.md
    6.5). Draws against the global Stream B (``rng()`` above), consistent
    with the JS which always closes over the same global.

    ``bag`` maps outcome -> weight; a value is picked with probability
    proportional to its weight, resolved by walking entries in order and
    subtracting weights from a single ``rng() * total`` draw until it goes
    non-positive -- not by pre-normalizing to [0, 1). Iteration order
    matters and follows ``bag``'s own order (Python dicts preserve
    insertion order, matching JS ``Object.entries`` for the non-numeric
    string keys every real call site in the bundle uses; if a caller ever
    used all-integer-string keys, plain JS objects would reorder those
    ascendingly where a Python dict would not -- not a real concern for any
    known call site, flagging only for completeness).
    """
    total = sum(bag.values())
    remaining = rng() * total
    for outcome, weight in bag.items():
        remaining -= weight
        if remaining <= 0:
            return outcome
    return next(iter(bag))
