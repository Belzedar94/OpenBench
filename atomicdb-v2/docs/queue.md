# AtomicDB v2 work queue (`av2-queue`)

Phase 4 of `docs/atomicdb/v2-plan.md`: the task model, lanes and fairness,
the lease protocol, the engine gate, requests, the selector and the
background arms, ported from `docs/rules-spec-queue.md` (families Q, PRI,
SOL-4, SOL-5) as **state applied by the single writer**.

## 1. Shape

```
HTTP (auth, form parsing: protocol::parse_*)      timers / selector thread / rules
        |  Input { now, ... }                                |  Input::{Tick, Pool, Arm, Mint, Landed, PositionClosed, Revive}
        v                                                    v
  QueueState::apply(input, env: &dyn QueueEnv, refill: &mut dyn Refill) -> Applied { output, deltas, events }
        |                                  ^ reads the graph (overlay)     ^ proposes tasks (selector)
        v
  fact::record(input, applied)  ->  one Fact in the batch frame  (kind 11, Fact::Queue)
```

- **Pure.** Every input carries `now` (OQ-10), and everything else that comes
  from outside: the fresh lease token (random, made by the HTTP layer), the
  engine-gate verdict (`gate::GateFiles::rejects`, Q-9), the certificate
  verdict of a proof (a preparer). The rules read no clock, no file and no
  random source; iteration is over `BTreeMap`/`BTreeSet` only; ties break by
  id (D3).
- **Deltas.** `apply` records every row it changed and returns their new
  values (`state::Delta`). The writer logs input + deltas + events in the
  frame. Recovery = last queue snapshot (`QueueState::snapshot`, JSON,
  canonical) + `fact::replay` of the queue facts after it. Neither the graph
  nor the rules are needed to rebuild the queue, and a LEASED row is in the
  log, with its token and heartbeat, the moment the lease is acknowledged:
  after a restart the worker replays, heartbeats and submits against the same
  row (`tests/restart.rs` does it through a real `av2_store::Store` log).
  Deltas are whole-row upserts, so replaying a frame twice is harmless.
- **Every test checks recovery.** The harness (`tests/common`) applies each
  input, checks the invariants (`QueueState::check_invariants`: one row per
  slot, lease fields, backers, indexes equal to a rebuild) and checks that
  the deltas rebuild the new state from the old one, byte for byte.
- **Graph reads** go through `env::QueueEnv` (`GraphEnv` over any
  `GraphRead`; `MemEnv` for tests). The queue never writes the graph.
- **Selector.** `selector::Refill` proposes (`MintPlan`), the writer mints
  (`apply_mint`: slots re-checked, PRI-4.11). `PnSelector` is the production
  loop (PRI-4.4..4.7) over a `selector::Descent` (df-pn / value walker /
  campaign roots / OR clamp), to be implemented over `av2-proof`'s tables
  (PN-6/PN-9 are not in `av2-proof` yet). `RegretSelector` is the regret
  engine; its formula and walk are `av2-proof`'s (`priority::*`, one copy).
- **Arms.** Each background arm is a *scan* (reader side, over the graph:
  `arms::scan_engine_debt`, `arms::scan_coverage`; dn repair and fragile take
  their candidates from `av2-proof` frontier rows / BKD-4) and an *enqueue*
  in the writer (`Input::Arm`), which applies caps and hourly rooms to the
  queue as it is at that moment.

### Integration contract for the writer

| The writer must send | When | Rule |
|---|---|---|
| `Input::Lease/Heartbeat/Submit/Release/Solve*` | each worker call (after auth + parsing) | Q-2 |
| `Input::Landed { pos, task, arbiter }` | a search landed (arbiter per EXP-5.1) | Q-3.13 / EXP-5.2 |
| `Input::PositionClosed { pos }` | every closure | CAS-7 |
| `Input::Revive { positions }` | expand / link / revocation / goto revive a tombstone | PRI-1.7 |
| `Input::Cascade { pos, open_before }` | after a landing's climb, status before the cascade | PRI-4.9 |
| `Input::Tick { now }` | every selector pass (and at least every few minutes) | SOL-5.4, retention |
| `Input::Arm`, `Input::Pool` | the selector cycle, in PRI-6.1 order | Q-8, PRI-4.3 |
| a queue snapshot | next to each checkpoint | recovery |

`SubmitOut::Accepted { claim }` is what the landing pipeline gets (v1's
`IngestJob` payload plus the task context `apply_job` reads).

## 2. Owner decisions applied

| Decision | Where |
|---|---|
| D1/OQ-10 inputs carry `now`, token, gate verdict, certificate verdict | `input.rs` |
| D3 ties by id | regret ranking, debt candidates (v1: by key), coverage scan (v1: `-updated` with no tie-break; v2: record `version`, then id), edge order (block order = v1 edge id order) |
| D5 priority computed in memory | `RegretSelector` over `av2_proof::priority`; **off for the lease order** (below) |
| D8 drop workers without lease tokens | a lease from a build < `LEASE_TOKEN_BUILD` answers `UpgradeRequired` and writes nothing; no legacy windows (Q-2.6), no legacy filter (Q-2.23); heartbeat, submit and release need the exact non-empty token; an imported tokenless LEASED row is recycled after 4 minutes like any other |
| Coordinator 28-Nov: AUTO/FILL leases ordered as v1 does today | `QueueState::queue_rank`: `DEAD` for a tombstone, 0.0 for a revived position, else the **stored** v1 value (env); `QueueConfig::computed_priority` (off) is the later switch |
| Constants in one place | priority constants, `MATE_BAND`, OR clamp: `av2-proof`, re-exported by `av2-queue::consts`; queue-only constants in `consts.rs` |

## 3. Open questions OQ-1..OQ-39

"As is" = v1 behaviour ported (brief: undecided questions keep v1 until the owner rules; the spec's recommendation stands).

| OQ | Decision here |
|---|---|
| 1 double apply | out of this crate (landing pipeline); the claim is one transition, each search is claimed once |
| 2 poison jobs | out of this crate (preparers) |
| 3 seeds never park | moot (no seed queue) |
| 4 live vs backfill absorption | live absorption ported (`Landed`); the hourly backfill (Q-3.25) **not ported**; recommendation unchanged |
| 5 untrusted mate stops | the caller passes `arbiter`; as is |
| 6 rejected TB results | landing pipeline; as is |
| 7 batch-size dependence | D2, not a queue matter |
| 8 provenance of deferred climbs | moot (no seeds) |
| 9 tokenless workers | **D8**, see §2 |
| 10 outside inputs | **recorded in the input**, see §1 |
| 11 starvation ages from `created` | as is |
| 12 sweep16 share state | as is: 60 s waiting flag (including the fleet-wide 60 s "not waiting" after one failed share pass) and the lease counter, both writer state (no cache, no random fallback) |
| 13 personal worker + campaign takes a stranger's request | as is |
| 14 searchmoves order / empty case | edge order (D3); empty case as v1 (no restriction) |
| 15 deep cap keyed by author | as is |
| 16 lock artefacts | gone by construction: "first eligible row in the total order"; one slot never holds two live leases (property-tested) |
| 17 solve submit not fenced under the lock | the checks and the transition are one writer step, so the race cannot happen (equivalent to fencing) |
| 18 release absorbs globally | as is (`absorb_requeued` at every lease and release) |
| 19 synchronous-ingest reply shape | API layer |
| 20 solve "dup" before the checks | as is |
| 21 FAILED SolveTasks re-bought by debt/fragile | as is |
| 22 regret selector / priority column | D5 + coordinator: computed in memory, lease order by stored values |
| 23 bounded approximation | moot (v1 global Dijkstra, `av2-proof`) |
| 24 tie-breaks | D3, see §2 |
| 25 pn counter = max task id + attempt | as is: the max task id is writer state, so it is exactly reproducible |
| 26 tombstones | boolean `dead` + `revived` set (revival = v1's 0.0), explicit `Revive` input |
| 27 `bootstrap_root` | not ported (no production caller); recommendation: drop |
| 28 production values | `QueueConfig` (code defaults, `production_known()` = pn, pool 64); the rest must be captured from production |
| 29 cascade reads the pre-closure status | as is (`open_before`) |
| 30 system callers lower a request's band | as is (bulk children with `source=AUTO` demotes, tested); the quality raise is not ported yet |
| 31 stale fields on revive | as is |
| 32 undo bypasses the cap | as is |
| 33 two meanings of "already searched" | as is (`completed_max_budget` vs `searched_budget`) |
| 34 MultiPV not recomputed on a raise | as is |
| 35 backer lost on the follow-up path | as is |
| 36 requests change other people's rows | as is (`then_verify`, background lift on every live USER row) |
| 37 state files | writer state (sweep plan / backlog cursors not ported yet) |
| 38 bulk click skips an absorbed slot | as is |
| 39 SolveTask uniqueness | as is |

## 4. Coverage

| Family | Status |
|---|---|
| Q-1 task model | ported (rows, states, slots, absorption, CANCELLED, indexes as row sets). Q-1.21 notifications: projection |
| Q-2.1..2.29 lease | ported; Q-2.21 head pages not needed (lazy order, §5); Q-2.29.2 console line: HTTP layer; auth (Q-2.1/2.30): HTTP layer |
| Q-2.30..2.51 heartbeat/submit/release/gate | ported; parsers in `protocol.rs` (Python `int()`/`float()`/`round()` semantics) |
| Q-2.52..2.61, SOL-5 proof leases | ported; verification is a preparer verdict in the input |
| Q-9 engine gate | ported (`gate.rs`, file stamps cached like v1, exact `REJECTION_DETAIL`) |
| Q-3, Q-4 ingest/propagation queues | replaced by the single writer; interface = `Claim` out, `Landed`/`PositionClosed` in |
| Q-5 lanes and fairness | ported (5.1-5.8, 5.10, 5.11); 5.9 queue table: projection |
| Q-6 requests | ported 6.1-6.22 (breadth swap off, `request_subtree` answers `saturated`), 6.23-6.26 door, 6.27 `then_verify`, bump/withdraw/undo/clear doors; **not**: 6.28-6.34 door parsing (API), 6.36 verify-PV (MPV-6), 6.40-6.45 lists, backlog adoption/subscriptions |
| Q-7 frontier/subtree/breadth swap/uncertainty/witness arms | **not ported** |
| Q-8 arms | ported 8.1-8.21 (rates, receipts, debt, coverage, dn repair, fragile, bulk children; 8.15 quality in `quality.rs`: the queue answers the backed ascent's `av2_rules::purchase::TaskSink`) |
| Q-10 sweep16, idle repair | lease side ported (share pass, `fits_slot`); drip/plan/backlog **not ported** |
| SOL-4 autosolve | **not ported** (the proof lease that serves it is) |
| Q-11 campaigns | lease side ported (worker campaign band); ownership/rotation: graph + people facts, not here |
| PRI-1, PRI-2.1 | `av2-proof` (formula, walk); tombstones here |
| PRI-3 refresh passes | replaced by in-memory compute (D5) |
| PRI-4 | ported 4.1-4.9, 4.11 (regret minting, pool, pn loop with `Descent`, value budget, cascade); 4.10 not ported (OQ-27) |
| PRI-5 budgets | ported, all |
| PRI-6 | the cycle is the writer's orchestration (§1 table); planner/shadow not ported |

Tests: 132 Rust tests; about 250 v1 tests of `test-map.csv` are ported by
name (each test's doc comment names the v1 tests it ports). Property tests
(`tests/properties.rs`): 12 random workloads x 400 inputs (no double lease,
superseded tokens fenced, expiry, one live lease per slot, snapshot + log
replay = live state), the lazy lease order equal to the full sort (60 random
queues x 5 workers), a cost-weighted fairness bound (served nodes of two
waiting accounts never differ by more than the largest budget) and the
single-worker round robin (counts differ by at most 1).

Corrections the port made to test expectations (they were mine, not v1's):
none of them changed a rule; they are where v1 semantics surprised the
porter (backers win ties against a plain stranger row; a hand-over
re-resolves the lane to the lighter backer; a LIFO account's first turn can
go to an older id of another account).

## 5. Numbers

Windows 10, Ryzen 9 5950X, release build, `av2-queue-bench` on the
production slice (1,560,369 positions, `av2-load --root b1791d7fc9ae3d38966568c2`),
stored v1 priorities read from the slice export (`--pos-csv`).

| Measure | Result |
|---|---|
| Lease, 1,000 USER requests + 64 AUTO, 100 busy slots | mean 69 us, p50 60 us, p99 140 us |
| Lease, 10,000 requests | mean 340 us, p99 514 us |
| Lease, 20,000 requests (v1 took 1-3 s at this size, 25-Nov) | mean 487 us, p99 671 us |
| Lease, 50,000 requests | mean 995 us, p50 928 us, p99 2.5 ms |
| Queue fact per lease (input + deltas + events) | 0.84-1.10 KB |
| Snapshot 50,064 tasks | 23 MB, write 48 ms, restore 156 ms |
| Regret Dijkstra, whole slice (warm) | 0.26 s; full priority pass 0.35 s |
| Descents (stand-in greedy walk, depth x branching reads) | 1.65 M/s (0.61 us each) |
| `next_tasks(8)` plan with the pn loop over that walk | 24.5 us |
| `next_tasks(8)` plan, regret engine (ranked list) | 1.9 us |
| `queue_ahead` at 50,000 requests (display only, read copy) | 24 ms |

The first lease implementation sorted every live row per lease (15 ms at
50,000). The lazy order walks each fair-share partition once and keeps only
the head of each (own first, named) class of each partition in a heap; it
is exact (property test) and 16x faster. The stand-in descent is not
`av2-proof`'s df-pn: replace the number when PN-6 lands.

## 6. Not done / next

- Q-7, Q-10 drips, SOL-4 autosolve, backlog adoption, Q-3.25 backfill.
- `Descent` over `av2-proof` tables (PN-6/PN-9 descents are not ported yet).
- `queue_ahead` is O(position): fine on a read copy; a counting structure if
  it ever moves back into the writer.

## 6b. Alignment with `api-contract.md` (#344)

- Errors and bodies of the worker protocol map one to one onto the outputs
  (`LeaseOut`, `HeartbeatOut`, `SubmitOut`, `SolveSubmitOut`, release
  count); the form rules are the parsers of `protocol.rs`.
- `RequestOut.detail` carries the 4.1 task fields (`task`, `budget_nodes`,
  `task_state` waiting/searching, `priority` low/normal, `backed`); `ahead`
  is added by the HTTP layer after the commit (display only).
- D8: `LeaseOut::UpgradeRequired` is `400 {"error": "upgrade-required"}`
  (contract section 2.2 and section 10; compat scenario `upgrade-required`,
  `av2-compat worker --decision-d8`).

## 7. Owner decisions of 28-Nov (applied)

1. **Stored v1 priority:** a read-only column. `sql/export.sql` exports
   `priority`; `av2-load` writes `<store>/v1prio.col` (f64 LE per id,
   `av2_model::record::V1PRIO_FILE`); `env::V1Priority` maps it and
   `GraphEnv { priority: Some(&v1prio) }` feeds `QueueState::queue_rank`.
   Tombstones and revivals stay queue state. 1,217,498 of the slice's
   1,560,369 positions carry a non-zero value (124 tombstones).
2. **Ladder memory:** two `u32` fields in the ext record
   (`Ext::ladder_bought`, `Ext::ladder_searched`, 1,000-node units floored,
   which keeps every comparison against a rung or clamp exact; ext 72 B,
   store format 2). Retention (`Input::Tick`) folds a row into them and
   returns the write in `Applied::ladder`; the writer stores it with
   `Ladder::store_in` in the same batch, before the next input. The queue
   reads them through `QueueEnv::ladder`. `export.sql` computes them for the
   rows the queue does not import (COMPLETED, generation < visits) and exports
   the rows it does (`analysistask_queue.csv`), plus every SolveTask and the
   people tables the queue reads.
3. **Queue facts:** `av2_facts::Fact::Queue`, kind 11 (payload unchanged;
   `fact::decode` still reads the earlier `Opaque "queue.v1"`).
4. **D8 wire answer:** see 6b.
