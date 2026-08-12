# Horde engine onboarding

Horde workloads use Cute Chess' native `horde` variant. The client accepts the
`HORDE` book token and the registered engine name `Horde-Stockfish`. New
workloads also carry the explicit
`variant_contract: LICHESS_HORDE_V1` value from the server to the client. A disagreement
between the contract, book token, or engine fallback aborts before a game is
started.

## Activation boundary

Only `Horde-Stockfish` and `HORDE_openings.epd` are schedulable. The historical
Fairy-Stockfish Hordetest descriptor remains checked in for reproducibility,
but keeps `onboarding_ready: false` and is absent from `Config/config.json`.
Search experiments use Horde-Stockfish on both sides and isolate one code or
option change per workload.

Activation requires all of the following receipts:

1. A frozen Horde-Stockfish commit and deterministic bench.
2. Measured single-thread NPS on the reference worker.
3. A public ZIP containing exactly `HORDE_openings.epd`, with both normalized
   text SHA-256 and raw-byte SHA-256 recorded in the book manifest.
4. Private-engine credentials installed out of band on the server and worker.
5. Successful play and DATAGEN artifacts for Horde-Stockfish.
6. A Run 6B network receipt shared by both sides of each self-comparison.
7. The verified `LICHESS_HORDE_V1` referee installed on **every** worker that
   may receive Horde work, and its SHA-256 recorded in
   `Client/referees/LICHESS_HORDE_V1/manifest.json` for both platforms.

Receipt 7 is the blocking one, and it is not optional bookkeeping. A stock
`cutechess-ob` answers to `-variant horde` and will arbitrate a game with
non-Lichess semantics and upload the result as if it counted. Client 45
therefore hashes the referee before launching any workload whose
`variant_contract` is `LICHESS_HORDE_V1` and refuses the workload unless it
matches the recorded build (`worker.REFEREE_PINS`, kept equal to the manifest
by `UnitTests/test_horde_referee_artifacts`). A platform whose reproducible
build has not been recorded yet is refused exactly like a mismatch, so
registering the engines before receipt 8 produces refused workloads and a
server-side event -- never silently mis-arbitrated games.

Install the referee with the pair produced by one `Horde referee` workflow run:

```text
python Client/referees/LICHESS_HORDE_V1/install_artifacts.py \
  --windows <windows-artifact-directory> \
  --linux <linux-artifact-directory> \
  --expected-source-commit <40-hex-commit> \
  --expected-run-id <run-id> \
  --install
```

After those receipts exist, replace every pending value, set the specialist's
`onboarding_ready` to `true`, add `Horde-Stockfish` and the book to
`Config/config.json`, and run the configuration and onboarding test suites
before restarting the server. The historical Fairy-Stockfish Hordetest
descriptor remains inert: search experiments compare one Horde-Stockfish
revision against another Horde-Stockfish revision with identical network and
runtime settings.

## V1 activation receipt

The schedulable V1 play configuration is frozen to Horde-Stockfish
`bce34feb0602c2640a8659a34f954fbee8f1a9e1` with bench `315576` and
1,488,566 measured single-thread NPS. The Hordetest baseline is frozen to
`fd044be239564a489056e358d157a4064f0b01a0` with bench `130284` and
527,465 measured single-thread NPS, but that historical engine is not registered
for workloads. Horde-Stockfish's Windows and Linux private artifact workflow
completed successfully as run `31172522698`.

The dedicated DATAGEN producer is frozen separately to
`f176a518166b7c27632a211127148c8e361b3844` with bench `440088`; its four
role-separated artifact jobs completed successfully in run `31174361886`.
The installed Windows and Linux Horde referees come from reproducible workflow
run `31168366529` and match the hashes pinned in the referee manifest. Every V1
workload uses Run 6B SHA-256
`B71108587968AC544EB2E62C2333FECA880DA5ACA52866787F1402163444ADF7` and
the opening payload SHA-256
`93E97B27D5DF054B8A649B8BE92A0A8B058384DAE35BAD142F9A610896EB6958`.

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

## Horde dataset boundary

Horde-Stockfish DATAGEN uses the dedicated producer commit
`212b67e7c5600b4067bfa9314f6c519a5ac4607d`. The playing executable does not
expose generation commands. The role-specific producer writes
`HORDE_BIN_V1`, whose frozen schema SHA-256 is
`B46ADE18AB8954A6AB232593484273E50C12B51550A938763A7A7D94DCCB63E4`.
Records contain physical Horde positions, so White pawns remain `P`; the
legacy `H` identity is introduced only by the Run 6B evaluator boundary.

Client 45 validates every uncompressed Horde chunk before compression or
upload. It requires publication protocol 41 and binds the file to all of the
following assigned identities:

- the clean 40-digit source commit and authenticated producer executable;
- Run 6B SHA-256
  `B71108587968AC544EB2E62C2333FECA880DA5ACA52866787F1402163444ADF7`;
- the raw Horde opening-book SHA-256
  `93E97B27D5DF054B8A649B8BE92A0A8B058384DAE35BAD142F9A610896EB6958`;
- chunk count, seed, thread count, depth, hash, exploration and write bounds;
- fixed header and record framing, canonical manifest JSON and payload hash;
- kingless-White and single-Black-king piece constraints, castling and
  en-passant state, move encodings, results and terminal reasons.

Any validation mismatch aborts the chunk before compression. The compressor
then hashes the exact bytes streamed into bzip2 and requires their SHA-256 and
length to match the validated file; replacement or in-place drift removes the
partial archive and aborts before upload. The server's v41 lease and receipt
remain the outer transport and publication contract; the embedded manifest
independently authenticates the uncompressed training payload.

The first preset is exactly two chunks of 250,000 records with campaign
`horde-v1-run6b-canary-20260806`, workload
`horde-v1-run6b-g0-canary`, role `g0-canary` and cohort `run6b-d6`. Priority is
intentionally not stored in the preset: calculate it from live Spell and
Atomic workloads immediately before creating the canary.

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
