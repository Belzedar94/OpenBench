# AtomicDB opening-name authority

AtomicDB publishes names for Atomic chess positions only. It does not import
orthodox chess opening names merely because the moves or FEN happen to match.

## Admission and display policy

An opening name is admissible only when its Atomic position is attested by one
or more of these sources:

1. an Atomic-specific historical corpus (EAO or ATOMIX);
2. an Atomic study or an explicit naming discussion in The House;
3. an exact line confirmed by the Atomic-Stockfish project owner.

The catalogue is keyed by the canonical Atomic position, so transpositions are
recognized. A position with no name of its own shows the name of the deepest
named position of the line it was reached by, with that position's reference
line. Approved community names (`atomicdb/community_names.py`) take part in
that rule as names of their own positions: a new community name never replaces
a catalogued name on the same position, an approved correction does, and
between a catalogued name and a community name further up the line the deeper
one wins. Past the 64-ply replay limit the explorer carries the catalogued
name only.

When several Atomic names genuinely refer to the same position, current
project terminology wins the display label and the other source-attested names
remain aliases. A disputed name for a different position is documented as a
conflict, not silently attached as an alias.

## Maintainer-confirmed openings (2026-07-24)

The following exact Atomic lines and spellings are current project
terminology:

| Current name | Exact line |
| --- | --- |
| Wayward Queen | `1. e3 e6 2. Qh5` |
| Cowboy Attack | `1. e3 e6 2. Qh5 g6 3. Nf3 Qh4 4. g3 f6 5. Qxh7` |
| Fake Cowboy Attack | `1. e3 e6 2. Qh5 g6 3. Nf3 Qh4 4. g3 Qb4 5. c3 f6 6. Qxh7` |
| Xeransis Attack | `1. Nh3 h6 2. d4 e6 3. e4 Na6` |
| Chronatog Scambit | `1. Nf3 f6 2. e3 e6 3. Nd4 Bb4 4. c3 Bxc3` |
| Russian Attack | `1. Nf3 f6 2. Nd4 Nh6 3. e3 Ng4 4. f4 b5 5. h3` |
| Villager Defense | `1. Nf3 d6` |

`Qxh7` is intentional in both Cowboy lines: it is a capture under Atomic
rules, not `Qh7`.

For the exact Chronatog position, **Chronatog Gambit** is a source-attested
historical alias; the current display name is **Chronatog Scambit**. For the
exact Russian position after `5.h3`, EAO C21's
**Trojanknight-Opossum, Mainline, 5.h3 Variation** remains a historical alias;
the current display name is **Russian Attack**.

The House also contains contested or adjacent terminology that must not leak
across position boundaries:

- `2...g6` after Wayward Queen has separately been called Fried Queen Attack;
- Xeransis has occasionally been proposed for a different `3.Bg5` branch;
- Cowboy/cow-emoji terminology has been debated and was also proposed for an
  unrelated `1.Nh3` line.

Those facts are provenance notes, not permission to label those other
positions.
