# Crazyhouse onboarding

Status: **inactive local preparation**. This document is not an official
OpenBench canary, a strength result, a production deployment, or publication
authorization.

## Frozen identities

- Variant contract: `LICHESS_CRAZYHOUSE_2026_08_12`
- Engine commit: `97fe071f2de738da0f7a570419f0bc89382eef19`
- Engine tree: `f07f7c84d05726089e9c915eaa7f4f6859e33a8e`
- Engine `src` tree: `74b74e4adbd01f8bc46c0597fb64b30733a1506b`
- Official Stockfish ancestor: `229f6339e537a097a79831cd06dbfdb3e623d4ac`
- Crazyhouse bench: `113485`
- Legacy network: 58,534,811 bytes, SHA-256
  `8ebf84784ad20fa33df403e60211818a7486db7cb8c3decfc86a80238d254f43`
- Opening payload: `CRAZYHOUSE_openings_v1.epd`, 100,204 bytes,
  1,024 unique roots, SHA-256
  `a8976a380a6cc4b3a1a6aae3bf14249b2ab6d1bac6cf4a2715625d7c01747603`
- Opening archive: 26,842 bytes, SHA-256
  `d919a19e3192a0457991dfafc95d320f33047a458277386334ef34d1bd14d820`
- Windows referee: 2,293,660 bytes, SHA-256
  `f465025b2ad21526e2cbab2b7da1a231ff3d64f6e8a01a0be5963f525a0bddae`
- Qualified referee source commit:
  `d25294c1b1084f8854c0dc026ca3b150c911b4ee`
- Qualified referee source tree:
  `208335f2040d7aac3e5c3b869cadf46b18fb5503`

`Crazyhouse_v1.nnue` is only a candidate public alias. The inactive descriptor
continues to name the authenticated legacy file and records that neither an
alias switch nor a champion change is authorized.

## Routing contract

The Server accepts the contract only when the workload, both engine records and
the book agree. A Crazyhouse engine or book without the contract is rejected.
Mixed protected families, unknown contracts and inferred/declared conflicts are
also rejected.

The Client maps the contract to `cutechess/crazyhouse`, emits
`-variant crazyhouse`, and selects
`Client/referees/LICHESS_CRAZYHOUSE_2026_08_12/<platform>/...`. It never falls
back to the shared `Client/cutechess-ob` artifacts. The Windows executable is
hashed before launch. No Linux artifact is qualified or pinned, so Linux
assignments are refused.

## Local verification receipt

The implementation was replayed from clean archive commit
`8224334f2acca6f797ae285557987ac4257f962a`, tree
`2025b98e535a6e3dc3b538f4d59916058444fc5a`.

- Source archive: 8,876,532 bytes, SHA-256
  `7feaa42f02d837489d4a37d96de8b669f89593874e416076d118791037bd5bd3`
- Targeted clean-export tests: 47 passed, 0 failed.
- The exact Windows referee passed byte-count, executable-magic and SHA-256
  verification, was installed only in the disposable export, was rehashed by
  the worker and reported `cutechess-cli 1.3.0-beta4` with Qt 5.15.19.
- The resolved runner path was contract-specific, the route was
  `cutechess/crazyhouse`, and the settings were
  `-repeat -recover -variant crazyhouse`.
- The shared Horde referee remained distinct at SHA-256
  `1c0bbab69e15a277c0b68bf032848b513f706749999cd5f6d09a1fb60f05b8a6`.
- Full UnitTests reported 13 failures and 3 errors in both the changed tree and
  a clean export of base `e20f0d9432f88fed1706d83fc93469be1a2a2cec`.
  These are pre-existing Horde manifest-fixture and stale client-v44 tests.
- The 184-test Django suite reported two stale client-version failures; both
  were reproduced unchanged on the clean base.

The machine-readable local result is 4,937 bytes with SHA-256
`28ab89bc784d1dad7739bf0761821dbbaab2622d4d47bb36474d5b0037669665`
at the project-owned lease path
`D:/Crazyhouse-Stockfish/leases/p10-openbench-onboarding-clean-288/result.json`.
That local path is a receipt reference, not a public artifact URL.

## Activation gates

`Crazyhouse-Stockfish` and `CRAZYHOUSE_openings_v1.epd` deliberately remain
absent from `Config/config.json`; `onboarding_ready` is false and NPS is null.
Activation requires all of the following:

1. Publish and reauthenticate the corresponding engine and referee source.
2. Publish the Windows referee and opening archive at immutable identities.
3. Register the exact legacy network bytes.
4. Qualify and pin a Linux referee, or keep the engine's supported systems
   restricted to Windows.
5. Complete the local same-network ladder at `2+0.02`, `10+0.1` and
   `30+0.3`, with at least 50 games per rung and advancement only at displayed
   LOS 100.0%.
6. Obtain the required publication/resource authorization.
7. Pin and deploy a new Client commit/version before scheduling anything.
8. Run an authorized production canary at `https://belzedar.duckdns.org` and
   authenticate its assignment, referee, network, book, `-variant crazyhouse`
   logs and PGNs.

Until every applicable gate passes, scheduling or claiming official OpenBench
evidence is invalid.
