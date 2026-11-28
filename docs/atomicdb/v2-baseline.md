# AtomicDB v2, phase 0: the v1 baseline and the store on Linux

Status: **first report** (28-Nov-2026), written for [`v2-plan.md`](v2-plan.md),
section 9 (phase 0). Three things are here:

1. **The v1 baseline.** These are the numbers every later phase is compared
   with, measured the same way from now on (section 1). The sampler went
   live on 28-Nov at 07:22 UTC. This first version covers the first hours.
   The 24-hour and one-week tables come from the same script
   (`Scripts/atomicdb_perf_report.py`) and replace the tables below as the
   data comes in (section 1.5).
2. **The golden-case recorder.** It runs on production around v1's drainer
   and stage (a) (section 2).
3. **The storage benchmark rerun on the production box.** This is the gate
   of phase 0 ("store numbers confirmed on Linux"), together with the cost
   of one `fdatasync` on the md RAID1, which is open question 1 of the plan
   (section 3).

**In short.**

- **One durable commit costs about 16 ms on production's md RAID1**, against
  0.7 ms on the desktop where the store race ran (open question 1).
  - About half of it is ext4's journal commit for a growing file, which it
    shares with Postgres. Overwriting a preallocated log brings it to
    6.5 ms at the median.
  - The other half is the cache flush of two consumer NVMe drives (Samsung
    PM981, no power-loss protection).
  - For v2 this makes preallocated log segments and group commit
    requirements, not options. Even so, the box sustained 700 to 1,550
    searches a second at 32 per commit, which is 50 to 500 times
    production's rate.
- **The store choice holds on Linux.**
  - Crash recovery passed every time: 80 `kill -9` trials, nothing lost,
    the state identical to the reference each time. The mmap store
    recovered in 44 ms at the median.
  - The same inputs give the same final state on Linux as on Windows.
  - Reads keep their order: the mmap store reads 8 to 27 times faster than
    redb and fjall.
  - Ingest no longer separates the stores on this box, because every
    store waits for the same `fdatasync`.
  - Two things to look at in phase 1: on Linux the mmap store reads at
    half the speed of the in-memory graph (level on Windows), and its
    checkpoint flush on the writer thread costs 0.6 s at p99.
  - Not run: LMDB, SQLite and RocksDB, because the box has no C compiler,
    and the 200M graph.
- **The v1 baseline is being measured**, every minute since 28-Nov
  07:22 UTC. The first clean window (07:50 to 09:00) gives:
  - 2.4 searches a second;
  - a search's own result visible 0.8 s after the submit at the median
    (31 s at p99), and its ancestors climbed after 2.1 s (33 s at p99);
  - a climb batch takes 0.18 s at the median (4.3 s at p99);
  - lease 162 ms and submit 31 ms at the median;
  - `/api/query` 13 ms and explorer pages 238 ms at the median;
  - 1,305 bytes per position;
  - 90 stale rows found per sweep pass on average.
- **Something the baseline found:** the plan's "submit to climbed" (p50
  1.2 s, p99 760 s) was in fact submit to stage (a). The drainer's write
  never moves `IngestJob.updated`.
- **Golden cases are being recorded** for 20% of the climbs and 20% of the
  landings.
  - The recorder adds 18 ms at the median to a sampled climb (+8%), about
    1.6% of the drainer's time overall.
  - It writes about 1.4 GB a day, so the 20 GB cap holds about two weeks
    of production.

## 1. The v1 baseline

### 1.1 What is measured and how

The measuring code is in PR #336 (sampler) and PR #338 (recorder), both into
`spell-runner`.

| Plan metric | Source | How |
|---|---|---|
| Searches applied per second | `atomicdb/perf_events.py` | one line per applied job (`ingest_queue.process_job`) |
| Submit to visible, p50/p95/p99 | the same events plus `atomicdb_ingestjob` by primary key | **own result visible**: job `created` to `updated` at DONE. **Climbed**: job `created` to the commit of the drainer batch that climbed it (`commit_t` of the `drain` event, which names its jobs) |
| Climb batch latency | `drain` events | the climb transaction (`climb_ms`), and the batch including the after-commit work (`ms`) |
| Lease and submit latency | second nginx log with `$request_time` and `$upstream_response_time` | `/atomicdb/api/{lease,heartbeat,submit,release}` |
| Explorer and API latency | the same log | `explore`, `goto`, `lite`... and `/atomicdb/api/*` by route class (`api_query` on its own) |
| CPU and RAM by unit | cgroup v2 `cpu.stat`, `memory.current`, `memory.stat` | every `atomicdb-*` unit, `openbench` (gunicorn), `nginx`, `postgresql@16-main` |
| Disk bytes per position and per day | `pg_database_size`, `pg_total_relation_size`, `pg_class.reltuples`, `n_tup_ins`, `statvfs` | file stats and estimates only |
| Stale rows found per sweep pass | `DBEvent BACKED_SWEEP` (`payload.stale`, `changed`, `checked`) | read through the `(kind, ts)` index |
| Propagation queue depth | `atomicdb_propagationseed` (a small table), the partial index of open ingest jobs | count, oldest age |
| (context) | `/proc/diskstats`, `loadavg`, `meminfo`, `pg_stat_database`, `pg_stat_activity` | per-minute rates |

A timer runs the sampler every minute, as a plain script with no Django
start-up, at nice 10 and idle I/O class. It never scans a big table. Each
sample takes about 0.2 s, and the script writes that cost into every sample
(`sample_ms`). The events cost about 16 microseconds each to write
(measured on the tower). The output is one JSON line a minute in
`/var/lib/atomicdb-perf/samples-YYYYMMDD.jsonl`, plus the raw events in
`events-YYYYMMDD.jsonl` (kept 14 days) and the nginx timing log
(`/var/log/nginx/atomicdb-timing.log`, rotated daily like the others,
14 days). The report script reads these raw sources for exact percentiles.

### 1.2 Something the baseline found before it measured anything

`IngestJob.updated` **does not move** when the drainer writes a job's
`summary['propagated']`, because `propagation_queue._record` uses
`bulk_update`, which does not run `auto_now`. So "job `created` to its last
`updated`" is submit to **stage (a)**, not submit to climbed. That is how
the plan's section 1 measured "submit to climbed: p50 1.2 s, p90 4.9 s,
p99 760 s", so that row describes stage (a). The new `drain` events carry the commit
time of every climb, and the tables below give both numbers.

### 1.3 The first window: 28-Nov, 07:50 to 09:00 UTC

This is the first clean window: after the benchmark of section 3 (07:19
to 07:49, excluded) and after the golden-case recorder reached its working
rate (07:50). It is a Saturday morning and **70 minutes only**: enough to
fix the method and the orders of magnitude, not enough for a baseline. The
24-hour and one-week versions replace it (section 1.5). Every latency
percentile is exact, computed from raw events and raw nginx lines.

Window: 2026-11-28 07:50 UTC to 2026-11-28 09:00 UTC; 1.2 h measured, 70 minute samples, 13,547 events.

#### Throughput

| Metric | Value |
| --- | ---: |
| Searches applied (stage a), total | 10,177 |
| Searches applied per second, mean | 2.42 |
| Per second by minute, p50 / p95 / max | 2.15 / 4.77 / 7.32 |
| Landings climbed per second, mean | 2.40 |
| Failed job attempts | 3 |
| Climb batches | 3,367 |

#### Latency of a search (ms)

| Metric | n | p50 | p90 | p95 | p99 | max |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Submit to own result visible (job DONE) | 10,177 | 825 | 2,442 | 6,734 | 30,722 | 34,198 |
| Submit to climbed (drainer commit) | 10,071 | 2,075 | 8,020 | 14,034 | 33,370 | 39,675 |
| Stage (a) apply, one job | 10,177 | 146 | 1,402 | 1,556 | 2,061 | 5,731 |
| Climb transaction, one batch | 3,367 | 181 | 1,048 | 1,772 | 4,289 | 14,665 |
| Climb batch incl. after-commit work | 3,367 | 181 | 1,048 | 1,772 | 4,384 | 14,665 |
| Landings per climb batch | 3,367 | 2 | 6 | 8 | 16 | 108 |
| Climb transaction, golden-case batches only | 664 | 193 | 1,097 | 1,740 | 4,804 | 6,619 |
| Climb transaction, other batches | 2,703 | 179 | 1,014 | 1,772 | 4,277 | 14,665 |

#### HTTP latency by route class (request time as nginx saw it, ms)

| Class | Requests | p50 | p90 | p95 | p99 | max | upstream p50 | upstream p99 | 5xx | 429 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| lease | 10,159 | 162 | 357 | 493 | 843 | 1,809 | 162 | 842 | 0 | 0 |
| heartbeat | 18,875 | 43 | 102 | 237 | 797 | 2,101 | 42 | 796 | 0 | 0 |
| submit | 10,192 | 31 | 95 | 129 | 377 | 1,545 | 28 | 369 | 0 | 0 |
| api_query | 31,387 | 13 | 78 | 114 | 276 | 1,327 | 13 | 275 | 0 | 0 |
| api_public | 15,173 | 390 | 873 | 1,179 | 2,161 | 36,699 | 362 | 1,749 | 0 | 100 |
| explore | 5,136 | 238 | 690 | 1,005 | 2,699 | 60,131 | 294 | 2,821 | 0 | 489 |
| home | 7 | 276 | 2,600 | 2,600 | 2,600 | 2,600 | 276 | 2,600 | 0 | 0 |
| pages | 308 | 1 | 154 | 295 | 2,833 | 9,725 | 1 | 2,833 | 0 | 0 |
| engines | 137 | 0 | 45 | 265 | 494 | 501 |  |  | 0 | 0 |

#### CPU and memory by unit

CPU in % of one thread (the box has 12), per minute; memory is the cgroup's `memory.current` (includes page cache) and its anonymous part, MiB.

| Unit | CPU mean | CPU p95 | Mem mean | Mem max | Anon mean | Anon max |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| postgresql@16-main | 285.2 | 572.8 | 52,727 | 57,876 | 258 | 299 |
| openbench | 98.4 | 149.9 | 3,346 | 5,875 | 3,171 | 5,691 |
| atomicdb-propagate | 15.7 | 26.6 | 222 | 272 | 209 | 240 |
| atomicdb-ingest4 | 14.6 | 35.2 | 273 | 287 | 271 | 284 |
| atomicdb-ingest5 | 14.5 | 35.0 | 268 | 283 | 266 | 280 |
| atomicdb-ingest3 | 14.3 | 37.0 | 277 | 297 | 275 | 295 |
| atomicdb-ingest6 | 14.1 | 34.9 | 274 | 302 | 271 | 299 |
| atomicdb-ingest2 | 13.8 | 39.6 | 274 | 286 | 271 | 283 |
| atomicdb-ingest8 | 13.7 | 38.0 | 274 | 285 | 270 | 282 |
| atomicdb-ingest | 13.7 | 33.1 | 276 | 290 | 273 | 285 |
| atomicdb-ingest7 | 13.2 | 32.9 | 273 | 287 | 271 | 284 |
| atomicdb-backfill1127 | 10.9 | 29.4 | 153 | 241 | 128 | 214 |
| atomicdb-backed-sweep | 6.7 | 45.1 | 168 | 194 | 167 | 193 |
| nginx | 1.2 | 2.0 | 150 | 159 | 27 | 28 |
| redis-server | 0.5 | 1.0 | 62 | 69 | 40 | 44 |
| atomicdb-perf-sample | 0.1 | 0.2 | 23 | 25 | 22 | 23 |
| atomicdb-selector | 0.0 | 0.1 | 34 | 113 | 31 | 111 |

#### Disk and growth

| Metric | Value |
| --- | ---: |
| Database size at the end, GB | 271.4 |
| Positions (estimate) at the end | 207,942,372 |
| Bytes per position, whole database | 1,305 |
| Bytes per position, position + edge tables | 999 |
| New positions per day (inserts) | 7,110,426 |
| Database growth per day, GB | 11.90 |
| Root file system growth per day, GB | 21.94 |
| Free at the end, GB | 444 |
| md2 utilisation %, mean / p95 | 85 / 94 |
| Load average (1 min), mean / p95 | 9.5 / 17.5 |

#### Queues and the stale-value sweep

| Metric | mean | p50 | p95 | max |
| --- | ---: | ---: | ---: | ---: |
| Propagation queue depth (seeds waiting) | 3.9 | 1 | 17 | 46 |
| Oldest seed waiting, s | 1.1 | 0 | 9 | 12 |
| Ingest jobs pending | 4.8 | 0 | 7 | 234 |
| Stale rows found per sweep pass | 89.6 | 15 | 987 | 987 |
| Rows changed per sweep pass | 1,631.4 | 17 | 17,849 | 17,849 |
| Rows checked per sweep pass | 18,361.5 | 18,444 | 26,654 | 26,654 |
| Sweep passes in the window | 19 |  |  |  |

### 1.4 Reading the first window

- **Throughput:**
  - 2.4 searches a second on average, 7.3 at the busiest minute, which is
    the order of the plan's 2.5 average and 13 at the peak;
  - the drainer merged 2 landings per batch at the median and 108 at the
    largest;
  - about 7.1 million new positions a day at this pace, which matches the
    plan's 7 million.
- **Two submit-to-visible numbers, both new:**
  - a search's own result is on its page 0.8 s after the submit at the
    median, 31 s at p99. The tail is the ingest queue: up to 234 jobs
    waited in this window;
  - its parents have climbed 2.1 s after the submit at the median, 33 s at
    p99;
  - stage (a) itself takes 0.15 s at the median and 1.4 s at p90. That p90
    is the wall-clock mate-proof budget, 1.0 s on production.
- **Climb batch latency:** 0.18 s at the median, 4.3 s at p99, 14.7 s at
  the worst.
- **Worker protocol:** lease 162 ms at the median and 843 ms at p99; submit
  31 and 377 ms; heartbeat 43 and 797 ms. No 5xx.
- **Readers:**
  - `/api/query` answers in 13 ms at the median and 276 ms at p99;
  - the other public API calls in 390 ms and 2.2 s;
  - explorer pages in 238 ms and 2.7 s, with a 60 s worst case and 489
    rate-limited (429).
- **Resources:**
  - Postgres uses about 2.9 CPUs on average and 5.7 at p95 of the minutes,
    with 52 GB of memory (almost all page cache);
  - gunicorn about one CPU and 1.2 to 5.9 GB (it grew from 1.3 to 5.9 GB
    at 07:47, in workers both older and newer than the deploys);
  - the eight ingest writers about 14% of a CPU each, the drainer 16%;
  - md2 is 85% busy on average and 94% at p95; the load average is 9.5.
- **Disk:**
  - **1,305 bytes per position** over the whole database, 999 for the
    position and edge tables;
  - 271 GB in all;
  - the database grew about 12 GB a day at this hour's pace. One hour is
    a noisy estimator: the plan measured 8 GB a day over 22 hours, and the
    day-long window will settle it.
- **The sweep:** 19 passes of `sweep_stale_backed`, 90 stale rows found
  per pass on average (15 at the median, 987 at the worst), up to 17,849
  rows rewritten in one pass.
- **Propagation queue:** 4 seeds waiting on average, 46 at most, the oldest
  12 s old. The drainer kept up during this window.

### 1.5 Reproducing these tables, and the 24-hour and one-week versions

On the production box (read-only: raw files plus primary-key lookups of
the jobs in the window):

```
cd /opt/openbench; set -a; . /etc/openbench/atomicdb-pg.env; set +a
.venv/bin/python Scripts/atomicdb_perf_report.py \
    --since 2026-11-28T07:50 --until 2026-11-29T07:50
```

A window that starts before 07:50 must add `--exclude 2026-11-28T07:19/2026-11-28T07:50`,
the benchmark of section 3, which ran on the same box. The first full day
ends on 29-Nov at 07:50 UTC and the first full week on 5-Dec. Each
replaces the tables of section 1.3 as it completes.

## 2. The golden-case recorder

PR #338, `atomicdb/golden_cases.py`. A sampled fraction of the drainer's
batches (`propagation_queue.drain` → `drain_batch`) and of the stage (a) jobs
(`ingest_queue.process_job` → `apply_job`) is written as a self-contained
case. The case holds:

- the inputs: the job and its payload, or the PropagationSeed rows a climb
  claimed;
- what stage (a) prepared outside its transaction: the mate proofs, which
  run on a wall-clock budget (D1), and the tablebase verdicts;
- every statement on the AtomicDB connection, in order, with parameters,
  row counts and the rows returned. Each returned field carries its table
  and column, so `pre_image()` rebuilds the first image of every row read.
  Positions joined from an edge are attributed through the join column;
- at each COMMIT, the position rows written so far, read inside the
  transaction, as differences from the pre-image;
- the rule constants, settings, a sha256 of the rule modules, wall-clock
  times, the result and the recorder's own overhead.

Format: one zstd frame per case, appended to a file, rotated every 256 MB
of JSON or every hour, under `/var/lib/atomicdb-golden/<kind>/`.

**On production since 28-Nov 07:38 UTC** (`/root/deploy335`):

- 07:38 to 07:40 at 1% of climbs and 0.5% of jobs;
- 07:40 to 07:50 at 5% and 2%;
- since 07:50 at **20% of climb batches and 20% of stage (a) jobs**.

The rate is set in `/var/lib/atomicdb-golden/control.json` and the
recorder re-reads it every 30 s. `{"enabled": false}` stops it without a
restart.

Measured on the cases themselves (every case written from 07:38 to 09:00 UTC, most of them at the 20% rate):

| | climb cases (drain) | job cases (stage_a) |
|---|---:|---:|
| Cases | 683 | 2,098 |
| Recorder overhead per case, ms: p50 / p90 / max | 17.9 / 71.4 / 389 | 11.6 / 27.4 / 116 |
| of which reading and converting the rows (`capture`), p50 | 16.9 | 9.4 |
| of which the pre-commit read of written rows (`post_images`), p50 | 0.75 | 1.87 |
| Batch it rode on, ms: p50 / p90 / max | 197 / 1,117 / 6,621 | 151 / 1,448 / 5,335 |
| Statements per case: p50 / max | 67 / 1,863 | 34 / 738 |
| Rows read per case: p50 / max | 1,132 / 46,957 | 49 / 425 |
| Compressed size per case | 74 KB | 13 KB |
| JSON to zstd ratio | 9.9 | 9.2 |
| Capture errors, unkeyed position writes, uncaptured selects, dropped cases | 0 | 0 |
| Cases recording a failed batch (error kept) | 0 | 1 |

Climb transactions in the same window (perf events, section 1.3): golden
batches p50 193 ms, p90 1,097, p99 4,804; the other batches 179, 1,014 and
4,277. That is +8% on a sampled batch, which matches the recorder's own
accounting, so about 1.6% of the drainer's time at a 20% rate.

Reading it:

- **Latency.** The recorder adds about 16 ms at the median to a sampled
  climb, and about 10 ms to a sampled job. Nearly all of it is reading the
  rows a second time and converting them to JSON (`capture`). The
  pre-commit read of the written rows (`post_images`) costs 1 to 2 ms.
  Against the batch it rides on, that is about 5 to 10% for a typical
  climb and 1 to 4% for a long one. At a 20% rate the average climb is
  about 1 to 2% slower. The perf events tag each golden batch (`golden`),
  and the climb-latency table in section 1 compares golden and other
  batches directly.
- **Coverage.**
  - Every case so far has its written rows (`commits_with_rows`).
  - No position UPDATE without a key in its parameters
    (`unkeyed_position_writes = 0`).
  - No SELECT through a server-side cursor (`uncaptured_selects = 0`).
  - No capture error.
  - No case was dropped for queue, size or budget.
- **Disk.** 78 MB in the first 82 minutes, about 60 MB an hour at 20%, so about 1.4 GB a day. The cap is 20 GB and nothing is written while
  the disk has less than 300 GB free. At this rate the cap is reached in
  about two weeks, which is the length of production the plan asked for
  (section 7.2). Lower the rate in `control.json` to stretch it.
- **Replaying a case.** `golden_cases.iter_cases(path)` reads a file, and
  `golden_cases.pre_image(case)` gives `{table: {primary key: {column:
  first value read}}}`. `case['commits'][i]['positions']` gives the
  differences each commit left. Batch boundaries are the cases themselves:
  a climb case is exactly one drainer batch, with the seed rows it claimed
  (rules spec CAS, "golden cases must record batch boundaries").

## 3. The storage benchmark on the production box

### 3.1 What was run, and what could not be

- **Where:** the production box itself (i7-8700, 12 threads, 64 GB, ext4
  on md RAID1 over two NVMe drives, kernel 6.8), on 28-Nov from 07:19 to
  07:49 UTC, while production ran normally. The load average was
  7 to 10, md2 was 60 to 85% busy, and Postgres had its usual
  ~460,000 buffer hits and ~5,000 reads a second. The numbers are those of
  a busy box, not an idle one.
- **Confinement, as briefed:** `systemd-run --scope -p MemoryMax=8G -p
  CPUQuota=400% nice -n 19 ionice -c3`, in `/root/v2bench-tmp`, deleted
  afterwards. Four CPUs at the lowest priority means the compute and
  reader numbers are a floor. `ionice -c3` has no effect with the `none`
  scheduler these NVMe drives use, so the commits competed with Postgres
  at normal I/O priority.
- **Datasets:** the real slice (`ball`, 1.56M positions, the same CSV
  export as the Windows runs) and `syn20m` (generated on the box, seed 1).
  **Not** 200M.
- **Stores: only the pure-Rust ones** (in memory, memory + WAL, mmap
  columns + WAL, redb, fjall). The box has no C compiler: no gcc, no clang,
  no `libc6-dev`. LMDB, SQLite and RocksDB need one, and installing a
  toolchain on production with apt was not in the brief. The harness was
  therefore **cross-compiled on the tower** for `x86_64-unknown-linux-musl`
  and linked with `rust-lld`, as one static binary, so nothing was
  installed on the box: no rustup and no packages. The harness change is a
  cargo feature, `cstores`, on by default, that gates the three C stores
  (section 3.5).
- **musl, not glibc:** musl's allocator is slower than glibc's. That costs
  the allocation-heavy parts (the in-memory store, bulk loads), not the
  commit or the mmap reads. Where it matters, it makes Linux look worse,
  not better.
- **Same stages and counts as the Windows runs:** 2,000 descents, 5 s of
  reads, 8 readers, 500 single landings, 200 batches of 32, 200 re-search
  batches, 100 mixed batches. Crash test: 20 `kill -9` trials per store on
  the real slice, checkpoint at 2 MB.

### 3.2 One `fdatasync` on the md RAID1 (open question 1)

This is the number every commit pays. The probe appends a block, calls
`fdatasync`, and repeats (`scripts/fsync_probe.py`). Measured on the box at
07:19 UTC (load 8.4) and, for comparison, the same Python probe on the
tower that ran the Windows benchmark (`FlushFileBuffers`):

| ms per call | n | p50 | p90 | p99 | max |
|---|---:|---:|---:|---:|---:|
| **Production, append 4 KB + `fdatasync`** | 400 | **16.4** | 26.3 | 64.0 | 186 |
| Production, append 64 KB + `fdatasync` | 400 | 18.2 | 28.3 | 103.8 | 163 |
| Production, append 4 KB + `fsync` | 200 | 17.0 | 23.6 | 46.1 | 57 |
| Tower (Windows, NVMe), append 4 KB + flush | 400 | 0.70 | 0.74 | 3.4 | 3.9 |
| Tower, append 64 KB + flush | 400 | 0.80 | 2.6 | 4.0 | 4.3 |

**One durable commit costs about 16 ms on production, 23 times what it cost
in the benchmark.** The plan assumed 0.7 ms.

Where the time goes (`scripts/fsync_probe2.py`, 07:48, load 13, 600 calls
each, interleaved):

| 4 KB, ms | p50 | p90 | p99 | max |
|---|---:|---:|---:|---:|
| append + `fdatasync` (file grows: ext4 journal commit + data + flush) | 13.8 | 23.0 | 61.0 | 152 |
| overwrite inside a preallocated, written file + `fdatasync` (no metadata) | **6.5** | 11.2 | 35.7 | 159 |
| the same with `O_DIRECT \| O_DSYNC` | 7.0 | 9.2 | 35.5 | 373 |

About half of the cost is ext4's journal commit for a growing file. That
commit is shared with everything else on the file system, which means
Postgres's own WAL. The other half is the cache flush on both members of
the mirror. The drives are Samsung MZVLB1T0HALR (PM981 OEM): consumer
NVMe with a volatile write cache (`write back`, FUA on) and no power-loss
protection, so a flush really waits for the media.

What it means for the design (inputs for phase 1, not decisions):

- **Preallocate log segments and overwrite them** (as Postgres does with
  its WAL). That halves the commit, to about 6.5 ms at the median today.
- **Group commit is not optional.** At one commit per search, a 16 ms
  commit caps the writer at about 60 searches a second. That is still
  4.5 times production's peak minute (13 a second), but with little
  margin. With 32 per commit the box sustained 700 to 1,400 a second
  below.
- The p99 (35 to 100 ms) is set by the box's other I/O. Until v1 stops
  writing, v2's tail latency on this box will follow Postgres's.
- With v1 gone (no Postgres WAL, no 90 GB of B-tree writes), the journal
  share should shrink. That can only be measured after the cut-over or on
  the shadow box.

### 3.3 The suite, Windows tower vs production box

Each cell is **Windows / Linux**. Windows is the benchmark report's run;
Linux is this run on the busy production box, with 4 CPUs at nice 19.

**Real slice of production (1.56M positions):**

| Stage | in memory W / L | memory + WAL W / L | mmap + WAL W / L | redb W / L | fjall W / L |
|---|---:|---:|---:|---:|---:|
| Searches/s, 1 per commit | 11,809 / 3,295 | 1,173 / 37 | 1,130 / 53 | 164 / 34 | 691 / 59 |
| · commit p50, ms | 0.01 / 0.01 | 0.69 / 19.01 | 0.73 / 16.62 | 5.74 / 19.84 | 0.77 / 15.96 |
| · commit p99, ms | 0.02 / 0.04 | 2.15 / 256.72 | 2.23 / 83.82 | 8.61 / 152.96 | 2.21 / 36.35 |
| · compute p50, ms | 0.04 / 0.20 | 0.08 / 0.24 | 0.07 / 0.19 | 0.29 / 0.40 | 0.53 / 0.96 |
| Searches/s, 32 per commit | 35,461 / 9,622 | 16,173 / 1,420 | 10,878 / 696 | 492 / 389 | 2,229 / 842 |
| · batch p50, ms | 0.89 / 3.21 | 1.78 / 18.17 | 2.92 / 25.75 | 65.19 / 68.80 | 14.16 / 34.61 |
| · batch p99, ms | 1.32 / 7.60 | 3.33 / 100.08 | 5.63 / 621.71 | 73.42 / 469.20 | 21.29 / 117.33 |
| · commit p50, ms | 0.21 / 0.35 | 1.02 / 15.20 | 2.21 / 22.60 | 60.93 / 61.83 | 3.75 / 20.03 |
| · compute p50, ms | 0.67 / 2.88 | 0.71 / 2.83 | 0.81 / 2.48 | 4.13 / 6.14 | 10.29 / 14.81 |
| Re-searches/s, 32 per commit | 109,760 / 17,847 | 26,383 / 1,600 | 23,297 / 1,475 | 2,655 / 1,530 | 4,029 / 841 |
| Selector descents/s | 788,022 / 380,373 | 739,426 / 803,692 | 685,354 / 213,443 | 33,632 / 27,897 | 7,742 / 3,983 |
| Explorer reads/s, 1 reader | 701,260 / 154,431 | 727,167 / 202,900 | 764,782 / 208,095 | 83,411 / 63,553 | 26,767 / 19,799 |
| · p99, us | 2 / 14 | 2 / 12 | 2 / 14 | 27 / 34 | 81 / 123 |
| Explorer reads/s, 8 readers | 1,616,524 / 200,213 | 1,532,666 / 220,575 | 5,413,875 / 162,676 | 309,308 / 42,994 | 115,517 / 19,915 |
| Explorer reads/s, 8 readers + writer | 1,037,437 / 157,691 | 1,178,349 / 181,210 | 3,976,122 / 160,644 | 325,228 / 43,326 | 117,302 / 19,521 |
| · p99, us | 10 / 822 | 10 / 506 | 4 / 143 | 49 / 2,136 | 149 / 2,103 |
| · writer searches/s meanwhile | 14,668 / 865 | 9,749 / 496 | 9,479 / 540 | 453 / 161 | 1,365 / 186 |
| Bulk load, s | - / - | 0.5 / 1.9 | 0.5 / 2.5 | 2.9 / 9.0 | 4.3 / 8.5 |
| Bytes per position | 0 / 0 | 52 / 52 | 81 / 81 | 149 / 149 | 110 / 110 |
| Open, ms | 308.7 / 424.2 | 220.2 / 238.3 | 0.7 / 0.1 | 6.3 / 5.2 | 1,032.8 / 1,369.5 |

**Synthetic, 20M positions:**

| Stage | in memory W / L | memory + WAL W / L | mmap + WAL W / L | redb W / L | fjall W / L |
|---|---:|---:|---:|---:|---:|
| Searches/s, 1 per commit | 1,534 / 11,798 | 782 / 61 | 1,142 / 62 | 137 / 27 | 900 / 76 |
| · commit p50, ms | 0.01 / 0.01 | 0.70 / 15.14 | 0.75 / 14.31 | 6.94 / 21.97 | 0.76 / 9.64 |
| · commit p99, ms | 0.02 / 0.02 | 2.20 / 61.97 | 2.33 / 63.43 | 11.16 / 205.10 | 2.37 / 45.65 |
| · compute p50, ms | 0.03 / 0.07 | 0.06 / 0.11 | 0.05 / 0.14 | 0.19 / 0.26 | 0.27 / 0.40 |
| Searches/s, 32 per commit | 43,403 / 20,583 | 16,543 / 1,554 | 8,744 / 1,370 | 314 / 267 | 3,534 / 1,111 |
| · batch p50, ms | 0.73 / 1.51 | 1.74 / 19.29 | 3.68 / 21.07 | 102.07 / 96.19 | 9.16 / 25.19 |
| · batch p99, ms | 0.97 / 2.50 | 3.23 / 81.37 | 5.87 / 75.64 | 125.07 / 591.95 | 11.96 / 85.03 |
| · commit p50, ms | 0.24 / 0.24 | 1.12 / 17.60 | 3.04 / 19.55 | 98.50 / 91.13 | 3.87 / 17.78 |
| · compute p50, ms | 0.49 / 1.27 | 0.57 / 1.63 | 0.64 / 1.55 | 3.51 / 4.42 | 5.16 / 6.67 |
| Re-searches/s, 32 per commit | 124,876 / 40,716 | 20,114 / 916 | 16,704 / 1,069 | 1,593 / 921 | 5,624 / 1,038 |
| Selector descents/s | 1,020,669 / 688,785 | 865,614 / 694,952 | 858,443 / 119,550 | 41,798 / 30,432 | 8,345 / 5,561 |
| Explorer reads/s, 1 reader | 615,617 / 595,232 | 600,487 / 557,555 | 669,535 / 305,167 | 41,949 / 39,991 | 14,262 / 11,211 |
| · p99, us | 2 / 3 | 2 / 3 | 4 / 13 | 95 / 60 | 181 / 213 |
| Explorer reads/s, 8 readers | 1,517,166 / 230,294 | 1,571,465 / 218,977 | 4,591,600 / 152,261 | 368,879 / 44,002 | 120,720 / 21,479 |
| Explorer reads/s, 8 readers + writer | 1,214,199 / 159,622 | 1,350,907 / 208,909 | 2,323,057 / 184,229 | 350,832 / 40,638 | 112,474 / 20,837 |
| · p99, us | 11 / 214 | 9 / 512 | 19 / 290 | 43 / 1,322 | 162 / 2,455 |
| · writer searches/s meanwhile | 25,643 / 1,290 | 13,053 / 590 | 7,273 / 604 | 300 / 125 | 2,483 / 308 |
| Bulk load, s | - / - | 7.0 / 9.8 | 5.0 / 14.3 | 42.0 / 111.7 | 55.5 / 142.8 |
| Bytes per position | 0 / 0 | 49 / 49 | 78 / 78 | 112 / 112 | 94 / 103 |
| Open, ms | 5,817.7 / 4,763.1 | 3,543.7 / 4,401.3 | 0.8 / 0.1 | 5.9 / 9.6 | 1,675.5 / 16,784.0 |

State check: on the real slice the five stores ended in the same state on
Linux (hash `96c3638e…`), and on `syn20m` they ended in the same state as on
Windows (`7334b341…`). The real slice's Linux hash differs from the
report's Windows hash (`999599d0…`) because the Windows dataset was
imported by an earlier build of the importer: `nodes.bin` and `meta.txt`
differ, `edges.bin` does not. Re-importing the same CSV on the tower today
gives files bit-identical to the Linux import, and the in-memory suite on
it ends at `96c3638e…`, the Linux hash. **The operating system does not
change the state.**

**Crash recovery (`kill -9`, real slice, checkpoint every 2 MB of log):**

|---|---:|---:|---:|---:|---:|
| Store | Windows trials / clean / recovery p50 / max ms | Linux trials / clean / recovery p50 / max ms |
|---|---:|---:|
| mmap + WAL | 60 / 60 / 57.9 / 65.5 | 20 / 20 / 44.2 / 66.5 |
| memory + WAL | 30 / 30 / 1879.6 / 3470.8 | 20 / 20 / 404.7 / 546.1 |
| redb | 30 / 30 / 251.8 / 320.8 | 20 / 20 / 243.7 / 303.5 |
| fjall | 15 / 15 / 1936.9 / 3580.1 | 20 / 20 / 2561.8 / 4936.6 |

### 3.4 What the rerun confirms and what it changes

- **Durability holds on Linux.** 80 kills, no acknowledged batch lost, the
  state identical to the reference every time. The mmap store recovers in
  44 ms at the median (58 ms on Windows).
- **The commit decides ingest on this box, not the store.** Every durable
  store waits 14 to 22 ms per commit, so the stores' ingest numbers
  converge: at 32 per commit, mmap 696 to 1,370 searches a second, memory
  + WAL 1,420 to 1,554, fjall 842 to 1,111, redb 267 to 389. The 8x to 20x
  margin the mmap store had over fjall on Windows ingest is gone here.
  What remains is 50x to 500x production's actual rate (2.7 searches a
  second on average this morning, 13 at the peak minute of the plan's
  sample).
- **Reads still decide the race, and the order holds.** One reader on
  `syn20m`: mmap 305,167 reads a second against redb 39,991 and fjall
  11,211 (8x and 27x). Postgres with the production schema did 157 on
  Windows. The absolute numbers are 2x to 30x lower than on Windows. This
  run had 4 CPUs at nice 19 on a box with load 8 to 13, the i7-8700 is
  slower per core than the 5950X, and the binary uses musl. Eight readers
  gain little over one for the same reason: the readers had 4 throttled
  CPUs between them.
- **New on Linux: the mmap store reads at half the speed of the in-memory
  graph** (305,167 against 557,555 reads a second at 20M, and 213,443
  against 803,692 selector descents a second on the real slice). On
  Windows the two were level. Plausible causes are page faults on the
  mapped files after a write (Windows maps them differently), transparent
  huge pages, and the CPU quota. This is the first thing to look at in
  phase 1 (`MAP_POPULATE`, `madvise`, a prefault after checkpoint). It
  does not change the ranking: the mmap store stays 8x to 27x ahead of
  every other durable store on reads.
- **The mmap store's commit tail.** Batch p99 at 32 per commit was 622 ms
  on the real slice, against 5.6 ms on Windows. In the long run of the
  report the worst latency was the checkpoint flush on the writer thread.
  On a busy md RAID1 that flush is slow, which confirms the plan's point
  that the checkpoint must run off the writer thread.
- **Not measured on Linux:** LMDB, SQLite and RocksDB (no C toolchain on
  the box, section 3.1), and the 200M graph. The claim that LMDB's commit
  "should be considerably better on Linux" remains unverified. Given 3.2,
  every store's commit is bounded below by the same 7 to 16 ms here, so it
  cannot change the ranking on this box.

### 3.5 Reproduce

```
# on the tower (no C toolchain needed): pure-Rust stores, static Linux binary
rustup target add x86_64-unknown-linux-musl
CARGO_TARGET_X86_64_UNKNOWN_LINUX_MUSL_LINKER=rust-lld \
  cargo build --release -p atomicdb-v2-bench --no-default-features --target x86_64-unknown-linux-musl
# on the box, confined as above
atomicdb-v2-bench import --out data/ball --pos ball_pos.csv --edges ball_edges.csv
atomicdb-v2-bench gen --out data/syn20m --nodes 20000000 --seed 1
atomicdb-v2-bench load|suite|crash ... (same arguments as scripts/run_linux.sh)
python3 fsync_probe.py <file on the md RAID1>      # section 3.2
python3 fsync_probe2.py <file on the md RAID1>     # section 3.3
```

The `cstores` patch (Cargo.toml: the three C crates `optional`, a default
feature that enables them; `stores/mod.rs` and `store.rs`: `#[cfg(feature =
"cstores")]` on their modules and match arms) is in this PR under
`atomicdb-v2/bench/`. The probes and the raw results are in
`atomicdb-v2/bench/results/linux-prod-*`.
