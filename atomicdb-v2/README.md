# atomicdb-v2

The Rust workspace of AtomicDB v2 (plan: `docs/atomicdb/v2-plan.md`).

| Crate | What it is | Owner |
|---|---|---|
| `crates/av2-model` | Ids (u64 in code, 40 bits on disk), keys (sha256 of the canonical FEN, 96-bit fingerprint), 16-bit moves, the `Core`/`Ext` records with their v1 column mapping, packed FEN, the `GraphRead`/`GraphWrite` traits | core |
| `crates/av2-facts` | The facts of the log: versioned envelopes with JSON payloads, the only long-lived format | core |
| `crates/av2-store` | Mapped column files, redo log segments, group commit, checkpoint, recovery, backup (phase 1) | core |
| `crates/av2-rules` | The value rules. Today only the `Rules` trait the writer calls | rules |
| `crates/av2-chess` | The rules of atomic chess through the Fairy-Stockfish sources pyffish 0.0.89 is built from (vendored, C ABI shim): canonical FEN and key, legal moves, child FENs, terminal status (POS-1.1, POS-4.1); equivalence tools against v1 (`tools/golden.py`, `examples/equiv.rs`) | stage (a) |
| `crates/av2-tb` | Atomic Syzygy probes through `shakmaty-syzygy` with v1's fail-closed contract (TB-1.1, TB-1.2); comparison with python-chess (`tools/tb_golden.py`, `examples/tbequiv.rs`) | stage (a) |
| `crates/av2-expand` | Creating positions, expansion and reachability over the graph traits (POS-2.1, POS-5.1, EXP-1.1) | stage (a) |
| `crates/av2-tools` | `av2-load`, `av2-check`, `av2-replay`, `av2-crashtest`, `av2-perf` | core |
| `crates/av2-compat` | The outside contract (`docs/api-contract.md`): replays v1's golden answers (`tests/golden/`) against a base URL and diffs them field by field; runs the worker protocol scenarios against a test instance | api-golden |
| `crates/av2-server` | The writer service skeleton: single writer, group commit, stub rules, Prometheus `/metrics` | core |
| `crates/av2-web` | The website: Leptos pages rendered on the server (same URLs and look as v1), WASM islands for the board and the request row, page cache keyed on page versions, precompressed static files, Django session bridge. `SiteRead` is the trait the store implements for it; a fixture implements it today. Build and design: `docs/web.md` | web |
| `bench` | The storage race behind the plan (`docs/atomicdb/v2-storage-benchmark.md`), kept as the regression benchmark | |

```
cargo test                      # the v2 crates (default members)
cargo clippy --all-targets -- -D warnings
cargo fmt --all --check
cargo build --release -p atomicdb-v2-bench   # the bench: needs libclang, see bench/README.md
```

Docs: `docs/store.md` (storage design, protocol, measurements),
`docs/server.md` (writer service). Export SQL for the loader:
`sql/export.sql`. Power-cut test (Linux): `scripts/powercut/`. Core
measurements on the production box: `scripts/prod/run_core_prod.sh`.

Builds on this tower: keep to `-j 12` (an OpenBench worker uses 8 threads).
`av2-chess` compiles the vendored Fairy-Stockfish sources: it needs a C++17
compiler (MSVC Build Tools on Windows, g++ or clang on Linux).

Rules for code in this workspace:

- The rules are written against `av2_model::{GraphRead, GraphWrite}` only;
  they never see files, offsets or the log.
- A batch is a pure function of the committed state and its inputs: no wall
  clock, no hash-map iteration order, no thread timing (plan section 2.2).
- The facts format (`av2-facts`) changes only by adding optional fields or
  new kinds. Kind codes are never reused.
- Fields that v1 has and v2 must keep carry a doc line naming their v1
  column. Anything uncertain is marked `TODO(rules)`.
