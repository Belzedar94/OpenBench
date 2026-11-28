# atomicdb-v2

The Rust workspace of AtomicDB v2 (plan: `docs/atomicdb/v2-plan.md`).

| Crate | What it is | Owner |
|---|---|---|
| `crates/av2-model` | Ids (u64 in code, 40 bits on disk), keys (sha256 of the canonical FEN, 96-bit fingerprint), 16-bit moves, the `Core`/`Ext` records with their v1 column mapping, packed FEN, the `GraphRead`/`GraphWrite` traits | core |
| `crates/av2-facts` | The facts of the log: versioned envelopes with JSON payloads, the only long-lived format | core |
| `crates/av2-store` | Mapped column files, redo log segments, group commit, checkpoint, recovery, backup (phase 1) | core |
| `crates/av2-rules` | The value rules. Today only the `Rules` trait the writer calls | rules |
| `crates/av2-tools` | `av2-load`, `av2-check`, `av2-replay`, `av2-crashtest`, `av2-perf` | core |
| `bench` | The storage race behind the plan (`docs/atomicdb/v2-storage-benchmark.md`), kept as the regression benchmark | |

```
cargo test                      # the v2 crates (default members)
cargo clippy --all-targets -- -D warnings
cargo fmt --all --check
cargo build --release -p atomicdb-v2-bench   # the bench: needs libclang, see bench/README.md
```

Docs: `docs/store.md` (storage design, protocol, measurements). Export SQL
for the loader: `sql/export.sql`. Power-cut test (Linux): `scripts/powercut/`.

Builds on this tower: keep to `-j 12` (an OpenBench worker uses 8 threads).

Rules for code in this workspace:

- The rules are written against `av2_model::{GraphRead, GraphWrite}` only;
  they never see files, offsets or the log.
- A batch is a pure function of the committed state and its inputs: no wall
  clock, no hash-map iteration order, no thread timing (plan section 2.2).
- The facts format (`av2-facts`) changes only by adding optional fields or
  new kinds. Kind codes are never reused.
- Fields that v1 has and v2 must keep carry a doc line naming their v1
  column. Anything uncertain is marked `TODO(rules)`.
