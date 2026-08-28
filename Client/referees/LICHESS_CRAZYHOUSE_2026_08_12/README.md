# LICHESS_CRAZYHOUSE_2026_08_12 referee

This directory defines the only referee contract accepted for Crazyhouse
workloads. It is intentionally separate from the shared `Client/cutechess-ob`
artifacts, whose current binaries implement a different variant contract.

The Windows referee is pinned to the exact executable that passed the complete
Crazyhouse rule, result, serialization, capability, strict-bestmove, clock and
PGN qualification corpus. Its complete app-local runtime closure is shipped
beside it. The worker hashes the executable and every required DLL before a
workload can start. A missing or different file is rejected without falling
back to the shared referee.

No Linux referee has been qualified. Linux assignments therefore fail closed.
The exact Windows binary is shipped in this client tree and its corresponding
source is public at `Belzedar94/Crazyhouse-cutechess`, commit
`d25294c1b1084f8854c0dc026ca3b150c911b4ee`.

Validate an artifact without installing it:

```text
python Client/referees/LICHESS_CRAZYHOUSE_2026_08_12/install_artifact.py \
  --artifact <path-to-cutechess-cli.exe> --platform windows
```

The verifier expects the runtime DLLs beside the supplied executable by
default; use `--runtime-directory` only when validating a staged package.
Add `--install` to place an already verified package at the contract-specific
worker path. Installation refuses to overwrite different bytes. Runtime
provenance and license texts are recorded in `windows/THIRD_PARTY_NOTICES.md`.

Current OpenBench clients preserve this directory hierarchy during hot update.
Workers bootstrapped before client 47 flattened the hierarchy; the worker also
accepts that legacy placement, but only when the executable and the complete
runtime still match every contract hash.
