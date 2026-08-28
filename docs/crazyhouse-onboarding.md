# Crazyhouse onboarding

Status: **activation candidate**. This document records the public identities
and fail-closed route that must be reauthenticated after production deployment.
It is not itself a strength result.

## Frozen identities

- Variant contract: `LICHESS_CRAZYHOUSE_2026_08_12`
- Engine commit: `5883acbeffd53138d31b278894d1fee451adffe8`
- Engine tree: `ed166600c76a7ab0fd2abca5f6123c7e2eed1fbd`
- Engine `src` tree: `d01af0408fdeb642810fbe3aa76896f9110dacbe`
- Official Stockfish ancestor: `229f6339e537a097a79831cd06dbfdb3e623d4ac`
- Crazyhouse bench: `38919`
- Legacy network: 58,534,811 bytes, SHA-256
  `8ebf84784ad20fa33df403e60211818a7486db7cb8c3decfc86a80238d254f43`
- Opening payload: `CRAZYHOUSE_openings.epd`, 39,922 bytes,
  599 positions and 489 unique roots, SHA-256
  `1371e87ce3bdb875d922ad0061c96c4a123bc571daf4ae2bff24e5176287f0fa`
- Opening archive: 3,401 bytes, SHA-256
  `d24bb6d72015af9930f76f9191ba36c016652a6f2708a2cc79e9e2c8ec600d9c`
- Windows referee: 2,293,660 bytes, SHA-256
  `f465025b2ad21526e2cbab2b7da1a231ff3d64f6e8a01a0be5963f525a0bddae`
- Windows app-local runtime: 11 hash-pinned DLLs, 48,774,786 bytes total;
  package versions and license texts are preserved with the artifact.
- Qualified referee source commit:
  `d25294c1b1084f8854c0dc026ca3b150c911b4ee`
- Qualified referee source tree:
  `208335f2040d7aac3e5c3b869cadf46b18fb5503`
- Qualified referee source repository:
  `https://github.com/Belzedar94/Crazyhouse-cutechess`

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
back to the shared `Client/cutechess-ob` artifacts. The Windows executable and
all 11 runtime DLLs are hashed before launch. No Linux artifact is qualified or
pinned, so Linux assignments are refused. Client 47 preserves nested update
paths; workers with an older immutable bootstrap may use the legacy flattened
placement only when the same complete package passes every hash.

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

The post-review deployment hardening is pinned at commit
`e976699e78f27ceb9405496ec06ee31c32a67cd7`, Client tree
`5c2958965dd93a9cc7a2d8825c2bfebb80a94b1d`. It preserves nested paths during
hot update, authenticates the complete Windows runtime, supports the exact
legacy flattened placement, and terminates only registered runner process
trees. The packaged referee reported `cutechess-cli 1.3.0-beta4` / Qt 5.15.19
with `PATH` restricted to Windows system directories.

## Production contract

The active presets are single-thread `10.0+0.1` with Hash 32 and `30.0+0.3`
with Hash 128. Both use SPRT bounds `[0.00, 10.00]`, the same legacy network,
the official Crazyhouse book above, no adjudication, and no maximum-game cap.
Only Windows workers are eligible. The first production workload is a canary;
its assignment, referee digest, network, book, `-variant crazyhouse` command,
logs and PGNs must be checked before any strength campaign is interpreted.
