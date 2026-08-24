# LICHESS_CRAZYHOUSE_2026_08_12 referee

This directory defines the only referee contract accepted for Crazyhouse
workloads. It is intentionally separate from the shared `Client/cutechess-ob`
artifacts, whose current binaries implement a different variant contract.

The Windows referee is pinned to the exact executable that passed the complete
Crazyhouse rule, result, serialization, capability, strict-bestmove, clock and
PGN qualification corpus. The worker hashes the selected executable before a
workload can start. A missing or different file is rejected without falling
back to the shared referee.

No Linux referee has been qualified. Linux assignments therefore fail closed.
The manifest and engine scaffold are inactive until corresponding source and
artifacts are public, the engine repository and opening artifact are available,
the legacy network is registered, the local same-network strength ladder has
passed, and an official canary has been authorized.

Validate an artifact without installing it:

```text
python Client/referees/LICHESS_CRAZYHOUSE_2026_08_12/install_artifact.py \
  --artifact <path-to-cutechess-cli.exe> --platform windows
```

Add `--install` to place an already verified artifact at the contract-specific
worker path. Installation refuses to overwrite different bytes.
