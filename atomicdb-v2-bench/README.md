# atomicdb-v2-bench

The storage race behind `docs/atomicdb/v2-plan.md`. One Rust harness drives the
same AtomicDB-shaped workloads against every candidate store, and every store
must end in the same state (the suite prints a hash of the whole graph, and the
hashes must match).

Results and methodology: `docs/atomicdb/v2-storage-benchmark.md`.

## Stores

| Name | What it is |
|---|---|
| `mmap` | Fixed-stride column files mapped into memory, redo log with a CRC per frame, fsync before apply, a Bloom filter in front of the key map (`src/stores/mmapcol.rs`) |
| `memwal` | The in-memory graph with a logical write-ahead log and full snapshots (`src/stores/mem.rs`) |
| `lmdb` | LMDB through its C API (`lmdb-master-sys`) |
| `sqlite` | SQLite 3 in WAL mode, integer ids, `synchronous=FULL` |
| `rocksdb` | RocksDB 11, one column family per table, bloom filters, synced WAL |
| `redb` | redb 5 (pure-Rust copy-on-write B-tree) |
| `fjall` | fjall 3 (pure-Rust LSM tree) |
| `pgint` | PostgreSQL with integer ids and narrow tables |
| `pgv1` | PostgreSQL with the production schema: 64-character text keys, the same columns and indexes |
| `mem` | In-memory reference, no durability: the upper bound and the oracle of the crash test |

All of them implement `Store` in `src/store.rs`: batched reads, and one
`apply_batch` that must be atomic and durable when it returns.

## Build

```
# Windows (MSVC toolchain). RocksDB's bindings need libclang:
#   pip install --target <dir> libclang     and point LIBCLANG_PATH at <dir>/clang/native
set LIBCLANG_PATH=...
set LIBSQLITE3_FLAGS=-DSQLITE_MAX_MMAP_SIZE=1099511627776
cargo build --release
```

On Linux: `apt install clang libclang-dev build-essential`, then the same
`LIBSQLITE3_FLAGS` and `cargo build --release`.

## Datasets

```
# Synthetic, calibrated on a production sample (see the report for the calibration table)
atomicdb-v2-bench gen --out data/syn20m --nodes 20000000 --seed 1

# A real slice exported read-only from production (the export SQL is in the report)
atomicdb-v2-bench import --out data/ball --pos ball_pos.csv --edges ball_edges.csv

atomicdb-v2-bench describe --data data/ball
```

## Run

```
atomicdb-v2-bench load  --store lmdb --data data/ball --db work/ball_lmdb --out results.jsonl
atomicdb-v2-bench suite --store lmdb --data data/ball --db work/ball_lmdb --out results.jsonl
```

`scripts/run_scale.ps1` does both for every store. The suite runs, in order:
`select` (descents from the root), `explore_1_reader`, `explore_n_readers`,
`ingest_b1` and `ingest_b32` (full landings: expansion, seeds, mate lines,
climb, durable commit), `climb_b32` (re-searches of interior positions),
`explore_under_writes`, `footprint` and `digest`. One JSON line per stage.

Postgres stores take `--pg "host=127.0.0.1 port=5432 user=postgres dbname=bench"`.
`--no-fsync` relaxes durability (never used for the numbers in the report).

Environment switches of the mmap store: `MMAP_WAL_CKPT_MB` (log size that
triggers a checkpoint, default 256; the crash script sets 2 so that kills land
in checkpoints) and `MMAP_KEY_FILTER=0` (key filter off). The files are
created at a reserved capacity (`--cap-nodes`, `--cap-edges` at load time,
by default the dataset plus 6 million positions): a run that adds more than
that stops with "node capacity exhausted".

## Crash test

```
atomicdb-v2-bench crash --store mmap --data data/ball --db work/ball_mmap --trials 30 --out crash.jsonl
```

A child process runs the writer and acknowledges each durable batch; the parent
kills it at a random moment (`TerminateProcess` / `kill -9`; for Postgres it
kills the server, pass `--pgctl` and `--pgdata`), reopens the store, times the
recovery and compares the whole state with an in-memory reference that applied
exactly the recovered number of batches. A power cut is not simulated: see the
report for what that leaves untested.

## What the workload is and is not

It has AtomicDB's read and write pattern (to decide a position you read its
record, its edge block and every child record; a change wakes the parents; an
expansion inserts about 30 positions and edges after one key lookup each). It
does not implement AtomicDB's value rules: `src/rules.rs` holds two small
stand-ins. It measures stores, not semantics.
