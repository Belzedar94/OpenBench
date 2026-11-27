# AtomicDB graph engine in Rust: scoping document

Status: **draft for discussion** (27-Nov-2026). No code, no production change.
Direction approved by the owner on Discord the same day: *"If we think a full
rust rewrite is the ideal state, we go for it"*.

This document says what we would move, in what order, how we would know the
new engine is right, how we would move 200M rows without stopping the site,
and what it would honestly cost. It ends with the questions we need answered
before any code is written.

Contents

1. Inventory (from the code, plus read-only production numbers)
2. Target architecture and the service boundary
3. The store race: custom mmap columns, LMDB, SQLite, RocksDB
4. Correctness strategy
5. Migrating the rows
6. Measurement plan
7. Phased plan, effort, risks and gates
8. Open questions

---

## 0. Summary

- **What hurts today.** The single drainer (`process_propagation`) spends about
  88% of its time waiting on Postgres reads, one random 8 KB page at a time,
  to walk a graph whose hot part is a few bytes per node. `atomicdb_position`
  plus `atomicdb_edge` are about **190 GB on disk** (tables plus indexes); the
  columns the climbs actually read would fit in **roughly 8-12 GB** with
  integer ids. That gap is the whole case for the port.
- **Recommendation.** Build a Rust **graph service** that owns the graph
  columns in its own store, is the **only writer** of those columns, and keeps
  Postgres as the home of everything relational (accounts, tasks, events,
  pages, the JSON analysis blobs, the won lines). Move in this order: store,
  climbs, ingest stage (a), selector descent, and only then (optionally) the
  explorer reads. Leases stay on Postgres.
- **Django talks to it over gRPC on a Unix socket**; a read-only PyO3 binding
  that maps the store directly is an optimisation to add only if measurements
  ask for it.
- **The store is decided by a race, not by taste**: custom mmap columns
  (MoKy's design), LMDB, SQLite (WAL, `WITHOUT ROWID`, integer keys) and
  RocksDB, all driven by the same Rust climb over the same replay of real
  landings. Custom columns must beat LMDB and SQLite clearly to justify the
  extra code we would own forever.
- **Correctness**: the existing Python test suite runs against the Rust climbs
  through a shim, a golden replay from a restored backup, and a lockstep
  shadow fed by Postgres logical decoding that diffs every drainer batch.
- **Measurement**: a baseline of today's Python/Postgres system is captured
  before anything changes, and the same metrics, on the same replay
  workloads, are recorded at every gate and in shadow, with a before/after
  report per phase (section 6).
- **Effort, honestly**: about **8-12 months of focused work** for climbs plus
  ingest plus selector, with the climbs alone (the part that fixes today's
  bottleneck) at **5-7 months** including measurement and shadow time. The biggest risk is not
  Rust: it is that the semantics we are porting change every week.

---

## 1. Inventory

### 1.1 Modules and size

Counted on `origin/spell-runner` at `f59413e` (27-Nov-2026), raw line counts.
The `atomicdb/` app is **56,220 lines of non-test Python** and **74,878 lines
of tests** in 180 files (about 4,436 tests; SQLite locally, plus a Postgres CI
job). 80 migrations.

| Module | Lines | What it holds | Port? |
|---|---:|---|---|
| `ingest.py` | 10,427 | Stage (a), both climbs, mate distance, backed values, loops/echo/floor rules, rechecks, selector v1/v2, task arms, request ladder, solve/TB closures | **Core of the port** |
| `views.py` | 10,170 | Worker API (lease/heartbeat/submit/release), public API, explorer, pages | Lease stays; explorer reads late/optional |
| `proof.py` | 1,971 | pn/dn per campaign, df-pn and value descents, frontier | **Yes** (with the climbs) |
| `models.py` | 1,855 | 34 models | Schema reference |
| `conquest_map.py` | 1,658 | Map page builder | Stays (reader) |
| `backlog.py` | 1,362 | Community backlogs | Stays |
| `contributors.py` | 1,244 | Leaderboards | Stays |
| `live_request.py` | 1,112 | Live request tracking | Stays |
| `openings.py`, `opening_routes.py` | 1,086 + 237 | Opening names and routes | Stays |
| `roots.py`, `root_chart.py` | 951 + 159 | Root records and charts | Stays (reader) |
| `logic.py` | 755 | Rules via pyffish: canonical FEN, keys, legal moves, zeroing moves, slack, `backup_status` | **Yes**, see 2.4 |
| `survive.py`, `solve.py`, `autosolve.py`, `solve_estimate.py` | 773 + 330 + 387 + 298 | Solver integration and its arm | Partly (the arm) |
| `planner/` | ~2,800 | Planner shadow/master, cost model | Later, with the selector |
| `sweep.py`, `idle_repair.py` | 694 + 476 | Repair sweeps, idle arm | Arms: yes; sweeps become store scans |
| `propagation_queue.py` | 559 | The drainer: batches, retries, proof pipeline | **Replaced** by the service |
| `ingest_queue.py` | 516 | Durable ingest jobs, apply, trust | Stays as the job queue; `apply_job` calls the service |
| `lanes.py` | 333 | Queue lanes and fairness | Stays (lease side) |
| `management/commands/` | ~9,500 | 43 commands: drainers, sweeps, backfills, recascades, verifiers | Most graph sweeps become service commands |
| Others (names, campaigns, metrics, notifications, funquests, SAN, WDL, TB) | ~8,000 | Community and site features | Stays |

### 1.2 Hot paths

| Path | Where | What it does to the graph |
|---|---|---|
| **Ingest stage (a)** | `ingest.ingest_analysis` (ingest.py:984), called by `ingest_queue.apply_job`; 8 processes (`atomicdb-ingest`, `-ingest2`..`-ingest8`) | Locks the position, `expand()` (legal moves through pyffish, creates child positions and edges), seeds child evals, verifies mate PVs and closes children (CAS on `status='UNKNOWN'`), revokes refuted mates, arbitrates the showcase (`last_analysis`, `analysis_passes`, `eval_cp`, `best_move`), bumps `visits`/`nodes_invested`. Ends by writing a `PropagationSeed` (`Landing.to_payload`). |
| **Climb: status cascade** | `ingest.backup_cascade` (ingest.py:1754) | Fixed point upwards: closes UNKNOWN positions by minimax (`logic.backup_status`), re-derives `proof`, `clock_slack`, `mate_in`, `best_move` of decided positions (`mate_distance_refresh`, `support_reaches`, `_loser_distance`). Reads waves of 512 keys (`_CascadeBulkReader`). Emits closure side effects. |
| **Climb: backed values** | `ingest.backup_backed_evals` (ingest.py:4555) and `_decide_level` (4475) | Level-by-level negamax with coverage and quality guards, floors, echoes, returning lines, pointer loops and best exits. Writes `backed_eval/move/plies/nodes`. Buys quality-convergence and cycle-disambiguation tasks. |
| **Proof numbers** | `proof.refresh_proof_numbers` (proof.py:736) | pn/dn per campaign climbing from the cascade seeds; 39% of drainer time before the pipeline of 26-Nov. Writes `ProofNode` (39M rows, 17 GB). |
| **After-climb arms and rechecks** | `propagate_landings` (ingest.py:1397), `recheck_after_commit`, `recheck_meeting_points` (PR #333) | Uncertainty expansion, witness-refuted revisit, value cascade, idle repair, autosolve (all insert `AnalysisTask` rows); re-read the rows a concurrent climb could have changed. |
| **Drainer** | `propagation_queue.drain_batch` (32 landings per transaction) | Claims seeds with `SKIP LOCKED`, merges them, runs the climb, deletes the seeds, all in one transaction; transient deadlocks retried as a batch. |
| **Selector** | `refresh_selector` service: `refresh_priorities_v2` (ingest.py:6630), `next_tasks`, `top_up_analysis_pool`, `proof.descend` / `descend_value` | Writes `priority` on many rows; mints `AnalysisTask` rows (pool of 512). |
| **Lease** | `views.api_lease` (views.py:1004), `api_submit` (1549) | Pure task-table work (`AnalysisTask`, locks, lanes). Reads a few position columns. |
| **Explorer and public API** | `views.explore` (9586), `views._child_moves` (6141), `api_query` (7050), `api_principal_variations`, `_walk_lines_to_root` | Many small reads: a position, its children, their values and status, lines to the root, `last_analysis`, `won_line`, names. |

### 1.3 Tables the graph engine reads or writes

Production estimates from `pg_class`/`pg_stats` on 27-Nov-2026 (no scans; the
row estimates are `reltuples` and may run high after heavy update churn, the
brief's own figures were ~160M and ~129M):

| Table | Est. rows | Table | Indexes | Class |
|---|---:|---:|---:|---|
| `atomicdb_position` | ~204M | 59 GB | 40 GB | **Graph** (most columns) + relational blobs |
| `atomicdb_edge` | ~209M | 36 GB | 55 GB | **Graph** |
| `atomicdb_proofnode` | ~39M | 5.8 GB | 12 GB | **Graph** (pn/dn per campaign) |
| `atomicdb_propagationseed` | a handful | | | Graph queue (replaced) |
| `atomicdb_ingestjob` | ~6.2M | 11 GB | 1.8 GB | Relational (job queue, raw payloads) |
| `atomicdb_analysistask` | ~6.3M | 3.1 GB | 4.0 GB | Relational (tasks and leases) |
| `atomicdb_dbevent` | ~27M | 6.2 GB | 3.3 GB | Relational (events) |
| `atomicdb_closureprovenance` | ~22M | 4.8 GB | 2.7 GB | Relational (who closed what) |
| `atomicdb_pendingclaim` | ~27k | | | Relational (trust gate) |
| `atomicdb_campaign`, `proofcampaign`, `solvetask`, ... | small | | | Relational |

The edge indexes are bigger than the edge table: `uniq_parent_move` 26 GB and
the child index 24 GB, both on 65-byte text keys. The position primary key is
24 GB. The whole database is 249 GB; `shared_buffers` is 16 GB on a 64 GB box.

**`atomicdb_position` column by column** (pg_stats null fraction in brackets):

| Column | Read by | Written by | Class | Store encoding |
|---|---|---|---|---|
| `key` (sha256 hex, 64 chars) | everything | creation | Identity | key->id map; u32 id everywhere else |
| `fen` (57 B avg) | cascade (side to move, zeroing moves), pn leaves (branching), stage (a) | creation | Relational, but the climbs need 2 derived facts | `stm` bit in core; per-edge `zeroing` bit; fen stays in PG |
| `status`, `closure`, `proof` (closed: ~25%) | all climbs, selector, explorer | stage (a), cascade, revocation, TB/solve | **Graph** | 2 + 3 + 3 bits |
| `best_move` (non-null ~25%) | cascade, explorer | stage (a), cascade | **Graph** | u16 move code |
| `mate_in`, `clock_slack` (~25%) | cascade, backed ties | stage (a), cascade | **Graph** | u16, u8 |
| `won_line` (~22%, 54 B avg) | mate distance (its LENGTH only), explorer | stage (a), materialisation | Relational text; length is graph | witness length u8 in core; text stays in PG |
| `eval_cp` (~14%) | backed, pn leaves, selector, explorer | stage (a), seeding | **Graph** | i16 |
| `backed_eval/move/plies/nodes` (~11%) | backed climb, explorer, selector | backed climb | **Graph** | ext record |
| `expanded`, `reachable` | climbs, selector | stage (a), backfill | **Graph** | flag bits |
| `nodes_invested`, `visits`, `time_invested`, `depth_invested` (searched: ~2.7%) | backed quality, selector, explorer | stage (a) | **Graph** | ext record |
| `priority` | lease/selector | selector | **Graph** (derived) | f32 column, or not stored at all (see 2.5) |
| `last_analysis` (JSON, ~2.7% non-null, ~1.1 KB avg) | backed climb reads the **showcase bound and line heads only**; explorer reads all | stage (a) | Relational blob; 2 derived facts are graph | showcase bound i16 + head-move bitmask in ext; JSON stays in PG |
| `analysis_passes` (JSON) | explorer, API | stage (a) | Relational | stays in PG |
| `campaign_id` | selector bonus | campaign tagging | Graph-ish | u8/u16 campaign slot |
| `updated` | selector delta mode, coverage scans | most writers | Bookkeeping | replaced by a store change counter |

**`atomicdb_edge`**: `(id, parent, move_uci, child)`, unique `(parent,
move_uci)`, index on `child`. Edges are never deleted or updated (0 updates in
`pg_stat_user_tables`). In the store: an out-edge block per expanded parent
(`child u32, move u16, flags u8`: about 8 bytes) and an in-edge list per
child. The edge row id is not used by any rule.

**Side effects the climbs produce in relational tables** (they must survive the
port as an "effects" stream, see 2.3): `DBEvent` (`NODE_CLOSED`,
`CASCADE_GUARD`, `CASCADE_CYCLE`, `BACKED_GUARD`, `LANDING_RECHECK`, ...),
`ClosureProvenance`, `PendingClaim` resolution, absorption of `AnalysisTask`
rows on closed positions, `Campaign` state changes, and new `AnalysisTask`
rows bought by the arms (quality convergence, cycle disambiguation,
uncertainty, witness refuted, value cascade, idle repair, autosolve).

### 1.4 What the numbers say about the layout

From the null fractions above, out of ~204M positions:

- about **75% are UNKNOWN leaves** that carry little more than a status and
  maybe a seeded eval;
- about **25% are closed**, most of them never searched (materialised won
  lines), and they need `best_move`, `mate_in`, `clock_slack`, `proof`;
- about **11% carry backed values**, far more than the ~2.7% that were ever
  searched or expanded (floors on unsearched positions);
- about **2.7% were searched and expanded**.

So MoKy's split (a small core for everyone, an extension for searched nodes)
is the right shape, but the exact sizes must come from these read sets: with
`stm`, `expanded`, `reachable`, `best_move`, witness length and a version word
in the core, our own count lands at **24-32 bytes**, not 16; and the backed
values must live in an extension that 11% of positions have, or in their own
column. Rough sizing at 204M positions: core 32 B = 6.5 GB; backed 16 B x 22M
= 0.4 GB; searched ext 64 B x 5.5M = 0.35 GB; out-edges 8 B x 209M = 1.7 GB;
in-edges about the same; key->id map 7-10 GB (it can stay on disk). The hot
working set of the climbs is **under 12 GB**, against 190 GB in Postgres today.

The edge statistics are not trustworthy enough to size more finely (pg_stats
claims ~42M distinct parents, which does not square with 2.7% expanded). An
exact census, on a restored backup, is the first job of phase 0.

---

## 2. Target architecture

### 2.1 The boundary

```
                 workers                         visitors
                    |                               |
             Django (HTTP)  <---- reads ---->  Django views / API
             lease, submit                          |
                    |                               |
        PG: tasks, jobs, events,          gRPC over Unix socket
        accounts, pages, fen,                      |
        last_analysis, won_line       +----------------------------+
                    |                 |  atomicdb-graph (Rust)     |
        ingest workers (Python) ----->|  single writer             |
        call Land(job) per result     |  store: core/ext/edges/    |
                    ^                 |  pn-dn/key map             |
                    |                 |  climbs, arms, selector    |
                    +---- effects ----|  WAL + batch numbers       |
                     (tasks, events,  +----------------------------+
                      provenance, PG mirror of changed columns)
```

**Moves to Rust, in this order:**

1. **The store** and its loader, key->id map, edge blocks, WAL and recovery.
2. **The climbs**: `backup_cascade`, mate distance, `backup_backed_evals` with
   every rule of `_decide_level`, the proof numbers, the rechecks (which
   become unnecessary for races between climbs, since one process owns all of
   them, but stay for stage (a) interleavings until stage (a) moves too). The
   drainer is replaced by the service's batch loop.
3. **Ingest stage (a)**: expansion, child seeding, mate-PV verification and
   closure, revocation, showcase arbitration. Python keeps receiving the
   submit and the `IngestJob`, and calls `Land(job_id, payload)`.
4. **Selector**: priorities/regret, df-pn and value descents, arms with quotas.
   It mints task rows into Postgres; **leases stay in Postgres**: they are a
   relational queue with locks, lanes and fairness, and Postgres does that well.
5. **Explorer and public API reads** (optional): Django keeps rendering, but
   asks the service for the node, its children and lines to the root in one
   call instead of dozens of queries.

**Stays in Django and Postgres:** accounts, workers and pings, tasks, leases,
ingest jobs, events, provenance, pending claims, campaigns and votes, opening
names, backlogs, pages, notifications, solver tasks and certificates; the
per-position blobs (`fen`, `last_analysis`, `analysis_passes`, `won_line`).

### 2.2 How Django talks to the service

| Option | For | Against |
|---|---|---|
| **gRPC (tonic) over a Unix socket** | Typed schema shared by Python and Rust; batch and streaming calls; same API later over TCP if the service moves box; mature on both sides | A protobuf build step; ~50-100 µs per call |
| HTTP/JSON | Trivial to debug with curl | JSON of thousands of keys per call is slow; we would reinvent a schema |
| Raw local socket with our own framing | Fastest | Our own protocol, versioning and client code forever |
| PyO3 in-process library | Zero-copy reads | A single writer cannot live in N Django processes; a crash takes the web worker down |

**Recommendation: gRPC over a Unix domain socket** for all writes and most
reads. If explorer latency later demands it, add a **read-only PyO3 binding**
that maps the store files (or opens LMDB/SQLite read transactions) inside
Django; that is cheap once the format is fixed and needs no new protocol.

### 2.3 Consistency between the store and Postgres

The service is the single writer of graph columns; Postgres remains the home
of jobs and effects. The two are tied by **batch numbers** (MoKy's design,
generalised):

1. A batch (one `Land` or one climb batch) is computed in a private overlay.
2. Its redo records go to the store WAL with a CRC per frame, `fdatasync`,
   then they are applied. The frame carries the batch number and the ids of
   the ingest jobs it consumed.
3. Then one Postgres transaction writes the batch's **effects** (task rows,
   events, provenance, campaign changes, the job's `DONE` state and summary),
   the **mirror** of changed graph columns while the mirror exists, and the
   batch number in a one-row `atomicdb_graph_cursor` table.
4. On restart the service compares its last WAL batch with the PG cursor and
   re-sends the effects of the frames PG has not recorded. Effects are written
   idempotently (keyed by batch number and job id), so a replay is harmless.

A torn WAL frame is discarded; a complete one is replayed whole. A job that is
`PENDING` in PG and not in the WAL is simply landed again.

### 2.4 Chess rules inside the service

The climbs need only two facts from the rules, and both can be precomputed
when an edge is created: the **side to move** of a position and whether a move
is **zeroing** (capture or pawn move, `logic.is_zeroing`). pn/dn leaves also
need the **branching** (number of legal moves), known at expansion. With those
stored, the climbs never call a move generator.

Stage (a) does need real rules: canonical FEN, sha256 key, legal moves,
`apply_move`, mate-PV verification, `prove_forced_mate`, TB lines. Today they
come from pyffish (Fairy-Stockfish, atomic). Options:

- **Link the same Fairy-Stockfish build through a small C ABI** (recommended):
  the identical rules the site uses today, no second implementation.
- Use a Rust atomic implementation (shakmaty has atomic): fast and safe, but a
  second rules implementation, which AtomicDB has always refused (see the
  docstring of `logic.is_zeroing`). Usable as a differential test oracle only.
- Keep stage (a)'s rule work in Python and send the service ready-made facts
  (legal move list, verified proofs). A good intermediate step.

### 2.5 Things that become simpler

- **No climb-versus-climb races**: one process owns every climb, so the
  `recheck_meeting_points` class of bugs (PR #333) disappears by construction.
- **No deadlocks, lock timeouts or retry paths** between drainer and writers.
- **`priority` may not need storing** at all: the selector can compute the top
  of the queue in memory (it already only needs the top few thousand rows).
- **Sweeps become scans** of a few GB in RAM (minutes, not days): recascades,
  `sweep_stale_backed`, `backfill_reachable`, `backfill_mate_distance`.

---

## 3. The store race

### 3.1 Candidates

**A. Custom mmap columns (MoKy's proposal).** Fixed-stride column files,
`mmap` `MAP_SHARED`, created sparse with reserved capacity; a seqlock version
per record; redo WAL with CRC frames and `fdatasync` before apply.
- For: the smallest possible footprint and the fastest possible reads (a
  pointer add); in-place 16-32 B writes; MoKy's prototype recovered
  byte-identical in 24/24 `kill -9` trials on a 2M-node graph.
- Against: we own the storage engine forever: WAL, recovery, growth of edge
  blocks, compaction, backup, tooling, corruption detection. Multi-field
  consistency for readers in other processes rests on the seqlock. Every
  schema change is a file-format migration.

**B. LMDB** (via `heed` or `lmdb-rs`).
- For: mmap B+tree, exactly one writer and lock-free readers that never block
  it, which is our shape now that there is one drainer; ACID with no WAL to
  manage and no recovery step (copy-on-write, crash-safe by design); readers
  in Django through the `lmdb` Python package with no service call; tiny and
  battle-tested (OpenLDAP, Monero).
- Against: copy-on-write rewrites the page path on every update, so many
  scattered 16 B writes cost whole 4 KB pages per commit; a long-lived reader
  pins old pages and the file grows (free-list) until it ends; the map size
  must be set up front; no compression.

**C. SQLite** (via `rusqlite`), WAL mode, requested by the owner.
- For: WAL mode gives one writer and many readers, which is again our shape;
  embedded, ACID, crash-safe, with forty years of tooling (`sqlite3` shell,
  online backup API, `.recover`); **the test suite already runs on SQLite**,
  and AtomicDB itself lived on SQLite before the move to Postgres (the split
  and cut-over tooling is still in `management/commands/`), so the team knows
  its failure modes; SQL means ad-hoc queries for debugging and the same
  schema can be read by Django directly through a read-only connection.
- Against, and what the benchmark must probe:
  - **64-char text keys versus an integer rowid.** With `key TEXT PRIMARY KEY`
    in an ordinary table, every lookup descends two B-trees (the key index,
    then the rowid table) and the index costs ~80 B per row: ~16 GB for keys
    alone at 204M rows, the same disease as Postgres today. The candidate must
    use **`INTEGER PRIMARY KEY` ids** for positions (the rowid itself, one
    descent), edges as `WITHOUT ROWID` tables keyed `(parent, move)` and
    `(child, parent)`, and the sha256 kept as a **32-byte BLOB** in a separate
    key->id table, never as hex text.
  - **Record overhead.** Each row carries a header and varint-coded columns
    and pages are rarely full, so expect 2-3x the raw byte count; still a few
    tens of GB, not hundreds.
  - **Page cache versus `mmap_size`.** SQLite's page cache is private to each
    connection, so N reader processes each keep their own copy. Readers can
    share the OS page cache through `PRAGMA mmap_size`, but the compile-time
    ceiling `SQLITE_MAX_MMAP_SIZE` is about 2 GB by default: mapping a 20-40 GB
    file needs a custom build (`rusqlite` with a bundled, reconfigured
    SQLite). Writes still go through the pager and the WAL, never through the
    map.
  - **WAL checkpoints.** A reader that holds a snapshot open prevents the
    checkpoint from resetting the WAL, so a long explorer read or backup can
    make the WAL grow without bound; checkpoints also stall the writer
    briefly. The benchmark must run with concurrent readers.
  - Updating a 16-byte field rewrites a whole page into the WAL, as in LMDB.

**D. RocksDB** (optional, for torom's question).
- For: LSM tree, excellent write throughput, compression, column families,
  snapshots; used at huge scale.
- Against: the climbs are read-dominated random point lookups, where an LSM
  pays several levels per miss and needs bloom filters and a large block
  cache; compaction stalls; heavy C++ dependency and many knobs. Included so
  that the answer to "did you try RocksDB?" is a number.

**Baselines.** (0) **Today's drainer**: Python on Postgres, measured on the same
replay. (0') **Postgres with a narrow, integer-keyed hot table** driven by the
Rust climb: if this comes close, the store question changes completely, and we
should know before we own a storage engine.

### 3.2 The benchmark

**Data.** A full graph snapshot restored from the nightly restic backup on a
separate box (never production), converted once into each store by a shared
loader. Sizes: the full graph, plus a 10% induced subgraph for quick runs.

**Workload: a replay of N real landings.** From the same backup and the
`IngestJob` payloads that followed it, the current Python code is run once on
the restored database with a recorder that writes, per landing, the stage (a)
row deltas and the seed lists of `Landing.to_payload`. This trace (the "golden
replay", also used in section 4) is the input of every candidate. N = 1M
landings for the race (about a week of production), plus a 100k "flood" trace
where many landings share ancestors.

**The climb.** One Rust implementation of the climb's access pattern, written
once against a small `GraphStore` trait that every candidate implements:
status cascade with mate distance, a backed-value level loop with the real
read sets (children, their core and ext fields, the showcase bound), and pn/dn
over one campaign. It does not need every semantic corner to be a fair
performance test; it does need the same reads and writes.

**Measurements**, each at 1, 8 and 32 landings per batch:

| Metric | How |
|---|---|
| Batch latency p50/p99/max | Wall time per climb batch, cold and warm cache |
| Throughput | Sustained landings/s over the 1M trace and the flood trace |
| Memory | RSS of the service and page cache used (`/proc/<pid>/smaps`, `vmtouch`) |
| Disk | Store size, bytes written per landing (write amplification), fsyncs/s |
| Readers | The same runs with a concurrent reader load (200 random explorer-style reads/s from another process): their latency, and the writer's slowdown |
| Recovery | `kill -9` at random points (100 trials per candidate, MoKy's harness generalised): time to be serving again, and a **byte-identical comparison** of the graph columns with an uninterrupted run |
| Maintenance | Backup of a live store (time, consistency), growth after 10M landings, compaction or vacuum cost |

**Decision rule** (to agree before the race runs): the simplest candidate that
meets the targets wins. Proposed targets: p99 batch of 32 landings under
250 ms warm; 2,000 landings/s sustained; recovery under 60 s with zero
differences; readers p99 under 5 ms. A custom store must beat the best
off-the-shelf candidate by **at least 2x on batch latency or throughput** to
pay for owning it.

---

## 4. Correctness strategy

The semantics in `ingest.py` and `proof.py` are the product of months of
community bug reports (floors, echoes, returning lines, pointer loops, mate
distance in both directions, fresh-context slack). Most of them exist because
a specific position went wrong. A port that is "basically right" would
reintroduce them one by one. So correctness gets more effort than speed.

### 4.1 The test suite as the specification

Rewriting 75k lines of tests in Rust is neither possible nor desirable. The
tests stay in Python and run against the Rust engine:

- A **shim** replaces `ingest.backup_cascade`, `backup_backed_evals`,
  `proof.refresh_proof_numbers` (and later `ingest_analysis`) with calls that
  export the test database's position and edge rows to an in-memory Rust store,
  run the Rust function, and write the changed columns and effects back. Each
  existing test then runs twice in CI: on Python and on Rust.
- A list of **intentionally Python-only tests** is kept and justified one by
  one (tests of the drainer's retry paths, of SQL query counts, and so on).
- **Differential fuzzing**: random DAGs with transpositions, cycles, mixed
  statuses, slack and showcases (Hypothesis on the Python side, a seed file
  shared with `proptest` on the Rust side); every changed value must match.
- Pure helpers (`backup_status`, `minimax_slack`, `_child_contribution`,
  `_showcase_bound`, saturating pn/dn arithmetic) get table-driven Rust unit
  tests generated from the Python implementation.

### 4.2 The golden replay

The trace of section 3.2 records not only inputs but the **outputs** of the
Python climbs (every changed column of every row, every effect). The Rust
engine replays it from the same snapshot and must produce the same final
values. Differences are triaged into: bug in Rust, order dependence allowed by
the design (documented in `propagation_queue.py`: the noise cut, guards, seed
order), or a latent Python bug (which then gets its own issue and fix in
Python first).

### 4.3 Shadow mode in production

A **lockstep shadow** fed by **Postgres logical decoding** (a replication slot
on `atomicdb_position`, `atomicdb_edge`, `atomicdb_propagationseed`,
`atomicdb_proofnode`):

1. The service keeps its own copy of the graph, updated from every committed
   change.
2. When it sees a drainer transaction (it contains the deletion of the seeds it
   climbed, which carry the exact landing payloads), it first **predicts** the
   climb from its pre-state, then **diffs** its prediction against the changes
   the Python drainer committed, then **applies** the real changes so the two
   never drift.
3. Every changed value that differs is logged with the batch, key, column and
   both values. A nightly report (Discord and a page) gives batches, rows
   compared, differences by column, and links to sample positions in the
   explorer.

Caveat: the drainer reads under READ COMMITTED while stage (a) writers commit
beside it, so a small number of differences will come from interleaving, not
from bugs; the report classifies those by checking whether a stage (a) commit
touched the row inside the batch window.

Operational guard: a stalled slot retains WAL and can fill the disk (the
September outage was a full disk), so the slot gets `max_slot_wal_keep_size`
and an alert, and dropping it is the documented first response.

### 4.4 Cut-over criteria

- Python and Rust both green on the full test suite through the shim, with the
  Python-only list reviewed.
- Golden replay: zero unexplained differences over 1M landings.
- Shadow: **14 consecutive days** with zero unexplained differences, over at
  least 2M batches, including at least one flood.
- Store race targets met on production-sized data; `kill -9` drill passed on
  the production box.
- Rollback rehearsed end to end (5.3), with its timing written down.
- The owner's go.

---

## 5. Migrating the rows

### 5.1 Ids

- Dense **u32** ids. 204M today against a ceiling of 4.29B: at the current
  pace of inserts that is many years, but the id type is a single alias so
  that a move to u40/u64 for edges or positions is a recompile, not a redesign.
- Ids are assigned **by the service** at export time, in the order of the
  export (no meaning attached), and from then on by the service when stage (a)
  creates a position. They never change and are never reused.
- Postgres learns them through a separate insert-only table
  `atomicdb_position_gid(key, gid)` loaded with `COPY`. **Not** a new column
  on `atomicdb_position`: back-filling 204M rows would write 204M new tuple
  versions into a 59 GB table.

### 5.2 Backfill while live

1. Create a logical replication slot with an exported snapshot.
2. Under that snapshot, `COPY` only the needed columns of position, edge and
   proofnode to files (about an hour, run in the quietest window; it holds the
   vacuum horizon for that time, which the tables tolerate).
3. The loader builds the store from the files (ids, key map, core, ext,
   edge blocks, the derived bits: `stm`, `zeroing`, branching, witness length,
   showcase bound and heads).
4. The service starts consuming the slot from the snapshot's position and is
   then current within seconds. This is the shadow of 4.3.
5. **Census check**: counts per status, closure, proof; sums of
   `nodes_invested`; edge counts per parent for a random sample; byte-for-byte
   comparison of 1M random positions between the store and Postgres.

### 5.3 Cut-over and rollback

Cut-over is per component, behind a setting, like every AtomicDB rollout:

1. **Climbs**: stop `atomicdb-propagate`; the service takes over the seeds
   (it reads them from Postgres at first, so stage (a) does not change). The
   service writes the **mirror** of changed graph columns back to Postgres in
   the batch transaction (2.3). Rollback: stop the service, start the Python
   drainer; Postgres is complete. Minutes.
2. **Stage (a)**: ingest workers call `Land` instead of `ingest_analysis`.
   The mirror still writes Postgres. Rollback as above.
3. **Mirror off** (the one-way door): once Postgres no longer receives graph
   columns, rollback needs a bulk export from the store back into Postgres
   (new tables, `COPY`, swap): hours, and rehearsed before the door is closed.
   This is also where the big savings appear (Postgres stops rewriting tuples
   of a 59 GB table; the edge indexes can go). This gate deserves its own
   decision and can wait months.

---

## 6. Measurement plan

Every phase has to show what it changed, in numbers that can be compared with
the phase before. So the measurements are defined once, captured from today's
system **before any change**, and recorded again, the same way, at every gate.

### 6.1 The metrics

| Metric | Today's source | Unit |
|---|---|---|
| Climb latency per batch, p50/p95/p99/max | Timer around `propagation_queue.drain_batch` (cascade, backed ascent, proof refresh and commit timed separately) | ms per batch, and per landing |
| Drainer throughput | Landings settled per minute (`drain()` totals) | landings/min |
| Drainer backlog | `propagation_queue.backlog()`: seeds waiting and age of the oldest | rows, seconds |
| Ingest results per minute | `IngestJob` rows reaching `DONE` per minute, and the open queue (`PENDING`/`PROCESSING`) | jobs/min, rows |
| Time to visible | From submit (`IngestJob.created`) to the end of its climb (`summary.propagated`) | seconds, p50/p95/p99 |
| Lease latency | Server time of `api_lease`, `api_submit` (nginx `$request_time` plus an in-view timer) | ms, p50/p95/p99 |
| Explorer page latency | `explore`, `api_query`, `api_principal_variations` server times, cold and cached | ms, p50/p95/p99 |
| Box CPU, RAM, IO | `node_exporter`-style sampling: CPU by process group (postgres, ingest, drainer, selector, web, service), RSS, page cache, disk read/write bytes and IOPS, `iowait`, NVMe utilisation | %, GB, MB/s, IOPS |
| Postgres | Buffer hit ratio, blocks read per landing, lock waits and deadlocks per hour, autovacuum time on position/edge, table and index sizes | counts, GB |
| Staleness | Rows `sweep_stale_backed` and `recascade_status` find to repair, per pass (backed values a climb should have written, positions a cascade should have closed) | rows per pass, and per 1M positions |
| Guards and events | `CASCADE_GUARD`, `CASCADE_CYCLE`, `BACKED_GUARD`, `LANDING_RECHECK` per hour | events/h |

The staleness line matters as much as the speed lines: a faster climb that
leaves more stale rows behind is not an improvement.

### 6.2 When they are recorded

- **Baseline (phase 0, before any change):** one week of production capture
  of everything in 6.1, plus a run of today's Python drainer over the golden
  replay and the flood replay on the dev box (section 3.2). The production
  week tells us what users see; the replay gives a number that later phases
  can be compared with like for like.
- **At every gate** (end of phases 1-8): the same replays on the same dev
  hardware and the same snapshot, and, from phase 4 on, a week of production
  capture after the cut-over.
- **In shadow** (phase 3 and the shadow of phase 5): the service records its
  predicted climb latency and resources for every real batch beside the
  Python drainer's actual numbers, so each production batch is a paired
  comparison, together with the difference counts of 4.3.

Replays are frozen once recorded (files with a checksum); a phase that needs a
new replay keeps running the old one too, so the series never breaks.

### 6.3 Where the numbers live

- A table **`atomicdb_perf_sample`** (append-only, one row per metric, source
  and interval: phase, run id, replay id or `production`, p50/p95/p99/max,
  count), written by a small capture command on a timer, like
  `capture_atomicdb_progress` writes `ProgressSnapshot` today. Replay runs
  write the same rows with their run id.
- A **dashboard** reading that table (an internal page next to the progress
  views, or a Grafana panel if we add Prometheus for the service), with the
  baseline drawn on every chart.
- A **before/after report per phase**, committed under
  `docs/atomicdb/rust-port/phase-N-report.md`: the metrics table with
  baseline, previous gate and this gate side by side, the shadow difference
  counts, what got better, what got worse and why, and the gate decision. The
  short version goes to Discord.

---

## 7. Phased plan

Effort is in **focused weeks of one engineer with agent assistance**, and it is
deliberately conservative: AtomicDB's semantics are dense, documented in long
comments, and they keep moving. Elapsed time is longer where shadow time or
review waits.

| Phase | Content | Effort | Gate at the end |
|---|---|---|---|
| **0. Measure and record** | Restore a backup on a dev box; exact census of the graph (degrees, null fractions, closed-but-unsearched, cycles); build the trace recorder and record the golden replay; the metrics capture and dashboard of section 6; the baseline week and baseline replay runs | 3-4 wk | Layout and targets agreed; the replay exists |
| **1. Store race** | Shared loader; `GraphStore` trait; candidates A-D and baseline 0'; the benchmark climb; the kill-9 harness | 4-6 wk | **Pick the store** (owner and MoKy) |
| **2. Climbs in Rust** | Cascade, mate distance and support walks, backed values with all of `_decide_level` (floors, echoes, returning lines, loops, best exits), proof numbers, guards and events, effects stream; test shim; fuzzing; golden replay | 10-14 wk | Golden replay clean; shim suite green |
| **3. Service and shadow** | gRPC service, WAL and batch numbers, logical-decoding feed, lockstep diff, nightly report, ops (systemd, metrics, backup) | 4-5 wk (+2-4 wk elapsed of shadow) | 4.4 criteria |
| **4. Climb cut-over** | Service replaces the drainer, mirror on; one week of watch | 1-2 wk | Drainer retired; rollback rehearsed |
| **5. Stage (a)** | Expansion (FSF via C ABI), seeding, mate-PV verification and closure, revocation, trust gate, showcase arbitration, TB lines; `Land(job)`; shadow of stage (a) | 8-10 wk | Shadow clean; ingest workers call `Land` |
| **6. Selector** | Priorities/regret, df-pn and value descents, arms with quotas, pool top-up minting tasks into PG | 5-7 wk | Selector service retired |
| **7. Reads (optional)** | Node/children/lines-to-root calls for explorer and API; PyO3 read binding if needed | 4-8 wk | Explorer latency targets |
| **8. Mirror off (optional)** | Stop writing graph columns to PG; drop the edge indexes; reclaim space | 2-3 wk | One-way door, owner decides |

Every gate also produces the before/after report of section 6.3; a gate is
not passed without it.

**Totals.** Phases 0-4 (the climbs: the part that removes today's bottleneck):
**22-31 weeks, about 5-7 months elapsed.** Through phase 6: **35-48 weeks,
about 8-12 months.** Phases 7-8 add 1-3 months and are optional.

A useful halfway house, if the full plan stalls: after phase 1, the chosen
store with a **PyO3 read binding** could serve the *Python* climbs their reads
from RAM while Postgres stays the writer. It removes most of the 88% read
wait for a few weeks of work, at the price of keeping the store in sync from
the logical-decoding feed. It is not the target, but it is a real fallback.

### Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| **Moving semantics**: new rules land every week (PR #333 today) and the port chases a moving target | High | A rule: during phases 2-6 every semantic PR includes the Rust change or an issue for it, and the shadow catches the misses; or a declared freeze window per component |
| Hidden order dependence in the climbs (batch versus sequence) | Medium | Already documented in `propagation_queue.py`; the golden replay quantifies it before anything ships |
| Effects stream incomplete (a side effect of a closure forgotten) | Medium | Effects are diffed in shadow too (task rows, events, provenance), not only graph columns |
| Owning a storage engine (if A wins) | Medium | The 2x rule; reuse MoKy's harness; keep a store-to-PG export forever |
| Logical slot fills the disk | Low-medium | `max_slot_wal_keep_size`, alert, drop-slot runbook |
| Rules divergence in stage (a) | Low if FSF is linked | Same Fairy-Stockfish build through a C ABI; no second implementation |
| Bus factor: Rust knowledge in one or two heads | Medium | Code in the OpenBench repo, reviewed like the rest; docs per module mirroring the Python ones |
| The box: CPU-bound under load already | Medium | The service should reduce load (no PG round-trips); measure in shadow, where it runs beside everything else |

---

## 8. Open questions

### For the owner

1. **Semantics during the port**: a freeze window per component, or the
   "every semantic PR touches both implementations" rule?
2. **Where the Rust lives**: a cargo workspace inside the OpenBench repo
   (recommended, so one PR can change Python and Rust together), or a
   separate repo?
3. **Scope**: stop after the climbs (phases 0-4) and decide again, or commit
   to stage (a) and the selector now? Are the explorer reads (7) and mirror-off
   (8) in scope at all?
4. **Resources on the box**: may the service use 12-20 GB of RAM and the dev
   box or a temporary server for the race and the golden replay?
5. **Logical replication slot in production** for the shadow, given the disk
   history: acceptable with a WAL cap and an alert?
6. **Downtime** acceptable at each cut-over (minutes of paused ingest)?
7. **Proof numbers** (`ProofNode`, 17 GB): move with the climbs, as proposed?
8. **SQLite**: if it wins the race, are you happy for the graph store to be a
   SQLite file that Django can also open read-only, with the custom mmap build
   it needs?

### For MoKy

1. Given the census (25% closed and mostly unsearched, 11% with backed values,
   2.7% searched), how would you revise the 16 B core / 64 B ext split? Where
   do `best_move`, `stm`, witness length, the showcase bound and head moves go?
2. Edge storage: how do out-edge blocks grow when a parent is expanded late or
   `link_existing_children` adds an edge, and how are in-edges stored past the
   first parent? Compaction?
3. Readers in other processes: the seqlock protects one record; how does a
   reader see a **consistent batch** (a climb that rewrote 500 rows), and does
   the explorer need that?
4. Overlay size: what happens when a batch's overlay exceeds the memory you
   planned for (a flood climb touching 100k rows)?
5. Can your kill -9 harness be generalised to drive LMDB, SQLite and RocksDB
   through the same `GraphStore` trait, so that recovery is compared like for
   like?
6. Postgres sync: with the effects stream added (tasks, events, provenance),
   do you see any hole in "WAL commit, then one PG transaction with the batch
   number, reconcile newer frames on restart"?
7. Ids: any objection to u32 behind a type alias, assigned at export and by
   the service afterwards, with the key->id map possibly on disk?

### For torom and the community

1. Any other store worth a lane in the race (for example `redb`, `sled`,
   `fjall`)? A lane costs about a week.
2. Targets in 3.2: are 250 ms p99 per batch of 32 and 2,000 landings/s the
   right bar, or should we aim at what the fleet will be in a year?
