# Crazyhouse onboarding

Status: **activation candidate**. This document records the public identities
and fail-closed route that must be reauthenticated after production deployment.
It is not itself a strength result.

## Frozen identities

- Variant contract: `LICHESS_CRAZYHOUSE_2026_08_12`
- Engine commit: `c48e463b01fc2f17a634fe52b0ba355663804c33`
- Engine tree: `35bf7df9c6aade171ffa0a457ab7576b744d8f14`
- Engine `src` tree: `2e0ba8b66317ef3e899a53e752ef9266fc17ac92`
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

## Physical DATAGEN v1 canaries

The production producer is a separate `datagen` build role. A public source
archive has no `.git` directory, so the worker passes the exact commit, full
tree and `src` tree from the qualified engine descriptor into the producer
build. The producer remains fail-closed if any identity is absent or malformed.
The exact public archive at the engine commit above built successfully and
reported those three identities with `producer_source_dirty=false`.

Two protocol-41 canaries are frozen before activation. Each has one 512-record
chunk, priority 400, one scientific search thread and a separately authenticated
`{THREADS}` worker-capacity transport. Both use fixed work of 16,384 nodes per
position, the legacy network, the official book, the same label-free partition
and no game adjudication.

- Train campaign: `0ba8e277-fe6d-5762-9f14-83c234134be0`, base seed
  `202608290100000`.
- Validation campaign: `6916cbda-69e9-5cb4-a85c-cf82876b42db`, base seed
  `202608290200000`.
- Campaign-set SHA-256:
  `e1313c53951334350a8a25195b800f47e2a9366e00f0697a8845cbed603120af`.
- Split seed: `12`; validation threshold: `2305843009213693952`.
- Partition SHA-256:
  `694e984f910aa3e6c1dce749521dc79c2fae928630aef494ecdd0a029b25bf01`.

These canaries prove routing, authenticated production generation, exact
framing and role separation only. They are not a dataset, training result,
model-selection result, Elo result or release result.

## Incident addenda

| ID | Symptom | False inference | Cause | Prevention | Gate |
| --- | --- | --- | --- | --- | --- |
| CH-OB-001 | A production worker archive would reach the DATAGEN Makefile without a Git repository and fail its complete source-identity check. | Passing `GIT_SHA_FULL` is sufficient provenance for an authenticated producer. | The client transported the commit but not the full tree or `src` tree required by the producer. | Pin both trees in the public engine descriptor, validate lowercase 40-hex identities, pass all three identities only to the `datagen` build role, and compile the exact public archive before activation. | P11/G11 official archive-build routing |
| CH-OB-002 | The validation canary aborted after 64 candidates with 56 role-ineligible and eight complete trajectories; exact quota 512 was not reachable. | A candidate budget adequate for the 7/8 train partition is also adequate for the 1/8 validation partition. | Role filtering happens before search, so the validation pool is intentionally much sparser. | Preregister 256 candidates for both roles, preserve the same fixed work per searched position, and require both exact local quotas before activation. | P11/G11 role-separated canary quota |
