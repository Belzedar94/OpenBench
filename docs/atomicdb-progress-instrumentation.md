# AtomicDB hourly progress instrumentation

This is the append-only data foundation for the future Progress view described
in `atomicdb-conquest-map.md`. It does not expose an API, build charts or alter
solver scheduling.

## Capture contract

Run:

```bash
python manage.py capture_atomicdb_progress --json
```

once per hour, preferably a few minutes after the hour. The command:

1. normalizes the current aware timestamp to the beginning of its UTC hour;
2. returns the existing row without querying or changing counters when that
   bucket already exists;
3. otherwise reads the current cumulative counters and inserts one row;
4. emits a JSON receipt suitable for an operations log.

`ProgressSnapshot.bucket_start` is unique. Loaded model instances reject
`save()` and `delete()`, so normal application code cannot rewrite history.
Operators with direct SQL or bulk-query privileges can still mutate the
database and therefore remain inside the trust boundary.

The command has deliberately no `--at`, backfill or overwrite option. The first
row is the first real observation after deployment. History before that instant
must never be synthesized from current rows.

## Metric glossary

Every value is a cumulative gauge observed near `captured_at`, except the live
worker gauges. A UI derives hourly deltas from adjacent rows; negative deltas
are data-quality warnings, not work to hide.

| Field | Exact meaning |
|---|---|
| `positions_total` | All stored unique `Position` rows. |
| `positions_unknown` | Positions whose exact status remains `UNKNOWN`. This is not a claim that every row belongs to the relevant startpos frontier. |
| `positions_closed` | Positions with any exact non-`UNKNOWN` status. |
| `positions_expanded` | Positions for which the complete legal outgoing edge set was materialized. |
| `engine_nodes_total` | Sum of `nodes_searched` on completed analysis tasks. These are accepted worker-reported real nodes, capped by the submit contract. |
| `engine_seconds_total` | Sum of accepted `elapsed_seconds` on completed tasks. This is engine compute time, not calendar wall time. |
| `analyses_completed` | Completed task rows. |
| `tasks_pending` / `tasks_leased` | Queue-state gauges at capture time. |
| `tasks_retried` | Task rows with more than one recorded lease attempt. |
| `lease_retries_total` | Sum of `max(attempts - 1, 0)` over all tasks. A lost/recycled lease increments this; an HTTP replay of the same fenced lease does not. |
| `recorded_rejections_total` | Persisted `TB_REJECTED` events only. Stateless malformed, authentication and stale-fence HTTP rejections are not persisted today and are intentionally excluded. |
| `active_workers` / `active_threads` | Worker pings seen during the existing 180-second live window, and their declared non-negative thread capacity. |
| `active_nps` | Sum of fresh NPS from live workers that report a current task. Idle or stale rates are excluded by the dashboard's existing honesty rules. |
| `closure_*` | Disjoint counts among closed positions by `TERMINAL`, `TB`, `MATE_PV`, `MINIMAX`, or any currently unclassified closure. |
| `trust_*` | Disjoint counts among closed positions. `TERMINAL`/`TB` are `verified`; remaining rows are classified by `ANDOR`, `ENGINE`, `DISPUTED`, or `unclassified`. |

## Metrics intentionally unavailable

- **Historical/irrelevant positions:** the current `Position` schema has no
  created timestamp or durable relevant/historical classification. A present
  row cannot honestly say when it entered the graph or whether it used to be
  frontier. No such counter is recorded.
- **Relevant frontier:** `positions_unknown` includes every stored unknown
  position, including ad-hoc roots. Reachability and frontier relevance belong
  in the Conquest Map snapshot projection, not in this cheap hourly capture.
- **All rejected submissions:** only tablebase rejection events are durable.
  Adding a broader number requires a separate persisted rejection ledger.
- **Progress before deployment:** there is no backfill.

These omissions are part of the public metric semantics, not TODO values to
estimate.

## Operations

Schedule one invocation per hour and retain stdout/stderr. Repeated invocations
are safe, including overlapping invocations: the unique UTC bucket makes one
insert win and the other reuse the same row.

Before enabling the schedule:

```bash
python manage.py migrate
python manage.py capture_atomicdb_progress --json
python manage.py capture_atomicdb_progress --json
```

The first receipt must say `"created":true`; the second,
`"created":false`, with an identical snapshot. Do not run a cleanup job against
this table. Retention, downsampling and the `/api/progress/v1` contract require
a separate reviewed change.
