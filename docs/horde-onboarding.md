# Horde engine onboarding

Horde workloads use Cute Chess' native `horde` variant. The client accepts the
`HORDE` book token and the registered engine names `Horde-Stockfish` and
`Fairy-Stockfish-Hordetest-Baseline`. New workloads also carry the explicit
`variant_contract: LICHESS_HORDE_V1` value from the server to the client. A disagreement
between the contract, book token, or engine fallback aborts before a game is
started.

## Inert scaffolds

The two engine descriptors and `HORDE_openings.epd` manifest are deliberately
checked in with `onboarding_ready: false` and are not listed in
`Config/config.json`. This prevents an unmeasured NPS, an unfrozen branch or
bench, or a placeholder book identity from becoming schedulable.

Activation requires all of the following receipts:

1. A frozen Horde-Stockfish commit and deterministic bench.
2. A frozen Fairy-Stockfish Horde baseline commit and deterministic bench.
3. Measured single-thread NPS for each engine on the reference worker.
4. A public ZIP containing exactly `HORDE_openings.epd`, with both normalized
   text SHA-256 and raw-byte SHA-256 recorded in the book manifest.
5. Private-engine credentials installed out of band on the server and worker.
6. Successful play artifacts for both engines and a DATAGEN artifact for
   Horde-Stockfish.
7. A network receipt for each side, including an identical full SHA-256 when a
   same-network comparison is claimed.

After those receipts exist, replace every pending value, set
`onboarding_ready` to `true`, add both engines and the book to
`Config/config.json`, and run the configuration and onboarding test suites
before restarting the server.

## Private artifact roles

Private artifact workflows must declare the executable role in the final
hyphen-separated component of each GitHub Actions artifact name:

```text
<engine>-<os>-<simd>-<bitops>-play
<engine>-<os>-<simd>-<bitops>-datagen
```

For example:

```text
horde-linux-avx2-pext-play
horde-linux-avx2-pext-datagen
```

An engine opts in through `build.artifact_roles`. Play-only engines declare
`["play"]`; an engine with an in-engine generator declares
`["play", "datagen"]`. A DATAGEN workload never falls back to an untagged or
play artifact. Legacy untagged artifacts remain accepted only for play when a
workflow has no explicit role tags at all.

## Scheduling and first gates

Horde priority must be computed immediately before creation:

```text
min(priority of every live Spell or Atomic workload) - 1
```

Use priority 99 only when no live Spell or Atomic workload exists. The first
official game workload is a paired self-play smoke with zero crashes, time
losses, illegal moves, or runner aborts. The foundational comparison then uses
fixed GAMES workloads at 2.0+0.02, 10.0+0.1, and 30.0+0.3. Fixed-node matches
are diagnostic only.

Do not use early win or draw adjudication for the initial Horde baseline.
