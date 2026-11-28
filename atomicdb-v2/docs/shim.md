# The semantic bridges: shim and golden cases

Plan `docs/atomicdb/v2-plan.md`, sections 7.1 and 7.2. Two ways of checking
that the v2 rules do what v1 does, both built on the same crate:

| Piece | What it is |
|---|---|
| `crates/av2-shim` | v1 `atomicdb_position` rows <-> v2 records (exact, or an error naming the column), `ShimGraph` (v1 rows in the in-memory v2 graph, the rows to write back), the registry of v2 entry points by v1 name |
| `crates/av2-py` | the `av2` Python extension (PyO3) that v1's test suite calls |
| `atomicdb/v2shim/` | the Django side: export, call, write back (`bridge`), routing of ported functions (`intercept`), the test runner and its per-test report (`runner`) |
| `crates/av2-golden` | replays v1's recorded batches (`atomicdb/golden_cases.py`, PR #338) on v2 and classifies every difference |
| `scripts/build_av2_py.py`, `scripts/shim_report.py` | build the extension; summarise a run and keep the ratchet |
| `.github/workflows/atomicdb-v2-shim.yml` | CI for both |

## Registering a ported function

One line in `crates/av2-shim/src/registry.rs`, block "ported v1 functions":

```rust
entry!("ingest.backup_cascade", adapters::backup_cascade),
```

The name is the v1 dotted name (`module.function` under `atomicdb/`). The
function has the signature `EntryFn`:

```rust
fn backup_cascade(ctx: &mut Ctx<'_>, args: &Args) -> Result<Value, EntryError> {
    let seeds = args.positions(ctx.graph, 0, "seed_keys")?;
    let report = av2_rules::cascade::backup_cascade(ctx.graph, &seeds, &mut env)?;
    ctx.writes.insert("$out".into(), json!({"report": {"changed": ...}}));
    Ok(json!(report.changed.len()))
}
```

- `ctx.graph` is a `ShimGraph`: it implements `GraphRead + GraphWrite`, so
  the rules run on it unchanged; whatever they change is written back.
- `Args` decodes the call as Python made it: positions (`{"$pos": key}`, a
  `Position`, a key string), lists, sets and querysets of them, integers,
  booleans, strings, moves; `args.kwargs` has the rest as JSON.
- `ctx.facts`: facts to log. An `Opaque` fact whose kind is upper case is a
  `DBEvent` kind: the adapter writes that event with the fact's data as
  payload (`av2_rules::effects::Event::to_fact` produces exactly that).
- `ctx.writes`: rows for tables outside the graph, `{table: [rows]}`
  (created, or updated by primary key); `$out` fills a dict the caller
  passed (`report=`); `$calls` asks for follow-up v1 calls after the
  write-back (a side effect another port owns, such as
  `proof.refresh_proof_numbers` at the end of a cascade; it goes to v2 as
  soon as that one is registered too).
- `entry!(name, f, tables = ["atomicdb_analysistask"])` exports extra tables
  into `ctx.extra.tables`.
- `ctx.constant("ingest", "CASCADE_GUARD_LIMIT")` and
  `ctx.setting("ATOMICDB_FRESH_CONTEXT")`: v1's module constants and
  settings as they are at the call (a test that patches a constant sends
  the patched value; a golden case sends the values it was recorded with).
  Build the rules' configuration from them, not from Rust defaults.
- `ctx.stubbed("ingest", "_loop_at_best_exit")`: whether the caller had
  replaced that function of those modules with a `unittest.mock` object
  (`bridge.export_stubs`), for v1 tests that switch a rule off that way.
- The return value is JSON; `{"$pos": key}` comes back to Python as a
  `Position`, `{"$set": [...]}` as a set, `{"$tuple": [...]}` as a tuple.

- `ctx.extra.prepared`: what a Python preparer read from v1's other models
  before the call (`atomicdb/v2shim/prepare.py`, `PREPARE[name]`), v2's
  "prepared inputs". Preparers only read; writes go back as `$calls` to the
  `apply_*` helpers of the same module (e.g. the quality arm's purchases of
  the ascent: room in, `apply_purchase` out).

Adapters for the rules live in `crates/av2-shim/src/adapters.rs`
(`ingest.backup_cascade`, `ingest.backup_backed_evals` and, for golden
cases, `propagation_queue.drain_batch`, the deferred climb, without the
queue arms that follow it). Effects
the rules report through `av2_rules::effects` map to v1 like this:
`NODE_CLOSED` runs v1's `_emit_closure_events` after the write-back (its
provenance, claims, tasks and campaigns belong to other ports), other
events become their `DBEvent`, the proof-number refresh is a follow-up
`proof.refresh_proof_numbers`, loops are `V1Loops` with the
`BACKED_LOOP_MAX_SECONDS` / `BACKED_CYCLE_MAX_SECONDS` the caller had
(`adapters::loops_from`; a test that mocks `_loop_at_best_exit` out gets a
zero loop budget, which skips the rule as the mock does).

Nothing else is needed on the Python side: `intercept` routes every name
`av2.entries()` lists. Golden cases use two names: `propagation_queue.drain_batch`
(a climb) and `ingest_queue.apply_job` (a landing).

## Running v1's tests on v2

```
python atomicdb-v2/scripts/build_av2_py.py            # once per Rust change
ATOMICDB_ENGINE=v2 python manage.py test atomicdb \
    --testrunner atomicdb.v2shim.runner.ShimRunner --portability portable \
    --shim-report shim-v2.json
python atomicdb-v2/scripts/shim_report.py shim-v2.json --v1 shim-v1.json \
    --baseline atomicdb-v2/docs/shim-baseline.txt
```

Without `ATOMICDB_ENGINE=v2` the same command runs on v1. What happens on v2:

1. The runner wraps every v1 function the tests drive (the `functions`
   column of `test-map.csv`) and every ported name, in its module and in
   every `atomicdb` module that imported it by name.
2. A call the **test** makes to a ported function goes to v2: all
   `Position` and `Edge` rows are exported (a test database holds a few
   hundred), loaded into a fresh `ShimGraph` (ids in creation order, the
   loader's rule), the entry runs, and the changed columns, created
   positions and edges, events and other rows are written back; the
   `Position` instances the test passed in are updated in place.
3. A call to a function not ported yet runs on v1, and so does everything
   that function calls inside itself: setup stays on v1, the act under test
   moves to v2 as soon as it is ported. `ATOMICDB_V2_STRICT=1` makes an
   unported non-setup function raise instead.
4. A v1 value v2 cannot hold (a centipawn value beyond 16 bits, a move that
   is not UCI on 8x8, the empty string where v1 means NULL...) fails the
   call with `V2Error('unrepresentable')` naming the column.

`ATOMICDB_V2_ONLY` / `ATOMICDB_V2_EXCLUDE` (comma-separated names) narrow
what is routed, to bisect a failure.

Classes in the report: `v2-pass`, `v2-fail` (at least one call went to v2),
`v1-only` (passed with every rule call still on v1; the report lists the
functions those tests wait for), `no-rule-call`, `fail-without-v2` (the
shim broke a test: a CI failure), `skip`. CI fails when a test of
`shim-baseline.txt` stops passing on v2; `--update-baseline` adds new passes.

Not covered: the `mixed` tests (they assert through views and commands that
call rules from places the shim does not route) and the `v1-specific`
ones; `--parallel` (the counters live in one process).

## Replaying golden cases

```
cargo build --release -p av2-golden
av2-golden --mode v2|echo|noop [--kind drain|stage_a] [--limit N] \
           [--json report.json] [--cases cases.jsonl] [--fail-on-diff] DIR_OR_FILES...
```

Reads `.jsonl`, `.jsonl.gz` and `.jsonl.zst` (one zstd frame per case; a
file copied while the recorder writes it ends in a torn frame, which is
reported and skipped). For each case:

1. **Pre-image**: the first value read of every row the case can attribute
   (a port of `golden_cases.pre_image`, including positions read through an
   edge join), plus the edges: a row carrying an edge's parent, move and
   child is an edge (the drainer never selects edge ids); a row saying only
   "this position has that parent" is a *parent link*, counted, its ends
   loaded without columns. Rows v1 updated without reading them, and ends
   of edges it inserted, are loaded without columns too.
2. **Run**: `v2` calls the batch's entry on the isolated graph; `echo`
   writes v1's own writes through the v2 graph (no rules: a difference is a
   value v2 cannot hold, or a bug of the tool); `noop` writes nothing.
3. **Diff**, per (position, column), from the case's commits (later commits
   override earlier ones; a ROLLBACK leaves nothing in the commits):

| class | meaning | failure |
|---|---|---|
| `match` | same value written, or v1 wrote what v2 already had (also an edge v1 "inserted" with `ON CONFLICT DO NOTHING` that was already there) | no |
| `value_diff` | both wrote, different values | yes |
| `missing_write` / `extra_write` | only v1 / only v2 wrote | yes |
| `missing_row` / `extra_row`, `missing_edge` / `extra_edge` / `edge_child_diff` | positions and edges inserted by one side | yes |
| `unrepresentable`, `load_unrepresentable` | a v1 value the v2 records cannot hold | yes |
| `unread_input` | a difference on a column the case never read: v2 started from a default, not from v1's value | listed |
| `not_modeled` | `priority` (D5), `updated` (record version) | no |

Case status: `clean`, `diff`, `not_ported`, `v2_error`, `load_error`,
`v1_error`; incomplete cases (oversize, uncaptured selects, unkeyed writes,
capture errors) are counted with their reason.

## Numbers (28-Nov, Windows tower, release build)

Golden cases copied from production (9 files, 16 MB compressed, 532 cases:
158 climbs, 374 landings, recorded 07:39-08:07 UTC at rate 0.2):

| mode | result | wall |
|---|---|---|
| echo | 532/532 clean: 41,828 fields and edges matched, 0 unrepresentable | 3.5 s |
| noop | 27,909 missing writes, 12,744 missing edges, 393 unread inputs | 5.6 s |
| v2, climbs (`drain_batch` on av2-rules steps 1-9, `NoLoops`) | **145 of 158 clean**; 13 differ: 1,342 extra writes, 320 value diffs, 88 missing writes, all in `backed_*` columns; 984 unread inputs | 4.3 s |
| v2, landings | 374 not ported (stage (a)) | |

Per case: load p50 0.43 ms, p99 50 ms, max 160 ms (the largest pre-image
held 22k positions); the cases read 143,903 positions, 136,421 edges and
366 parent links without a move.

Python bridge, one `av2.call` (JSON in, load, run, collect changes, JSON
out), synthetic rows with a 0.5 kB analysis each:

| positions | edges | ms per call |
|---:|---:|---:|
| 100 | 99 | 2.5 |
| 1,000 | 999 | 32 |
| 10,000 | 9,999 | 330 |

Portable suite (1,314 tests, SQLite, serial): 153 s on v1 with the runner,
138 s on v2 with every function wrapped and nothing ported (1,294 `v1-only`,
18 `no-rule-call`, 2 skips, 0 broken by the shim).

With `ingest.backup_cascade` and `ingest.backup_backed_evals` registered
(131 s): **114 pass on v2**, 45 `v2-fail`, 1,135 `v1-only`, 18
`no-rule-call`, 2 skips, 0 broken. 21 of the passes use fixtures that name
moves freely and ran with UCI stand-ins. The 45 failures are loop and
repetition tests (`test_loop_best_exit` 17, `test_backprop_repetition` 8,
`test_backed_eval` repetition classes 8, `test_repetition_progress` 5,
`test_cycle_disambiguation` 3, `test_pointer_loop_grounding` 2,
`test_backup_echo` 1, `test_stale_backed_sweep` 1): the adapters run
`NoLoops` because steps 11-14 are not ported.
