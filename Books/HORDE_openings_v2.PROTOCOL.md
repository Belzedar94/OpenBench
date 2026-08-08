# Horde opening-book V2 protocol

## Objective

The Horde book is optimized for paired-test information, not for an artificial
50/50 game-score split. Horde naturally gives Black a large practical advantage.
The useful target is therefore to reduce openings where Black wins both engine
assignments and to increase positions where a small strength difference changes
the pair score.

For a two-game color-reversed pair, let the tested engine's pair score be
`S in {0, 0.5, 1, 1.5, 2}`. The primary diagnostics are:

- assignment-decisive rate: `D = mean(S != 1)`;
- squared pair displacement: `U = mean((S - 1)^2)`;
- Black-Black rate: the fraction of pairs won by Black in both assignments;
- middle-pair rate: `1 - D`.

These are book diagnostics. They do not replace GSPRT, and a large probe-strength
gap must not be interpreted as an Elo estimate.

## V1 diagnosis

The current production payload is the unchanged Fairy-Stockfish Horde book
described in `HORDE_openings.epd.SOURCE.md`. A header-only audit of two completed
and in-progress PGN chunks from workload 228 produced:

- 2,859 games and 1,429 complete color-reversed pairs;
- 97.2368% Black wins, 2.2735% White wins, and 0.4897% draws;
- pentanomial `[26, 7, 1360, 7, 29]`;
- 4.8286% assignment-decisive pairs and 95.1714% middle pairs;
- 4.094% mean squared pair displacement;
- 94.8915% Black-Black pairs;
- 700 canonical openings, 2.041 pairs per canonical opening, and a maximum
  reuse of seven pairs for one opening.

This sample confirms that the current book supplies little paired-test
information. It also exposes duplicate weighting and book wrap as measurable
confounders.

## Candidate generation

`Scripts/generate_horde_book_candidates.py` produces a deterministic candidate
pool from natural Horde trajectories:

1. Start every trajectory from standard Horde start position.
2. Search with the exact pinned Horde-Stockfish binary and Run 6B network.
3. Use one thread, 16 MiB hash, cleared hash per trajectory, and MultiPV 4.
4. Play the best root move with 75% probability. Otherwise sample a non-best
   root move no more than 50 centipawns behind the best move.
5. Require at least two distinct root moves in the selected MultiPV frame;
   repeated output for one root move cannot define a top-two gap.
6. Emit at most one candidate from each trajectory, before a root with a small
   MultiPV top-two gap.
7. Enforce equal White-to-move and Black-to-move candidate counts.
8. Remove canonical duplicates and cap positions sharing the same trajectory
   prefix.
9. Record every engine, network, setting, root score, move, and file hash in the
   JSONL trace and manifest.

Candidate selection must never use the same games as the final audit. Static
evaluation is a generation constraint, not the ranking objective.

`Scripts/generate_horde_book_successors.py` provides a cheaper complementary
pool. It canonicalizes the V1 records and advances each Black-to-move source by
one or more natural MultiPV moves to produce unique White-to-move
successors. This directly tests whether the fixed Black-to-move parity of the V1
book is suppressing pair information, while retaining the upstream trajectories.
The `--plies` option can continue each source by any odd number of plies, allowing
the depth effect to be tested independently from side to move.
`Scripts/screen_horde_book_candidates.py` can then apply the canary's narrow,
finite MultiPV-root constraints to any generated pool while preserving a full
accepted/rejected trace. This is a generation screen only; paired games remain
the promotion authority.

Fresh-trajectory generation is scaled through independent deterministic shards.
`Scripts/merge_horde_book_shards.py` accepts only shards with identical engine,
network, and generation recipes, verifies their hashes, removes canonical
duplicates, restores exact side-to-move balance, enforces the global prefix cap,
and writes a merged provenance manifest.

`Scripts/select_horde_book_candidates.py` applies one explicit trace criterion
to a merged pool, then reapplies the same side-balance and prefix-family
invariants. It can exclude complete paired PGNs by normalized position before
selection and records those PGN hashes in its manifest. Every source alias that
shares an excluded normalized position key is conservatively excluded. This
makes the selection sample and the held-out audit disjoint without introducing
another chess heuristic.

The first full successor receipt used the pinned baseline binary and Run 6B at
1,000 nodes, MultiPV 4, a 75% best-move weight, and a 50-centipawn alternative
cap. It reduced 2,486 V1 records to 1,431 canonical sources and 1,391 canonical
successors; 40 convergent successors were removed. The resulting EPD SHA-256 is
`7c89d5b5ab3da2101d778e19478173dcc42ce81209996bb5a97f3ff180ac9df8`.
This is a candidate pool, not a production replacement.

The one-ply pool was rejected after a 20-complete-pair probe: it supplied only
5.0% assignment-decisive pairs and 95.0% Black-Black pairs. Changing side to
move alone is therefore insufficient. A separate nine-ply continuation produced
1,408 canonical successors from the same sources, with EPD SHA-256
`c26fdb29cffe20f39cbd4c046044e927feb22e982fea88596bd41ef0e1f3bbf6`,
for an independent test of trajectory depth.

The unfiltered nine-ply pool was also rejected after 21 complete pairs: 9.52%
were assignment-decisive and 90.48% were Black-Black. Screening its final roots
at 1,000 nodes retained 667 unique White-to-move positions with a finite top-two
gap no greater than 15 centipawns and an absolute best score no greater than 400
centipawns. The screened EPD SHA-256 is
`8582779193952fbb60f17cb7f4b5a15a7c3f5c322adb200d9a472f86aef1e976`.

The 667-position gap-15 pool improved only modestly in its complete 40-pair
probe: 15.0% assignment-decisive, `U = 0.15`, and 85.0% Black-Black. It is not a
promotion candidate. The next orthogonal rungs tighten the gap to five
centipawns and extend the natural continuations to 17 plies.

The nine-ply gap-5 probe exited unexpectedly after 58 complete games despite an
80-game command. No PGN error marker or Windows application error was present,
so the sample is infrastructure-invalid; its partial metrics did not improve on
V1 (`D = 13.79%`, `U = 0.1121`). The 17-ply gap-5 rung was stopped after its
first 20 complete pairs all scored exactly 1-1 (`D = 0`, `U = 0`). Both rungs
are rejected. Future local probes must capture referee stdout and stderr in
addition to PGN.

## First deterministic canary

The first valid fixed-frame canary is intentionally too small for production:

- 64 records: 32 White to move and 32 Black to move;
- 64 canonical positions from 27 capped prefix families;
- ply range 8-24, median 17;
- MultiPV top-two gap range 0-15 centipawns, median 1;
- EPD SHA-256
  `c0d5f4e5086d8b0f2ee65346b58d54d032138564d0a919b46b4183a8b6b0c8e7`;
- trace SHA-256
  `480715396bb29e9c0d50bdbc41e7bc83e8f157c1244e32e45f85c5733838e0ee`.

It is used only to test the generator and the sensitivity methodology. Its small
size would cause severe book wrap in a normal OpenBench workload.

A first controlled 40-pair node-limited smoke used the same pinned engine on
both sides at 50,000 versus 40,000 nodes. Relative to an otherwise identical V1
probe, the canary increased assignment-decisive pairs from 12.5% to 25.0%,
increased squared pair displacement from 0.10625 to 0.2125, and reduced
Black-Black pairs from 87.5% to 70.0%. Its 24 White-to-move pairs supplied a
37.5% assignment-decisive rate, while its 16 Black-to-move pairs supplied only
6.25%. This is evidence for scaling and independently auditing the method, not
for publishing the 64-record canary.

Two additional 256-record fresh-trajectory shards were merged under the same
recipe. Hash verification, cross-shard canonical deduplication, exact side
balance, and the global prefix-family cap yielded 492 positions (246 per side to
move), EPD SHA-256
`868db350de35d96046d9f17ec169fb4b5301586f8cbf1a23389b805a413144da`.
A held-out-seed 40-pair probe completed 80/80 games with referee exit code zero:

- 25.0% assignment-decisive pairs versus 12.5% for V1;
- `U = 0.23125` versus `0.10625` for V1;
- 62.5% Black-Black pairs versus 87.5% for V1;
- 75.0% Black game wins versus 93.75% for V1;
- tested-engine pentanomial `[1, 0, 30, 1, 8]` at 50,000 versus 40,000 nodes.

This passes the sensitivity smoke and justifies a larger merged pool plus a
larger held-out audit. It is not yet the production archive.

Eight deterministic 256-record shards were then merged under the same recipe.
After removing 218 cross-shard canonical duplicates and enforcing balance and
the prefix cap, the pool contained 1,810 positions, 905 per side to move. Its
EPD SHA-256 is
`c569deee113bdbfd544f557071fcdc0f2823d0b4cebcfda3141542e6c236779a`.

A 200-pair diagnostic audit rejected the unfiltered 1,810-position pool. Its
tested-engine pentanomial was `[6, 2, 170, 2, 20]`, with `D = 15.0%`,
`U = 0.135`, and 79.0% Black-Black pairs. The matched V1 diagnostic produced
`[10, 1, 170, 2, 17]`, `D = 15.0%`, `U = 0.13875`, and 84.0% Black-Black.
The new pool reduced Black's double wins but did not increase paired-test
information, so it is not a production candidate.

Trace stratification over the complete 200-pair new-pool sample found one
consistent structural signal. Candidate roots with an exact zero-centipawn
MultiPV top-two gap supplied `D = 17.14%`, `U = 0.15`, and 74.29%
Black-Black, versus 13.85%, 0.12692, and 81.54% for nonzero gaps. This is a
selection hypothesis, not a result to reuse as validation.

An exact-gap-zero held-out pool therefore excluded every position seen in that
diagnostic audit. It retained 526 positions, 263 per side to move, with EPD
SHA-256
`f9af581ce7c7a832f076399384c746ac54432558230de65e2294e5456f0fae28`.
On a disjoint 40-pair smoke it produced `[1, 1, 34, 0, 4]`, `D = 15.0%`,
`U = 0.13125`, and 80.0% Black-Black. A fresh V1 comparator produced
`[0, 1, 37, 0, 2]`, `D = 7.5%`, `U = 0.05625`, and 92.5% Black-Black.
The signal is large enough for a bigger independent audit, but the smoke alone
cannot promote the book.

The larger audit excluded all 109 exact-gap-zero positions used by either the
diagnostic sample or the smoke. The resulting pool contained 468 positions,
234 per side to move, from 53 bounded prefix families. Its EPD SHA-256 is
`5b0908c7900ec2602c38bbeb5949bb8e20e6ba22f38f63a57271864ba8a46552`.

The held-out audit completed 400/400 games, 200/200 pairs, and 200 unique
openings with referee exit code zero. Exact gap zero produced pentanomial
`[10, 1, 162, 8, 19]`, `D = 19.0%`, `U = 0.15625`, and 77.0%
Black-Black. A contemporaneous V1 control was composed exclusively from five
valid 40-pair shards at the same concurrency and node limits. It completed
400 games and 200 pairs over 184 canonical openings, with `[7, 1, 177, 5, 10]`,
`D = 11.5%`, `U = 0.0925`, and 88.0% Black-Black.

Exact gap zero therefore improved assignment-decisive rate by 7.5 percentage
points, squared pair displacement by 68.9%, and Black-Black rate by 11
percentage points. It passes the local paired-information gate. It does not yet
pass the production capacity gate: 468 records would wrap too often in a normal
10,000-game STC workload.

The first direct scale-up also exposed a generator defect before promotion.
In 975 of 5,624 pre-fix records, the alleged top two MultiPV entries described
the same root move. Those records did not represent a genuine choice and their
pool was rejected. The fixed parser scans depths from deepest to shallowest,
deduplicates root moves within each frame, and accepts only a frame containing
at least two distinct roots. Corrected manifests declare
`root_move_policy = distinct`, so they cannot be merged with pre-fix shards.

Thirty-two corrected deterministic shards, using seeds 20261001 through
20261032, supplied 8,192 input records under the unchanged exact-gap-zero
recipe. Merge validation removed 2,384 canonical duplicates, trimmed 176
records to restore side balance, and trimmed 24 records for the global prefix
cap. The final pool contains 5,608 records, 2,804 per side to move, across 75
bounded prefix families. Every retained frame has an exact zero-centipawn gap
between distinct root moves. Its hashes are:

- EPD SHA-256
  `05753975c2baf80e0908988186113d2b72c7eb781b9ff628a7e1d6e945d4ff99`;
- trace SHA-256
  `30cb89e5920d067f83a3e50a6e1f290d4aee2af62e447d9aa2c78623afdbc774`;
- merge-manifest SHA-256
  `832d3c2a6260b01e266575e4dcf7a3b816b67eadebeeaff2eee0bf43920d500b`.

The pool can supply 5,000 pairs without book wrap at the 10,000-game neutral
cutoff. Four normalized exclusion-key collisions exist among the 5,608 exact
FEN records; conservative held-out selection removes every alias of an audited
position rather than treating an alias as fresh.

A single 400-game local referee invocation was terminated by the host after 57
games and was correctly marked invalid. The capacity audit was rerun as five
independent 80-game shards, seeds 20261041 through 20261045. Each referee exited
zero, and each successive candidate pool excluded all earlier audit PGNs. The
combined receipt contains exactly 400 games, 200 complete pairs, 200 unique
openings, no incomplete games, and a maximum opening reuse of one pair.

The corrected production candidate produced:

- pentanomial `[15, 2, 159, 3, 21]`;
- 20.5% assignment-decisive pairs;
- `U = 0.18625`;
- 72.0% Black-Black pairs;
- 82.0% Black wins, 16.75% White wins, and 1.25% draws.

Against the contemporaneous V1 control, this is a 9.0 percentage-point increase
in assignment-decisive pairs, a 101.35% increase in squared pair displacement,
and a 16.0 percentage-point reduction in Black-Black pairs. The combined
analysis SHA-256 is
`988bb263f460910a9b1f111f240549e535585a6b0a34b5d2f171ba489a988801`.
The result passes both the paired-information and production-capacity gates
without targeting an artificial 50/50 color split.

## Promotion gates

A production replacement requires all of the following:

- a sufficiently large, canonical-unique pool with bounded prefix-family reuse;
- no illegal positions, engine errors, crashes, time losses, or incomplete
  color-reversed pairs;
- a selection probe and an independent held-out audit using disjoint openings;
- identical engine, network, referee, node limits, concurrency, and pairing
  between the V1 and V2 comparisons;
- higher assignment-decisive rate and squared pair displacement on the held-out
  audit, with a materially lower Black-Black rate;
- no reliance on deterministic engine noise, repeated positions, or a single
  favorable probe setting;
- a fresh archive descriptor, payload hash, source receipt, and unit-test pass.

The V2 payload passes these gates. It is registered as the separate supported
book `HORDE_openings_v2.epd`; the V1 key remains available so an active workload
can never change opening bytes in the middle of a test.

`Scripts/run_horde_book_probe.py` is the canonical local probe runner. It keeps
the referee in the foreground, streams and hashes its complete log, hashes every
input artifact, requires a zero exit code and the exact requested number of
complete pairs, and emits a machine-readable analysis manifest. Samples from a
detached referee process are invalid even when their completed PGNs look clean.
Probe concurrency or shard size must also keep each referee invocation below the
host job-runtime limit. A partial PGN never turns its own manifest valid; only
complete paired fragments may be combined later as explicitly listed inputs.
