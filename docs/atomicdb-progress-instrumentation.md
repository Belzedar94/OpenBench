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

`--preview` computes the same counters and prints them **without writing a
row**. It exists for one situation: right after a migration that adds columns,
this hour's bucket may already exist with zeroes in them, and the append-only
guarantee correctly refuses to fix it. Preview lets an operator read the
numbers anyway. It is not a backfill and cannot become one.

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
| `frontier_and_nodes` | Size of the AND part of the proof frontier at capture time (see below). |
| `frontier_dn_median` | Median `dn` over `frontier_and_nodes`. A median, not a mean: a handful of saturated nodes must not be able to hide a thousand thin ones. |
| `frontier_dn_thin` | How many of those sit at or below `ingest.DN_REPAIR_FLOOR` — claims one unanswered question away from falling over. |
| `closures_user` / `closures_fill` / `closures_auto` / `closures_solve` / `closures_none` | Cumulative `NODE_CLOSED` events carrying each `source` label. |
| `human_close_median_seconds` / `human_close_samples` | Median seconds between the FIRST public request for a position and its closure, over closures in the trailing 7 days, and the sample size that median came from. |

### The proof frontier, exactly

`atomicdb/proof.py` defines it and the definition is load-bearing for three
metrics and one scheduling arm, so it is written down in both places. A
`ProofNode` row belongs to the frontier of its campaign when all three hold:

1. the row exists — the proof has reached that position at least once; a DAG
   node no cascade ever touched is position cache, not anybody's frontier;
2. `Position.status` is still `UNKNOWN` — a closed node is a leaf of the proof
   carrying its truth value, not an open question;
3. `pn` and `dn` are both finite. Infinite `pn` means the goal is refuted
   there and the node is no longer an obligation; infinite `dn` means it is
   proven. Neither has effort left to estimate.

The read is capped at `proof.PROOF_FRONTIER_SCAN` rows ordered by ascending
`pn`, which is where the descent actually goes. The bias is deliberate and
stated here rather than hidden: this measures the part of the tree the proof
believes it is close on. A `dn=1` spine in a corner nobody visits topples
nothing; one on the main line topples the branch.

`frontier_and_nodes` restricts that set to nodes where the campaign's
DEFENDER is to move. Those are the obligations — every reply has to be
refuted — and they are the only ones where a low `dn` is bad news. At an
attacker (OR) node one good move is enough, so a low `dn` there means nothing
of the sort.

### Closure attribution

`NODE_CLOSED` payloads carry a `source` from the moment
`ingest.closure_attribution` was deployed: `USER`, `FILL`, `AUTO` (the analysis
queue the task came from), `SOLVE` (a verified certificate) or `NONE` (no task
behind it — maintenance commands, seeding). It is recorded at closure time
because it is not derivable afterwards: all four paths write identical rows.

**These counters start at zero on deployment.** Closures older than that carry
no `source` key and are counted in no category, `NONE` included — `NONE` is a
claim, not a bucket for everything unlabelled. The five therefore do not sum
to `positions_closed`, and the home page states the labelled fraction next to
every percentage. A counter that lied to make the arithmetic work would be
worse than one that admits it does not cover the past.

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

## The adversarial arms, and the order they must be switched on

Two scheduling arms make the automatic explorer refute itself the way a human
explorer does. Both live in the `refresh_selector` service pass, both are
bounded, and both are **off by default**:

- **dn repair** (`ingest.enqueue_dn_repair`): for AND nodes of the frontier
  sitting at or below `DN_REPAIR_FLOOR`, buy up to `DN_REPAIR_REPLIES` of the
  defender replies nobody has looked at. Tasks go out as `FILL`, sharing the
  `COVERAGE_QUEUE_CAP` allowance with coverage completion, capped again at
  `DN_REPAIR_MAX_PER_CYCLE` tasks and `DN_REPAIR_MAX_NODES` examined nodes per
  pass. The lease ordering is untouched: `-source` already serves `USER`
  first.
- **fragile mate claims** (`ingest.enqueue_fragile_mate_solves`): an `UNKNOWN`
  position claiming `|backed_eval| >= MATE_BAND` with partial coverage gets an
  F0 `SolveTask` on arm `fragile`, with the goal taken from the SIGN of the
  claim. Its own small cap (`FRAGILE_QUEUE_CAP`), separate from the ENGINE
  debt cap so a debt purge cannot starve it. A `DISPROVED` answer stays
  advisory and closes nothing.

Deploy order matters, because the point of the package is measuring whether
this helps:

```bash
python manage.py migrate                             # 0024, ten new columns
python manage.py capture_atomicdb_progress --preview  # read them right away
python manage.py capture_atomicdb_progress --json     # the "before" row
# ...only once that row has a non-zero frontier_and_nodes:
export ATOMICDB_ADVERSARIAL=1                  # or set it in the unit file
systemctl restart atomicdb-selector
```

The reference row must be captured with the code deployed and the arms still
quiet. If the hourly cron already wrote this hour's bucket before the migration
ran, that row carries zeroes in the new columns and append-only correctly
refuses to fix it: read the numbers with `--preview` and wait for the next
hour's row before switching the arms on.

After the switch, watch `DN_REPAIR` / `FRAGILE_ENQUEUED` events for the
receipts, and `frontier_dn_median` / `frontier_dn_thin` across snapshots for
the effect. `refresh_selector --no-adversarial` stops one pass without a
settings change; `--adversarial` forces one.
