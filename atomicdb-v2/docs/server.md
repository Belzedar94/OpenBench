# AtomicDB v2 writer service (`av2-server`)

The skeleton of the one process of plan section 3.1: the single writer with
group commit, fed by a channel of prepared inputs, calling the
`av2_rules::Rules` trait, publishing each batch to lock-free readers through
the store's epoch, and a Prometheus endpoint with the per-stage timings of
plan section 3.6. The rules are stubs (`stub::StubRules`: log a fact, or
land a synthetic search with production's write shape) until `av2-rules`
lands; preparers, queue, API and pages plug in as producers of inputs and as
readers.

```
let svc = Service::start(store, rules, ServiceConfig { max_batch: 256, metrics_addr: Some("127.0.0.1:9187".into()) })?;
let ack = svc.submit_wait(input)?;        // durable and visible when it returns
let r = svc.reader();                     // lock-free reads from any thread
```

## The writer loop

1. With nothing in flight, wait for an input.
2. While batch n's fsync is in flight, gather everything that arrives; the
   store's sync thread posts "durable" into the same channel, so the writer
   wakes the moment the fsync completes.
3. Apply n, answer n's senders (`Ack { seq, output, batch_inputs }`).
4. Run the rules once over everything gathered, start batch n+1's commit
   (`Store::commit_start_notify`).
5. If the batch fills up before n's fsync completes, n+1 is computed on an
   overlay that reads through n (`Store::batch_after`), then n is finished
   and n+1 started: the fsync and the computation overlap.

A rejected batch of several inputs is retried input by input.

## Metrics (`GET /metrics`, `GET /healthz`)

| Metric | What |
|---|---|
| `av2_writer_stage_seconds{stage=...}` | histogram per stage: `queue_wait` (per input), `compute` (rules), `finalize`, `write` (frame write and file growth), `fsync`, `apply`, `batch` (start to acknowledged) |
| `av2_submit_to_visible_seconds` | submit to acknowledgement: durable and visible to readers |
| `av2_fsync_wait_seconds` | part of each fsync the writer waited for |
| `av2_batch_inputs`, `av2_frame_kib` | batch sizes, frame sizes |
| `av2_inputs_total`, `av2_batches_total`, `av2_rejected_total`, `av2_retried_alone_total` | counters |
| `av2_queue_depth`, `av2_last_seq`, `av2_positions`, `av2_graph_live_bytes{file=...}`, `av2_log_bytes` | gauges |
| `av2_checkpoints_total`, `av2_checkpoint_seconds_max`, `av2_checkpoint_seconds_total`, `av2_segments_released_total`, `av2_keymap_compactions_total`, `av2_file_grows_total`, `av2_archive_bytes_total{form=raw|zstd}`, `av2_uptime_seconds` | store |

Not yet: read latency (readers live in `av2-web`), projector lag (phase 5),
lease latency (phase 4).

## Bench

```
av2-server --db DIR [--create] --listen 127.0.0.1:0 --bench-secs 8 --clients C [--rate R] [--fsync-delay-ms 16]
```

`C` clients submit synthetic landings, each as soon as its previous one is
acknowledged (or `R` per second). `--fsync-delay-ms` sleeps after every
fsync to simulate production's md RAID1 (16.4 ms median per `fdatasync`,
phase 0). Numbers are in the PR and `docs/store.md`.
