# AtomicDB v2: the storage race

Measured 27/28-Nov-2026 for [`v2-plan.md`](v2-plan.md). The harness is
[`atomicdb-v2/bench/`](../../atomicdb-v2/bench/); the raw output of every run
quoted here is in `atomicdb-v2/bench/results/`.

## 1. Result

**Mapped fixed-stride columns with a redo log win.** They are ahead of every
off-the-shelf store on every workload at every size. The margin is smallest
against the log-structured stores on ingest (1.3x to 2x when every search is
its own commit, because both are then waiting for the same fsync; 2.5x to 8x
in batches) and largest on reads (5x to 8x against LMDB, about 50x against
RocksDB, more than 600x against Postgres). No off-the-shelf store is second on every
workload: the ones that ingest well read slowly, and the ones that read well
pay for the key map at commit.

Ten stores ran the same batches and ended in the same state (one hash over
the whole graph, identical for all ten on the real slice). Every durable
store was killed with `kill -9` at random moments, 192 times in all: none
lost an acknowledged batch and none recovered to a wrong state.

The decisive numbers, all with one fsync per batch, on a Ryzen 9 5950X with
an NVMe, Windows 10:

| Real slice of production, 1.9M positions | mmap columns + WAL | LMDB | RocksDB | SQLite | Postgres, integer ids | Postgres, v1 schema |
|---|---:|---:|---:|---:|---:|---:|
| Searches applied per second (expansion, climb, commit), 32 per commit | 10,878 | 1,064 | 1,393 | 568 | 514 | 208 |
| The same, one per commit | 1,130 | 240 | 550 | 222 | 121 | 70 |
| Re-searches per second (propagation only) | 23,297 | 8,379 | 2,014 | 1,409 | 1,084 | 425 |
| Selector descents per second | 685,354 | 60,228 | 4,731 | 1,838 | 796 | 486 |
| Explorer reads per second, 8 readers, writer running | 3,976,122 | 719,441 | 57,500 | 129,457 | 7,473 | 6,059 |
| p99 of one such read | 4 us | 23 us | 334 us | 145 us | 1.4 ms | 1.8 ms |
| Bytes on disk per position | 81 | 148 | 115 | 96 | 341 | 1,086 |
| Recovery after kill -9, median | 58 ms | 6 ms | 135 ms | 53 ms | 1.3 s | not run |

| Larger graphs | mmap columns + WAL | LMDB | RocksDB | SQLite | Postgres, integer ids | Postgres, v1 schema |
|---|---:|---:|---:|---:|---:|---:|
| 20M positions: searches per second, 32 per commit | 8,744 | 293 | 3,240 | 433 | 514 | 89 |
| 20M: explorer reads per second, 1 reader | 669,535 | 83,475 | 10,430 | 36,150 | 1,179 | 157 |
| 200M, 20 GB of memory available: searches per second, 32 per commit | 4,064 | 158 | 1,466 | 336 | | |
| 200M: explorer reads per second, 8 readers | 3,261,758 | 17,513 | 11,703 | 16,812 | | |
| 200M: store size | 15.5 GB | 22.3 GB | 20.9 GB | 19.5 GB | | |
| 200M: bulk load | 70 s | 283 s | 420 s | 326 s | | |

What the numbers say:

1. **Reads decide it.** A climb is reads: about 230 positions per search on
   the real slice. The mmap store reads a record with a pointer addition;
   LMDB walks a B-tree (8x slower here), SQLite runs a statement per row
   (18x), RocksDB searches several levels (47x), Postgres makes a round
   trip per wave (more than 600x, with everything cached). This part does
   not depend on the operating system.
2. **The key map splits the others.** Every expansion inserts about 35
   random 16-byte keys. B-trees (LMDB, redb, SQLite, Postgres) dirty one
   page per key and pay for it at commit; log-structured stores (RocksDB,
   fjall) absorb that and then read slowly. The mmap store logs 16 bytes
   per key and writes the slot in place.
3. **Bytes per position become a cliff.** At 200M positions the mmap store
   is 15.5 GB and fitted in this machine's free memory; the others are 19.5
   to 22 GB and did not, and their reads dropped to disk speed. The first
   mmap run at this size, taken with only 8 GB available, shows the same
   drop (37,679 reads per second instead of 3.3 million).
4. **The production schema on Postgres** is 52x slower to apply a search
   than the mmap store on the real slice with everything cached, 98x at
   20M, and its reads go from 1,058 to 157 per second once the data
   outgrows the cache. This is with batched queries from Rust; production
   runs it from Python, row by row. Integer ids and narrow rows make
   Postgres 2.5x to 6x better and leave it 17x to 21x behind.
5. **An in-memory graph with a log and snapshots** (also run, see the
   tables) is in the same class as the mmap store while it is running: it
   is the same design without the files. It loses on restart (3.5 s at 20M
   against 1 ms, growing with the graph), on memory (3.2 GB of private
   memory at 20M against 14 MB plus evictable page cache) and it stops
   working when the graph outgrows RAM.

What they do not say is in section 8: these are Windows measurements, the
Linux cross-check could not be run, and the commit cost of LMDB in
particular is expected to be much better on Linux.

## 2. What was run

### 2.1 Machine

| | |
|---|---|
| CPU | AMD Ryzen 9 5950X (16 cores; Windows exposed 13 cores / 25 threads to the session) |
| RAM | 32 GB DDR4; between 8 and 20 GB available during the runs (a desktop in use: browsers, a game client, chat apps) |
| Disk | KIOXIA KXG60ZNV1T02, 1 TB NVMe, NTFS with 4 KB clusters. All datasets and stores were on this drive (`D:` on this machine is a hard disk and was not used for data) |
| OS | Windows 10 Home 19045, Microsoft Defender real-time protection on |
| Toolchain | Rust 1.100.1 (MSVC), `--release`, thin LTO |
| Threads | one writer thread, up to 8 reader threads, never more than 16 in total |

Production is a Linux box (i7-8700, 64 GB, two NVMe in md RAID1). Sections 7
and 8 say what should and should not be expected to transfer. A cross-check
on Linux was prepared and could not be run (section 7).

### 2.2 Stores and how each was configured

Every store got the same job: the same record layout, the same batches, and
**a commit that is durable when `apply_batch` returns**.

| Store | Version | Configuration | What "durable" is |
|---|---|---|---|
| mmap columns + WAL | this repo | 32 B record, 8 B edge-block offset and 4 B parent per position; edge blocks and parent lists in append-only arenas; open-addressing key map (16 B slots, load 0.65) behind a 10-bit Bloom filter; physical redo log, checkpoint when it passes 256 MB | `FlushFileBuffers` on the log before the batch is applied |
| in memory + WAL + snapshot | this repo | the in-memory store below plus a logical log (one frame per batch) and a full snapshot at the end of each write stage; the key map is rebuilt at start | `FlushFileBuffers` on the log before the batch is applied |
| LMDB | mdb.master 0.9.70 through `lmdb-master-sys` 0.2.6 (C API, no wrapper) | sub-databases with `MDB_INTEGERKEY`, `MDB_NOTLS`, no `WRITEMAP` | LMDB's synchronous commit |
| SQLite | 3.54.1 through `rusqlite` 0.43 (bundled) | WAL, `synchronous=FULL`, 4 KB pages, 2 GB cache, `mmap_size` 64 GB (built with a raised `SQLITE_MAX_MMAP_SIZE`), `wal_autocheckpoint` 20,000 pages; `INTEGER PRIMARY KEY` ids, one edge blob per parent, in-edges and key map `WITHOUT ROWID` | WAL fsync at commit |
| RocksDB | 11.8.1 through `rocksdb` 0.25 | one column family per table, bloom filter 10 bits, 2 GB LRU block cache, no compression, 64 MB write buffers, level compaction with dynamic level sizes | `WriteOptions::set_sync(true)` |
| redb | 5.0.0 | 2 GB cache | `Durability::Immediate` |
| fjall | 3.1.13 | 2 GB cache, defaults otherwise | `persist(SyncAll)` after each batch |
| PostgreSQL, integer ids | 16.15 (the same version production runs) | `node(id int4, rec bytea)`, `edge(parent, idx, child, mv)` plus an index on `(child, parent)`, `keymap(key bytea, id)`; `shared_buffers` 2 GB, `synchronous_commit=on`, `fsync=on`, `full_page_writes=on`, `wal_sync_method=fsync`, `jit=off` | commit |
| PostgreSQL, v1 schema | same server | `position` keyed by a 64-character text key with production's columns and five indexes, about 1.1 KB of `last_analysis` on searched rows; `edge(id, parent_id, move_uci, child_id)` with production's two indexes | commit |
| in memory | this repo | plain vectors and a hash map | none: the upper bound, and the oracle of the crash test |

Both Postgres baselines are driven by the same Rust climb with **one query
per wave of ids** (`= ANY($1)`, `unnest` for writes), never one per row. That
is much kinder to Postgres than Django is: it isolates the cost of the store
from the cost of Python. Production's own figures for v1 as it runs today are
in section 3.1.

### 2.3 Workloads

One landing is what a worker's result does to the graph:

1. read the position; if it was never expanded, **expand** it: one key
   lookup per legal move (the transposition check), about 29 new positions
   and edges, with a 6% chance per move of landing on an existing position;
2. store the search (eval, nodes, best move) and **seed** the first five
   replies;
3. with production's frequencies, prove a **mate**: close a reply and
   materialise the line as a chain of new, closed positions;
4. **climb**: a status cascade to a fixed point, then the backed-value
   ascent level by level. To decide a position the climb reads its record,
   its edge block and the record of every child; a change wakes the parents
   (all of them, or only those standing on that edge when the drift is under
   10 cp);
5. commit the batch durably.

The two rules of the climb (`src/rules.rs`) are stand-ins with AtomicDB's
read and write pattern. They are not AtomicDB's value semantics. The
workload was tuned until its footprint matched production's:

| Per landing | Production | This benchmark (real slice, 32 per commit) |
|---|---:|---:|
| New positions | about 33 (204M positions / 6.25M landings) | 34.7 |
| Key lookups | one per legal move | 36.4 |
| Backed values written by the climb | 4.75 mean, 2.95 median | 4.11 |
| Landings that close a reply with a mate line | 44% | 44% (by construction) |
| Positions read by the climb | not recorded | 231 |
| Logical bytes changed | not recorded | 2,320 |

The suite runs these stages in order on each store. Every stage is the same
sequence of operations for every store:

| Stage | What it measures |
|---|---|
| `load` | bulk load of the dataset |
| `select` | 2,000 descents from the root to a leaf, as the selector does |
| `explore`, 1 and 8 readers | a random position by key, its record, all its moves with their records and its parents: one explorer page or one `/api/query`. 70% of the reads are moved to the parent, so most hit positions with a full move list |
| `ingest`, 1 per commit | 500 landings, one fsync each |
| `ingest`, 32 per commit | 200 batches of 32 landings merged into one climb, as the drainer does |
| `climb`, 32 per commit | 200 batches of 32 re-searches of interior positions: almost no inserts, mostly propagation |
| `explore under writes` | 8 readers while the writer ingests 100 batches of 32 |
| `footprint` | disk, memory, bytes written |
| `digest` | a hash of every record, every edge block and every parent set. **All stores must print the same value**: that is the proof that they did the same work |

## 3. Data

### 3.1 What production looks like (read-only, 27-Nov-2026, 22:50 to 23:10 UTC, and one reading of counters at 03:19)

Nothing was written to production and neither big table was scanned. Four
exports (plus catalogue and statistics queries), each a `COPY (SELECT ...) TO STDOUT` under a `statement_timeout`,
niced and compressed on the server:

| Export | How | Size |
|---|---|---|
| Page sample of `atomicdb_position` | `TABLESAMPLE SYSTEM (0.02) REPEATABLE (42)`, plus the in- and out-degree of each sampled row through the two edge indexes | 40,471 rows, 20 s |
| The last 200,000 `atomicdb_ingestjob` rows | by primary key: id, position, timestamps, payload size and the climb counters of `summary` | 22.1 hours of landings, 18 s |
| The root ball | recursive walk from the start position through `uniq_parent_move`: every edge out of the positions within 5 plies, then the columns of every position reached (6 plies) | 1,560,369 positions, 1,994,101 edges, 170 s + 395 s |
| Depth of 400 recent landings | first-parent chain to the root through the child index | 2 s |

The SQL is in section 9. `pg_class` and `pg_stats` gave the sizes.

| | Production |
|---|---|
| Positions / edges (estimates) | 204M / 209M to 222M |
| Table + indexes: `position`, `edge` | 99 GB, 91 GB (the two edge indexes on text keys are 51 GB, the position key index 24 GB) |
| Open, never searched | 73% |
| Closed | 24.8%: mate line 22.2%, terminal 1.8%, minimax 0.8% |
| Expanded = searched | 2.9% |
| Moves of an expanded position | mean 28.7; p5 12, p25 23, p50 29, p75 35, p95 42, max 57 |
| Positions with exactly one move (mate-line chains) | 22.2% |
| Parents per position | none 0.1%, one 94.5%, two 4.6%, three or more 0.8% (max 16 in the sample); mean 1.066 |
| Parents per position inside the root ball | one 75.9%, two 21.3%, three or more 2.8%; mean 1.278 |
| Has an eval | 14.5% (9.8% are seeds on positions nobody searched) |
| Open with a backed value | 2.1% |
| `last_analysis` | 1,117 B mean on searched rows |
| Searches per minute | mean 150.7, p50 139, p90 230, p99 401, max 768 |
| Job payload | 1,260 B mean |
| Submit to climbed (`created` to the last `updated` of the job, which is when the drainer writes its summary) | p50 1.19 s, p90 4.85 s, p99 760 s, max 1,450 s |
| Landings merged per climb | p50 3, p90 32, max 128 |
| Backed values written per landing | mean 4.75, p50 2.95, p90 9.7, p99 31.5 |
| Landings that closed at least one reply | 44% |
| Depth of a landing (first-parent chain; 214 of the 400 reach the start position, the rest hang from positions entered by FEN) | p10 25, p50 52, p90 72, max 76 |
| Postgres | 16.15, `shared_buffers` 16 GB, database 249 GB |
| Box at the time | load average 7.8 to 12.4 on 12 threads; md RAID1 4,150 reads/s and 1,760 writes/s, 89% utilised |

Two observations that are not about stores but matter for the plan:

- **Growth.** 200,000 landings in 22.1 hours at about 33 positions each is
  about 7.2 million positions a day, 3.5% of the graph per day. A second
  reading 4.5 hours later agrees: `n_tup_ins` of `atomicdb_position` had
  grown by 1,226,315 and the job id by 35,343 (6.6 million positions and
  189,000 landings a day, 34.7 positions per landing).
- **Disk.** v1 costs about 1,220 B per position over all its tables (249 GB
  for 204M), so that rate is about 8 GB a day; `df` showed 418 GB free at
  the second reading, 2 to 3 GB less than at the first.

### 3.2 Datasets

| Name | What | Positions | Edges |
|---|---|---:|---:|
| `ball` | the real root ball, ids in production's creation order (the smallest edge id reaching each position), real statuses, evals and backed values; positions on the rim become leaves again | 1,560,369 | 1,994,101 |
| `syn20m` | synthetic, seed 1 | 20,000,029 | 21,035,257 |
| `syn200m` | synthetic, seed 1 | 200,000,006 | 210,264,639 |

The generator grows a graph the way production grows: descend from the root
along the best line still standing (98.5% per ply, otherwise a random open
reply), expand the leaf, seed five replies, close mates with production's
frequencies and materialise their lines. Its output against the sample:

| | Production sample | `syn20m` | `syn200m` | `ball` (real, top of the tree) |
|---|---:|---:|---:|---:|
| Edges per position | 1.087 | 1.052 | 1.051 | 1.278 |
| Closed | 24.8% | 23.5% | 23.5% | 7.0% |
| Expanded | 2.9% | 2.9% | 2.9% | 5.2% |
| Has an eval | 14.5% | 14.8% | 15.2% | 26.8% |
| Open with a backed value | 2.1% | 1.7% | 1.8% | 1.0% |
| No move | 74.1% | 76.0% | 76.0% | 93.6% |
| Exactly one move | 22.2% | 21.1% | 21.1% | 1.2% |
| Moves of an expanded position (mean) | 28.7 | 29.0 | 29.0 | 24.3 |
| More than one parent | 5.4% | 4.9% | 4.8% | 24.1% |
| Depth of a searched position, p10 / p50 / p90 | 25 / 52 / 72 | 27 / 78 / 121 | 30 / 82 / 132 | at most 6 |
| Generation time | | 5.1 s | 124 s | |
| Full recompute of every status and backed value (one pass of the stand-in rules, one thread) | | 0.4 s | 9.0 s | |

The real ball is the top of the tree, so it is denser in transpositions
(24% of positions have two or more parents) and shallower than the graph as
a whole. The synthetic sets have the global shape and realistic depth.

## 4. Results on the real slice (1.56M positions)

All numbers: one writer thread, a durable commit per batch, the store's
files and the OS cache warm (everything fits in RAM at this size).

**Bulk load**

| Store | Seconds | Positions/s | Disk MB | Bytes/position |
|---|---:|---:|---:|---:|
| in memory + WAL + snapshot | 0.5 | 3,442,776 | 82 | 53 |
| mmap columns + WAL | 0.5 | 3,300,424 | 128 | 82 |
| LMDB | 2.2 | 718,780 | 170 | 109 |
| SQLite (WAL) | 2.4 | 657,771 | 150 | 96 |
| RocksDB | 3.7 | 419,915 | 168 | 108 |
| redb | 2.9 | 532,242 | 143 | 91 |
| fjall | 4.3 | 362,246 | 328 | 210 |
| PostgreSQL, integer ids | 4.9 | 319,113 | 492 | 315 |
| PostgreSQL, v1 schema | 17.8 | 87,582 | 1471 | 942 |

**Ingest, one search per durable commit** (latency of one batch, ms)

| Store | Searches/s | p50 | p95 | p99 | max | compute p50 | commit p50 | commit p99 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| in memory (no durability) | 11,809 | 0.05 | 0.08 | 0.12 | 17.3 | 0.04 | 0.01 | 0.02 |
| in memory + WAL + snapshot | 1,173 | 0.77 | 0.94 | 2.25 | 18.2 | 0.08 | 0.69 | 2.15 |
| mmap columns + WAL | 1,130 | 0.81 | 1.69 | 2.25 | 4.58 | 0.07 | 0.73 | 2.23 |
| LMDB | 240 | 1.94 | 12.9 | 21.9 | 30.0 | 0.24 | 1.71 | 21.5 |
| SQLite (WAL) | 222 | 4.13 | 6.26 | 9.13 | 133 | 1.69 | 2.43 | 4.49 |
| RocksDB | 550 | 1.70 | 3.05 | 4.72 | 10.3 | 0.84 | 0.75 | 2.26 |
| redb | 164 | 6.03 | 8.28 | 9.08 | 9.92 | 0.29 | 5.74 | 8.61 |
| fjall | 691 | 1.35 | 2.38 | 3.21 | 5.89 | 0.53 | 0.77 | 2.21 |
| PostgreSQL, integer ids | 121 | 8.60 | 11.8 | 14.5 | 16.2 | 5.90 | 2.64 | 5.27 |
| PostgreSQL, v1 schema | 70 | 15.0 | 20.2 | 28.0 | 47.2 | 9.83 | 4.95 | 8.59 |

Per search: 487 position reads, 29.8 edge-block reads, 12.2 parent reads, 36.5 key lookups, 34.8 new positions, 36.5 new edges, 9.8 rewrites, 0.41 closures, 9.31 backed-value writes, 2,488 logical bytes; 8.6 levels per batch.

**Ingest, 32 searches per durable commit** (latency of one batch, ms)

| Store | Searches/s | p50 | p95 | p99 | max | compute p50 | commit p50 | commit p99 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| in memory (no durability) | 35,461 | 0.89 | 1.10 | 1.32 | 1.42 | 0.67 | 0.21 | 0.32 |
| in memory + WAL + snapshot | 16,173 | 1.78 | 3.05 | 3.33 | 3.59 | 0.71 | 1.01 | 2.58 |
| mmap columns + WAL | 10,878 | 2.92 | 4.75 | 5.63 | 6.30 | 0.81 | 2.21 | 4.11 |
| LMDB | 1,064 | 23.5 | 58.1 | 168 | 277 | 3.65 | 19.7 | 164 |
| SQLite (WAL) | 568 | 46.4 | 188 | 218 | 219 | 29.7 | 15.8 | 186 |
| RocksDB | 1,393 | 22.6 | 30.8 | 36.4 | 39.8 | 19.0 | 3.50 | 5.14 |
| redb | 492 | 65.2 | 70.4 | 73.4 | 97.3 | 4.13 | 60.9 | 68.2 |
| fjall | 2,229 | 14.2 | 18.6 | 21.3 | 21.3 | 10.3 | 3.75 | 5.94 |
| PostgreSQL, integer ids | 514 | 60.9 | 76.5 | 82.6 | 87.1 | 44.2 | 15.6 | 36.2 |
| PostgreSQL, v1 schema | 208 | 150 | 190 | 206 | 219 | 98.7 | 49.9 | 101 |

Per search: 231 position reads, 13.1 edge-block reads, 6.9 parent reads, 36.4 key lookups, 34.7 new positions, 36.4 new edges, 4.7 rewrites, 0.39 closures, 4.11 backed-value writes, 2,320 logical bytes; 10.5 levels per batch.

**Re-search of interior positions, 32 per durable commit** (latency of one batch, ms)

| Store | Searches/s | p50 | p95 | p99 | max | compute p50 | commit p50 | commit p99 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| in memory (no durability) | 109,760 | 0.24 | 0.36 | 0.39 | 0.48 | 0.23 | 0.01 | 0.03 |
| in memory + WAL + snapshot | 26,383 | 0.94 | 1.91 | 2.47 | 2.51 | 0.24 | 0.70 | 2.23 |
| mmap columns + WAL | 23,297 | 1.04 | 2.10 | 2.61 | 3.01 | 0.27 | 0.77 | 2.36 |
| LMDB | 8,379 | 2.97 | 5.23 | 7.04 | 7.43 | 1.09 | 1.92 | 5.20 |
| SQLite (WAL) | 1,409 | 18.8 | 25.0 | 28.6 | 28.6 | 15.3 | 3.40 | 5.80 |
| RocksDB | 2,014 | 13.2 | 17.6 | 19.8 | 19.8 | 12.1 | 1.00 | 2.66 |
| redb | 2,655 | 9.39 | 16.0 | 20.8 | 21.2 | 1.50 | 7.92 | 18.6 |
| fjall | 4,029 | 6.52 | 8.85 | 9.79 | 10.7 | 5.53 | 0.90 | 2.51 |
| PostgreSQL, integer ids | 1,084 | 24.7 | 31.7 | 35.3 | 36.7 | 21.1 | 3.52 | 7.65 |
| PostgreSQL, v1 schema | 425 | 62.0 | 82.6 | 92.9 | 96.9 | 54.9 | 7.02 | 14.3 |

Per search: 174 position reads, 10.0 edge-block reads, 3.4 parent reads, 1.3 key lookups, 1.3 new positions, 1.3 new edges, 3.2 rewrites, 0.04 closures, 1.58 backed-value writes, 186 logical bytes; 7.2 levels per batch.

**Selector: descents from the root to a leaf**

| Store | Descents/s | p50 ms | p95 ms | p99 ms |
|---|---:|---:|---:|---:|
| in memory (no durability) | 788,022 | 0.00 | 0.00 | 0.00 |
| in memory + WAL + snapshot | 739,426 | 0.00 | 0.00 | 0.00 |
| mmap columns + WAL | 685,354 | 0.00 | 0.00 | 0.02 |
| LMDB | 60,228 | 0.01 | 0.02 | 0.06 |
| SQLite (WAL) | 1,838 | 0.55 | 0.62 | 0.70 |
| RocksDB | 4,731 | 0.21 | 0.25 | 0.39 |
| redb | 33,632 | 0.03 | 0.03 | 0.17 |
| fjall | 7,742 | 0.12 | 0.16 | 0.28 |
| PostgreSQL, integer ids | 796 | 1.23 | 1.48 | 1.72 |
| PostgreSQL, v1 schema | 486 | 1.98 | 2.50 | 3.25 |

**Explorer reads, 1 reader, nobody writing** (latency of one read, microseconds)

| Store | Reads/s | p50 | p95 | p99 | max |
|---|---:|---:|---:|---:|---:|
| in memory (no durability) | 701,260 | 1 | 2 | 2 | 43 |
| in memory + WAL + snapshot | 727,167 | 1 | 2 | 2 | 159 |
| mmap columns + WAL | 764,782 | 1 | 1 | 2 | 207 |
| LMDB | 98,227 | 10 | 17 | 21 | 334 |
| SQLite (WAL) | 43,034 | 25 | 32 | 38 | 230 |
| RocksDB | 16,428 | 68 | 103 | 145 | 1,077 |
| redb | 83,411 | 12 | 19 | 27 | 245 |
| fjall | 26,767 | 42 | 62 | 81 | 1,346 |
| PostgreSQL, integer ids | 1,232 | 842 | 990 | 1116 | 5,866 |
| PostgreSQL, v1 schema | 1,058 | 1004 | 1257 | 1393 | 7,984 |

**Explorer reads, 8 readers, nobody writing** (latency of one read, microseconds)

| Store | Reads/s | p50 | p95 | p99 | max |
|---|---:|---:|---:|---:|---:|
| in memory (no durability) | 1,616,524 | 4 | 7 | 8 | 279 |
| in memory + WAL + snapshot | 1,532,666 | 5 | 7 | 8 | 519 |
| mmap columns + WAL | 5,413,875 | 1 | 2 | 3 | 399 |
| LMDB | 791,312 | 10 | 17 | 20 | 321 |
| SQLite (WAL) | 266,886 | 30 | 47 | 58 | 447 |
| RocksDB | 103,508 | 82 | 152 | 196 | 438 |
| redb | 309,308 | 27 | 43 | 50 | 460 |
| fjall | 115,517 | 80 | 122 | 141 | 493 |
| PostgreSQL, integer ids | 7,919 | 1051 | 1270 | 1373 | 13,416 |
| PostgreSQL, v1 schema | 6,274 | 1366 | 1666 | 1831 | 22,675 |

**Explorer reads, 8 readers, while the writer ingests 32 searches per commit** (latency of one read, microseconds)

| Store | Reads/s | p50 | p95 | p99 | max | Writer searches/s | vs alone |
|---|---:|---:|---:|---:|---:|---:|---:|
| in memory (no durability) | 1,037,437 | 5 | 8 | 10 | 63,942 | 14,668 | 41% |
| in memory + WAL + snapshot | 1,178,349 | 5 | 8 | 10 | 61,165 | 9,749 | 60% |
| mmap columns + WAL | 3,976,122 | 1 | 2 | 4 | 1,542 | 9,479 | 87% |
| LMDB | 719,441 | 11 | 19 | 23 | 312 | 1,136 | 107% |
| SQLite (WAL) | 129,457 | 47 | 100 | 145 | 66,088 | 409 | 72% |
| RocksDB | 57,500 | 154 | 262 | 334 | 1,412 | 859 | 62% |
| redb | 325,228 | 25 | 41 | 49 | 911 | 453 | 92% |
| fjall | 117,302 | 77 | 128 | 149 | 428 | 1,365 | 61% |
| PostgreSQL, integer ids | 7,473 | 1098 | 1325 | 1433 | 15,928 | 422 | 82% |
| PostgreSQL, v1 schema | 6,059 | 1391 | 1689 | 1838 | 17,961 | 198 | 95% |

**Footprint and writes**

| Store | Positions | Disk MB | Bytes/position | Peak RSS MB | Private MB | Write amplification (ingest 32) | Open ms |
|---|---:|---:|---:|---:|---:|---:|---:|
| in memory (no durability) | 1,916,783 | 0 | 0 | 430 | 350 | 0.0 | 308.7 |
| in memory + WAL + snapshot | 1,916,783 | 100 | 52 | 341 | 334 | 7.0 | 220.2 |
| mmap columns + WAL | 1,916,783 | 155 | 81 | 520 | 12 | 17.0 | 0.7 |
| LMDB | 1,916,783 | 284 | 148 | 325 | 33 | 87.7 | 0.5 |
| SQLite (WAL) | 1,916,783 | 185 | 96 | 1275 | 298 | 126.0 | 1.2 |
| RocksDB | 1,916,783 | 220 | 115 | 252 | 234 | 3.0 | 44.7 |
| redb | 1,916,783 | 285 | 149 | 298 | 273 | 93.2 | 6.3 |
| fjall | 1,916,783 | 210 | 110 | 368 | 370 | 2.2 | 1032.8 |
| PostgreSQL, integer ids | 1,916,783 | 654 | 341 | 17 | 7 | 43.9 | 25.0 |
| PostgreSQL, v1 schema | 1,916,783 | 2082 | 1086 | 19 | 8 | 171.4 | 23.8 |

State hash after the suite: **identical for all 10 stores** (`999599d0a7ea73c47100480e089180b7`).

## 5. Results at scale

### 5.1 20M positions (synthetic)

**Bulk load**

| Store | Seconds | Positions/s | Disk MB | Bytes/position |
|---|---:|---:|---:|---:|
| in memory + WAL + snapshot | 7.0 | 2,850,665 | 978 | 49 |
| mmap columns + WAL | 5.0 | 4,034,691 | 1549 | 78 |
| LMDB | 27.6 | 723,746 | 2121 | 106 |
| SQLite (WAL) | 32.9 | 607,594 | 1920 | 96 |
| RocksDB | 41.1 | 486,534 | 2092 | 105 |
| redb | 42.0 | 476,589 | 2282 | 114 |
| fjall | 55.5 | 360,538 | 2236 | 112 |
| PostgreSQL, integer ids | 56.4 | 354,870 | 5798 | 290 |
| PostgreSQL, v1 schema | 206.9 | 96,652 | 16947 | 847 |

**Ingest, one search per durable commit** (latency of one batch, ms)

| Store | Searches/s | p50 | p95 | p99 | max | compute p50 | commit p50 | commit p99 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| in memory (no durability) | 1,534 | 0.04 | 0.06 | 0.08 | 307 | 0.03 | 0.01 | 0.02 |
| in memory + WAL + snapshot | 782 | 0.75 | 0.92 | 2.25 | 239 | 0.06 | 0.70 | 2.20 |
| mmap columns + WAL | 1,142 | 0.81 | 1.24 | 2.39 | 2.63 | 0.05 | 0.75 | 2.33 |
| LMDB | 154 | 4.79 | 19.2 | 27.0 | 35.9 | 0.17 | 4.60 | 26.7 |
| SQLite (WAL) | 228 | 3.43 | 4.74 | 5.88 | 496 | 1.05 | 2.41 | 3.84 |
| RocksDB | 809 | 1.18 | 1.75 | 2.51 | 3.65 | 0.39 | 0.78 | 2.01 |
| redb | 137 | 7.11 | 9.50 | 11.4 | 12.7 | 0.19 | 6.94 | 11.2 |
| fjall | 900 | 1.05 | 1.92 | 2.65 | 3.99 | 0.27 | 0.76 | 2.37 |
| PostgreSQL, integer ids | 127 | 7.78 | 10.5 | 13.6 | 24.5 | 3.97 | 3.81 | 7.83 |
| PostgreSQL, v1 schema | 38 | 23.7 | 48.0 | 58.8 | 67.7 | 9.40 | 14.1 | 38.6 |

Per search: 209 position reads, 10.8 edge-block reads, 8.4 parent reads, 36.6 key lookups, 34.9 new positions, 36.6 new edges, 6.2 rewrites, 0.40 closures, 5.59 backed-value writes, 2,379 logical bytes; 6.0 levels per batch.

**Ingest, 32 searches per durable commit** (latency of one batch, ms)

| Store | Searches/s | p50 | p95 | p99 | max | compute p50 | commit p50 | commit p99 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| in memory (no durability) | 43,403 | 0.73 | 0.84 | 0.97 | 1.00 | 0.49 | 0.24 | 0.36 |
| in memory + WAL + snapshot | 16,543 | 1.74 | 3.01 | 3.23 | 3.45 | 0.57 | 1.12 | 2.60 |
| mmap columns + WAL | 8,744 | 3.68 | 5.27 | 5.87 | 6.05 | 0.64 | 3.04 | 5.08 |
| LMDB | 293 | 98.5 | 221 | 363 | 416 | 3.54 | 93.9 | 360 |
| SQLite (WAL) | 433 | 35.6 | 667 | 699 | 703 | 19.3 | 16.3 | 678 |
| RocksDB | 3,240 | 9.77 | 12.3 | 13.4 | 14.4 | 6.93 | 2.65 | 4.57 |
| redb | 314 | 102 | 113 | 125 | 128 | 3.51 | 98.5 | 121 |
| fjall | 3,534 | 9.16 | 11.1 | 12.0 | 12.2 | 5.16 | 3.87 | 6.08 |
| PostgreSQL, integer ids | 514 | 60.4 | 78.0 | 81.9 | 83.2 | 40.1 | 20.3 | 31.2 |
| PostgreSQL, v1 schema | 89 | 372 | 498 | 626 | 628 | 136 | 224 | 374 |

Per search: 108 position reads, 6.5 edge-block reads, 5.4 parent reads, 36.3 key lookups, 34.7 new positions, 36.3 new edges, 3.2 rewrites, 0.39 closures, 2.62 backed-value writes, 2,271 logical bytes; 8.4 levels per batch.

**Re-search of interior positions, 32 per durable commit** (latency of one batch, ms)

| Store | Searches/s | p50 | p95 | p99 | max | compute p50 | commit p50 | commit p99 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| in memory (no durability) | 124,876 | 0.15 | 0.24 | 0.27 | 0.28 | 0.14 | 0.01 | 0.05 |
| in memory + WAL + snapshot | 20,114 | 0.91 | 1.77 | 2.17 | 2.52 | 0.18 | 0.72 | 2.08 |
| mmap columns + WAL | 16,704 | 1.08 | 2.13 | 2.61 | 2.92 | 0.20 | 0.86 | 2.35 |
| LMDB | 6,033 | 3.05 | 5.84 | 6.62 | 8.51 | 0.70 | 2.40 | 5.58 |
| SQLite (WAL) | 1,313 | 12.8 | 19.9 | 29.7 | 433 | 8.96 | 3.56 | 17.4 |
| RocksDB | 3,080 | 6.35 | 9.10 | 9.99 | 10.4 | 5.49 | 0.85 | 2.40 |
| redb | 1,593 | 12.0 | 20.8 | 23.4 | 27.4 | 0.92 | 11.0 | 22.1 |
| fjall | 5,624 | 3.46 | 4.96 | 5.83 | 6.99 | 2.33 | 1.03 | 2.56 |
| PostgreSQL, integer ids | 1,024 | 18.5 | 30.1 | 36.3 | 36.9 | 14.1 | 4.29 | 14.8 |
| PostgreSQL, v1 schema | 300 | 59.6 | 109 | 151 | 172 | 38.5 | 20.4 | 54.7 |

Per search: 133 position reads, 6.6 edge-block reads, 3.6 parent reads, 2.7 key lookups, 2.7 new positions, 2.7 new edges, 4.3 rewrites, 0.04 closures, 1.73 backed-value writes, 307 logical bytes; 5.9 levels per batch.

**Selector: descents from the root to a leaf**

| Store | Descents/s | p50 ms | p95 ms | p99 ms |
|---|---:|---:|---:|---:|
| in memory (no durability) | 1,020,669 | 0.00 | 0.00 | 0.00 |
| in memory + WAL + snapshot | 865,614 | 0.00 | 0.00 | 0.00 |
| mmap columns + WAL | 858,443 | 0.00 | 0.00 | 0.01 |
| LMDB | 61,192 | 0.02 | 0.02 | 0.03 |
| SQLite (WAL) | 2,058 | 0.49 | 0.57 | 0.75 |
| RocksDB | 5,466 | 0.18 | 0.19 | 0.35 |
| redb | 41,798 | 0.02 | 0.03 | 0.07 |
| fjall | 8,345 | 0.12 | 0.13 | 0.57 |
| PostgreSQL, integer ids | 1,126 | 0.89 | 1.08 | 1.29 |
| PostgreSQL, v1 schema | 538 | 1.48 | 1.91 | 20.0 |

**Explorer reads, 1 reader, nobody writing** (latency of one read, microseconds)

| Store | Reads/s | p50 | p95 | p99 | max |
|---|---:|---:|---:|---:|---:|
| in memory (no durability) | 615,617 | 1 | 2 | 2 | 24 |
| in memory + WAL + snapshot | 600,487 | 1 | 2 | 2 | 311 |
| mmap columns + WAL | 669,535 | 1 | 2 | 4 | 85 |
| LMDB | 83,475 | 12 | 21 | 26 | 91 |
| SQLite (WAL) | 36,150 | 28 | 42 | 49 | 326 |
| RocksDB | 10,430 | 100 | 175 | 217 | 5,811 |
| redb | 41,949 | 19 | 54 | 95 | 206 |
| fjall | 14,262 | 70 | 139 | 181 | 3,441 |
| PostgreSQL, integer ids | 1,179 | 859 | 1110 | 1438 | 17,007 |
| PostgreSQL, v1 schema | 157 | 7010 | 13481 | 15510 | 21,798 |

**Explorer reads, 8 readers, nobody writing** (latency of one read, microseconds)

| Store | Reads/s | p50 | p95 | p99 | max |
|---|---:|---:|---:|---:|---:|
| in memory (no durability) | 1,517,166 | 5 | 7 | 9 | 500 |
| in memory + WAL + snapshot | 1,571,465 | 5 | 7 | 9 | 296 |
| mmap columns + WAL | 4,591,600 | 1 | 2 | 3 | 333 |
| LMDB | 711,844 | 12 | 19 | 23 | 334 |
| SQLite (WAL) | 223,715 | 34 | 61 | 77 | 537 |
| RocksDB | 94,882 | 88 | 166 | 218 | 692 |
| redb | 368,879 | 22 | 35 | 43 | 529 |
| fjall | 120,720 | 72 | 130 | 161 | 4,753 |
| PostgreSQL, integer ids | 6,910 | 1177 | 1614 | 1841 | 17,388 |
| PostgreSQL, v1 schema | 1,727 | 3727 | 10380 | 12795 | 22,952 |

**Explorer reads, 8 readers, while the writer ingests 32 searches per commit** (latency of one read, microseconds)

| Store | Reads/s | p50 | p95 | p99 | max | Writer searches/s | vs alone |
|---|---:|---:|---:|---:|---:|---:|---:|
| in memory (no durability) | 1,214,199 | 5 | 8 | 11 | 540 | 25,643 | 59% |
| in memory + WAL + snapshot | 1,350,907 | 5 | 8 | 9 | 622 | 13,053 | 79% |
| mmap columns + WAL | 2,323,057 | 1 | 7 | 19 | 1,949 | 7,273 | 83% |
| LMDB | 635,322 | 13 | 22 | 25 | 334 | 628 | 214% |
| SQLite (WAL) | 92,346 | 56 | 111 | 151 | 623,761 | 340 | 79% |
| RocksDB | 48,224 | 177 | 333 | 430 | 1,210 | 1,682 | 52% |
| redb | 350,832 | 23 | 36 | 43 | 56,357 | 300 | 96% |
| fjall | 112,474 | 78 | 137 | 162 | 425 | 2,483 | 70% |
| PostgreSQL, integer ids | 6,162 | 1270 | 1847 | 2147 | 23,146 | 403 | 78% |
| PostgreSQL, v1 schema | 3,525 | 2170 | 3853 | 6073 | 68,916 | 107 | 121% |

**Footprint and writes**

| Store | Positions | Disk MB | Bytes/position | Peak RSS MB | Private MB | Write amplification (ingest 32) | Open ms |
|---|---:|---:|---:|---:|---:|---:|---:|
| in memory (no durability) | 20,360,850 | 0 | 0 | 3943 | 3391 | 0.0 | 5817.7 |
| in memory + WAL + snapshot | 20,360,850 | 996 | 49 | 2850 | 3227 | 68.8 | 3543.7 |
| mmap columns + WAL | 20,360,850 | 1577 | 78 | 1906 | 14 | 43.6 | 0.8 |
| LMDB | 20,360,850 | 2782 | 137 | 2836 | 50 | 169.1 | 0.6 |
| SQLite (WAL) | 20,360,850 | 1946 | 96 | 11934 | 784 | 139.0 | 1.3 |
| RocksDB | 20,360,850 | 2145 | 105 | 2400 | 2427 | 3.0 | 62.0 |
| redb | 20,360,850 | 2282 | 112 | 2767 | 2793 | 161.6 | 5.9 |
| fjall | 20,360,850 | 1921 | 94 | 2164 | 2226 | 2.2 | 1675.5 |
| PostgreSQL, integer ids | 20,360,850 | 5895 | 290 | 16 | 7 | 125.2 | 22.0 |
| PostgreSQL, v1 schema | 20,360,850 | 17247 | 847 | 18 | 8 | 544.5 | 27.2 |

State hash after the suite: **identical for all 5 stores** (`7334b341333292f4ff353b64de480706`).

### 5.2 200M positions (synthetic, production's size)

A 200M-position store is 15 to 23 GB, and this machine had between 8 and 20
GB of memory to spare depending on the moment, so at this size the result
depends on whether the store fits. The runs below note the memory available
(Windows' "Available MBytes") just before each suite:

| Run | Available before the suite | Store size |
|---|---:|---:|
| mmap columns + WAL | 19.6 GB | 15.5 GB |
| mmap columns + WAL, key filter off | 19.9 GB | 15.2 GB |
| LMDB | 19.8 GB | 21.2 GB after load, 22.3 GB after the suite |
| SQLite | 19.2 GB | 19.5 GB |
| RocksDB | 19.7 GB | 20.8 GB |
| mmap columns + WAL, first run | about 8 GB (right after generating the dataset) | 15.2 GB |

So in the first five rows the mmap store fits in memory and the other three
do not, by a few GB. That is a consequence of bytes per position, but where
exactly the line falls is this machine's RAM, not a law: production has 64
GB and would hold any of them today. The last row is the mmap store when it
does not fit either. The 20 GB runs used shorter stages (300 single
landings, 100 batches of 32, 100 re-search batches) than the first run (500,
200, 200).

The key filter (a blocked Bloom filter in front of the key map, 10 bits per
position) was added after the first run showed 18.5 ms of compute per batch,
almost all of it page faults on key-map lookups of positions that did not
exist yet. With 20 GB available it makes no difference, as the first two
rows show. Its effect when memory is short was not measured: recreating that
condition with a memory ballast drove the machine to its commit limit and
was abandoned.

**Bulk load**

| Store | Seconds | Positions/s | Disk MB | Bytes/position |
|---|---:|---:|---:|---:|
| mmap columns + WAL | 69.7 | 2,869,364 | 15489 | 77 |
| mmap columns + WAL, key filter off | 27.9 | 7,172,794 | 15239 | 76 |
| LMDB | 283.2 | 706,278 | 21208 | 106 |
| SQLite (WAL) | 325.9 | 613,777 | 19478 | 97 |
| RocksDB | 420.1 | 476,042 | 20846 | 104 |
| mmap columns + WAL, 8 GB available | 41.3 | 4,842,849 | 15239 | 76 |

**Ingest, one search per durable commit** (latency of one batch, ms)

| Store | Searches/s | p50 | p95 | p99 | max | compute p50 | commit p50 | commit p99 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| mmap columns + WAL | 1,012 | 0.81 | 1.93 | 3.78 | 11.6 | 0.05 | 0.77 | 3.74 |
| mmap columns + WAL, key filter off | 1,154 | 0.81 | 0.97 | 3.65 | 4.47 | 0.06 | 0.75 | 3.61 |
| LMDB | 118 | 6.38 | 23.1 | 40.2 | 51.0 | 0.48 | 5.65 | 38.7 |
| SQLite (WAL) | 258 | 3.80 | 5.52 | 7.19 | 8.40 | 1.32 | 2.55 | 4.15 |
| RocksDB | 542 | 1.68 | 3.23 | 4.49 | 5.74 | 0.91 | 0.75 | 2.35 |
| mmap columns + WAL, 8 GB available | 693 | 1.33 | 2.19 | 4.50 | 5.61 | 0.58 | 0.73 | 3.65 |

Per search: 221 position reads, 11.0 edge-block reads, 8.3 parent reads, 35.5 key lookups, 33.9 new positions, 35.5 new edges, 6.1 rewrites, 0.41 closures, 5.45 backed-value writes, 2,311 logical bytes; 6.0 levels per batch.

**Ingest, 32 searches per durable commit** (latency of one batch, ms)

| Store | Searches/s | p50 | p95 | p99 | max | compute p50 | commit p50 | commit p99 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| mmap columns + WAL | 4,064 | 7.65 | 15.2 | 16.3 | 16.3 | 1.16 | 6.65 | 15.2 |
| mmap columns + WAL, key filter off | 3,957 | 7.35 | 14.6 | 21.1 | 21.1 | 2.02 | 5.38 | 19.0 |
| LMDB | 158 | 195 | 317 | 514 | 514 | 11.3 | 182 | 496 |
| SQLite (WAL) | 336 | 46.5 | 845 | 870 | 870 | 27.0 | 19.6 | 840 |
| RocksDB | 1,466 | 21.5 | 30.9 | 35.2 | 35.2 | 18.4 | 2.95 | 5.29 |
| mmap columns + WAL, 8 GB available | 1,175 | 21.0 | 129 | 161 | 163 | 18.5 | 2.12 | 10.5 |

Per search: 115 position reads, 6.5 edge-block reads, 5.5 parent reads, 36.5 key lookups, 34.9 new positions, 36.5 new edges, 3.2 rewrites, 0.41 closures, 2.60 backed-value writes, 2,281 logical bytes; 7.8 levels per batch.

**Re-search of interior positions, 32 per durable commit** (latency of one batch, ms)

| Store | Searches/s | p50 | p95 | p99 | max | compute p50 | commit p50 | commit p99 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| mmap columns + WAL | 16,347 | 1.06 | 1.55 | 4.31 | 4.31 | 0.20 | 0.85 | 4.21 |
| mmap columns + WAL, key filter off | 16,506 | 1.00 | 1.78 | 3.95 | 3.95 | 0.23 | 0.76 | 3.77 |
| LMDB | 4,509 | 4.02 | 7.35 | 10.4 | 10.4 | 0.86 | 2.98 | 7.61 |
| SQLite (WAL) | 1,534 | 11.5 | 20.5 | 32.8 | 32.8 | 7.87 | 3.35 | 15.0 |
| RocksDB | 2,598 | 6.85 | 10.8 | 17.9 | 17.9 | 5.68 | 0.87 | 9.69 |
| mmap columns + WAL, 8 GB available | 13,382 | 1.33 | 3.61 | 5.66 | 5.93 | 0.56 | 0.76 | 3.62 |

Per search: 128 position reads, 6.3 edge-block reads, 3.6 parent reads, 2.2 key lookups, 2.2 new positions, 2.2 new edges, 4.2 rewrites, 0.03 closures, 1.77 backed-value writes, 271 logical bytes; 5.5 levels per batch.

**Selector: descents from the root to a leaf**

| Store | Descents/s | p50 ms | p95 ms | p99 ms |
|---|---:|---:|---:|---:|
| mmap columns + WAL | 563,555 | 0.00 | 0.00 | 0.01 |
| mmap columns + WAL, key filter off | 624,239 | 0.00 | 0.00 | 0.01 |
| LMDB | 17,454 | 0.02 | 0.03 | 1.22 |
| SQLite (WAL) | 2,103 | 0.42 | 0.58 | 1.74 |
| RocksDB | 4,639 | 0.16 | 0.23 | 1.97 |
| mmap columns + WAL, 8 GB available | 59,570 | 0.00 | 0.00 | 0.58 |

**Explorer reads, 1 reader, nobody writing** (latency of one read, microseconds)

| Store | Reads/s | p50 | p95 | p99 | max |
|---|---:|---:|---:|---:|---:|
| mmap columns + WAL | 193,319 | 4 | 10 | 14 | 1,374 |
| mmap columns + WAL, key filter off | 196,585 | 4 | 10 | 14 | 2,548 |
| LMDB | 1,515 | 619 | 1534 | 2024 | 3,171 |
| SQLite (WAL) | 1,199 | 743 | 1771 | 2314 | 5,879 |
| RocksDB | 1,162 | 876 | 1516 | 1914 | 21,221 |
| mmap columns + WAL, 8 GB available | 3,639 | 164 | 839 | 1077 | 2,853 |

**Explorer reads, 8 readers, nobody writing** (latency of one read, microseconds)

| Store | Reads/s | p50 | p95 | p99 | max |
|---|---:|---:|---:|---:|---:|
| mmap columns + WAL | 3,261,758 | 2 | 4 | 6 | 364 |
| mmap columns + WAL, key filter off | 3,411,730 | 2 | 4 | 5 | 381 |
| LMDB | 17,513 | 402 | 1108 | 1435 | 2,347 |
| SQLite (WAL) | 16,812 | 417 | 1106 | 1451 | 5,659 |
| RocksDB | 11,703 | 634 | 1333 | 1602 | 3,114 |
| mmap columns + WAL, 8 GB available | 37,679 | 17 | 769 | 1054 | 3,400 |

**Explorer reads, 8 readers, while the writer ingests 32 searches per commit** (latency of one read, microseconds)

| Store | Reads/s | p50 | p95 | p99 | max | Writer searches/s | vs alone |
|---|---:|---:|---:|---:|---:|---:|---:|
| mmap columns + WAL | 578,153 | 10 | 27 | 36 | 1,953 | 2,894 | 71% |
| mmap columns + WAL, key filter off | 668,785 | 9 | 24 | 32 | 1,281 | 3,160 | 80% |
| LMDB | 13,572 | 496 | 1480 | 2109 | 6,207 | 53 | 34% |
| SQLite (WAL) | 11,030 | 484 | 1232 | 1866 | 221,693 | 349 | 104% |
| RocksDB | 10,197 | 733 | 1451 | 1754 | 12,482 | 1,478 | 101% |
| mmap columns + WAL, 8 GB available | 44,951 | 24 | 630 | 981 | 4,388 | 749 | 64% |

**Footprint and writes**

| Store | Positions | Disk MB | Bytes/position | Peak RSS MB | Private MB | Write amplification (ingest 32) | Open ms |
|---|---:|---:|---:|---:|---:|---:|---:|
| mmap columns + WAL | 200,236,800 | 15507 | 77 | 16664 | 49 | 99.5 | 1.0 |
| mmap columns + WAL, key filter off | 200,236,800 | 15257 | 76 | 16340 | 48 | 67.4 | 1.1 |
| LMDB | 200,236,800 | 22255 | 111 | 13855 | 191 | 216.8 | 5.1 |
| SQLite (WAL) | 200,236,800 | 19499 | 97 | 2705 | 1293 | 143.9 | 5.7 |
| RocksDB | 200,236,800 | 20883 | 104 | 2455 | 2464 | 3.0 | 240.4 |
| mmap columns + WAL, 8 GB available | 200,360,506 | 15266 | 76 | 4545 | 51 | 66.3 | 1.0 |

In the first run the mmap store and LMDB each also computed the state hash
over all 200M positions after the suite: identical
(`d680448dbce091c13563a6a7e44387c7`). The first run's rows for LMDB, RocksDB
and SQLite (same pattern, slightly lower numbers) are in
`results/L-syn200m-first-run.jsonl`.

In-memory with snapshots was not run at this size: with plain vectors and a
hash map it needs about 32 GB of private memory.

### 5.3 A long run, and what a checkpoint costs

The stages above are short (6,400 landings), so the mmap store never reached
its checkpoint threshold inside a timed stage. This run does: 6,000 batches
of 32 (192,000 landings, 6.7 million new positions) on the 20M dataset.

| Store | Searches/s | Batch p50 ms | p99 ms | max ms | Bytes written / bytes changed |
|---|---:|---:|---:|---:|---:|
| mmap columns + WAL | 11,323 | 2.54 | 4.88 | 659 | 8.1 |
| in memory + WAL + snapshot | 15,711 | 1.76 | 5.07 | 207 | 3.7 (one snapshot at the end) |
| fjall | 2,361 | 13.3 | 20.8 | 136 | 6.4 |
| RocksDB | 1,942 | 16.6 | 25.0 | 70 | 3.9 |

The mmap store's 659 ms is a checkpoint: this harness flushes the maps on
the writer thread. Three checkpoints took 1.9 s in total and wrote 702,000
pages (2.9 GB); the log itself was 664 MB, 1.5 times the 437 MB of record
bytes that changed. Two things follow for the plan. The checkpoint must run
off the writer thread (the log protects the data until the flush is done, so
nothing forces the writer to wait). And most of those pages are the key map:
35 random slots per landing touch 35 different pages, so every checkpoint
rewrites nearly the whole table. At production's size and rate that is about
5 GB per checkpoint, a checkpoint every several hours, and 10 to 30 GB of
writes a day: harmless next to what Postgres writes today (`iostat` showed
19 MB/s), but it is the first thing to restructure if write volume ever
matters (a sorted base plus a small in-memory delta).

The "write amplification" column of the footprint tables in sections 4 and
5 includes one forced checkpoint (or snapshot, or flush) at the end of a
200-batch stage, so for the stores that write whole pages or whole
snapshots it overstates the steady state. The table above is the better
figure for the mmap store; for the B-tree stores the per-commit page writes
are the steady state.

## 6. Crash recovery

A child process runs the writer (three ingest batches of 8, then one batch
of re-searches, repeating) and prints `ACK <seq>` after each durable commit.
The parent waits for at least one acknowledgement, sleeps a random 0.2 to
2.5 s, and kills the child with `TerminateProcess` (for Postgres it kills the
server's whole process tree and restarts it with `pg_ctl`). It then reopens
the store, times the open, and checks:

- the recovered sequence number is at least the last one acknowledged
  (nothing durable was lost) and at most one more (the batch in flight);
- the hash of the whole state equals that of an in-memory reference that
  applied exactly the recovered number of batches.

Trials are cumulative: each one continues from the state the previous
recovery left, so the test also covers recovering a store that has been
recovered before. For the mmap store the checkpoint threshold was lowered to
2 MB so that kills land in checkpoints as well as in commits.

| Store | Trials | Clean | Lost an acknowledged batch | State differs | Recovery ms p50 | p95 | max | Batches by the end |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| mmap columns + WAL | 60 | 60 | 0 | 0 | 57.9 | 64.2 | 65.5 | 20,014 |
| in memory + WAL + snapshot | 30 | 30 | 0 | 0 | 1879.6 | 3376.7 | 3470.8 | 43,202 |
| LMDB | 30 | 30 | 0 | 0 | 6.2 | 7.3 | 7.4 | 6,508 |
| redb | 30 | 30 | 0 | 0 | 251.8 | 294.6 | 320.8 | 2,247 |
| RocksDB | 15 | 15 | 0 | 0 | 135.2 | 166.4 | 166.4 | 1,588 |
| fjall | 15 | 15 | 0 | 0 | 1936.9 | 3580.1 | 3580.1 | 2,867 |
| SQLite (WAL) | 8 | 8 | 0 | 0 | 52.6 | 70.8 | 70.8 | 435 |
| PostgreSQL, integer ids | 4 | 4 | 0 | 0 | 1299.3 | 2757.7 | 2757.7 | 84 |

With the mmap store's production threshold (checkpoint at 256 MB of log) and
kills after 3 to 12 seconds of writing, so that up to a full log has to be
replayed: 5 trials, all clean, recovery between 102 and 343 ms
(`results/S-real-ball-crash-biglog.jsonl`; the run then stopped because the
store had been created with too little spare capacity for that many
batches). An earlier build of the mmap store, before the key filter, passed
another 60 trials.

The in-memory store's 1.9 s is the replay of its whole log, because the
crash writer never takes a snapshot: 43,000 batches (345,000 landings) in
3.5 s, which is also a measure of how fast a history can be replayed.
PostgreSQL's time is the restart of the server. The v1-schema database was
not crash-tested: it is the same server and the same write-ahead log.

What this does **not** test: a power cut. Killing a process leaves the OS
alive, so its cache still reaches the disk; a store that forgot an fsync
would pass. What stands behind the durability of each store here is its
setting, listed in 2.2. A power-cut test (a VM with a
write-dropping block device, or pulling the plug on a spare box) belongs in
phase 1 of the plan.

## 7. Linux cross-check

**Not run.** The plan was to repeat the suite and the crash test for the
embedded stores inside a Linux container on the same machine
(`scripts/run_linux.sh`, with the stores on a native ext4 volume). Docker
Desktop on the test machine was stuck in a "stopping" state, a restart from
its CLI did not bring the engine back, and forcing it was not this
benchmark's call. No other Linux machine was available that is not
production.

So everything in this report is a Windows measurement. That matters for some
numbers and not for others:

| Result | Depends on the OS? |
|---|---|
| Reads from a warm store (selector, explorer, the compute part of a batch) | No. They are memory reads and CPU; the mmap store makes no system call on this path, the others make the same calls on both systems |
| The mmap store's commit | One sequential append and one `fdatasync` instead of `FlushFileBuffers`. The latency of that call on production's md RAID1 is the number to measure first |
| LMDB's and redb's commit | Probably much better on Linux. Their commit writes a few thousand scattered 4 KB pages; LMDB does it through a write-through handle on Windows and through `pwritev` plus one `fdatasync` on Linux. Their ingest numbers here should be read as a lower bound |
| SQLite, RocksDB, fjall, Postgres | Sequential log writes plus an fsync on both systems; no reason to expect a different order |
| Crash recovery | The logic is the same; the file-system guarantees it relies on (a completed fsync is durable, a torn tail is detectable) hold on both |
| Bulk load | NTFS zero-fills when a mapped file is first written beyond its valid length; ext4 does not. The mmap load should be no slower on Linux |

The first gate of the plan (phase 0) is therefore to run `run_linux.sh` on a
clone of the production box before any store code is written. The margin
that decides the race is on the read side, where the OS does not take part.

## 8. What these numbers are and are not

- **Windows, not Linux.** Production is Linux on ext4 over md RAID1. The CPU
  work (reads from memory, the climb) transfers directly. The cost of a
  durable commit does not: here one `FlushFileBuffers` on this NVMe is about
  0.7 ms, and every durable store pays at least one per batch. LMDB and redb
  write their dirty pages one by one and grow their file as they go, which
  is cheap on ext4 and not on NTFS. Section 7 lists which results depend on
  this.
- **Mapped files on Linux.** The mmap store only writes a page after the
  frame that describes the change is on disk, so the kernel flushing a dirty
  page early cannot expose an uncommitted batch. Things to verify on the
  production kernel in phase 1: `msync` time at checkpoint with many dirty
  pages, writer stalls when `vm.dirty_ratio` is reached, and the behaviour
  under memory pressure when the maps are larger than RAM.
- **Defender was on** and the desktop was in use. Runs were not repeated
  enough to give confidence intervals; differences under about 20% between
  stores mean nothing. The conclusions rest on differences of 3x to 1000x.
- **Everything was cached at 1.56M and 20M.** That flatters every store, and
  Postgres most of all: in production its 190 GB do not fit in 64 GB, which
  is where the drainer's read wait comes from. The 200M runs are the ones
  where the data stops fitting.
- **The rules are stand-ins.** The real rules read more per position (the
  showcase of positions with gaps, walks along returning lines). That raises
  the compute part for every store alike and makes cheap reads matter more,
  not less.
- **One writer.** That is the design of the plan, not a limitation of the
  test: no store was asked to serve concurrent writers.
- **Tuning.** Each store was configured sensibly, not exhaustively. LMDB was
  not tried with `MDB_WRITEMAP`, RocksDB not with direct I/O or a hash
  index, SQLite not with `synchronous=NORMAL` (which gives up durability of
  the last commits on power loss).

## 9. Reproduce

Everything is in `atomicdb-v2/bench/`; its README has the build line.

```
# datasets
atomicdb-v2-bench gen --out data/syn20m  --nodes 20000000  --seed 1
atomicdb-v2-bench gen --out data/syn200m --nodes 200000000 --seed 1
atomicdb-v2-bench import --out data/ball --pos ball_pos.csv --edges ball_edges.csv

# every store on one dataset (PowerShell; scripts/run_linux.sh is the Linux equivalent)
scripts/run_scale.ps1 -Exe <exe> -Data data/ball -Work work -Out results/S.jsonl -PgBin <postgres bin>
scripts/run_crash.ps1 -Exe <exe> -Data data/ball -Work work -Out results/S-crash.jsonl -PgBin <bin> -PgData <data dir>

# tables of this report
python scripts/report_tables.py results/S-real-ball.jsonl --crash results/S-real-ball-crash.jsonl
```

The synthetic datasets are deterministic: the same `--nodes` and `--seed`
give the same files, and the suite gives the same state hash.

The real slice is not in the repository. It is reproduced with the queries in
`atomicdb-v2/bench/sql/` (`ball_edges.sql`, `ball_pos.sql`, `sample.sql`,
`landings.sql`, `depths.sql`), each run as
`psql -X -q -f file.sql | zstd > out.csv.zst` on a **restored backup** (they
were run once on production for this report, read-only, outside the backup
window; a copy is the right place from now on). `scripts/analyze_sample.py`,
`analyze_ball_edges.py` and `analyze_ball_pos.py` print the figures of
section 3.1.

Raw results of the runs quoted here: `results/S-real-ball.jsonl`,
`results/S-real-ball-crash.jsonl`, `results/M-syn20m.jsonl`,
`results/L-syn200m.jsonl` and the extra runs named in the text.

Software fetched for the benchmark, all into a scratch directory: the Rust
toolchain (rustup), the crates in `Cargo.lock`, the `libclang` wheel from
PyPI (RocksDB's bindings need it at build time) and the PostgreSQL 16.15
Windows binaries from EnterpriseDB.
