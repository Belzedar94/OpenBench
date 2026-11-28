# AtomicDB v2: a from-scratch plan in Rust

Status: **plan for the owner's decision** (28-Nov-2026). No production change.
Requested on 27-Nov-2026: think it through bottom-up, plan a v2 from scratch
in Rust, and choose the store empirically.

The store was chosen by a race that was built and run for this plan. The raw
numbers, the method and how to reproduce them are in
[`v2-storage-benchmark.md`](v2-storage-benchmark.md); the harness is in
[`atomicdb-v2-bench/`](../../atomicdb-v2-bench/). This document is the plan.

## 0. The plan on one page

- **What v2 is.** One Rust process that owns the graph, the queue and the API.
  A single writer applies an ordered log of facts (searches, proofs, requests)
  in deterministic batches; everything the site shows is derived from that
  log. Values propagate to rest inside the batch that causes them, so the
  drainer, the rechecks, the sweeps and the READ COMMITTED races have nothing
  left to do.
- **The store, decided by measurement.** Fixed-stride column files mapped into
  memory, with a redo log (CRC per frame, fsync before apply). Ten stores ran
  the same workloads on a real slice of production and on synthetic graphs up
  to production's size; this one is ahead of every off-the-shelf store on
  every workload, by 5x to 8x over LMDB on reads and by 50x to several
  hundred over the rest. Section 5 has the table. SQLite is the right tool
  for the relational side and is used there.
- **What it buys, measured** (real slice, one fsync per batch, a desktop
  Ryzen with an NVMe; the benchmark's stand-in rules, not yet AtomicDB's):
  - a batch of 32 searches applied, climbed and durable in **3 ms** (10,900
    searches a second), a single one in 0.8 ms (1,130 a second). Production
    takes 1.2 s at the median and 760 s at p99 from submit to climbed, and
    receives 2.5 a second on average, 13 at its peak;
  - the data of an explorer page in **1 microsecond**; 4 million a second
    from 8 threads with a p99 of 4 microseconds while the writer runs flat
    out. The production schema on Postgres, fully cached and queried in
    batches from Rust, gives 6,000 a second at 1.8 ms;
  - **81 bytes per position** on disk in the benchmark's layout against
    931 in production for the same tables;
  - every status and backed value of a 200M-position graph recomputed in
    **9 seconds**. The nightly recascade of v1 stopped fitting in a night;
  - restart in 1 ms; recovery after `kill -9` in 58 ms, 0.1 to 0.34 s with a
    full log to replay; 125 kills of this store without one lost or wrong
    batch.
- **What it costs.** 9 to 12 months of focused work for one engineer with
  agents (36 to 49 weeks), 6 to 8 months elapsed with two. The long pole is
  not storage, it is reproducing about 40 value rules exactly (10 to 14
  weeks). First usable result after about 4 months: v2 running in shadow
  beside v1 and reporting every difference.
- **What cannot wait for v2.** v1 is adding about 7 million positions and
  8 GB a day (measured twice: 217,000 searches a day over 22 hours, and
  1.23 million new positions in 4.5 hours the same night), and the
  production disk has 418 GB free: **about seven weeks of runway**. v2
  stores the same graph in about a ninth of the space, but it will not be
  live in seven weeks. Section 10 lists the stopgaps; this needs an owner
  decision now, independent of v2.

## 1. What was measured before designing anything

Production, read-only, 27-Nov-2026 (queries and method in the benchmark report):

| Fact | Value | Source |
|---|---|---|
| Positions / edges | ~204M / ~222M (1.087 edges per position) | `pg_class`, page sample of 40,471 rows |
| Never searched, still open | 73% of positions | sample |
| Closed | 24.8% (22.2% are materialised mate lines, one edge each) | sample |
| Searched and expanded | 2.9%, mean 28.7 moves (p5 12, p95 42, max 57) | sample |
| Parents per position | 1 for 94.5%, 2 for 4.6%; mean 1.066; near the root 24% have 2 or more | sample; root ball of 1.56M |
| Same parent, same child, two moves | exists in production (atomic captures) | root ball: unique `(parent, child)` fails |
| Searches landing | 150 per minute mean, p99 401, max 768 (last 200,000) | `atomicdb_ingestjob` |
| New positions per search | ~33 | 204M positions / 6.25M jobs |
| Rows a climb writes per search | mean 4.75 backed values (p50 2.95, p99 31.5), ~1.8 closures | job summaries |
| Depth of a searched position | p10 25, p50 52, p90 72 plies | first-parent walk of 400 recent landings |
| Submit to climbed | p50 1.2 s, p90 4.9 s, p99 760 s | job `created` to its last `updated` |
| Disk per position, v1 | 931 B for position + edge with indexes; 1,220 B all tables | `pg_class` (the v1-schema baseline of the benchmark measured 942 B on the same tables) |
| Raw result payloads kept | all 6.2M (1.26 KB mean) | `atomicdb_ingestjob` |
| Box | i7-8700 (12 threads), 64 GB, load average 8 to 12, md RAID1 at 89% utilisation | `uptime`, `iostat` |

Three things follow, and they shape the whole design:

1. **The hot data is tiny.** What a climb reads fits in 20 to 32 bytes per
   position. 204M positions are 6 GB, not 190 GB. The graph should live in
   RAM and the disk should only make it durable.
2. **Locality is free if ids follow creation order.** 94.5% of positions have
   one parent and were created together with their siblings. With dense ids
   in creation order, "read the 29 children of this position" is one
   contiguous kilobyte. With sha256 text keys it is 29 random 8 KB pages
   behind a 24 GB index, which is where the drainer's 88% read wait comes
   from.
3. **Growth is the real deadline.** 7 million positions a day is 2.6 billion a
   year. Ids must not be 32-bit, the store must degrade gracefully when the
   graph outgrows RAM, and bytes per position decide how long a disk lasts.

## 2. The domain model, from first principles

### 2.1 Facts and views

Everything AtomicDB holds is one of two things.

**Facts** arrive from outside and are never recomputed. They are the only
state that must never be lost.

| Fact | Content | v1 home today |
|---|---|---|
| Position created | canonical FEN (v2 rules: four fields + `0 1`), key = sha256 | `Position.key`, `fen` |
| Moves of a position | the full legal list at expansion, or single edges from a mate line, a click, a relink | `Edge` |
| Search landed | position, budget, MultiPV, lines (move, eval, mate, PV), nodes, time, engine and net sha, account, machine | `IngestJob.payload`; the winner in `last_analysis` |
| Proof accepted | terminal; mate line verified move by move; AND/OR certificate; tablebase result; solver certificate; the server's verdict on each | columns `status/closure/proof/won_line/mate_in/clock_slack`, `ClosureProvenance` |
| Proof revoked | which closure, why | `DBEvent CLOSURE_REVOKED` |
| People | requests and their queue order, votes, names, campaigns, trust, preferences | relational tables |

**Views** are functions of the facts. Any of them can be thrown away and
rebuilt.

| View | Function of |
|---|---|
| The showcase of a position (which search the page stands behind) | its searches, by budget then width, in log order |
| Own eval, best move, visits, nodes invested | the showcase and the searches |
| Seeded evals ("claims") on replies | the parent's showcase lines |
| Status closed by minimax, its proof grade, mate distance, clock slack | children's statuses and distances |
| Backed value, move, distance, support | children's best-known values and the ~40 rules of `docs/value-semantics.md` |
| Proof and disproof numbers | statuses, evals, edges, per campaign |
| Reachable from the root, regret, priority | edges and values |
| Key to id, parents of a position | positions and edges |
| Leaderboards, counters, progress snapshots, pages | all of the above |

v1 stores facts and views in the same rows and updates the views with
row-at-a-time SQL from several processes. Most of its hard-won machinery
(`recheck_after_commit`, `recheck_meeting_points`, `carry_cut_ascents`,
`sweep_stale_backed`, the recascades, the guards) exists to repair views
that a concurrent or truncated update left stale. v2 separates the two:

- the **log of facts** is the system of record, append-only, in a format a
  shell script can read;
- the **graph columns** are a materialised view of it, kept current by one
  writer, checkpointed, and rebuildable from the log;
- the relational side (queue history, events, names, votes, leaderboards) is
  a second view, projected from the same log.

### 2.2 Correct by construction

1. **One writer.** A single thread owns every mutation. Inputs are totally
   ordered by a sequence number. There is no second transaction to race
   with, so the class of bug fixed by PR #333 (two climbs each deciding an
   ancestor from the other's stale sibling) cannot be written.
2. **A batch is a pure function.** `batch = f(committed state, inputs)`. It
   is computed against the committed state plus a private overlay and
   committed atomically. No wall clock, no row order, no hash-set iteration
   order inside `f`: budgets count steps, ties break by id.
3. **A batch runs to rest.** The cascade and the ascent finish inside the
   batch that causes them. Nothing is owed to ancestors afterwards
   (`PropagationSeed` disappears), and "time to visible" is the commit.
4. **Every value has a version.** Each record carries the sequence number of
   its last change. Readers, caches and the selector's delta mode key on it;
   `updated` timestamps are not needed for correctness.
5. **Replay is the test.** The same log gives the same state, byte for byte,
   on any machine. That one property gives the crash test, the shadow, the
   audit trail ("why did this number change at sequence N?") and the
   recascade after a rule change, which becomes a replay instead of a
   week-long sweep.
6. **A property v1 cannot promise and v2 will test:** batch-size
   independence. Applying the same inputs one at a time or 128 at a time
   must end in the same state. v1 documents that it does not
   (`propagation_queue.py`, "the noise cut"), and relies on the sweep to
   converge. This is owner decision D2 in section 11.

## 3. Architecture

```
 workers ── lease / heartbeat / submit / release ─┐        visitors, bots
                                                  │              │
                         nginx (rate limits, static engines, unchanged)
                                                  │              │
            ┌─────────────────────────── atomicdb (one Rust process) ───────────────────────────┐
            │  HTTP: worker protocol, public JSON API, explorer data and pages                   │
            │        │ submit                         ▲ reads (lock-free, in memory)             │
            │        ▼                                │                                          │
            │  preparers (pool): rules via the      graph columns (mmap)   key map    blobs      │
            │  Fairy-Stockfish library, mate-PV       ▲ apply                                    │
            │  and AND/OR proofs, tablebases          │                                          │
            │        │ prepared inputs                │                                          │
            │        ▼                                │                                          │
            │  WRITER (one thread): land, cascade, ascent, proof numbers, queue state            │
            │        │ one frame per batch: facts + redo records, CRC, fsync                     │
            │        ▼                                                                           │
            │  log segments ──► projector ──► meta.sqlite (history, events, names, leaderboards) │
            │        └────────► archive (facts only, compressed) ──► restic                      │
            │  selector (reads the graph in memory, proposes tasks to the writer)                │
            └────────────────────────────────────────────────────────────────────────────────────┘
        Django stays for OpenBench (SPRT) and for accounts; it no longer touches the graph.
```

### 3.1 Processes

One binary, one systemd unit, in place of today's gunicorn workers plus five
to eight ingest processes, the drainer, the selector, the planner and the
timers. Inside it:

- **Writer**: one thread. Lands inputs, climbs, updates the queue state,
  appends one log frame per batch, fsyncs, applies. Batches are whatever
  arrived while the previous fsync was in flight (group commit), so latency
  stays near one fsync at any load.
- **Preparers**: a thread pool that does everything expensive and pure
  before the writer sees an input: legal moves and child keys, mate-PV
  verification, AND/OR proof search, tablebase probes. Their budgets are
  counted in positions, not seconds, so a verdict never depends on load.
- **Readers**: the HTTP handlers read the mapped columns directly. A page
  costs microseconds of memory reads instead of 15 to 25 SQL statements.
- **Selector**: a task that reads the graph in memory (descent from the
  root, regret) and proposes tasks to the writer. `priority` is not stored.
- **Projector**: tails the log into `meta.sqlite`. It can lag or be rebuilt;
  nothing waits for it.

### 3.2 Data directory

```
graph/   core.col     20 B per position  (flags, eval, backed value/move, best move,
                                           distance, slack, version, ext index)
         ext.col      48 B per searched or backed position (exact node counts,
                                           showcase bound and line heads, blob offset)
         out.col       5 B per position  (offset of its edge block)
         parent.col    5 B per position  (its parent, or the head of a parent list)
         out.arena     2 B + 7 B per edge, append-only (child id 40 bits, move 16 bits)
         inx.arena     10 B per extra parent, append-only
         keymap.tbl    open addressing, 96-bit fingerprint -> id
         keymap.flt    10 bits per position: a Bloom filter in front of the key map,
                       so that the lookup of a position that does not exist yet
                       (almost every lookup of an expansion) touches no page of it
         ctl           counters, checkpoint sequence, epoch
log/     NNNNNN.seg   frames: [length][crc][seq][inputs (facts)][redo records]
blobs/   fen.col      packed position, ~28 B, read by the API and by stage (a)
         analysis.log showcase lines of searched positions, append-only
meta.sqlite            projection: tasks and their history, events, provenance, names,
                       votes, campaigns, backlog, notifications, workers
archive/               closed log segments, facts only, zstd
```

Ids are dense, assigned in creation order, **40 bits on disk** (a trillion
positions) and `u64` in the code. The sha256 key stays the public identity:
URLs and the API do not change.

About 63 bytes per position in `graph/` at today's shape, 130 to 150 with
blobs, archive and projection, against 1,220 in v1. The benchmark's simpler
layout (32 B records, 32-bit ids, the key map included) measured 77 to 81
bytes per position.

### 3.3 Commit, checkpoint, recovery

This is MoKy's protocol, measured in the benchmark:

1. Compute the batch in a private overlay.
2. Append one frame: the inputs it consumed and its redo records (absolute
   byte writes: "these bytes at this offset of this file"). CRC. `fdatasync`.
3. Apply the redo records to the maps between two bumps of an epoch counter.
4. Acknowledge (the worker's submit returns here).
5. Checkpoint now and then, on another thread: flush the maps, record the
   sequence number, release older segments to the archive. The benchmark
   checkpoints on the writer thread and that is its worst latency (0.66 s
   in a long run); the log protects the data until the flush is done, so
   the writer has no reason to wait for it.

Recovery replays every valid frame newer than the checkpoint and discards a
torn tail. Redo records are idempotent, so a crash during recovery is
harmless. Measured: 125 `kill -9` trials on the real slice, no acknowledged
batch lost, the whole state identical to a reference every time; recovery in
58 ms at the median, 102 to 343 ms when a full 256 MB log has to be replayed.
A power cut was not simulated; that test belongs in phase 1.

Because nothing is applied to a map before its frame is on disk, the OS
writing a dirty page early can never expose an uncommitted batch.

### 3.4 Readers

In-process readers take no lock. A read notes the epoch, reads, and repeats
if the epoch moved (a batch applies in microseconds). Measured under a
writer at full speed: 4.0 million explorer reads a second from 8 threads with
a p99 of 4 microseconds on the real slice, while the writer kept 87% of its
own speed; 2.3 million with a p99 of 19 microseconds at 20M positions.

### 3.5 Backup, restore, standby

- **Backup** is the archive of facts (append-only, tiny, shipped by restic
  as today) plus a periodic copy of `graph/` taken while the service runs:
  note the checkpoint sequence, copy the files, keep the log from that
  sequence. Restoring replays the kept log over the copy, exactly as crash
  recovery does.
- **Disaster with no graph copy**: rebuild from the archive by replaying
  facts. This is also how a rule change is recascaded.
- **Warm standby** (later, optional): a second process tails the log.

### 3.6 Observability

Every stage of a batch is timed (prepare, compute, fsync, apply), with queue
depths, submit-to-visible, lease latency, read latency, map and log sizes,
checkpoint time and projector lag, on a Prometheus endpoint and in an
`atomicdb_perf_sample` table. An `explain` endpoint returns, for a position,
the facts and the batch that produced its current values. Section 9 fixes
which numbers are tracked from the v1 baseline onwards.

## 4. Why not the alternatives (short)

- **Keep Django, port the climbs** (PR #334): keeps two writers of the same
  rows for a year and keeps the 190 GB. It fixes the drainer and nothing
  else.
- **Any store behind an ORM or SQL per row**: the cost is the round trip and
  the 8 KB page, not the engine behind it. Postgres with integer ids and
  batched queries from Rust is 2.5 to 6 times faster than the production
  schema in the same harness, and still 17 to 21 times behind.
- **An in-memory graph with a log and snapshots**: measured too, and while
  it runs it is as fast as mapped columns (faster to commit, its log is
  smaller). But a restart reloads everything (3.5 s at 20M positions against
  1 ms, and growing), a snapshot rewrites everything, it needed 3.2 GB of
  private memory at 20M positions against 14 MB, and it has no answer for a
  graph larger than RAM. At 3.5% growth a day that day is within a year.
  The mapped files give incremental checkpoints, instant restart and a
  graceful slope instead of a wall.
- **One of the embedded stores, as it is**: LMDB is the only one that reads
  fast enough, and it pays for the random key map at every commit (20 to
  180 ms for a batch of 32 here; less on Linux, see the report). RocksDB
  and fjall commit well and read 50 times slower. Either would work at
  today's load. Neither leaves room for the thing this plan depends on,
  which is that replaying and recascading the whole graph is cheap.

## 5. The store, by evidence

Full tables in the benchmark report. The decisive ones, all with a durable
commit (fsync) per batch, on a Ryzen 9 5950X with a 1 TB NVMe, Windows 10
(see the report for how this transfers to the Linux production box):

| Real slice of production, 1.9M positions | mmap columns + WAL | in memory + snapshots | LMDB | RocksDB | SQLite | Postgres, integer ids | Postgres, v1 schema |
|---|---:|---:|---:|---:|---:|---:|---:|
| Searches applied per second (expansion, climb, commit), 32 per commit | **10,878** | 16,173 | 1,064 | 1,393 | 568 | 514 | 208 |
| The same, one per commit | **1,130** | 1,173 | 240 | 550 | 222 | 121 | 70 |
| Re-searches per second (propagation only) | **23,297** | 26,383 | 8,379 | 2,014 | 1,409 | 1,084 | 425 |
| Selector descents per second | **685,354** | 739,426 | 60,228 | 4,731 | 1,838 | 796 | 486 |
| Explorer reads per second, 8 readers, writer running | **3,976,122** | 1,178,349 | 719,441 | 57,500 | 129,457 | 7,473 | 6,059 |
| p99 of one such read | **4 us** | 10 us | 23 us | 334 us | 145 us | 1.4 ms | 1.8 ms |
| Bytes on disk per position | **81** | 52 | 148 | 115 | 96 | 341 | 1,086 |
| Restart of a clean store | **1 ms** | 220 ms | 1 ms | 45 ms | 1 ms | | |
| Recovery after kill -9, median | **58 ms** | 1.9 s | 6 ms | 135 ms | 53 ms | 1.3 s | not run |

| Larger graphs | mmap columns + WAL | in memory + snapshots | LMDB | RocksDB | SQLite | Postgres, integer ids | Postgres, v1 schema |
|---|---:|---:|---:|---:|---:|---:|---:|
| 20M positions: searches per second, 32 per commit | **8,744** | 16,543 | 293 | 3,240 | 433 | 514 | 89 |
| 20M: explorer reads per second, 1 reader | **669,535** | 600,487 | 83,475 | 10,430 | 36,150 | 1,179 | 157 |
| 20M: restart | **1 ms** | 3.5 s | 1 ms | 62 ms | 1 ms | | |
| 200M positions, 20 GB of memory available: searches per second, 32 per commit | **4,064** | needs ~32 GB of RAM | 158 | 1,466 | 336 | | |
| 200M: re-searches per second | **16,347** | | 4,509 | 2,598 | 1,534 | | |
| 200M: explorer reads per second, 8 readers | **3,261,758** | | 17,513 | 11,703 | 16,812 | | |
| 200M: store size | **15.5 GB** | | 22.3 GB | 20.9 GB | 19.5 GB | | |
| 200M: bulk load | **70 s** | | 283 s | 420 s | 326 s | | |

All ten stores of the race (these seven, plus redb, fjall and the plain
in-memory reference) ended the suite on the real slice in the same state,
checked by a hash over every record, edge and parent set.

Reading it:

1. **Reads decide it, and reads are what a climb is made of** (about 230
   positions read per search). The mapped columns read a record with a
   pointer addition. LMDB is 5 to 8 times slower, SQLite 18, RocksDB about
   50, Postgres more than 600 with everything cached. None of this depends
   on the operating system.
2. **No off-the-shelf store is good at both halves.** The B-trees pay for
   the key map at commit (35 random keys per search, one dirty page each);
   the log-structured stores commit well and read slowly.
3. **Postgres is not slow because of Python alone.** The production schema,
   queried in batches from Rust, is 52 times slower to apply a search on
   the real slice and 98 times at 20M positions, where its reads fall from
   1,058 to 157 a second because the data no longer fits in its cache. That
   is production's read wait, reproduced on a desk.
4. **Size is speed.** At 200M positions the mapped store is 15.5 GB and
   fitted in the test machine's free memory; the others are 19.5 to 22 GB,
   did not fit, and read from disk. Production's 64 GB would hold any of
   them today, but the smaller store reaches that wall last.
5. **The honest alternative is the in-memory graph**, not another database.
   Section 4 says why the mapped files are preferred.

Caveats that matter (all in the report): the measurements are on Windows and
the Linux cross-check could not be run; LMDB's commit should be considerably
better on Linux; the rules are stand-ins that read less than the real ones;
at 200M the outcome depends on how much memory is free.

**Decision: mapped columns with a redo log for the graph; SQLite (WAL) for
the relational projection; plain append-only files for the log.**

What the decision costs: about 1,500 lines of storage code that we own. What
keeps that safe: the store holds nothing that the log cannot rebuild, the
log format is the only long-lived format, the crash test runs in CI, and
page checksums written at checkpoint let a scrubber find silent corruption.

## 6. The outside stays the same

- **Workers.** Same endpoints, same form fields, same replies and error
  strings (`stale-lease`, `not-your-lease`, `not-leased`,
  `engine-not-accepted`), same engine manifest and auto-update paths. No
  worker needs to update for the cut-over. Leases live in the writer's
  state and in the log, so a restart does not waste a running 2B search.
- **Public API.** `/atomicdb/api/query`, `principal-variations`,
  `backed-source`, `request`, `request-children`, `my-queue`, `my-workers`,
  `queue-bump`, `queue-cancel`, `roots`, `map/v1`: same fields, signs,
  constants and error bodies. A **golden file** of recorded v1 responses
  (keys sampled from production) is replayed against v2 in CI; a difference
  is a failure unless it is a listed semantic decision. API changes are
  announced on Discord first, as agreed after #222.
- **URLs.** `/atomicdb/explore/<key>/` and the rest keep working: keys are
  still sha256 of the canonical FEN.
- **Accounts.** OpenBench users and `Profile.enabled` stay in Django. The
  service verifies credentials against the same password hashes (with the
  5-minute cache v1 has) and trusts the Django session for the site.
- **Rate limits and static files**: nginx, unchanged.
- **Rules of chess.** The same Fairy-Stockfish build the workers run, linked
  as a library. No second implementation of atomic rules. Tablebases through
  `shakmaty-syzygy`, which reads the same atomic Syzygy files.

## 7. Keeping the semantics

The rules are the product. They are ported exactly first, and simplified
only where the owner says so.

1. **The test suite is the specification.** The 4,436 Python tests keep
   running against v2 through a shim: export the test's rows to an in-memory
   v2 graph, run the v2 function, write the result back. Each test that
   exercises a ported function runs on both engines in CI. Tests of things
   v2 does not have (SQL counts, retry paths) are listed and justified.
2. **Golden cases from production.** A recorder around v1's drainer writes,
   for each batch, the rows it read, the payloads, and the rows it wrote: a
   self-contained case. v2 replays each case in isolation. Two weeks of
   production are about three million landings: they cover the paths real
   positions take, without needing the whole graph or the same global order.
3. **Whole-history replay.** v1 kept all 6.2M raw payloads. v2 replays them
   from the start position. The positions and edges that searches create
   and the closures that proofs make must match v1's; values are compared
   and every difference classified (v1's own rules changed along the way,
   so not every difference is a v2 bug).
4. **Shadow.** v2 follows production by tailing three append-only tables by
   id (`ingestjob`, `edge`, `dbevent`), no replication slot needed, and
   reports differences nightly on Discord.
5. **Property tests**: idempotence of a climb, batch-size independence,
   termination on cycles, random DAGs with loops against the Python rules.

Where v1's rules should be simplified is not for this plan to decide. The
candidates, each an owner decision in section 11: wall-clock budgets become
step budgets; the noise cut and the size guards go, because exact
propagation is now cheap; ties break by id instead of row order; claims
live on the edge of the search that made them; proof numbers and
`priority` are computed, not stored.

## 8. Migration

1. **Export** once, from a restored backup (never from the live box): the
   needed columns of `position`, `edge` and the relational tables.
2. **Load**: ids in creation order (first incoming edge id), the derived
   bits (side to move, zeroing move, branching), the key map, the blobs.
   The benchmark loaded 200 million positions into the store in 70 seconds.
3. **Check**: counts by status, closure and proof; a million positions
   compared field by field with Postgres.
4. **Follow**: v2 tails v1 (section 7.4) and stays seconds behind.
5. **Cut over**: stop v1's ingest (workers retry on 5xx by design), let v2
   drain the tail, switch the nginx upstream of `/atomicdb/`. Minutes.
6. **Roll back**: v2's log holds every raw payload it accepted. Switching
   nginx back and feeding those payloads into v1's `IngestJob` queue brings
   v1 up to date with its own code. v1 is kept runnable for four weeks.

## 9. Phases, gates and what gets measured

Measured the same way from the v1 baseline through every phase: searches
applied per second, submit-to-visible (p50/p95/p99), climb batch latency,
lease and submit latency, explorer and API latency, CPU and RAM by unit,
disk bytes per position and per day, stale rows found per sweep pass, and
the differences reported by the shadow. A phase is not passed without its
before/after report.

| Phase | Content | Weeks | Gate |
|---|---|---:|---|
| 0. Baseline | One week of production capture; the recorder; exact census on a restored backup; rerun this benchmark on Linux on that box | 2 | Baseline report; store numbers confirmed on Linux |
| 1. Core | Log, columns, recovery, checkpoint, backup and restore, loader from Postgres, replay tool, crash test in CI | 4 | Full production graph loaded and checked; 1,000 kill -9 trials and a power-cut test clean |
| 2. Rules | Cascade, mate distance, backed values with every rule, loops, proof numbers; shim, golden cases, property tests | 10 to 14 | Shim suite green; golden cases with zero unexplained differences |
| 3. Stage (a) | Fairy-Stockfish library, keys, expansion, showcase, seeding, mate-PV and AND/OR proofs, tablebases, trust gate, revocation | 6 to 8 | Whole-history replay: the same positions, edges and proven closures as v1 |
| 4. Queue | Tasks, lanes and fairness, lease protocol, engine gate, selector and arms | 5 to 7 | Protocol tests against the real worker client |
| 5. Read side | Public API with the golden file, explorer data and pages, projection, Django adapters for the pages that stay | 5 to 8 | API golden file clean; latency targets met |
| 6. Shadow and cut-over | Three weeks of shadow at least, migration rehearsals, cut-over, rollback drill | 4 to 6 | 14 days with zero unexplained differences; the owner's go |

**36 to 49 focused weeks.** Phases 3 to 5 can overlap with 2 if a second
person joins. The estimate assumes the semantics hold still while phase 2
runs; every rule that changes in v1 during that time is paid twice.

## 10. Risks

| Risk | What we do about it |
|---|---|
| **v1 runs out of disk before v2 exists** (about seven weeks at the measured rate) | Owner decision now: more disk or a bigger box, cap growth with the pool valve, drop the indexes nothing reads (`atomic_pos_updated` 6.2 GB with 200 scans ever, `position_status` 2.2 GB, `position_priority` 5.5 GB while the selector runs `--no-priorities`), move old `IngestJob` payloads to the Storage Box |
| The rules keep moving while they are ported | Freeze window for phase 2, or the rule "every semantic PR changes both"; the shadow catches the rest |
| The loop rules (returning lines, pointer loops, best exit) are the hardest to reproduce and the only ones that depend on a clock in v1 | Port them last, behind the golden cases; decide D1 before starting |
| We own a storage engine | Small, rebuildable from the log, crash-tested in CI, checksummed; the benchmark stays in the repo as the regression test |
| Fairy-Stockfish as a library inside a threaded service | Validate thread safety in phase 3; fall back to a small pool of rule processes |
| Measurements were taken on Windows | Phase 0 reruns the harness on Linux before any store code is written; the report lists what could differ |
| One box, no standby | Same as today; the log makes a standby cheap later |
| One or two people understand it | Code in this repo, reviewed like the rest; this plan and the report kept current |

## 11. Decisions for the owner

| # | Decision | Recommendation |
|---|---|---|
| D0 | v1's disk runway (section 10), independent of v2 | Decide this week |
| D1 | Replace wall-clock budgets (2.5 s mate proof, 1 s loop walks) by step budgets | Yes: it is the precondition of replay |
| D2 | Drop the noise cut (`BACKED_EPSILON_CP`) and the size guards; propagate exactly | Yes, after the exact port passes; it removes the sweep |
| D3 | Ties by id, never by row or set order | Yes |
| D4 | Claims stored on the edge of the search that made them, not on the child | Discuss: cleaner, but changes what a second parent sees |
| D5 | Proof numbers and `priority` computed in memory, not stored | Yes |
| D6 | Scope of "from scratch": the whole site in Rust, or the graph, queue and API in Rust with Django rendering the community pages from v2's API | Second option first; pages move when someone wants them to |
| D7 | Relational projection in SQLite inside the service, Postgres only for OpenBench | Yes |
| D8 | Old worker builds without lease tokens (before 2026072203): keep or drop at cut-over | Drop |
| D9 | Where v2 shadows: beside v1 on the same box (needs about 25 GB of RAM and 40 GB of disk on a box that is short of both) or on a second box | Second box |
| D10 | Semantics freeze during phase 2, or double PRs | Freeze, 10 weeks |
| D11 | Code location: a cargo workspace in this repo | Yes |

### Open questions

1. What one `fdatasync` costs on production's md RAID1. Every commit pays
   one; here it was 0.7 ms.
2. What the real rules cost per position. The stand-ins read a record, an
   edge block and the children; the real ones also read the showcase of
   positions with gaps and walk returning lines.
3. Whether the Fairy-Stockfish library can be called from several threads
   of one process, or needs a pool of processes.
4. The key map: an in-place hash table is the simplest thing that works and
   rewrites most of itself at every checkpoint. A sorted base with a small
   delta would write far less. Decide when the write volume is measured on
   the real box.
5. How production's tablebase probes (python-chess today) compare with
   `shakmaty-syzygy` on the same positions. They read the same files; the
   answers must be checked to match before one replaces the other.
