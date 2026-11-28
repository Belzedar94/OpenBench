# AtomicDB v2 storage core (`av2-store`)

Phase 1 of `docs/atomicdb/v2-plan.md` (sections 3.2 to 3.5). This file
records the design decisions taken inside the storage layer, why, and what
was measured. Code: `crates/av2-store`; tools: `crates/av2-tools`.

## 1. What the store guarantees

1. **Durable before visible.** A batch is acknowledged only after its frame
   (facts + redo records) is on disk (`fdatasync`), and no byte of the
   mapped files changes before that. An acknowledged batch survives a
   process kill; whether it survives a power cut is what the power-cut test
   (section 7) checks.
2. **Atomic batches.** Recovery replays whole frames; a frame that is
   incomplete or fails its CRC is a torn tail and is discarded. Readers see
   either all of a batch or none of it.
3. **Replay over any newer state gives the same result.** The checkpoint
   only promises that the files are *at least* at its sequence number; they
   may hold any later frame too (the OS writes pages when it likes, and
   after a kill the page cache holds everything). Every redo record is
   idempotent and replays correctly over a state that is already ahead of
   it. The test `replay_over_a_state_ahead_of_the_checkpoint` exists because
   a first version of the key-map rebuild record broke this property and
   hung recovery (section 4.5).
4. **The log alone rebuilds every change since a checkpoint**, including
   rebuilt key-map shards, so a copy of the files taken while the writer runs
   plus the log from the copy's checkpoint is a valid store (backups).
5. **Lock-free readers.** Readers in other threads never block the writer
   and are never blocked by it, except for the microseconds of a batch apply,
   during which they retry.

## 2. Files

```
<store>/graph/ctl            4 KiB: magic, format version, shard bits, counters
              core.col       32 B per position   (av2_model::Core)
              ext.col        72 B per ext record (av2_model::Ext), arena indexed from core
              out.col         5 B per position   offset+1 of its out-edge block
              parent.col      5 B per position   parent id+1, or LIST|head of a parent list
              fen.col        36 B per position   packed canonical FEN (or a blob reference)
              out.arena      blocks [count u16][child u40 + move u16]*, append-only
              inx.arena      parent-list cells [parent u40][next u40], append-only
              blob.arena     [len u32][bytes], append-only (analysis JSON, witness lines, odd FENs)
              keymap.dir     2^shard_bits entries [region u64][slots u32][count u32]
              keymap.arena   shard regions [Bloom filter][slots of 17 B], append-only;
                             keymap.arena.N after the Nth compaction
              ckpt           two checkpoint slots (generation, sequence, CRC)
              manifest       page checksums of the last checkpoint
<store>/log/NNNNNNNNNNNNNNNN.seg     frames, named by the first sequence number
<store>/archive/NNNN....seg.zst      released segments, facts only (no redo), zstd
```

All integers are little-endian at fixed offsets; ids are 40 bits on disk
(`av2_model::id`). Every file grows on demand by at least half its size;
growing maps the file again and publishes the new mapping to readers
(`arc_swap`), while readers that still hold the old mapping keep reading a
valid prefix.

### Differences from the plan's sketch (section 3.2)

| Plan | Here | Why |
|---|---|---|
| core 20 B | **32 B** | The 48-bit `version` (plan 2.2 point 4) does not fit in 20 B with the other fields; 5 bytes are kept free for the rules port. 12 B more per position. |
| ext 48 B | **72 B** | Exact counters (nodes, backed nodes, time, depth, visits, campaign), three blob references (`last_analysis`, `analysis_passes`, `won_line`) and the queue's two ladder fields (bought and searched rung of the task rows folded out of the queue, 1000-node units; format 4). 4 B are reserved for the showcase bound (`TODO(rules)`). Only 17.7% of positions have one in the production slice. |
| key map 96-bit fingerprint -> id, open addressing | same, **sharded** (4096 shards by default), 17 B slots | One table cannot be resized without rewriting all of it; a shard is rebuilt alone (section 4). |
| Bloom filter 10 bits per position | same, **one blocked filter per shard** (6 probes in one cache line) | The filter is rebuilt with its shard. |
| fen.col ~28 B packed | **36 B**, packed only when unpacking gives back the same string, else a blob | No chess rules in the store; exactness by construction. |
| `analysis.log` | `blob.arena` | One arena for all immutable blobs. |
| parents: 10 B per *extra* parent | **10 B per parent once a position has two or more** | `parent.col` holds one value: a parent, or the head of a list that then holds all of them. |

## 3. Commit, apply, checkpoint, recovery

**Commit** (`Store::commit`): the writer's `Batch` (an overlay on the
committed state, `GraphRead`/`GraphWrite`) is turned into redo records in a
deterministic order (`Batch::finalize`), files are grown if needed, one frame
is appended to the current segment, `fdatasync`, then the records are applied
to the maps between two increments of the epoch, then the batch is
acknowledged.

**Frame**: `[len u32][crc32 u32][seq u64][facts_len u32][facts][redo]*`,
redo = `[file u8][offset varint][len varint][bytes]` (format 2; format 1
used fixed 8 + 4 byte fields), plus three logical records: `0x80` a key-map
shard rebuild, `0x81` a key-map insert (slot + filter bits), `0x82` a
key-map compaction. Every record, logical or not, replays to the same bytes
over a state that is already ahead of it.

**Pipelined commit** (`commit_start` / `batch_after` / `commit_finish`):
`commit_start` finalizes the batch, writes its frame and hands the fsync to
the store's sync thread; meanwhile the writer computes the next batch on an
overlay that reads through the one in flight; `commit_finish` waits for the
fsync and applies. One commit is in flight at a time, frames stay in order,
and nothing is applied before it is durable, so recovery is unchanged. The
writer service (`av2-server`) and the crash test (odd trials) use it.

**Segments** are 64 MiB files **written in place, never extended**: a
background thread (`log::SegPool`) prepares each one by writing zeros over
the whole file (recycling released segments when it can) and syncing it, so
rolling over is a rename and a commit's `fdatasync` flushes data only. No
size change, no block allocation, no unwritten-extent conversion for the
filesystem to journal: on production's ext4 over md RAID1 that is the
difference between 13.8 ms and 6.5 ms per `fdatasync` (phase 0,
`v2-baseline.md`). `fallocate` alone would not do: it leaves unwritten
extents whose conversion is journaled. A zero frame header marks the end of
the written part. Without `prealloc` (tests, tools) segments are plain
appended files.

**Group commit** (`av2-server`'s writer): one thread owns the store. While
batch n's fsync is in flight it gathers whatever arrives; the sync thread
wakes it the moment the fsync completes (a callback into the writer's
channel: polling would cost a 15.6 ms timer tick on Windows); it applies n,
answers n's senders, computes batch n+1 from everything gathered and starts
its fsync. Batches therefore grow with the load and the latency of an input
is at most about two fsyncs. If the batch fills up (`max_batch`) before the
fsync completes, n+1 is computed on an overlay over n (the pipelined path
above). If the rules reject a batch of several inputs, each is retried
alone. `av2_store::group::Writer` is the same loop without the pipeline or
the metrics, kept for tools.

**Checkpoint** (own thread, `Shared::checkpoint`): read the applied sequence
number `s`, then load the maps (in that order: maps published before frame
`s` was applied cover every file length it needs), flush every map and
`fdatasync` every file, write the page manifest, write the checkpoint slot
`(generation+1, s)` and fsync it, then release the segments whose frames are
all `<= s`. The writer does not wait for any of it. Released segments are
rewritten without their redo records, compressed with zstd (level 3 by
default), into `archive/` (the facts are the system of record) unless the
store is configured to delete them. The sequence number, the key-map
generation and the maps are read together while the epoch is even, and the
checkpoint slot records the generation: recovery maps that arena file.

**Recovery** (`Store::open`): take the valid checkpoint slot with the highest
generation; read the segments in order; replay every frame with a sequence
number above the checkpoint, checking that sequence numbers are consecutive;
overwrite a torn frame at the end of the last segment with zeros and make
that durable (only the last segment may end badly: anywhere else it is
corruption and opening fails); resume writing in the last segment after its
last valid frame.

Why a page written early by the OS is harmless: nothing is applied to a map
before its frame is durable, so any page on disk holds only committed bytes;
recovery rewrites every byte that frames after the checkpoint touched.
Why a torn page write is harmless: a page that is half old and half new
differs from the correct page only in bytes some frame after the checkpoint
wrote, and those are rewritten by replay; bytes no such frame touched are the
same in the old and the new version. (Silent corruption, where a device
returns bytes nobody wrote, is what the page checksums are for.)

## 4. Key map

### 4.1 Variant chosen
Sharded open addressing: `2^shard_bits` shards (12 by default), shard =
top bits of the fingerprint, slot = next bits (multiply-shift), linear
probing; slot = 12 B fingerprint + 5 B id+1. A blocked Bloom filter per
shard (10 bits per key at maximal load, 6 probes in one 64-byte block)
answers most lookups of positions that do not exist yet (almost every lookup
an expansion makes) without touching the slots.

### 4.2 Growth
A shard is rebuilt when an insert would push it past 3/4 load, into a fresh
region at the tail of `keymap.arena` with slots for 3/8 load (doubling, so
the cost per insert stays constant). The bulk loader builds shards at 0.6
load, so the first wave of rebuilds comes after +25% growth (about a week at
today's 7M positions a day on 200M; the wave logs only one small record per
shard, section 4.5).

### 4.3 Garbage and compaction
The arena is append-only: a rebuilt shard's old region becomes garbage.
With doubling, garbage stays just under the live size forever, so the
trigger is **garbage >= live / 2** (and at least `km_compact_min`, 64 MiB by
default): the arena stays under 1.5x its live part. Steady state is between
17/0.75 and 17/0.375 bytes of live slots per position (23 to 45 B) plus at
most half of that in garbage.

A compaction is **incremental**: it moves the live regions into the next
arena generation (`keymap.arena.<generation+1>`) at most `MOVE_BUDGET`
(1 MiB) per commit, a shard at a time in shard order, so the writer never
stops for the whole copy. The two generations live in two file slots by
parity (`F_KM_ARENA` for even generations, `F_KM_ARENA_ODD` for odd ones);
each directory entry says in which slot its region is (bit 63 of the
region). While it runs, rebuilt shards go straight into the next
generation and inserts go wherever their shard currently is. Records:
`MIGRATE_START` (the next generation's file takes its slot), one `MOVE` per
shard (the old region, frozen from then on, and the new one: replay copies
it again), `MIGRATE_END` (the next generation becomes current). The
compaction state (next tail, next live bytes, cursor) is in `ctl`.

A compaction starts only when the last checkpoint is at the current
generation and no backup holds the log: then no recovery can need the
generation before, whose slot the new one takes. That makes the start
depend on checkpoint timing; the logical state never does. The checkpoint
records the generation; files of older generations are deleted after it
(best effort on Windows while a reader still maps one).

Measured on the production slice, landing 32 searches per commit until the
first compaction: before it, commits took 3.0 ms on average (p99 9.4 ms,
max 19.8 ms); the compaction moved 140.5 MB down to 93.9 MB of live
regions over **89 commits**, which took 4.9 ms on average (p99 14.1 ms,
max 14.3 ms). The first version copied everything in one 48 ms commit,
which would have been a stall of about 2 s at 200M positions; now that is
about 5,500 commits of +1 to 2 ms each.

### 4.4 Inserts
An insert is one logical record `(slot offset, fingerprint, id, filter block
offset)`: applying it writes the slot and sets the key's six bits in the
filter block. The filter costs no log bytes (format 1 logged the whole
64-byte block), and setting bits is idempotent. The directory entry's count
is written as 4 bytes, not the whole entry.

### 4.5 The rebuild record
A shard rebuild is logged as one logical record
`(shard, old region, old slots, old count, new region, new slots)`, not as the
bytes of the new region: replay recomputes the new region from the old one.
The old region is never written again after the rebuild (the arena is
append-only), so the record replays to the same bytes even over a state that
is already ahead of it. A first version that read the *current* directory
entry instead of naming the old region was not idempotent and hung recovery
from an old checkpoint; it is kept as a test.

## 5. Readers

### Read-path hints (Linux)
Phase 0 measured the benchmark's mapped store reading at half the speed of
the in-memory graph on the production box (level on Windows): page-table
and TLB cost of 4 KiB file pages, and first-touch minor faults. The
mappings take two hints from the environment so that they can be compared on
that box without a rebuild: `AV2_MMAP_POPULATE=1` (`MAP_POPULATE`: page
tables filled at open and after growth) and
`AV2_MMAP_ADVICE=random|willneed|hugepage|sequential|normal` (`madvise`).
`scripts/prod/run_core_prod.sh` runs the read benchmark with each; the
result decides the default. Transparent huge pages do not apply to ext4
file mappings, so if the hints do not close the gap the alternative is an
anonymous in-memory copy of the hot columns (core, out, parent, key map)
refreshed from the maps, which is a design change and a separate PR.

### Lock-free reads

`Reader::read(|view| ...)` runs the closure on the committed graph and
repeats it if a batch was applied meanwhile (an epoch counter, odd during an
apply: a seqlock over the whole batch). Every read is bounds-checked against
the mapping it uses, so a racing read can see stale bytes but never fault.
The closure must have no side effects other than building its result.

## 6. Page checksums, scrubber, backup

**Manifest**: at each checkpoint (every `manifest_every` checkpoints), the
xxh3 of every 1 MiB chunk of the live part of every file, for chunks no batch
touched after the checkpoint's sequence number (the writer marks the chunks a
frame touches *before* applying it; the checkpoint re-checks after hashing).
Unchanged chunks keep their previous checksum. **Scrubber**
(`Store::scrub`, `av2-check --scrub`): recompute every chunk the manifest
covers and nothing touched since; a mismatch is a page that changed without
a frame. On the first full manifest of a 200M store this is about 15 GB of
hashing on the checkpoint thread (seconds); afterwards only changed chunks.

**Backup while running** (`Store::backup`, `BackupHandle`): checkpoint (by
asking the checkpoint thread, so as not to starve behind it), pin the log
from that sequence number, copy the live prefix of every graph file, write a
checkpoint record for the copy, copy the log segments that may hold later
frames, unpin. Opening the copy replays that log: restore is recovery.
Tested while a writer commits (`backup_while_running_restores_to_a_committed_state`).
`av2-perf` copies the 1.56M-position slice this way in 0.2 s.

## 7. Crash and power-cut tests

**kill -9** (`av2-crashtest`, CI runs 60 trials on Linux): a child commits
synthetic batches (a pure function of the graph and the sequence number:
positions with FENs, edges, transpositions, value rewrites, ext records,
blobs) and prints `ACK n` after each durable commit; the parent kills it
(`TerminateProcess` / `SIGKILL`) at a random moment after its first
acknowledgement, or (20% of trials) a few milliseconds after start, which
lands in recovery; reopens the store; checks that no acknowledged batch is
missing and that at most the one in flight was added; and compares the
digest of the whole state (`digest::digest`: records, edges, parents, FENs,
blob contents, key map) with an **independent in-memory model**
(`mem::MemGraph`) that applied exactly the recovered number of batches.
Segments of 256 KiB, a checkpoint every 512 KiB of log, 8 key-map shards and
a 2,048-position initial capacity make kills land in segment rolls,
checkpoints, archive rewrites, shard rebuilds and file growth.

**Power cut** cannot be simulated on this Windows tower. The script
`scripts/powercut/dm-log-writes.sh` does it on Linux the way fstests
generic/482 does: the store runs on a filesystem on a `dm-log-writes`
device, every acknowledgement is marked in the write log, then `replay-log`
rebuilds the device at every mark and at random write entries in between
(states a power cut can leave, torn writes included), and
`av2-crashtest --verify` checks each image: recovery succeeds, the
acknowledged batch is there, the state equals the model. Phase 0/1 runs it
on the Linux box (ext4 and xfs) before the gate.

## 8. Measurements

Windows 10, Ryzen 9 5950X, 1 TB NVMe, release build, one fsync per commit;
the production slice of the benchmark (1,560,369 positions and 1,994,101
edges around the root, exported read-only on 27-Nov-2026). The plan's
benchmark numbers are for the same slice and machine; its workloads are
similar in shape, not identical (see notes).

| | v2 store (this PR) | benchmark `mmap` store | note |
|---|---:|---:|---|
| Load from CSV | **3.4 s** | | CSV parsing dominates (key pass 0.5 s, edges 0.9 s, positions 1.5 s) |
| Field-by-field check against the export | 1,560,369 rows, **0 differences**; 1,994,101 edges, 0 missing | | every v1 column of the export, counts by status/closure/proof equal |
| Bytes per position after load | **133.6** (97.6 without the FEN column) | 81 | 32 core, 11.3 ext, 5 out, 5 parent, 36 FEN, 9.1 edges, 5.2 parent lists, 30.0 key map; the benchmark stored no FEN and had 32-bit ids and 16 B key slots |
| Landing, 1 per commit | **1,020 to 1,054 /s**, p50 0.83 ms, p99 2.4 ms | 1,130 /s | fsync 0.78 ms mean |
| Landings, 32 per commit | **6,024 to 6,238 /s**, p50 5.0 ms | 10,878 /s | 2.1 ms of the 5.1 ms is building the synthetic batch (FEN generation and packing); every landing writes 20 to 38 positions with FENs, a 1.2 KB blob and a 1.3 KB fact; the key-map rebuild wave (section 4.2) is inside this run |
| Climbs, 32 per commit | 38,100 /s | 23,297 /s (re-searches) | different stand-in rules |
| Explorer page, 1 reader | 1.59 M/s, p99 1.4 us | | key lookup, record, moves and their records, parents; 70% interior |
| Explorer page, 8 readers | 10.9 M/s, p99 1.7 us | | |
| Explorer page, 8 readers, writer landing at full speed | **10.0 M/s, p99 1.9 us**, writer 996 /s | 3.98 M/s, p99 4 us | |
| Restart after clean close | **5 ms** quiet; 35 ms right after the perf run | 1 ms | 11 files mapped, segment scan, a new segment created |
| Recovery, 5,000 frames (49 MB) to replay | **140 to 148 ms** | 102 to 343 ms (256 MB) | |
| kill -9 trials (crash test) | **1,000 of 1,000 clean**: no acknowledged batch lost, state equal to the model every time (208 kills during start/recovery) | 125 | 366 s; graphs up to ~70,000 positions, reset every 100 trials |
| Recovery after kill -9 | **p50 67 ms**, p90 81 ms, p99 119 ms, max 229 ms (87 frames replayed at the median, 419 at most) | 58 ms median | small segments and frequent checkpoints on purpose |
| Checkpoint of the 1.95M-position store after the runs | 310 ms (first manifest hashes everything) | 660 ms worst, on the writer thread | here on its own thread |
| Online backup of the slice | 0.2 s | | |

### 8.1 After the follow-ups (format 2, prepared segments, pipelining)

Same machine and slice, while about ten other agents built on the tower
(numbers move by 10 to 20% between runs; each line is one of two runs).
"Full" landings write 20 to 38 positions with FENs, a 1.2 KB blob and a
1.3 KB fact each; "lean" ones carry what the benchmark's did (no FEN, blob or
fact). One fsync per commit; `--fsync-delay-ms 16` adds production's
median fdatasync (phase 0) after each.

| 32 landings per commit | searches/s | per commit (ms): rules+overlay / finalize / write / fsync (wait) / apply | frame |
|---|---:|---|---:|
| before (#340), full | 5,400 to 6,100 | 2.1 / 3.2 for finalize+write+fsync+apply together | 299 KB |
| full | 8,104 | 1.10 / 0.31 / 0.07 (+0.31 growth) / 1.80 / 0.27 | 200 KB |
| full, pipelined | **10,842 to 10,871** | 0.93 / 0.34 / 0.08 / 0.95 wait / 0.23 | 200 KB |
| lean | 14,775 | 0.36 / 0.25 / 0.04 / 1.23 / 0.19 | 84 KB |
| lean, pipelined | **15,372 to 17,324** | 0.44 / 0.26 / 0.04 / 0.84 wait / 0.21 | 84 KB |
| benchmark `mmap` store, its own landings | 10,878 | | |
| full, 16 ms fsync | 1,558 | p50 20 ms, p99 29 ms per commit | |
| full, 16 ms fsync, pipelined | 1,646 | | |

What changed in the commit path: redo records with varint headers (3,949
records and 299 KB per commit became 2,100 and 200 KB), filter bits derived
from insert records instead of logged 64-byte blocks, one run for the parent
words of new positions, no FEN run without FENs, an allocation-free FEN
check, a cheaper hash for the overlay's key map, no fsync on file growth,
and the pipeline.

Through the writer service (`av2-server`, group commit, stub landings,
closed-loop clients each submitting when its previous landing is
acknowledged):

| fsync | clients | landings/s | mean batch | submit to visible |
|---|---:|---:|---:|---|
| tower (0.8 ms) | 1 | 1,130 | 1 | mean 0.9 ms |
| tower | 32 | 6,353 | 16 | mean 5.0 ms, p99 <= 25 ms |
| tower | 128 | 10,476 to 10,822 | 64 | mean 12 ms, p99 <= 100 ms |
| +16 ms | 1 | 56 | 1 | mean 17.8 ms (one fsync) |
| +16 ms | 16 | 429 | 8 | mean 37 ms |
| +16 ms | 128 | 2,740 | 64 | mean 47 ms, p99 <= 100 ms |
| +16 ms | 512 | 8,830 | 255 | mean 58 ms, p99 <= 250 ms |
| +16 ms, open load 64/s (5x production's peak) | 64 at 1/s | 64 | 32 | mean 46 ms, p99 <= 250 ms |
| +16 ms, open load 640/s | 64 at 10/s | 637 | 32 | mean 35 ms, p99 <= 100 ms |

Closed-loop clients split into two groups that alternate between the batch
in flight and the next one, hence a mean batch of half the clients and a
latency of about two fsyncs; with 16 ms fsyncs the writer still takes 8,800
landings a second, 680 times production's peak.

Other numbers: explorer pages 10.1 M/s from 8 readers (8.8 M/s with the
writer landing at full speed, p99 2.2 us); recovery of 5,000 frames (33 MB)
194 ms; restart 13 ms (it reads the frames of the last segment and a 16 MiB
window after them, not the whole 64 MiB); kill -9: **1,000 of 1,000 clean**
with compaction at a small threshold and half the trials pipelined
(recovery p50 62 ms, p99 99 ms), then 300 of 300 after the bounded read;
archive: 194.7 MB of facts became 5.2 MB with the pure-Rust zstd encoder
(about zstd level 1; the synthetic payloads repeat, real ones will compress
less).

### 8.2 On the production box (28-Nov-2026, 10:10-10:23 UTC)

i7-8700 (12 threads), ext4 on md RAID1 over two consumer NVMe, kernel
6.8, transparent huge pages `madvise`. The box was serving v1 at a load
average of 9 to 21 the whole time, so every number below includes that
contention. Static musl binaries, each run capped at 6 GB, `nice 10`, idle
I/O class (`scripts/prod/run_core_prod.sh`, then an `--explore-only` run).

| | production | tower |
|---|---:|---:|
| fsync of a commit (prepared segment, written in place), 1 landing | 7.8 to 10.7 ms mean | 0.78 ms |
| 1 landing per commit | 90 to 122 /s | 1,044 /s |
| 32 landings per commit | 1,660 to 2,800 /s (p50 9 to 22 ms) | 8,104 /s |
| 32 lean landings per commit | 2,030 to 2,850 /s | 14,775 /s |
| writer service, 1 client | 105 /s, mean 9.5 ms to visible | 1,130 /s |
| writer service, 128 clients | 2,239 /s, batches of 64, mean 57 ms (p99 <= 1 s) | 10,476 /s |
| writer service, open load 64/s (5x production's peak) | 62 /s, mean 65 ms | |
| restart / recovery of 500 frames | 40 to 53 ms / 68 to 109 ms | 13 ms / |
| background checkpoint of 225 MB | 1.3 to 1.9 s (writer not stopped) | 0.34 s |
| kill -9 | **200 of 200 clean**, recovery p50 11 ms | |

The fsync is the whole story of the commit path: in place it costs 8 to 11
ms here, against 13.8 ms for an appending write and 16.4 ms with `fsync`
measured in phase 0. Pipelining does not help on this disk (2,100 to 2,200
landings a second pipelined against 2,400 to 2,800 sequential: the compute
it hides is 2 ms of a 12 ms commit); group commit does, by growing the
batch.

**The read path: no 2x gap for this store.** The phase-0 finding (the
benchmark's mapped store reading at half the speed of the in-memory graph
on Linux) does not hold for `av2-store`. `av2-perf --explore-only` reads
explorer pages with the same code from the mapped files and from a copy of
the same bytes in anonymous memory (eligible for transparent huge pages):

| production, explorer pages/s | mmap | anonymous copy |
|---|---:|---:|
| 1 reader, round 1 / round 2 (no hint) | 1.30 M / 1.01 M | 1.32 M / 0.98 M |
| 4 readers, round 1 / round 2 (no hint) | 3.48 M / 2.55 M | 3.31 M / 2.24 M |
| 1 reader, `MAP_POPULATE` (two rounds) | 0.97 M / 1.04 M | 1.04 M / 1.05 M |
| 1 reader, `madvise(HUGEPAGE)` (two rounds) | 1.00 M / 1.03 M | 1.04 M / 1.10 M |

Mapped and anonymous are within the noise of a box at load 15 to 20, with
and without hints; the full run gave 1.26 to 1.55 M pages/s for one reader
and 4.6 to 5.2 M for four across the four hint settings, also within the
noise. Through the `Reader` (epoch check, `arc_swap` load per read) pages
cost 10 to 20% more than through a plain `View`. So no hint becomes a
default, and an in-memory copy of the hot columns is not worth building on
these numbers.

## 9. Not done here (and where it goes)

- Restart time: 5 ms on a quiet store here; Linux numbers come from phase 0.
- The relational projection (`meta.sqlite`) and the projector: phase 5.
- The power-cut test has only been written, not run (needs the Linux box).
