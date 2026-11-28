# AtomicDB v1 rules: queue, leases, priority and background arms

Part of the v1 rules specification for the v2 port: see
[`rules-spec.md`](rules-spec.md) for the conventions, the landing pipeline,
the value rules, the determinism register and the port order, and
[`rules-spec-proofs.md`](rules-spec-proofs.md) for proof numbers, mate proofs,
tablebases, certificates and score bands.

- **Scope.** Families Q (task model, lease protocol, ingest and propagation
  queues, lanes and fairness, requests, background arms, sweep16, idle repair),
  PRI (priority, selector, budgets) and the queue side of SOL-4 and SOL-5.
- **Source.** `atomicdb/` at `origin/spell-runner` `f59413e` (identical on
  `origin/atomicdb-v2`). Paths are `atomicdb/<file>.py:<function>:<line>`.
- **Tests.** From [`test-map.csv`](test-map.csv), filtered by rule group and
  function; the key tests of each family were read to confirm them.
- **IDs.** The groups of the test map (Q-1..Q-10, PRI-1..PRI-6, SOL-4,
  SOL-5) with stable sub-IDs (`Q-2.18`, `PRI-4.6`). Two additions: **Q-11**
  (campaign ownership and rotation), and budget sizing filed as **PRI-5.x**
  although the test map files some of it under EXP-4 (each such section says
  "also EXP-4").
- **Layout.** Six parts, in lifecycle order: (1) the task model and the lease,
  (2) heartbeat, submit, release, solve leases and the engine gate, (5) the
  ingest and propagation queues, (3) priority, selector and budgets, (4)
  requests, frontier and lanes, (6) background arms, sweep16, idle repair,
  autosolve and campaigns. Part numbers are kept as written, so the
  cross-references inside the sections stay valid.

## What is live in production (read this before porting a branch)

Production configuration that changes which branches matter. These come from files in the repo, not from the live environment (see open question OQ-28):

- `ATOMICDB_SELECTOR=pn` (`docs/solver-allocation.md:15`). `next_tasks` takes the proof/value path (PRI-4.4). `Position.priority` is never used to choose work.
- The selector runs `refresh_selector --loop --interval 120 --no-priorities --no-coverage --no-adversarial --autosolve-cap 0 --pool-target 64`. This comes from `docs/server/atomicdb-selector.service.d/70-reserva-acotada.conf`, an emergency valve set on 11-Oct-2026 and kept since. As a result:
  - the regret selector (PRI-1.5, PRI-2, PRI-3) is not running; `Position.priority` survives only as a tombstone flag (`> DEAD/2`, PRI-1.7);
  - the coverage, dn-repair and fragile arms are off, and so is the autosolve backlog walk;
  - the pool target is 64, not the 512 in the code.
- Deferred propagation (`ATOMICDB_DEFER_PROPAGATION`) is on, with five concurrent ingest writers and one propagation drainer (Q-3.24, Q-4.18).
- Synchronous ingest (`ATOMICDB_SYNCHRONOUS_INGEST`) is an emergency path and is off (Q-2.46, Q-3.22).
- Not in the repo, so these must be read from the production environment: `ATOMICDB_DESCENT`, `ATOMICDB_ARM_RATE_*`, `ATOMICDB_BREADTH_SWAP`, `ATOMICDB_SWEEP16_SHARE`, `ATOMICDB_TB_SERVER_MAX_MEN`, `ATOMICDB_CLOSURE_TRUST_GATE`, and the engine policy file `/etc/openbench/atomicdb-engine-policy.json`.

## Confirmed v1 defect a porter must not reproduce

**Q-3.7 / Q-3.16: a job can be applied twice.** `apply_job` commits the job as `DONE` (`ingest_queue.py:342`). After that commit it runs `proof.refresh_proof_numbers` (`:360`) outside any try block. If that call raises, `process_job` catches the exception (`:421`) and calls `_mark_failure` (`:425`), which unconditionally writes `PENDING` (or `FAILED`) over the committed `DONE`. The next claim applies the same landing again: the idempotence guard (`:315`) only tests for `DONE`. The same happens when a non-`DatabaseError` exception escapes the carry or verify steps (`:366-380`). v2 must apply each search exactly once. See OQ-1.

## Overlaps between parts (same behaviour, written twice; they agree)

The parts were written in parallel. Where two sections describe the same code, they were compared and found consistent. The **canonical** section is the one to port from. The other is the call-site view.

| Behaviour | Canonical | Also described in |
|---|---|---|
| Fair share `lane_ahead`, partition, window order | Q-5.6 | Q-2.18 (lease side) |
| Named tier, 24 h starvation, `served_rank` | Q-5.7 | Q-2.17, Q-2.19 |
| Absorption idiom (`absorb_tasks` / `is_absorbed`) | Q-3.14 | Q-1.11 |
| One task per (position, generation) | Q-1.3 | PRI-4.11 |
| IngestJob record and index | Q-3.1 | Q-1.15 |
| PropagationSeed record | Q-4.1 | Q-1.16 |
| SolveTask fields and lease | Q-2.52..Q-2.61, SOL-5.* | Q-1.17 |
| Requeue field shape of an analysis lease | Q-2.51 | Q-2.7, Q-2.49 |
| Worker authentication | Q-2.30 | Q-2.1 |
| Capacity touch (WorkerPing) | Q-2.31 | Q-2.3 |
| IngestJob enqueued in the submit commit | Q-2.43 | Q-3.2 |
| Synchronous ingest switch | Q-2.46 | Q-3.22 |
| Idle-repair slot routing (`fits_slot`) | Q-10.28 | Q-2.25 |
| Sweep16 fleet share and waiting cache | Q-10.9, Q-10.10 | Q-2.28.1 |
| `enqueue_unexplored_children` | Q-8.16..Q-8.20 (finer) | Q-6.35 |
| Campaign inheritance at birth | Q-11.1 | PRI-1.9 |
| Campaign subtree tagging at activation | Q-11.2 | PRI-1.10 |
| Selector cycle step order | PRI-6.1 | Q-8.22, SOL-5.4 |

## Index of rules (312)

**Part 1: AnalysisTask model and lease protocol**

- `Q-1.1` AnalysisTask states
- `Q-1.2` Source bands and their service rank
- `Q-1.3` One task per (position, generation)
- `Q-1.4` Generation semantics
- `Q-1.5` Search payload fields: budget, MultiPV, route, then_verify
- `Q-1.6` Requester identity: author, backers, lane account
- `Q-1.7` Account-internal order: queue_seq and background
- `Q-1.8` Lease identity fields
- `Q-1.9` Delivery fields
- `Q-1.10` Creation and request timestamps
- `Q-1.11` Absorption idiom
- `Q-1.12` Cancelled rows hold their slot and stay out of the ladder
- `Q-1.13` AnalysisTask indexes and the predicates that are contracts
- `Q-1.14` Producer arm and the arm/created index
- `Q-1.15` IngestJob: at most one per task; open-row index
- `Q-1.16` PropagationSeed fields
- `Q-1.17` SolveTask fields and states
- `Q-1.18` ContributorPref: per-account serving preferences
- `Q-1.19` WorkerServing: per-machine override
- `Q-1.20` WorkerPing: one row per (machine, user)
- `Q-1.21` RequestNotification de-duplication
- `Q-2.1` Worker authentication
- `Q-2.2` max_budget parsing
- `Q-2.3` Worker touch (capacity telemetry)
- `Q-2.4` Token capability and session
- `Q-2.5` Recycle of silent token leases
- `Q-2.6` Recycle of legacy tokenless leases
- `Q-2.7` Requeue row shape
- `Q-2.8` Absorb requeued rows whose position closed
- `Q-2.9` Active lease of the same slot
- `Q-2.10` Replay of a lost lease response
- `Q-2.11` Restart relief
- `Q-2.12` One task per slot
- `Q-2.13` Per-machine lease cap
- `Q-2.14` Serving preferences of this worker
- `Q-2.15` Candidate set
- `Q-2.16` Lease order keys
- `Q-2.16.1` own_first values
- `Q-2.17` Named stratum and starvation
- `Q-2.18` Fair share (lane_ahead)
- `Q-2.19` Least-recently-served tie-break (served_rank)
- `Q-2.20` Two-step order then lock
- `Q-2.21` Head-of-queue reading
- `Q-2.22` Runnable gate: open position and current generation
- `Q-2.23` Legacy-build filter
- `Q-2.24` Deep-lease cap per account
- `Q-2.25` Idle-repair slot routing
- `Q-2.26` Positions the server's tables cover are never served
- `Q-2.27` Tablebase-capable routing and fallback
- `Q-2.28` Pass sequence
- `Q-2.28.1` Sweep share pass (contract with Part 6)
- `Q-2.29` Lease minting
- `Q-2.29.1` Lease response and searchmoves
- `Q-2.29.2` WorkerPing current task and console line

**Part 2: Heartbeat, submit, release, solve leases, engine gate**

- `Q-2.30` Worker authentication (enabled account, cached verification)
- `Q-2.31` Capacity touch (WorkerPing upsert without touching leases)
- `Q-2.32` Heartbeat request validation
- `Q-2.33` Heartbeat lease renewal (fencing)
- `Q-2.34` Heartbeat live-view update
- `Q-2.35` Submit payload validation (before any database read)
- `Q-2.36` Submit scalar sanitation (elapsed, nodes, tb_wdl, tb_dtz)
- `Q-2.37` Provenance sha sanitation (`_sha_field`)
- `Q-2.38` Submit ownership checks on the unlocked snapshot (error precedence)
- `Q-2.39` Duplicate submit is idempotent
- `Q-2.40` Engine gate at submit: refuse, requeue, absorb
- `Q-2.41` Atomic claim of the task (compare-and-set) and loser classification
- `Q-2.42` Reported nodes clamped to twice the authoritative budget
- `Q-2.43` IngestJob enqueued in the claim's commit
- `Q-2.44` AnalysisTask completion fields
- `Q-2.45` WorkerPing delivery stamp
- `Q-2.46` Synchronous ingest switch and the submit reply
- `Q-2.47` Release request parsing (all-or-nothing)
- `Q-2.48` Release ownership filter and lock order
- `Q-2.49` Release requeue (same row shape as an expired lease)
- `Q-2.50` Release absorption and live-view cleanup
- `Q-2.51` Analysis requeue field contract (shared helper)
- `Q-2.52` Solve acquire: request handling and max budget
- `Q-2.53` Solve acquire: in-request stale sweep
- `Q-2.54` Solve acquire: replay of the live assignment (same session)
- `Q-2.55` Solve acquire: one live lease per machine identity, restart relief
- `Q-2.56` Solve acquire: selection order
- `Q-2.57` Solve acquire: assignment and payload
- `Q-2.58` Solve heartbeat (strict token)
- `Q-2.59` Solve submit: validation and fencing precedence
- `Q-2.60` Solve submit: certificate cap, telemetry and advisory fields
- `Q-2.61` Solve submit: task state transitions (apply_solve_result boundary)
- `SOL-5.1` Stale solve-lease predicate and window
- `SOL-5.2` Solve requeue field contract
- `SOL-5.3` SOLVE_LEASE_RECYCLED event
- `SOL-5.4` Who runs the solve recycler, and when
- `Q-9.1` Engine policy file: mode and extra engines (fail-open)
- `Q-9.2` Reference engines discovery
- `Q-9.3` Accepted engine set
- `Q-9.4` Gate decision (`rejects`)

**Part 5: Ingest queue and propagation queue**

- `Q-3.1` IngestJob record, states and the one-job-per-task invariant
- `Q-3.2` Enqueue in the submit transaction
- `Q-3.3` Ready set of the ingest queue
- `Q-3.4` Claiming a job
- `Q-3.5` Failure, backoff and parking of a job
- `Q-3.6` Manual requeue of failed jobs
- `Q-3.7` Applying one job: process_job and drain
- `Q-3.8` apply_job: payload parsing and the preparation outside the transaction
- `Q-3.9` apply_job: task context, attribution and trust
- `Q-3.10` apply_job: idempotence under the job lock
- `Q-3.11` apply_job: the three landing branches
- `Q-3.12` Arbiter budget of a mate-stopped search (EXP-5)
- `Q-3.13` Live absorption of shadowed requests (EXP-5)
- `Q-3.14` The absorption idiom (absorb_tasks / is_absorbed)
- `Q-3.15` Engine time accounting
- `Q-3.16` Job completion (DONE)
- `Q-3.17` "Analysis ready" notification to the requesters
- `Q-3.18` Proof numbers of the landed position after the job
- `Q-3.19` Carrying a cut ascent after the job commits
- `Q-3.20` Request + verify PV ("then_verify") after the job
- `Q-3.21` Hand-off of the climb to the propagation queue
- `Q-3.22` Synchronous ingest switch (emergency path)
- `Q-3.23` Tablebase sweep of queued positions (TB-5)
- `Q-3.24` The ingest processor loop (process_ingest_queue)
- `Q-3.25` Backfill of zombie requests (absorb_shadowed_tasks)
- `Q-4.1` PropagationSeed record
- `Q-4.2` Deferral switch
- `Q-4.3` Writing a seed (defer)
- `Q-4.4` Ready set of the propagation queue
- `Q-4.5` Claim size: a row that failed before climbs alone
- `Q-4.6` Claiming a batch
- `Q-4.7` One climb per closure source
- `Q-4.8` Merging seeds of one position (_merge)
- `Q-4.9` Batch seeds and per-landing arms (contract with EXP-6)
- `Q-4.10` Recording the climb on its jobs (_record)
- `Q-4.11` Settling: deleting the climbed rows
- `Q-4.12` Transient retry of a whole batch
- `Q-4.13` Failure path of a batch: per-row backoff, never parked
- `Q-4.14` Proof numbers after the batch commit
- `Q-4.15` Proof refresh pipeline thread
- `Q-4.16` After the batch commit: landed pn/dn, carried cuts, deferred verifications
- `Q-4.17` Draining loop
- `Q-4.18` The propagation drainer service (process_propagation)
- `Q-4.19` Leftover seeds drained by the ingest writers when deferral is off
- `Q-4.20` Seeds in flight hold off the stale-backed repair
- `Q-4.21` Batch-dependence of the deferred climb (documented semantics)

**Part 3: Priority, selector and budgets**

- `PRI-1.1` Priority constants
- `PRI-1.2` Value fed to the selector (best-known precedence)
- `PRI-1.3` Regret units and out-of-ball prices
- `PRI-1.4` Campaign bonus
- `PRI-1.5` The priority formula
- `PRI-1.6` Priority ceiling (horizon bound)
- `PRI-1.7` Tombstones (DEAD) and their revival
- `PRI-1.8` `_still_reachable` (does analysing it still matter upward)
- `PRI-1.9` Campaign ownership by inheritance (priority input)
- `PRI-1.10` Campaign subtree adoption on activation
- `PRI-2.1` Global regret Dijkstra (v1)
- `PRI-2.2` Bounded regret Dijkstra (v2)
- `PRI-2.3` Horizon cut and top-K
- `PRI-2.4` Batch-boundary approximation of the bounded Dijkstra
- `PRI-3.1` `refresh_priorities` dispatcher and engine switch
- `PRI-3.2` Refresh throttle, `force` and compute-only mode
- `PRI-3.3` Full pass, v1 engine
- `PRI-3.4` Pass, v2 engine (full or delta)
- `PRI-3.5` Delta-mode selection (`_delta_since`)
- `PRI-3.6` Delta key set
- `PRI-3.7` Pass record, marker persistence and write histogram
- `PRI-3.8` Inline emergency refresh
- `PRI-4.1` `next_tasks(n)` dispatch
- `PRI-4.2` Regret-mode minting
- `PRI-4.3` Analysis pool top-up
- `PRI-4.4` Proof/value selector loop (`_next_tasks_by_proof`)
- `PRI-4.5` Descent mode and the walker's hourly gate
- `PRI-4.6` Budget composition in the proof/value selector
- `PRI-4.7` Arm marking of selector tasks
- `PRI-4.8` `value_budget(target)`
- `PRI-4.9` Value cascade after a landing
- `PRI-4.10` `bootstrap_root`
- `PRI-4.11` Task identity at minting (dedupe by position and generation)
- `PRI-5.1` Exploration ladder (`BUDGET_LADDER`)
- `PRI-5.2` Request ladder and derived request constants
- `PRI-5.3` `budget_for(pos)` (visit ladder + mate band jump)
- `PRI-5.4` Claimed mate distance and the short-mate clamp (also EXP-4)
- `PRI-5.5` `multipv_for(visits, budget_nodes=None, seeding=False, clamp=None)`
- `PRI-5.6` Server-picked floor (`server_picked_budget`)
- `PRI-5.7` No deep rungs on interior positions (`caps_interior`)
- `PRI-5.8` Rung helpers
- `PRI-6.1` Selector service cycle (`refresh_selector`)
- `PRI-6.2` Production selector configuration
- `PRI-6.3` Proof-directed planner (shadow only)
- `PRI-6.4` Planner cost model file
- `PRI-6.5` Selector shadow comparison
- `PRI-6.6` Switches that change selection (summary)

**Part 4: Requests, frontier and lanes**

- `Q-5.1` Contributor delivery window
- `Q-5.2` Right to choose a rung, and what a chosen budget may be
- `Q-5.3` Effective (lane) account of a request
- `Q-5.4` Charged loads
- `Q-5.5` Lane resolution when the requester set changes
- `Q-5.6` Fair share: partition and `lane_ahead`
- `Q-5.7` Service keys after the fair share (named tier, backers, least-recently-served)
- `Q-5.8` Place in the queue (`ahead`) served to the requester
- `Q-5.9` Queue table (served values of /queue/)
- `Q-5.10` Per-account cap of waiting requests
- `Q-5.11` Queue-full refusal, receipt and HTTP contract
- `Q-6.1` Request outcome vocabulary
- `Q-6.2` Bought rung vs searched budget
- `Q-6.3` Next rung, ladder state, ladder exhausted
- `Q-6.4` Purchase: lock and closed check
- `Q-6.5` Purchase gates: `only_unexplored` and `only_if_missing`
- `Q-6.6` Purchase floor
- `Q-6.7` Purchase: new slot
- `Q-6.8` Purchase: revive a withdrawn or absorbed slot
- `Q-6.9` Purchase: existing PENDING row (raise, promote, adopt, join)
- `Q-6.10` Purchase: LEASED row too shallow → follow-up generation
- `Q-6.11` Purchase: LEASED row that already covers the rung
- `Q-6.12` Purchase: delivered-but-unapplied COMPLETED row on the slot
- `Q-6.13` Request priority merge (`background`)
- `Q-6.14` Request time stamp
- `Q-6.15` Record a requester (join / adopt)
- `Q-6.16` add_requester: join, lend lane, move to front
- `Q-6.17` Front of own queue (bump floor)
- `Q-6.18` Newest-first (LIFO) preference
- `Q-6.19` Withdraw a request (author only)
- `Q-6.20` Clear own queue
- `Q-6.21` Undo a withdrawal
- `Q-6.22` request_analysis dispatch
- `Q-6.23` Which served tasks notify their requester
- `Q-6.24` `_queue_request`: only-if-missing pre-answer
- `Q-6.25` `_queue_request`: ip + position dedup
- `Q-6.26` `_queue_request`: cap, route, purchase, receipt, payload
- `Q-6.27` Explorer single-request door
- `Q-6.28` `expect` field
- `Q-6.29` Script identity and priority
- `Q-6.30` Public request API (`/atomicdb/api/request`)
- `Q-6.31` `moves` label on API requests
- `Q-6.32` Multi-select of moves (`/atomicdb/request-moves/<key>/`)
- `Q-6.33` Request children API (`/atomicdb/api/request-children`)
- `Q-6.34` Unexplored-moves button (`/atomicdb/request-unexplored/<key>/`)
- `Q-6.35` enqueue_unexplored_children (request path and AUTO arm path)
- `Q-6.36` Verify-PV door (queueing side only)
- `Q-6.37` Move to front (bump), web and script
- `Q-6.38` Withdraw / undo, web and script
- `Q-6.39` Clear own queue door
- `Q-6.40` My queue (served list)
- `Q-6.41` Newest-first toggle
- `Q-6.42` Backlog adoption: batch, places and caps
- `Q-6.43` Backlog adoption: verdict, budget and width
- `Q-6.44` Backlog adoption: mint on the slot
- `Q-6.45` Backlog subscriptions (periodic top-up)
- `Q-7.1` Frontier ranking
- `Q-7.2` Frontier width and slice
- `Q-7.3` Spent-ladder probe and expansion on read
- `Q-7.4` Frontier expansion one ply below
- `Q-7.5` Frontier descent (breadth swap walk)
- `Q-7.6` Breadth-swap eligibility
- `Q-7.7` Subtree attacker
- `Q-7.8` Subtree walk: parents, replies, candidates
- `Q-7.9` Subtree coverage classification
- `Q-7.10` Subtree request: purchase loop, counting, stop, event
- `Q-7.11` Uncertainty expansion (AUTO arm after a landing)
- `Q-7.12` Witness-refuted revisit (AUTO arm after a landing)

**Part 6: Background arms, sweep, idle repair, autosolve, campaigns**

- `Q-8.1` Arm tag versus service band
- `Q-8.2` Hourly rate setting per arm
- `Q-8.3` Hourly budget room (rolling window)
- `Q-8.4` Arm-rate receipt event
- `Q-8.5` Which producers pay the hourly ceiling
- `Q-8.6` Engine debt: candidates
- `Q-8.7` Engine debt: cap, task shape, receipt
- `Q-8.8` Coverage gap predicate
- `Q-8.9` Coverage completion: scan and minting
- `Q-8.10` Adversarial-arms switch
- `Q-8.11` dn repair: which nodes
- `Q-8.12` dn repair: what is bought
- `Q-8.13` Fragile mate claim predicate
- `Q-8.14` Fragile mate claims: minting
- `Q-8.15` Quality arm purchases (task shape only)
- `Q-8.16` "Unexplored" predicate (human button)
- `Q-8.17` "Unjudged" predicate (automatic arms)
- `Q-8.18` Bulk child purchase: budget and width
- `Q-8.19` Bulk child purchase: joining a live task
- `Q-8.20` Bulk child purchase: create, lost race, withdrawn slot
- `Q-8.21` Bulk unexplored endpoint (call site)
- `Q-8.22` Background steps of the selector cycle
- `Q-8.23` Background producers riding on a landing
- `Q-10.1` Sweep16: the qualifying rule
- `Q-10.2` Sweep16: walk of White's strategy tree
- `Q-10.3` Sweep16: scan of the rest and its order
- `Q-10.4` Sweep16: the plan and its files
- `Q-10.5` Sweep16 drip: gates and room
- `Q-10.6` Sweep16 drip: per-key recheck and minting
- `Q-10.7` Sweep16: reviving its own absorbed task
- `Q-10.8` Sweep16 drip: cursor save and receipt
- `Q-10.9` Sweep16 fleet share at lease time
- `Q-10.10` Sweep16 waiting cache
- `Q-10.11` Sweep16: stop, withdraw, rewind, status
- `Q-10.20` Idle repair: shallow showcase predicate
- `Q-10.21` Idle repair: repair budget
- `Q-10.22` Idle repair: connector predicate
- `Q-10.23` Idle repair: once per position
- `Q-10.24` Idle repair: room
- `Q-10.25` Idle repair: minting
- `Q-10.26` Idle repair at a landing
- `Q-10.27` Idle repair and autosolve: the backlog walk
- `Q-10.28` Idle repair: slot routing at lease
- `SOL-4.1` Autosolve: what "scores a mate" means
- `SOL-4.2` Autosolve: claims of a showcase
- `SOL-4.3` Autosolve: targets
- `SOL-4.4` Autosolve: never ask twice
- `SOL-4.5` Autosolve: room
- `SOL-4.6` Autosolve: minting and receipt
- `SOL-4.7` Autosolve at a landing
- `SOL-4.8` Autosolve: backlog walk specifics
- `Q-11.1` Campaign ownership lives on the position
- `Q-11.2` Subtree tagging at activation
- `Q-11.3` Community rotation of the active campaign
- `Q-11.4` Owner campaign actions
- `Q-11.5` A campaign closes when its root closes
- `Q-11.6` Where campaigns touch the queue (pointers)


## State diagrams

Notation: `A -> B | function:line | trigger`. Lines refer to `atomicdb/ingest.py` unless another file is named; `views` = `atomicdb/views.py`. "Absorbed" means `COMPLETED` with `nodes_searched = 0` and `machine = ''` (Q-3.14).

### AnalysisTask

```
                 create (14 producers, see table)
                               |
                               v
  CANCELLED <--withdraw/clear-- PENDING --absorb_tasks--> COMPLETED (absorbed)
      |                        ^ |  ^ ^                       |
      +---undo / revive--------+ |  | +-------revive----------+
                                 |  |   (_revive_withdrawn, sweep._revive)
                    api_lease    |  |  recycle: token 4 min, legacy 60 min / 24 h;
                                 |  |  restart relief 3 min; api_release; engine-gate refusal
                                 v  |
                               LEASED --api_submit claim (CAS)--> COMPLETED (delivered)
                                 ^ |
                                 +-+ heartbeat (renews lease_heartbeat_at)

  PENDING (sweep16 FILL) --sweep.withdraw_pending--> row deleted
  There is no FAILED state for analysis tasks; readers filter states positively (Q-1.1).
```

**Creation, `∅ -> PENDING`.** Every producer mints on the slot `(position, generation = position.visits)`. The one exception is the follow-up at `visits + 1`. Uniqueness is enforced by Q-1.3.

| Producer | Source / arm / budget | Code | Trigger |
|---|---|---|---|
| regret selector | AUTO, arm `''` | `next_tasks:6856` | lease refill (`views:1407-1412`) or `top_up_analysis_pool` (selector pass) |
| proof/value selector | AUTO, arm `campaign` / `descend` / `''` | `_next_tasks_by_proof:7135` | same as above, `ATOMICDB_SELECTOR=pn` |
| value cascade | AUTO, arm `cascade`, 8M | `_queue_value_cascade:7021` | landing (value descent only) |
| bootstrap | USER, 2B, MultiPV 5 | `bootstrap_root:7172` | manual call |
| request, new slot | USER | `_request_rung:7759` | a person's request |
| request, follow-up | USER, `generation = visits+1` | `_request_rung:7845` | deeper request while a shallower search is LEASED |
| bulk children | USER or AUTO | `enqueue_unexplored_children:9728` | unexplored button, request-children API, uncertainty arm |
| witness refuted | AUTO | `_witness_refuted_revisit:8496` | landing |
| backlog adoption | USER, backlog arm | `backlog._mint:651` | community backlog |
| coverage | FILL, `coverage` | `enqueue_coverage_completion:9268` | selector pass (off in production) |
| dn repair | FILL, `dn` | `enqueue_dn_repair:9411` | selector pass, adversarial switch (off in production) |
| quality | FILL, `quality` | `_queue_cycle_disambiguation:4212`, `_queue_quality_convergence:4282` | backed ascent (BKD-11) |
| idle repair | AUTO, `repair` | `idle_repair._mint:270` | landing (`queue_from_landing:290`) or backlog walk (`drip_backlog:415`) |
| sweep16 | FILL, `sweep16` | `sweep.drip:643` | selector pass |

Also out of this catalog's scope: `enqueue_pv_verification` / `_buy_verification` (MPV-6) and `_queue_disputed_reanalysis` (REV-3).

**State transitions.**

| Transition | Code | Trigger |
|---|---|---|
| PENDING -> LEASED | `views:api_lease:1424-1436` | lease picks the row (Q-2.29) |
| LEASED -> LEASED (renew `lease_heartbeat_at`) | `views:api_heartbeat:1531` | heartbeat with the same machine and token (Q-2.33) |
| LEASED -> PENDING | `views:api_lease:1034-1038` | token lease silent for more than 4 min (Q-2.5) |
| LEASED -> PENDING | `views:api_lease:1042-1047` | tokenless lease: heartbeat older than 60 min, or no heartbeat and leased more than 24 h ago (Q-2.6) |
| LEASED -> PENDING | `views:api_lease:1373-1377` | same slot returns with a new session; holder silent for more than 3 min (Q-2.11) |
| LEASED -> PENDING | `views:api_release:1876` via `_requeue_leases:973` | worker hands the lease back (Q-2.49) |
| LEASED -> PENDING | `views:api_submit:1641` | engine gate refuses the result; CAS on attempts, leased_at and token (Q-2.40) |
| LEASED -> COMPLETED | `views:api_submit:1670`, fields `:1708-1725` | accepted submit; the IngestJob is created PENDING in the same commit (Q-2.41..Q-2.44) |
| PENDING -> COMPLETED (absorbed) | `absorb_tasks:5784`, called from the four rows below | |
|   ↳ | `views:_absorb_requeued:990` | every lease and every release: all requeued rows on closed positions (Q-2.8, Q-2.50) |
|   ↳ | `views:api_submit:1654` | gate refusal on a closed position |
|   ↳ | `ingest_queue._absorb_shadowed:234` | landing with budget >= task budget (Q-3.13) |
|   ↳ | `_emit_closure_events:5827`; `absorb_shadowed_tasks` command `:89-90` | any closure (CAS-7); hourly backfill (Q-3.25) |
| PENDING -> CANCELLED | `withdraw_requester:7440`; `clear_own_queue:7491` | author withdraws with no other backers; clears own queue (Q-6.19, Q-6.20) |
| CANCELLED -> PENDING | `restore_cancelled:7513` | undo, only if the position is still open (Q-6.21) |
| CANCELLED -> PENDING | `_revive_withdrawn:7642` via `_request_rung:7774`, `enqueue_unexplored_children:9763`, `backlog._mint:661` | new request on a withdrawn slot (Q-6.8) |
| COMPLETED (absorbed) -> PENDING | `_revive_withdrawn` via `_request_rung:7795`, `backlog._mint:661` | new request on an absorbed slot (Q-6.8) |
| COMPLETED (absorbed, sweep16 FILL) -> PENDING, `created = now` | `sweep._revive:582` | drip finds its own absorbed row (Q-10.7) |
| PENDING (sweep16 FILL) -> deleted | `sweep.withdraw_pending:691` | `sweep_plus16 --withdraw` (Q-10.11) |

**Rewrites inside a state** (no state change, but the band, owner or budget changes, and with it the order of service):

| Rewrite | Code |
|---|---|
| PENDING AUTO/FILL -> USER (promotion) | `_request_rung:7811`, `:7860`; `backlog._mint:668`; `enqueue_unexplored_children:9710` |
| LEASED AUTO/FILL -> USER | `_request_rung:7873` |
| PENDING USER -> AUTO (**demotion**, OQ-30) | `enqueue_unexplored_children:9710`, called by `_uncertainty_expand:8531` |
| PENDING any -> FILL `quality` with a raised budget (**may demote USER**, OQ-30) | `:4221`, `:4295` |
| PENDING AUTO -> FILL `coverage` / `dn` | `:9285`, `:9429` |
| PENDING any -> USER with budget raised to the maximum | `bootstrap_root:7158-7165` |
| owner hand-over: author withdraws while others back the row | `withdraw_requester:7425` |
| field writes: `queue_seq` | `add_requester:7387`, `_apply_lifo_preference:7595`, bump `views:8162` / `:2385` |
| field writes: `lane_account` | `_resolve_lane_account:7536` |
| field writes: `also_requested_by` / `backers` | `_record_requester:7349` |
| field writes: `background` | `_merge_priority:7671`, `views._queue_request:2963`, `request_subtree:8399` |
| field writes: `then_verify` | `views._api_request_once:2764` |
| field writes: `route` (only when empty) | `views._api_moves_outcome:3546` |

### IngestJob (Q-3)

```
 ∅ --enqueue (submit commit)--> PENDING --claim_next--> PROCESSING --apply_job commit--> DONE (terminal*)
                                  ^  ^                     |   ^
                                  |  +--_mark_failure------+   +-- stale reclaim after 900 s (attempts unchanged)
                                  |     (attempts<5; backoff 5/30/120/600 s)
          retry_failed (manual) --+                         \--_mark_failure (5th attempt)--> FAILED
   * DONE -> PENDING/FAILED happens through the double-apply defect (process_job:425 after ingest_queue:360)
```

| Transition | Code | Trigger |
|---|---|---|
| ∅ -> PENDING | `ingest_queue.enqueue:68-70`, called at `views:1693` | inside the submit's claim transaction (Q-2.43) |
| PENDING -> PROCESSING | `ingest_queue.claim_next:96` | `next_attempt_at <= now` or NULL; `FOR UPDATE SKIP LOCKED`, lowest id first (Q-3.4) |
| PROCESSING -> PROCESSING | `claim_next:96` | stale claim (`claimed_at` older than 900 s); attempts not counted |
| PROCESSING -> DONE | `apply_job:342` | applied, in the same transaction as the tree write (Q-3.16) |
| PENDING -> DONE | `apply_job:342` via `views:1753` | synchronous switch, no claim (Q-3.22) |
| PROCESSING/PENDING -> PENDING | `_mark_failure:175` | exception and `attempts < 5`; backoff 5, 30, 120, 600 s (Q-3.5) |
| PROCESSING/PENDING -> FAILED | `_mark_failure:175` | 5th failed attempt; parked |
| **DONE -> PENDING / FAILED (defect)** | `_mark_failure:175` via `process_job:425` | exception after the commit (`ingest_queue:359-380`); re-applied on the next claim |
| FAILED -> PENDING | `retry_failed:508` | manual `--retry-failed`; `attempts = 0` (Q-3.6) |
| any -> deleted | FK `on_delete=CASCADE` | task or position deleted |

### PropagationSeed (Q-4)

There is no state column. A row's existence and its row lock are the state.

```
 ∅ --defer (stage a commit)--> ready --drain_batch claim (SKIP LOCKED)--> locked --climb commits--> deleted
                                 ^                                         |  |
                                 |  next_attempt_at reached                |  +-- transient 40P01/40001: same rows retried (<=2)
                                 +---------- backoff <--_mark_failure------+      (attempts+1, no maximum, never parked)
```

| Transition | Code | Trigger |
|---|---|---|
| ∅ -> ready | `propagation_queue.defer:168` | end of stage (a) with deferral on (`ingest.py:1309`) (Q-4.3) |
| ready -> locked | `drain_batch:418` | batch claim (Q-4.6); a row that failed before climbs alone (`_claim_size:395`, Q-4.5) |
| locked -> deleted | `drain_batch:455` | climb committed (Q-4.11) |
| locked -> ready (unchanged) | `drain_batch:464` | transient SQLSTATE 40P01 or 40001, up to 2 retries (Q-4.12) |
| locked -> backoff | `_mark_failure:189` | any other error, or retries exhausted (Q-4.13) |
| backoff -> ready | `ready_seeds:175` | `next_attempt_at` reached |
| any -> deleted | CASCADE | position deleted |

### SolveTask (SOL-4, SOL-5, Q-2.52..Q-2.61)

| Transition | Code | Trigger |
|---|---|---|
| ∅ -> PENDING (arm `debt`, F0, 2M) | `enqueue_engine_debt:8989` | selector pass (Q-8.7) |
| ∅ -> PENDING (arm `fragile`, F0, 2M) | `enqueue_fragile_mate_solves:9543` | selector pass, adversarial switch on (Q-8.14) |
| ∅ -> PENDING (arm `autosolve95`, F0, 8M) | `autosolve._mint:246` | landing or backlog walk (SOL-4.6) |
| PENDING -> LEASED (attempts+1, new token) | `views:api_solve_acquire:1984-1991` | acquire; order: non-debt first, budget descending, id ascending, SKIP LOCKED (Q-2.56) |
| LEASED -> LEASED (heartbeat) | `views:api_solve_heartbeat:2019` | exact token required (Q-2.58) |
| LEASED -> PENDING | `recycle_stale_solve_leases:8904` via `requeue_solve_leases:8873` | `leased_at` and heartbeat both older than 20 min; run from acquire (`views:1936`) and the selector step `solve-leases` (`refresh_selector.py:302`) (SOL-5.1..5.4) |
| LEASED -> PENDING | `views:api_solve_acquire:1961` | restart relief: same machine, other session, holder silent for more than 3 min (Q-2.55) |
| LEASED -> PENDING | `views:api_release:1873` | release with a valid token |
| LEASED -> COMPLETED | `apply_solve_result:8788` | valid submit: UNKNOWN, DISPROVED, or PROVED with a certificate that verifies (Q-2.61) |
| LEASED -> FAILED (terminal for the endpoints) | `apply_solve_result:8778` | PROVED with a missing or failing certificate; emits `SOLVE_REJECTED`. The debt and fragile arms buy the slot again (OQ-21) |
| PENDING or LEASED-by-another -> COMPLETED / FAILED (race) | `apply_solve_result:8766` | ownership was checked on an unlocked read (`views:2045`) (OQ-17) |

### Campaign (Q-11) and the Position tombstone (PRI-1.7)

| Transition | Code |
|---|---|
| Campaign PROPOSED/PAUSED -> ACTIVE | `community_campaign.rotate:66`; `views.campaign_state:8822` (activation also tags the subtree, Q-11.2) |
| Campaign ACTIVE -> PAUSED | `rotate:62`; `views.campaign_state` |
| Campaign ACTIVE -> DONE | `_emit_closure_events:5832` (root closed); `views.campaign_state` (`done`) |
| Position.campaign_id NULL -> id | at birth by inheritance (Q-11.1); `views._tag_campaign_subtree:8572-8627` (Q-11.2) |
| Position.priority live -> DEAD (-1e9) | `next_tasks:6851`, `_next_tasks_by_proof:7114` (every parent closed) |
| Position.priority DEAD -> 0.0 | `expand:752`, `link_existing_children:670`, `_revive_tombstones:5496` (the only one that also sets `updated`), `_walk_pv_frontier:10102`, `views.goto:4401` |

## Global ordering of the rules (one landing, one lease)

1. **Lease** (Q-2.1..Q-2.29). The order inside `api_lease` is:
   1. authenticate and touch;
   2. recycle silent leases (Q-2.5, Q-2.6) and absorb requeued rows on closed positions (Q-2.8);
   3. replay or restart relief for the slot (Q-2.9..Q-2.11);
   4. machine cap (Q-2.13);
   5. pass sequence (Q-2.28): sweep share first (Q-2.28.1), then personal or campaign bands, then the fair-share USER band (Q-5.6/Q-5.7), then AUTO/FILL by `Position.priority`;
   6. per-candidate gates under lock (Q-2.20..Q-2.27);
   7. if nothing is pending, refill with `next_tasks(8)` (PRI-4.1) inside the same transaction;
   8. mint the lease (Q-2.29).
2. **Heartbeat** (Q-2.32..Q-2.34) renews the lease until a submit or a release.
3. **Submit** (Q-2.35..Q-2.46): validation, then the unlocked ownership read, then the engine gate, then the CAS claim, then the node clamp, then `ingest_queue.enqueue` in the same commit.
4. **Ingest writer** (Q-3.3..Q-3.21): claim; prepare outside the transaction (mate proofs, TB probes); then, in one transaction, the landing (EXP-2..), `_absorb_shadowed` (Q-3.13), time accounting and DONE. It writes the PropagationSeed when deferral is on (Q-3.21). After the commit: pn/dn of the landed position, carry of cut ascents, `then_verify`, and notification.
5. **Propagation drainer** (Q-4.4..Q-4.16): claim a batch; merge per position; climb (CAS/BKD, out of scope); landing arms (`_queue_value_cascade`, `_uncertainty_expand`, `_witness_refuted_revisit`, idle repair, autosolve: Q-8.23); delete the seeds. After the commit: pn/dn (pipeline thread), carried cuts, deferred verifications.
6. **Selector pass** (PRI-6.1), every 60 s (120 s in production). Steps in order: priorities, solve-lease recycle (SOL-5), debt, coverage, adversarial (dn, fragile), autosolve backlog, repair backlog, sweep16 drip, pool top-up, counters.


---

## Part 1: AnalysisTask model and lease protocol

Scope: `atomicdb/models.py` (AnalysisTask, its neighbours' semantic constraints, serving preferences), the semantic migrations, and the worker lease endpoint `atomicdb/views.py:api_lease` with every helper it calls in `views.py`, `live_request.py` (service order) and `preferences.py`. Heartbeat, submit, release and the solve endpoints are Part 2 (Q-2.30+); lanes/requests Part 4 (Q-5/Q-6/Q-7); selector refill Part 3 (PRI-4); sweep/idle-repair internals Part 6 (Q-10). Line numbers are those of `origin/spell-runner` `f59413e`.

Vocabulary used below: a *slot* is the `machine` string a worker sends (for `--jobs K` it is `base#0`..`base#K-1`); the *machine base* is the part before the first `#`. "Now" is `django.utils.timezone.now()` (UTC) unless stated.

---

### Q-1.1 AnalysisTask states
- **Statement.** An `AnalysisTask` is one purchase of one engine search on one position. `state` is one of four strings: `PENDING` (waiting to be leased), `LEASED` (handed to exactly one slot), `COMPLETED` (either delivered by a worker, or *absorbed*: completed without a search, see Q-1.11) and `CANCELLED` (withdrawn by its requester before any engine looked at it). Default `PENDING`. There is no `FAILED` state for analysis tasks (only `SolveTask` has one). Every reader in v1 filters by state *positively* (`state='PENDING'`, `state__in=(PENDING, LEASED)`, ...); there is no `exclude(state=...)` on this table, which is what makes `CANCELLED` fall out of the queue, the fair share, the served lists, node accounting and the request ladder automatically. A port must keep the positive-filter semantics: any new state added later is invisible to every existing rule. `CANCELLED` is *not* `COMPLETED`: `ingest._completed_max_budget` reads COMPLETED rows to know which ladder rung was bought, so a cancelled 128M row must not make the next request buy 512M.
- **Inputs / outputs.** `AnalysisTask.state` (CharField 10, indexed). Transitions are listed in the state diagram of the assembled catalog; in this part: PENDING->LEASED (Q-2.29), LEASED->PENDING (Q-2.5, Q-2.6, Q-2.11), PENDING->COMPLETED absorbed (Q-2.8).
- **Code.** `atomicdb/models.py:AnalysisTask.TState:361-394`.
- **Tests.** `test_profile.py::QueueTests::test_the_three_states_land_in_their_own_lists`; `test_lanes.py::CancelledWorkCostsNothingTests::test_cancelled_rows_are_not_charged_to_an_account`; `test_queue_control.py::WithdrawOwnRequestTests::test_a_waiting_request_leaves_the_queue`; `test_queue_control.py::WithdrawCostsNothingTests::test_asking_again_revives_the_same_row`. **Gaps:** no test asserts that no code path writes a fifth state value; no test pins that `CANCELLED` rows are excluded from `_completed_max_budget` directly (covered indirectly by the revive tests).
- **Determinism hazards.** none (pure enumeration).
- **History.** `CANCELLED` added 2026-08-16 (commit 95ab6d4 "take back a request, empty your queue, and ask by API", migration 0041). Base states since tier 1 (b409c30, 2026-07-21).
- **Ordering.** Foundation for every Q rule.

### Q-1.2 Source bands and their service rank
- **Statement.** `source` is one of `AUTO` (selector best-first work), `FILL` (coverage completion and other server arms that close nodes), `USER` (a person's request). Default `AUTO`. The lease serves by `-source`, i.e. descending string order, which is `USER` > `FILL` > `AUTO`; this ordering is load-bearing and comes from the literal strings, so a port must sort by an explicit rank USER=2, FILL=1, AUTO=0 (or keep the strings). `source` says *how the row is served*; who produced it is `arm` (Q-1.14). A row can change source over its life (promoted to USER when someone requests it, degraded to AUTO by coverage expansion, see Part 4); fair-share keys are bounded to the USER band precisely because of degraded rows (Q-2.18).
- **Inputs / outputs.** `AnalysisTask.source` (CharField 4, indexed).
- **Code.** `atomicdb/models.py:AnalysisTask.Source:396-404`, field `:416`; consumer `atomicdb/views.py:api_lease.choose_pending:1291`.
- **Tests.** `test_coverage_completion.py::CoverageIsServedAheadOfExplorationTests::test_the_queue_order_is_user_then_fill_then_auto`; `test_queue_fairness.py::UserBandFifoTests::test_a_user_click_still_outranks_every_selector_task`. **Gaps:** none.
- **Determinism hazards.** Collation: `-source` relies on byte order of ASCII upper-case strings; any collation (PostgreSQL locale) gives the same order for these three values.
- **History.** `FILL` introduced with coverage completion (migration 0022, July 2026).
- **Ordering.** Read by Q-2.16.

### Q-1.3 One task per (position, generation)
- **Statement.** Unique constraint `uniq_task_per_generation` on `(position_id, generation)`: a position has at most one task row per visit number, whatever its state. Consequences a port must reproduce: (a) a COMPLETED, absorbed or CANCELLED row keeps occupying its slot, so a later purchase for the same generation must *revive* that row (`ingest._revive_withdrawn`, `ingest.restore_cancelled`, `sweep._revive`; Part 4/6) instead of inserting; (b) two concurrent producers racing to create the same generation get an IntegrityError and must re-read (the "lost race" path of `enqueue_unexplored_children`, Part 4/6); (c) a deeper follow-up for the *next* visit is created at `generation = visits + 1` and waits (Q-2.22).
- **Inputs / outputs.** `AnalysisTask.position_id`, `AnalysisTask.generation` (int, default 0).
- **Code.** `atomicdb/models.py:AnalysisTask.Meta:547-548`; `atomicdb/migrations/0001_initial.py:92`.
- **Tests.** `test_mate_clamp.py::UnexploredChildrenTests::test_a_lost_race_for_the_same_generation_does_not_break_the_click`; `test_queue_control.py::WithdrawCostsNothingTests::test_asking_again_revives_the_same_row`; `test_idle_repair.py::ShallowLandingTests::test_a_task_already_on_the_next_visit_is_the_next_search`. **Gaps:** no test inserts a second row for the same generation directly and asserts the database refuses it.
- **Determinism hazards.** Race between concurrent creators resolved by the database (first committer wins; the loser's IntegrityError path decides what it does). In a single-writer v2 the order of inputs decides.
- **History.** Since tier 1 (b409c30, 2026-07-21).
- **Ordering.** Before any task creation rule (PRI-4, Q-6, Q-8, Q-10).

### Q-1.4 Generation semantics
- **Statement.** `generation` is the visit index the task buys: a task created for a position with `visits = v` normally has `generation = v`; a follow-up queued while a visit is still running has `generation = v + 1`. A task is runnable only when `generation <= position.visits` (Q-2.22), i.e. after the previous visit's landing incremented `visits` (EXP-2). Generation is never decremented and a row never changes generation.
- **Inputs / outputs.** `AnalysisTask.generation`, `Position.visits` (read).
- **Code.** `atomicdb/models.py:415`; gate in `atomicdb/views.py:api_lease.choose_pending:1307-1309`.
- **Tests.** `tests.py::RequestTests::test_request_during_shallow_lease_preserves_deep_follow_up`; `tests.py::RequestTests::test_deep_follow_up_is_not_run_after_shallow_visit_solves_position`. **Gaps:** the case `generation < visits` (a stale lower generation still PENDING) is served without any check; no test pins it.
- **Determinism hazards.** none.
- **History.** Since 2026-07-22 (80b8b0c, "queued follow-up" gate).
- **Ordering.** The landing that increments `visits` (EXP-2) must commit before Q-2.22 lets the follow-up out.

### Q-1.5 Search payload fields: budget, MultiPV, route, then_verify
- **Statement.** `budget_nodes` (BigInteger, nodes, required) is the search size sent to the worker and the unit of the fair share; it can be *raised* while PENDING by a later request (Part 4) and is re-read under the row lock at lease time (Q-2.20). `multipv` (int, default 5) is sent verbatim to the worker. `route` (text, default '') is the comma-separated UCI path the requester used to reach the position (display and the worker's console line only, Q-2.29.2). `then_verify` (bool, default False) asks the ingest to walk the new PV like the Verify-PV button once the search lands (`ingest_queue._verify_after`, Part 5); it is **not** sent to the worker.
- **Inputs / outputs.** `AnalysisTask.budget_nodes`, `.multipv`, `.route`, `.then_verify`.
- **Code.** `atomicdb/models.py:407,410-414,489`; response `atomicdb/views.py:api_lease:1461-1469`.
- **Tests.** `test_max_budget.py::CappedLeaseTests::test_a_multi_line_task_is_judged_by_its_whole_budget`; `test_worker_console.py::LeaseLineTests::test_the_declared_route_wins_over_the_lineage`; `test_then_verify.py` (Part 5). **Gaps:** the default `multipv=5` on a row created without an explicit value is not pinned by a lease test.
- **Determinism hazards.** none.
- **History.** `route` migration 0028; `then_verify` 2026-11-05 (be5b897, PR #163).
- **Ordering.** Written by producers (PRI-4, Q-6, Q-7, Q-8, Q-10); read by Q-2.29.1.

### Q-1.6 Requester identity: author, backers, lane account
- **Statement.** `requested_by` (CharField 64, '' = anonymous) is the OpenBench account that made the request; it is the *author* (notification, profile queue, worker affinity, deep-lease cap). `also_requested_by` (JSON list, default []) holds the other accounts that asked for the same row, in arrival order; `backers` (int, default 0) is `len(also_requested_by)`, denormalised because the service order reads it in SQL and must always equal the list length. `lane_account` (CharField 64, default '') names the account whose queue position the row is charged to when it is not the author's; '' means "the author's". The *effective account* of a row is `lane_account` if non-empty else `requested_by` (Q-2.18). The anonymous tide (`requested_by=''`, `lane_account=''`) is one account for every fairness rule. How these are set (add_requester, withdraw, `lanes.effective_account`) is Part 4.
- **Inputs / outputs.** `AnalysisTask.requested_by`, `.also_requested_by`, `.backers`, `.lane_account`.
- **Code.** `atomicdb/models.py:428-459`; `atomicdb/live_request.py:_effective_account:134-143`, `_account_q:253-261`.
- **Tests.** `test_lanes.py::BackedRequestLaneTests::test_a_backed_request_rides_the_backers_empty_queue`; `test_worker_serving.py::BackedCountsAsOwnTests::test_the_owner_is_still_the_one_charged`; `test_queue_fairness.py::AnonymousBucketTests::test_the_anonymous_flood_is_shared_out_as_a_single_account`. **Gaps:** no test asserts the invariant `backers == len(also_requested_by)` after every writer.
- **Determinism hazards.** `also_requested_by` order is arrival order of concurrent requests (DB commit order).
- **History.** `requested_by` migration 0026; `also_requested_by`/`backers` migration 0040 (Eclipsia's "more precedence" request, 2026-08-14 abf61a0); `lane_account` migration 0045 (0e3975d, 2026-08-16).
- **Ordering.** Read by Q-2.15..Q-2.19, Q-2.24.

### Q-1.7 Account-internal order: queue_seq and background
- **Statement.** `queue_seq` (BigInteger, default 0) is the task's place *inside its effective account's queue*; 0 means untouched and then `id` decides (arrival order). Moving a request to the front writes a value below its siblings' and only on rows of the account that moves it (Part 4, `front_of_own_queue`/`api_queue_bump`). `background` (bool, default False) marks a script/API request with `priority=low`: inside its account's partition every non-background PENDING row sorts before every background one; a normal request on the same row clears the flag, a background request never sets it on a normal row (Part 4). Neither field changes the account's share against other accounts, only which of its own rows takes its turn (Q-2.18).
- **Inputs / outputs.** `AnalysisTask.queue_seq`, `.background`.
- **Code.** `atomicdb/models.py:471,483`; window in `atomicdb/live_request.py:fair_share:227-234`.
- **Tests.** `test_request_priority.py::DoorTests::test_an_existing_row_is_normal`; `test_request_priority.py::OrderTests::test_the_accounts_clicks_go_before_its_older_api_requests`; `test_request_priority.py::OrderTests::test_other_accounts_keep_their_turns`. **Gaps:** none for the lease side.
- **Determinism hazards.** none.
- **History.** `queue_seq` migration 0040 (Wolfram's "push to the front"); `background` 2026-11-25 (8668313, #316, migration 0076).
- **Ordering.** Read by Q-2.18, Q-2.21.

### Q-1.8 Lease identity fields
- **Statement.** While `LEASED`, a row carries: `machine` (CharField 64, the slot string; '' when not leased), `leased_at` (immutable assignment time; *kept* after a requeue, it is how `_absorb_requeued` recognises a row that once had a lease), `lease_heartbeat_at` (keepalive; set to the assignment time at lease and refreshed by heartbeats, Part 2), `lease_token` (opaque fencing token, `secrets.token_urlsafe(32)`, '' for leases issued to pre-token builds), `lease_session` (the worker process's session string, at most 64 chars, '' for pre-token builds), `attempts` (int, incremented once per lease handed out, never decremented; also kept across requeues). Requeue clears `machine`, `lease_heartbeat_at`, `lease_token`, `lease_session` (Q-2.7).
- **Inputs / outputs.** `AnalysisTask.machine`, `.leased_at`, `.lease_heartbeat_at`, `.lease_token`, `.lease_session`, `.attempts`.
- **Code.** `atomicdb/models.py:492,502-512`; writers `atomicdb/views.py:api_lease:1424-1436`, `_requeue_leases:961-975`.
- **Tests.** `tests.py::MachineVisibilityTests::test_heartbeat_tracks_current_task_and_keeps_original_lease_time`; `test_lease_release.py::ReleaseOwnLeasesTests::test_the_row_is_left_exactly_as_the_recycler_leaves_an_expired_one`. **Gaps:** none.
- **Determinism hazards.** `lease_token` is random (`secrets`); `leased_at`/`lease_heartbeat_at` are wall clock.
- **History.** Token/session fencing 2026-07-22 (80b8b0c, PR #13; migration 0012).
- **Ordering.** Read by Part 2 fencing (Q-2.30+).

### Q-1.9 Delivery fields
- **Statement.** Set when a worker's result is claimed and applied (Part 2/5), never by the lease: `nodes_searched` (BigInteger, default 0, real nodes, clamped at submit to 2x budget), `elapsed_seconds` (float, engine-reported), `delivered_by` (the authenticated account that delivered; '' for rows before 2026-08-16 and for rows never delivered; *not* backfilled), `engine_sha`, `net_sha` (provenance, '' when not sent), `completed` (timestamp; also set by absorption and by cancellation). A delivered row always has `machine` non-empty; an absorbed one never (Q-1.11).
- **Inputs / outputs.** `AnalysisTask.nodes_searched`, `.elapsed_seconds`, `.delivered_by`, `.engine_sha`, `.net_sha`, `.completed`.
- **Code.** `atomicdb/models.py:408-409,501,518-519,539`.
- **Tests.** `test_lanes.py::DeliveryEvidenceTests::test_polling_for_work_never_stamps_a_delivery`; `test_lanes.py::DeliveryEvidenceTests::test_submitting_a_result_stamps_one`; `tests.py::NodesAccountingTests::test_actual_nodes_recorded_not_budget`. **Gaps:** see Part 2.
- **Determinism hazards.** `completed` is wall clock.
- **History.** `delivered_by` fb2baa0 (2026-08-16, migration 0046); `nodes_searched` migration 0009.
- **Ordering.** After Q-2.29 (lease) and Part 2 submit.

### Q-1.10 Creation and request timestamps
- **Statement.** `created` (auto_now_add) is the row's birth; it is *also* the clock of the starvation rule (Q-2.17) and of the arm hourly budgets (Q-8). `requested_at` (nullable) is when the row last *became a person's request* (stamped on create, revive, promotion to USER, adoption by name; not on join, bump or undo). `request_time = requested_at or created` (`Coalesce('requested_at','created')` in SQL) and is display-only: the queue order never reads `requested_at`. `sweep._revive` rewrites `created` to now (Part 6), `_revive_withdrawn` does not.
- **Inputs / outputs.** `AnalysisTask.created`, `.requested_at`, property `request_time`.
- **Code.** `atomicdb/models.py:526-544`.
- **Tests.** `test_request_time.py::RequestTimeTests::test_rows_from_before_the_column_fall_back_to_created`; `test_request_time.py::RequestTimeTests::test_a_withdrawn_request_asked_again_is_a_fresh_wait`. **Gaps:** nothing pins that a revived withdrawn row keeps its old `created` and is therefore immediately "starved" if it is anonymous and older than 24 h (Q-2.17; open question).
- **Determinism hazards.** Wall clock (`auto_now_add`).
- **History.** `requested_at` 2026-11-08 (18dc092, PR #190, migration 0060).
- **Ordering.** Read by Q-2.17.

### Q-1.11 Absorption idiom
- **Statement.** *Absorbing* a task means: among the rows of a queryset, only those in `PENDING` become `COMPLETED` with `completed = now` and `nodes_searched = 0`; `machine` is left as is (it is already '' on every PENDING row). `LEASED` rows are never absorbed (they finish on their own with their nodes and machine). `is_absorbed(task)` is `state == COMPLETED and not nodes_searched and not machine`. Absorption is the single idiom used by: `_absorb_requeued` (Q-2.8), the submit engine gate (Part 2), `ingest_queue._absorb_shadowed` (Part 5), `_emit_closure_events` (CAS-7) and the `absorb_shadowed_tasks` command. An absorbed row is "work done, not by this row"; it still holds its `(position, generation)` slot (Q-1.3) and is revived through `_revive_withdrawn`/`sweep._revive`. It emits no DBEvent by itself.
- **Inputs / outputs.** Reads `state`; writes `state`, `completed`, `nodes_searched`.
- **Code.** `atomicdb/ingest.py:absorb_tasks:5770-5786`, `is_absorbed:5789-5797`.
- **Tests.** `test_lease_release.py::ReleaseOwnLeasesTests::test_a_task_on_a_closed_position_is_absorbed_as_the_recycler_does`; `test_ingest_queue.py::ShadowBackfillTests::test_a_leased_row_on_a_closed_position_keeps_running`; `test_fencing.py::DeadLeaseOnClosedPositionTests::test_a_dead_lease_on_a_closed_position_is_absorbed_not_requeued`. **Gaps:** a delivered row that legitimately reported 0 nodes and kept its machine is not absorbed by definition; no test of `is_absorbed` on a row with `nodes_searched=0` and a machine.
- **Determinism hazards.** `completed = timezone.now()`.
- **History.** Single idiom consolidated with the release work (6f38398, PR #87, 2026-10-27).
- **Ordering.** Callers decide which rows; this rule is the effect.

### Q-1.12 Cancelled rows hold their slot and stay out of the ladder
- **Statement.** A `CANCELLED` row has `completed` set to the cancel time, keeps every other field (budget, generation, route, queue_seq) so that *undo* (`restore_cancelled`) returns it exactly as it was (`CANCELLED -> PENDING`, `completed=None`, only if the position is still `UNKNOWN`), while a *new* request for the same generation revives it as a fresh request (`_revive_withdrawn`: author, backers, queue place, budget, multipv, route, background all reset to the new request's). Lease-side consequence: a CANCELLED row is never in `CHARGED` (Q-2.15), never leased, never recycled, never absorbed (`_absorb_requeued` only reads PENDING). Detailed withdraw rules are Part 4 (Q-6).
- **Inputs / outputs.** `AnalysisTask.state`, `.completed`.
- **Code.** `atomicdb/ingest.py:restore_cancelled:7496-7516`, `_revive_withdrawn:7614-7658`, writers at `ingest.py:7441,7491-7492`.
- **Tests.** `test_queue_control.py::WithdrawCostsNothingTests::test_undo_puts_it_back_exactly_where_it_was`; `test_queue_control.py::WithdrawCostsNothingTests::test_undo_is_refused_once_the_position_has_a_verdict`; `test_request_priority.py::UpgradeTests::test_a_withdrawn_api_row_comes_back_at_the_level_asked_now`. **Gaps:** none on the lease side.
- **Determinism hazards.** none beyond wall-clock `completed`.
- **History.** 95ab6d4 (2026-08-16); revive resets budget since the 11-Oct Eclipsia report ("impossible to send a 128M here").
- **Ordering.** See Part 4.

### Q-1.13 AnalysisTask indexes and the predicates that are contracts
- **Statement.** The indexes are performance only, except that three partial-index predicates are copied verbatim into raw SQL and define *which rows the head-of-queue reader sees* (Q-2.21): author queue `source='USER' AND state='PENDING' AND lane_account=''` ordered `(requested_by, background, queue_seq, id)`; lane queue `source='USER' AND state='PENDING' AND lane_account<>''` ordered `(lane_account, background, queue_seq, id)`; irregular rows `source='USER' AND state IN ('PENDING','LEASED') AND (backers>0 OR lane_account<>'' OR requested_by='')`. Others: `(state, completed)`, `(requested_by, state, id)`, served-stamp partials `(requested_by, leased_at) WHERE source='USER'` and `(lane_account, leased_at) WHERE source='USER' AND lane_account<>''`, `(arm, created)` (raw SQL, migration 0048, Q-1.14). A port needs the predicates (as row-set definitions), not the indexes.
- **Inputs / outputs.** none written.
- **Code.** `atomicdb/models.py:AnalysisTask.Meta:549-590`; `atomicdb/live_request.py:364-370`; migrations `0035`, `0048`, `0077`, `0078` (all `RunPython` with `CREATE INDEX ... IF NOT EXISTS` so production could build them `CONCURRENTLY` first).
- **Tests.** `test_lease_heads.py::QueuedAccountsTests::test_heads_are_the_first_rows_in_window_order`; `test_lease_heads.py::HeadCostTests::test_the_window_reads_only_the_heads`. **Gaps:** none.
- **Determinism hazards.** Partial-index use depends on the planner seeing literal `'USER'`; irrelevant to results.
- **History.** 0077/0078 with #317/#318 (2026-11-25); 0075 dropped `atomic_pos_queue (status, priority DESC)` and `atomic_ingest_ready`.
- **Ordering.** n/a.

### Q-1.14 Producer arm and the arm/created index
- **Statement.** `arm` (CharField 16, '' = the normal selector or a click, indexed) names which producer created the task (e.g. `campaign`, `descend`, `cascade`, `coverage`, `dn`, `quality`, `repair`, `sweep16`; constants in `ingest.py`, Part 3/6). It never affects lease order directly; the lease reads it only through `idle_repair.fits_slot` (`arm='repair'`, Q-2.25) and the sweep share (`arm='sweep16'`, Q-2.28.1). The composite index `atomic_task_arm_created (arm, created)` serves the per-arm hourly creation count (Q-8).
- **Inputs / outputs.** `AnalysisTask.arm`.
- **Code.** `atomicdb/models.py:423-424`; `atomicdb/migrations/0048_analysistask_arm_rate_index.py:21-25`.
- **Tests.** see Part 6 (`test_arm_rate_budget.py`). **Gaps:** none here.
- **Determinism hazards.** none.
- **History.** e5ab327 (2026-07-29) "cada brazo cuenta SU cupo"; migration 0030.
- **Ordering.** n/a.

### Q-1.15 IngestJob: at most one per task; open-row index
- **Statement.** `IngestJob.task` is a OneToOne to `AnalysisTask` (unique): a task has at most one ingest job, which is what makes a duplicate submit impossible to apply twice. States `PENDING`, `PROCESSING`, `DONE`, `FAILED` (default PENDING); `attempts`, `next_attempt_at`, `claimed_at`, `last_error`, `summary`, `payload` (raw JSON as received), `created`, `updated`. Partial index `atomic_ingest_open_by_id (id) WHERE state IN ('PENDING','PROCESSING')` is what `claim_next` walks (by id). Lifecycle rules are Part 5 (Q-3).
- **Inputs / outputs.** `IngestJob.*`.
- **Code.** `atomicdb/models.py:IngestJob:665-713`; `atomicdb/migrations/0016_ingest_queue.py`, `0073_ingestjob_open_by_id.py`.
- **Tests.** `test_ingest_queue.py::SubmitEnqueueTests::test_the_claim_and_the_enqueue_are_one_commit`; `test_ingest_queue.py::SubmitEnqueueTests::test_a_replayed_submit_is_still_a_duplicate`. **Gaps:** see Part 5.
- **Determinism hazards.** see Part 5.
- **History.** 0016 (durable ingest queue); 0073 partial index (ingest claim index, 23-Nov).
- **Ordering.** Created in the submit transaction (Part 2).

### Q-1.16 PropagationSeed fields
- **Statement.** `PropagationSeed` rows are the climb a landing still owes its ancestors: `position`, `source` (closure attribution string, max 16), `payload` (JSON with `cascade`, `backed`, `parents`, `others`, `closed`, `job`, optional `verify`), `attempts`, `next_attempt_at`, `last_error`, `created`. No unique constraint: several seeds per position may coexist and are merged by the drainer. Index `(next_attempt_at, id)`. Lifecycle is Part 5 (Q-4).
- **Inputs / outputs.** `PropagationSeed.*`.
- **Code.** `atomicdb/models.py:PropagationSeed:716-749`; migration `0058_propagationseed.py`.
- **Tests.** see Part 5. **Gaps:** —
- **Determinism hazards.** see Part 5.
- **History.** 0058 (deferred propagation).
- **Ordering.** —

### Q-1.17 SolveTask fields and states
- **Statement.** `SolveTask` is a separate table for proof requests, leased by `/api/solve/acquire` with the *same* lease pattern (machine, `leased_at`, `lease_heartbeat_at`, `lease_token`, `lease_session`, `attempts`). States `PENDING`, `LEASED`, `COMPLETED`, `FAILED`; `outcome` in `PROVED`, `DISPROVED`, `UNKNOWN`, `DISPROVED_WHITE_WIN` (the last never closes a Position); `budget_stage` F0..F4 (only F0 and F4 used), `budget_nodes` default 10,000,000, `goal` default WHITE_WIN, `campaign` FK nullable (SET_NULL), `arm` (max 32, indexed), certificate blob and magnitudes, advisory pn/dn (hints, never facts). **No uniqueness** on (position, goal, stage): de-duplication is done by producers (Part 6). Index `(state, created)`. Endpoint rules: Part 2 (SOL-5).
- **Inputs / outputs.** `SolveTask.*`.
- **Code.** `atomicdb/models.py:SolveTask:752-877`.
- **Tests.** `test_solve.py::SolveProtocolTests::test_the_same_session_replays_its_assignment`; `test_lease_release.py::SharedRecyclerTests::test_the_solve_recycler_and_a_release_leave_the_same_row`. **Gaps:** —
- **Determinism hazards.** see Part 2.
- **History.** migration 0020 (2026-07-27, 38f086a).
- **Ordering.** —

### Q-1.18 ContributorPref: per-account serving preferences
- **Statement.** One row per account (`account` unique, max 64), created on first use; an account without a row has every default and reading never writes. Fields read by the lease and the request path: `lifo_queue` (default False; new requests go to the front of the account's own queue, Part 4), `serve_order` in `{'own','queue','others'}` (default `'own'`; any other stored value reads as `'own'`), `serve_others_first` (legacy boolean kept equal to `serve_order=='others'`; migration 0055 set `serve_order='others'` on every row that had it True), `serve_for` (comma/space separated account names, parsed by `parse_serve_for`: split on commas and whitespace, each name cut to 64 chars, de-duplicated case-insensitively keeping the first spelling, at most `SERVE_FOR_MAX = 20` names), `serve_campaign` (nullable int; NULL or <= 0 means none; a plain id, not an FK, so a deleted campaign silently matches nothing). `serve_for(username)` returns the list only when `serve_order` is `'own'` or `'others'` (`[]` for `'queue'`); `serve_for_names(username)` returns it whatever the order (used by personal workers). `notify_ready` is notifications only.
- **Inputs / outputs.** `ContributorPref.*` (read by Q-2.14).
- **Code.** `atomicdb/models.py:ContributorPref:286-325`; `atomicdb/preferences.py:serve_order:31-41`, `parse_serve_for:48-57`, `serve_for:60-72`, `serve_for_names:75-86`; `atomicdb/migrations/0055_contributorpref_serve_order.py:8-11`.
- **Tests.** `test_serve_others_first.py::ServeOthersFirstLeaseTests::test_queue_order_serves_the_older_request_whoever_asked`; `test_worker_serving.py::WorkerServingLeaseTests::test_personal_mode_serves_the_listed_accounts`. **Gaps:** no test of `parse_serve_for` truncation at 20 names or 64 chars.
- **Determinism hazards.** `.first()` on a unique column: none.
- **History.** 0049 (`lifo_queue`, 2026-08-28), 0054/0055 (`serve_others_first` then `serve_order`, PR #154, 2026-11-03), 0057 (`serve_for`, 2026-11-05), `serve_campaign` with PR #189 (2026-11-08).
- **Ordering.** Read at the start of each lease (Q-2.14).

### Q-1.19 WorkerServing: per-machine override
- **Statement.** Optional row per `(account, machine)` with unique constraint `uniq_worker_serving`; `machine` is the machine base (slot suffix cut, then truncated to 64), so every `--jobs` slot of one machine shares it, and two accounts may use the same machine name independently. `mode` in `{'', 'server', 'personal'}` (any other value reads as `''`): `''` follows the account, `server` serves the queue with no preference for the account's own or listed requests, `personal` takes only the account's own (including backed) and listed accounts' requests plus its campaign. `campaign`: NULL inherits `ContributorPref.serve_campaign`, 0 means none, >0 a campaign id. `preferences.worker_serving(username, machine)` returns `(mode, campaign_id_or_None)`, `('', None)` for an empty username.
- **Inputs / outputs.** `WorkerServing.account`, `.machine`, `.mode`, `.campaign`; `ContributorPref.serve_campaign`.
- **Code.** `atomicdb/models.py:WorkerServing:328-357`; `atomicdb/preferences.py:_machine_base:95-98`, `worker_serving:101-119`; migration `0059_worker_serving.py:33`.
- **Tests.** `test_worker_serving.py::WorkerServingLeaseTests::test_no_row_reads_the_account_default`; `test_worker_serving.py::WorkerServingLeaseTests::test_every_slot_of_the_machine_shares_the_row`; `test_worker_serving.py::WorkerServingLeaseTests::test_another_account_with_the_same_machine_name_is_unaffected`; `test_worker_serving.py::WorkerServingLeaseTests::test_no_campaign_on_the_worker_overrides_the_account`; `test_worker_serving.py::WorkerServingLeaseTests::test_the_account_campaign_reaches_a_worker_without_a_row`. **Gaps:** none.
- **Determinism hazards.** none.
- **History.** a55acdc (PR #189, 2026-11-08).
- **Ordering.** Q-2.14.

### Q-1.20 WorkerPing: one row per (machine, user)
- **Statement.** Unique `(machine, user)` (`uniq_worker_machine`); `machine` is the full slot string. Lease-relevant fields: `last_seen` (auto_now, indexed; the "live pool" for the deep cap counts rows with `last_seen >= now - metrics.LIVE_SECONDS` where `LIVE_SECONDS = 180`), `threads`, `hash_mb`, `os`, `worker_build` (nullable; only overwritten by a request that carries a usable build), `current_task_id`, `last_nps`, `nps_updated` (written after the lease, Q-2.29.2), `tasks_done`, `last_result_at` (written by submit, Part 2).
- **Inputs / outputs.** `WorkerPing.*`.
- **Code.** `atomicdb/models.py:WorkerPing:1330-1360`; migration `0006_workerping_workerping_uniq_worker_machine.py:28`.
- **Tests.** `tests.py::LeaseReclaimTests::test_lease_stores_the_worker_build`; `tests.py::LeaseReclaimTests::test_request_without_build_keeps_the_stored_build`; `tests.py::MachineVisibilityTests::test_capacity_touch_does_not_overwrite_concurrent_telemetry`. **Gaps:** —
- **Determinism hazards.** `last_seen` wall clock feeds the deep cap (Q-2.24).
- **History.** 0006; `worker_build` column with the worker-update work (Nov 2026).
- **Ordering.** Written by Q-2.3 before the lease transaction.

### Q-1.21 RequestNotification de-duplication
- **Statement.** At most one ready-notification per `(task, username)` when `task` is not NULL (conditional unique constraint `uniq_notification_per_task`). It was `(task)` alone until migration 0040, which prevented a backer from ever getting its own notice. Notification rules are outside this part (Part 5 `_notify_requester`); the constraint is listed because it changes what can be stored.
- **Inputs / outputs.** `RequestNotification.task`, `.username`.
- **Code.** `atomicdb/migrations/0040_queue_bump_and_dedupe.py:42-65`; earlier 0029, 0032.
- **Tests.** see `test_notifications.py` (Part 5). **Gaps:** —
- **Determinism hazards.** Concurrent inserts resolved by the DB.
- **History.** 0040 (2026-08-14).
- **Ordering.** —

---

### Q-2.1 Worker authentication
- **Statement.** Every worker endpoint authenticates `username`/`password` from the POST form. A request succeeds only if Django `authenticate` accepts the pair *and* the user has an OpenBench `Profile` with `enabled=True`; otherwise the lease answers HTTP 403 `{"error": "bad credentials"}` before anything is read or written. A success is cached for `AUTH_CACHE_SECONDS = 300` under `atomicdb:auth:` + HMAC-SHA256(SECRET_KEY, username + '\0' + password); every cache hit is re-validated in one query (profile enabled, user active, same username, and SHA-256 of the stored password hash equal to the cached fingerprint), so disabling an account or changing its password takes effect on the next request. Failures are never cached; empty username or password never use the cache. `api_lease` is `csrf_exempt` and does not check the HTTP method (a GET has empty POST and fails auth).
- **Inputs / outputs.** POST `username`, `password`; `auth.User`, `OpenBench.Profile.enabled`; Django cache.
- **Code.** `atomicdb/views.py:_auth:407-418`, `_auth_pair:421-438`, `_auth_cache_key:455-461`, `_auth_cache_hit:470-498`, `_auth_cache_store:501-508`; `api_lease:1005-1007`.
- **Tests.** `test_fencing.py::ExpelledAccountTests::test_a_disabled_account_cannot_lease`; `test_fencing.py::ExpelledAccountTests::test_re_enabling_the_account_gives_the_protocol_back`; `test_auth_cache.py::AuthCacheTests::test_a_password_change_invalidates_at_once`; `test_auth_cache.py::AuthCacheTests::test_a_disabled_profile_is_refused_on_a_hit`; `test_auth_cache.py::AuthCacheTests::test_a_wrong_password_never_hits_and_is_never_cached`; `test_worker_jobs.py::LeaseCsrfTests::test_a_tokenless_worker_post_can_lease`. **Gaps:** none.
- **Determinism hazards.** Shared cache (Django cache backend); TTL is wall clock. Result is the same with or without the cache by construction.
- **History.** Enabled-profile check 2026-07-29 (e71d3e5, bug-hunt backlog); cache 2026-11-18 (c2c6f8c, PR #279).
- **Ordering.** First step of the lease.

### Q-2.2 max_budget parsing
- **Statement.** Optional POST `max_budget` (`--max-budget N`, worker builds >= 2026111704). Absent or `''` means no cap (None). Present and not an integer, or <= 0, answers HTTP 400 `{"error": "max_budget must be a positive whole number of nodes"}` and changes nothing (checked before `_touch_worker`). The cap is inclusive: a task is eligible iff `budget_nodes <= max_budget`. It is applied to the id list before any lock (so larger tasks are never locked by this worker and keep their place for others) and re-checked under the lock (Q-2.20).
- **Inputs / outputs.** POST `max_budget`.
- **Code.** `atomicdb/views.py:BadMaxBudget:547`, `reported_max_budget:551-570`, `api_lease:1008-1014`; use in `_lease_order:752-759`, `_lease_order_pages:794`, `_lease_head_pages:887-888`, `_locked_in_order:956-958`.
- **Tests.** `test_max_budget.py::BadValueTests::test_an_unusable_cap_is_refused_and_nothing_is_leased`; `test_max_budget.py::BadValueTests::test_an_empty_field_reads_as_no_cap`; `test_max_budget.py::CappedLeaseTests::test_the_cap_is_inclusive`; `test_max_budget.py::CappedLeaseTests::test_the_larger_task_keeps_its_place_for_the_others`; `test_max_budget.py::CappedLeaseTests::test_the_id_list_already_leaves_the_larger_tasks_out`; `test_max_budget.py::UncappedLeaseTests::test_without_the_field_the_first_task_goes_as_before`. **Gaps:** none.
- **Determinism hazards.** none.
- **History.** 8736cd0 (PR #278, 2026-11-17).
- **Ordering.** Before Q-2.3; feeds Q-2.20, Q-2.21, Q-2.28.

### Q-2.3 Worker touch (capacity telemetry)
- **Statement.** Before the lease transaction, in its own autocommit, upsert `WorkerPing(machine, user)` with `machine = POST machine (default: the username)[:64]`, `threads = clamp(int(POST threads or 0), 0, 1024)` (0 on parse error), `hash_mb = max(0, int(POST hash or 0))` (0 on error), `os = POST os[:64]`, `last_seen = now`, and `worker_build` only if POST `worker_build` parses to an integer in `1..2,147,483,647`. Existing rows are updated with a field-level UPDATE (never a full save). The ping's `machine` is the slot identity used by the rest of the lease. Never touches any lease.
- **Inputs / outputs.** POST `machine`, `threads`, `hash`, `os`, `worker_build`; writes `WorkerPing.threads/hash_mb/os/last_seen/worker_build`.
- **Code.** `atomicdb/views.py:reported_threads:519-528` (`MAX_REPORTED_THREADS = 1024` at 516), `reported_worker_build:534-544`, `_touch_worker:573-600`; `api_lease:1015-1017`.
- **Tests.** `tests.py::MachineVisibilityTests::test_reported_threads_are_capped_at_what_the_engine_can_use`; `tests.py::MachineVisibilityTests::test_capacity_touch_does_not_overwrite_concurrent_telemetry`; `tests.py::LeaseReclaimTests::test_lease_stores_the_worker_build`; `tests.py::LeaseReclaimTests::test_worker_that_never_sent_a_build_has_none`. **Gaps:** none.
- **Determinism hazards.** `last_seen` wall clock feeds Q-2.24; get_or_create under concurrency relies on the unique constraint.
- **History.** 80b8b0c (2026-07-22).
- **Ordering.** After Q-2.2, before Q-2.5. The reported `threads` of *this request* (not the stored one) drives Q-2.25.

### Q-2.4 Token capability and session
- **Statement.** `worker_build = int(POST worker_build or 0)` (0 on parse error; no range check here, unlike Q-2.3). The worker is *token-capable* iff `worker_build >= LEASE_TOKEN_BUILD = 2026072203`. `lease_session = POST lease_session[:64]` ('' when absent). A token-capable worker receives a fresh token and its session is stored; a non-capable one gets `lease_token=''`, `lease_session=''` (Q-2.29). Capability also gates Q-2.10 (replay) and Q-2.23 (legacy filter).
- **Inputs / outputs.** POST `worker_build`, `lease_session`.
- **Code.** `atomicdb/views.py:LEASE_TOKEN_BUILD:86`; `api_lease:1019-1024`.
- **Tests.** `tests.py::LeaseReclaimTests::test_recycled_task_waits_for_token_capable_worker`; `tests.py::LeaseReclaimTests::test_deep_first_attempt_waits_for_token_capable_worker`. **Gaps:** a token-capable build that sends no `lease_session` gets a token with an empty session and can never replay; untested.
- **Determinism hazards.** none.
- **History.** 80b8b0c (PR #13, 2026-07-22). Owner decision D8 (drop pre-token builds at cut-over) applies.
- **Ordering.** Before Q-2.9.

### Q-2.5 Recycle of silent token leases
- **Statement.** At the start of the lease transaction, with `now` taken once and `stale = now - LEASE_MINUTES` (`LEASE_MINUTES = 4` minutes), every row with `lease_token > ''` and `leased_at < stale` and (`lease_heartbeat_at IS NULL` or `lease_heartbeat_at < stale`) is requeued (Q-2.7). Strict `<`: a lease silent exactly 4 minutes stays. The filter does not test `state`; `_requeue_leases` restricts to `LEASED`. This runs on *every* lease request, from any worker, and is the only time-based recycler of analysis leases. `LEASE_MINUTES` must stay above `RESTART_RECYCLE_MINUTES` (3) and above `live_request.PROMISE_FRESH_SECONDS` (180 s), pinned by tests.
- **Inputs / outputs.** Reads `lease_token`, `leased_at`, `lease_heartbeat_at`, `state`; writes per Q-2.7.
- **Code.** `atomicdb/views.py:LEASE_MINUTES:72`; `api_lease:1027-1038`.
- **Tests.** `test_fencing.py::LeaseWindowTests::test_the_window_is_four_minutes`; `test_fencing.py::LeaseWindowTests::test_five_silent_minutes_give_the_task_to_somebody_else`; `test_fencing.py::LeaseWindowTests::test_three_and_a_half_silent_minutes_do_not`; `test_fencing.py::LeaseWindowTests::test_the_edge_is_the_constant_and_nothing_else`; `test_fencing.py::LeaseWindowTests::test_a_restarted_worker_is_relieved_before_a_stranger_is`; `tests.py::LeaseReclaimTests::test_recent_heartbeat_prevents_expired_assignment_reclaim`; `tests.py::LeaseReclaimTests::test_stale_assignment_and_stale_heartbeat_are_reclaimed`; `test_lease_release.py::SharedRecyclerTests::test_the_analysis_recycler_leaves_live_leases_alone`. **Gaps:** the exact-boundary (`== stale`) case is not tested.
- **Determinism hazards.** Wall clock; recycling happens only when *some* worker calls lease, so the moment a dead lease returns depends on fleet traffic. Under READ COMMITTED two concurrent leases may both run the UPDATE; the second updates zero rows (idempotent).
- **History.** Window 10 -> 4 minutes on 2026-10-13 (83eba82).
- **Ordering.** Before Q-2.8, Q-2.9, Q-2.13, Q-2.24 (counts are taken after recycling, same transaction).

### Q-2.6 Recycle of legacy tokenless leases
- **Statement.** Two further requeues for leases with `lease_token = ''` (issued to pre-2026072203 builds): (a) `lease_heartbeat_at IS NOT NULL` and `lease_heartbeat_at < now - POST_DEPLOY_LEGACY_LEASE_MINUTES` (60 minutes); (b) `lease_heartbeat_at IS NULL` and `leased_at < now - LEGACY_LEASE_MINUTES` (24 x 60 = 1440 minutes). Rationale: a tokenless lease always gets a heartbeat stamp at assignment since 2026-07-22, so (a) is the normal path (one-hour window) and (b) only drains rows assigned before that deploy.
- **Inputs / outputs.** Same as Q-2.5.
- **Code.** `atomicdb/views.py:LEGACY_LEASE_MINUTES:73`, `POST_DEPLOY_LEGACY_LEASE_MINUTES:74`; `api_lease:1031-1047`.
- **Tests.** `tests.py::LeaseReclaimTests::test_predeploy_tokenless_deep_lease_gets_drain_window`; `tests.py::LeaseReclaimTests::test_postdeploy_tokenless_lease_recycles_after_one_hour`. **Gaps:** none.
- **Determinism hazards.** Wall clock, traffic-driven as Q-2.5.
- **History.** 80b8b0c (2026-07-22). Removable if D8 = drop.
- **Ordering.** Same position as Q-2.5.

### Q-2.7 Requeue row shape
- **Statement.** Requeuing a set of leases updates only rows still `LEASED` to: `state='PENDING'`, `machine=''`, `lease_heartbeat_at=NULL`, `lease_token=''`, `lease_session=''`. `leased_at` and `attempts` are kept (`leased_at` marks "has had a lease" for Q-2.8; `attempts > 0` keeps the row away from tokenless builds, Q-2.23). Returns the row count. The same shape is produced by the restart relief (Q-2.11, written inline), by `api_release` and by the submit engine gate (Part 2). No DBEvent.
- **Inputs / outputs.** Writes `state`, `machine`, `lease_heartbeat_at`, `lease_token`, `lease_session`.
- **Code.** `atomicdb/views.py:_requeue_leases:961-975`; inline copy `api_lease:1373-1377`.
- **Tests.** `test_lease_release.py::ReleaseOwnLeasesTests::test_the_row_is_left_exactly_as_the_recycler_leaves_an_expired_one`; `test_lease_release.py::SharedRecyclerTests::test_the_analysis_recycler_leaves_live_leases_alone`. **Gaps:** none.
- **Determinism hazards.** none.
- **History.** Factored out with worker release (6f38398, PR #87, 2026-10-27).
- **Ordering.** Effect used by Q-2.5, Q-2.6, Part 2.

### Q-2.8 Absorb requeued rows whose position closed
- **Statement.** Right after the recycles, absorb (Q-1.11) every row with `state='PENDING'`, `machine=''`, `lease_heartbeat_at IS NULL`, `leased_at IS NOT NULL` whose position's `status <> 'UNKNOWN'`. Reason: a closure absorbs PENDING rows but skips LEASED ones, so a lease that returns after its position closed would otherwise be a PENDING row nobody ever serves (the lease skips closed positions). Never makes rows `CANCELLED`. Positions that are tombstoned by priority (`DEAD`) but still `UNKNOWN` are *not* absorbed and remain serveable.
- **Inputs / outputs.** Reads `AnalysisTask.state/machine/lease_heartbeat_at/leased_at`, `Position.status`; writes `state='COMPLETED'`, `completed=now`, `nodes_searched=0`.
- **Code.** `atomicdb/views.py:_absorb_requeued:978-992`; call `api_lease:1054`.
- **Tests.** `test_fencing.py::DeadLeaseOnClosedPositionTests::test_a_dead_lease_on_a_closed_position_is_absorbed_not_requeued`; `test_lease_release.py::ReleaseOwnLeasesTests::test_a_task_on_a_closed_position_is_absorbed_as_the_recycler_does`; `test_ingest_queue.py::ShadowBackfillTests::test_a_tombstoned_position_is_not_a_zombie_and_is_still_served`. **Gaps:** a row requeued by the restart relief (Q-2.11) *after* this step on a closed position is only absorbed by the next lease call; untested.
- **Determinism hazards.** Scans all PENDING rows that ever had a lease (not only the ones just recycled) on every lease; result is order-independent.
- **History.** Zombie requests "hanging forever" on profiles (2026-09-07); current form with PR #87 (2026-10-27).
- **Ordering.** After Q-2.5/Q-2.6, before Q-2.9.

### Q-2.9 Active lease of the same slot
- **Statement.** Lock (row lock on the task only, `FOR UPDATE OF self`, waiting, not skip) and read the oldest (`ORDER BY leased_at, id`, first row) `LEASED` task whose `machine` equals the requesting slot string *exactly* and which is still alive by one of: tokenless, no heartbeat, `leased_at >= now - 1440 min`; tokenless, `lease_heartbeat_at >= now - 60 min`; tokened, `lease_heartbeat_at >= now - 4 min`; tokened, no heartbeat, `leased_at >= now - 4 min`. Its existence decides between replay (Q-2.10), restart relief (Q-2.11) and "busy" (Q-2.12). Slots `base#0` and `base#1` are different identities here (the per-machine cap is Q-2.13).
- **Inputs / outputs.** Reads `AnalysisTask.state/machine/lease_token/lease_heartbeat_at/leased_at/lease_session`.
- **Code.** `atomicdb/views.py:api_lease:1061-1072`.
- **Tests.** `tests.py::LeaseReclaimTests::test_same_machine_does_not_steal_healthy_lease`; `tests.py::LeaseReclaimTests::test_same_machine_recycles_only_stale_lease`; `test_worker_jobs.py::LeaseConcurrencyTests::test_two_slots_of_one_machine_hold_leases_at_the_same_time`; `test_lease_position_lock.py::LeaseWithThePositionLockedTests::test_a_replayed_lease_does_not_wait_for_its_position`. **Gaps:** none.
- **Determinism hazards.** Wall clock windows; lock waits. If two lease requests for the *same* slot with no held lease run concurrently, nothing is locked and both can lease a task: one slot then holds two LEASED rows (v1 does not prevent it).
- **History.** 80b8b0c (2026-07-22); `of=('self',)` 26a7229 (PR #95, 2026-10-28).
- **Ordering.** After Q-2.8.

### Q-2.10 Replay of a lost lease response
- **Statement.** If the slot has an active lease (Q-2.9) and the worker is token-capable and sends a non-empty `lease_session` equal (constant-time compare) to the row's `lease_session`, the response returns that same task with its existing token, and nothing is written: `attempts`, `leased_at`, `lease_heartbeat_at` and token unchanged. The response is rebuilt, so `searchmoves` (Q-2.29.1) and `line` are recomputed from current state and may differ from the first response.
- **Inputs / outputs.** Reads the held row; writes nothing on the task (the WorkerPing update of Q-2.29.2 still happens with `current_task_id` = the task).
- **Code.** `atomicdb/views.py:api_lease:1352-1359`.
- **Tests.** `tests.py::LeaseReclaimTests::test_same_session_replays_lost_lease_response_idempotently`; `test_worker_jobs.py::LeaseConcurrencyTests::test_a_lost_response_still_replays_within_its_slot`; `test_worker_update.py::WorkerAutoUpdateTests::test_valid_lease_response_uses_the_logical_request_nonce`. **Gaps:** none.
- **Determinism hazards.** none beyond Q-2.9.
- **History.** 80b8b0c (2026-07-22).
- **Ordering.** Alternative to Q-2.11/Q-2.12.

### Q-2.11 Restart relief
- **Statement.** If the slot has an active lease but the request is not a replay (different or missing session, or a non-capable build), compute `beat = lease_heartbeat_at or leased_at`. If `beat < now - RESTART_RECYCLE_MINUTES` (3 minutes), the row (if still `LEASED`) is requeued inline with the Q-2.7 shape and the *same request* proceeds to choose a task as if no lease were held (it can get the same task back). Otherwise the slot is busy (Q-2.12). Because Q-2.9 only returns tokened leases with a beat within 4 minutes, for tokened leases relief applies in the window 3 to 4 minutes; for tokenless leases (window 60 minutes) any beat older than 3 minutes is relieved.
- **Inputs / outputs.** Reads `lease_heartbeat_at`, `leased_at`; writes the Q-2.7 fields.
- **Code.** `atomicdb/views.py:RESTART_RECYCLE_MINUTES:79`; `api_lease:1360-1382`.
- **Tests.** `test_worker_jobs.py::LeaseConcurrencyTests::test_a_restarted_worker_reclaims_its_dead_predecessors_lease`; `test_fencing.py::LeaseWindowTests::test_a_restarted_worker_is_relieved_before_a_stranger_is`; `test_bug_hunt_findings.py::SolveLeaseRestartTests::test_a_restarted_solver_is_relieved_like_an_analysis_worker` (solve analogue). **Gaps:** relief of a tokenless lease (beat 3..60 min) is not tested; relief does not run Q-2.8, untested.
- **Determinism hazards.** Wall clock.
- **History.** cf6ce53 (2026-07-29) after the t24 incident (about 15 minutes idle with 755 tasks queued).
- **Ordering.** After Q-2.9, before Q-2.13.

### Q-2.12 One task per slot
- **Statement.** If the slot holds a live lease that is neither replayed nor relieved, the response is `{"tasks": []}` and nothing is leased; `WorkerPing.current_task_id` is set to the held task's id (Q-2.29.2). A lease never returns more than one task (`TASK_REFILL_COUNT` is the selector refill size, not a batch size).
- **Inputs / outputs.** Response; `WorkerPing.current_task_id`.
- **Code.** `atomicdb/views.py:api_lease:1381-1384,1447-1451`; comment at `:81-82`.
- **Tests.** `test_worker_jobs.py::LeaseConcurrencyTests::test_one_slot_still_gets_only_one_task_at_a_time`; `tests.py::LeaseReclaimTests::test_same_machine_does_not_steal_healthy_lease`. **Gaps:** none.
- **Determinism hazards.** none.
- **History.** 80b8b0c.
- **Ordering.** Terminal branch.

### Q-2.13 Per-machine lease cap
- **Statement.** If no lease is held/replayed, count `LEASED` rows whose `machine` equals the base or starts with `base + '#'` (`base = machine.split('#',1)[0]`; `bob-pc` does not match `bob-pc2`). If the count is `>= LEASES_PER_MACHINE = 32`, answer `{"tasks": []}` (as an empty queue, not an error). An empty base never caps. Counted after recycling, inside the lease transaction, without a lock (concurrent slot leases can overshoot by a few).
- **Inputs / outputs.** Reads `AnalysisTask.state/machine`.
- **Code.** `atomicdb/views.py:LEASES_PER_MACHINE:98`, `machine_base:689-695`, `_machine_at_lease_cap:698-713`; `api_lease:1385-1391`.
- **Tests.** `test_worker_jobs.py::LeaseConcurrencyTests::test_the_shipped_cap_is_the_agreed_policy`; `test_worker_jobs.py::LeaseConcurrencyTests::test_the_cap_is_per_physical_machine`; `test_worker_jobs.py::LeaseConcurrencyTests::test_the_cap_does_not_penalise_a_different_machine`; `test_worker_jobs.py::LeaseConcurrencyTests::test_a_capped_machine_is_told_nothing_rather_than_failing`; `test_worker_jobs.py::LeaseConcurrencyTests::test_a_recycled_lease_frees_a_slot_of_the_cap`; `test_worker_jobs.py::LeaseConcurrencyTests::test_an_unsuffixed_machine_is_capped_with_its_slots`; `test_worker_jobs.py::MachineIdentityTests::test_a_lookalike_machine_is_not_the_same_machine`; `test_worker_search.py::DefaultJobsTests::test_the_cap_matches_the_server`. **Gaps:** the cap counts machines across *all accounts* (machine names are client-chosen; two accounts with the same base share one cap); untested.
- **Determinism hazards.** Unlocked count under READ COMMITTED.
- **History.** ea34599 (2026-07-28, `--jobs K`).
- **Ordering.** After Q-2.11, before Q-2.28.

### Q-2.14 Serving preferences of this worker
- **Statement.** Read once per lease: `serve_order = preferences.serve_order(user)` (Q-1.18), `serve_first = preferences.serve_for(user)`, `(worker_mode, worker_campaign) = preferences.worker_serving(user, slot)` (Q-1.19). If `worker_mode == 'server'`: `serve_order = 'queue'`, `serve_first = []`. If `'personal'`: `personal_for = serve_for_names(user)`. Derived predicates: `own_q` = USER rows with `requested_by == user` (exact) or backed by `user` (`also_requested_by` contains it); `requester_q(names)` = rows whose `requested_by` iequals one of `names` (the literal name `anonymous`, any case, means `requested_by=''`) or backed by any of those accounts (names resolved to exact usernames case-insensitively via `_account_names`; `anonymous` and '' never match a backer); `favoured = requester_q(serve_first)` if non-empty; `personal_q = requester_q(personal_for)` in personal mode, else empty; `backed_names = [user] + _account_names(serve_first + personal_for minus 'anonymous')` (used by Q-2.21).
- **Inputs / outputs.** `ContributorPref`, `WorkerServing`, `auth.User` (name resolution); `AnalysisTask.requested_by/also_requested_by`.
- **Code.** `atomicdb/views.py:api_lease:1076-1133`, `_account_names:2267-2284`, `_backed_q:2287-2311`; `atomicdb/preferences.py:31-119`.
- **Tests.** `test_worker_serving.py::WorkerServingLeaseTests::test_server_mode_serves_the_older_request_over_its_own`; `test_worker_serving.py::WorkerServingLeaseTests::test_server_mode_ignores_the_listed_accounts`; `test_worker_serving.py::BackedCountsAsOwnTests::test_mine_first_takes_the_backed_request_before_the_queue`; `test_worker_serving.py::BackedCountsAsOwnTests::test_a_request_backed_by_someone_else_is_not_mine`; `test_worker_serving.py::BackedCountsAsOwnTests::test_an_anonymous_backer_is_never_matched`; `test_worker_serving.py::BackedCountsAsOwnTests::test_postgres_uses_json_containment`. **Gaps:** case-insensitive `requested_by__iexact` for listed names vs exact `requested_by` for own; no test of a mixed-case own name.
- **Determinism hazards.** `_backed_q` has two implementations by DB vendor (`also_requested_by @> [name]` on PostgreSQL; a Python scan of CHARGED rows with backers on SQLite); same result set.
- **History.** serve order PR #154 (2026-11-03); serve_for 2026-11-05; per-worker mode/campaign PR #189 (2026-11-08); backed-as-own PR #196 (2026-11-09).
- **Ordering.** Before Q-2.15.

### Q-2.15 Candidate set
- **Statement.** The ordered candidate queryset starts from `AnalysisTask` rows in `CHARGED` (`state IN ('PENDING','LEASED')`) whose position is `SERVEABLE` (`Position.status = 'UNKNOWN'`; a priority-tombstoned position is serveable). LEASED rows are deliberately kept in the set because the fair-share window must sum them (Q-2.18); they drop out only when locking (Q-2.20 locks PENDING only). Row filters, applied before the window: (a) house accounts (`HOUSE_ACCOUNTS = {'codex_local_worker'}`): exclude USER rows whose `requested_by` is not the house account (it serves AUTO/FILL and its own requests only); (b) personal mode: keep only `USER AND (own_q OR personal_q)` rows, OR rows whose position's `campaign_id == worker_campaign` when a campaign is set (note: the campaign arm is not restricted by source). In the sweep-share pass the candidate set is instead `sweep.waiting_tasks()` (Q-2.28.1) and none of (a)/(b) applies.
- **Inputs / outputs.** `AnalysisTask.state/source/requested_by`, `Position.status/campaign_id`.
- **Code.** `atomicdb/live_request.py:SERVEABLE:82`, `CHARGED:89`; `atomicdb/views.py:HOUSE_ACCOUNTS:1000`; `api_lease.choose_pending:1229-1266,1285-1288`.
- **Tests.** `test_queue_fairness.py::HouseWorkerPolicyTests::test_house_worker_skips_a_foreign_click_for_background_work`; `test_queue_fairness.py::HouseWorkerPolicyTests::test_house_worker_may_serve_its_own_click`; `test_queue_fairness.py::HouseWorkerPolicyTests::test_community_worker_still_serves_foreign_clicks`; `test_worker_serving.py::WorkerServingLeaseTests::test_personal_mode_takes_its_own_request`; `test_worker_serving.py::WorkerServingLeaseTests::test_personal_mode_is_idle_without_own_requests`; `test_worker_serving.py::WorkerServingLeaseTests::test_personal_mode_takes_the_campaign_once_its_own_are_done`; `test_worker_serving.py::BackedCountsAsOwnTests::test_mine_only_serves_what_a_listed_account_backed`; `test_ingest_queue.py::ShadowBackfillTests::test_a_tombstoned_position_is_not_a_zombie_and_is_still_served`. **Gaps:** a personal worker with a campaign taking a *stranger's* USER request on a campaign position (permitted by the filter) is untested.
- **Determinism hazards.** Reads `Position.status` at statement time (READ COMMITTED snapshot per statement).
- **History.** SERVEABLE 2026-08-03 (0005687, PR #31); house accounts 2026-08-29 (e39a8dc); personal PR #189.
- **Ordering.** Before Q-2.16.

### Q-2.16 Lease order keys
- **Statement.** Outside the sweep pass, candidates are sorted by, in this order (ascending unless prefixed `-`):
  1. `-serve_band` — only when the worker follows a campaign: 2 for USER rows (personal mode: USER rows that pass its filter), 1 for rows on positions with `campaign_id == worker_campaign`, 0 otherwise; absent without a campaign.
  2. `-source` (USER > FILL > AUTO, Q-1.2).
  3. `-own_first` (Q-2.16.1).
  4. `-named_first` (Q-2.17).
  5. `lane_ahead` (fair share, Q-2.18).
  6. `-backer_rank` (= `backers` on USER rows, 0 elsewhere).
  7. `served_rank` (Q-2.19).
  8. `-queue_rank` (= `Position.priority` for non-USER rows, 0.0 for USER rows: inside the USER band position priority never decides).
  9. `id` (final, unique tie-break).
  Keys 3-7 are 0 on every non-USER row, so AUTO and FILL are ordered by position priority then id. Keys 4-7 are the shared `live_request.SERVICE_KEYS` used verbatim by the queue estimator (`queue_ahead`, Q-6).
- **Inputs / outputs.** Annotations on `AnalysisTask` + `Position.priority`, `Position.campaign_id`.
- **Code.** `atomicdb/views.py:api_lease.choose_pending:1150-1153,1171-1178,1195,1229-1236,1267-1273,1290-1293`; `atomicdb/live_request.py:SERVICE_KEYS:531`, `fair_share:247-250`.
- **Tests.** `test_queue_fairness.py::UserBandFifoTests::test_an_older_click_is_served_before_a_better_positioned_one`; `test_queue_fairness.py::UserBandFifoTests::test_the_selector_bands_still_follow_position_priority`; `test_campaigns.py::SelectorBonusTests::test_the_user_band_of_the_lease_is_untouched`; `test_worker_serving.py::WorkerServingLeaseTests::test_a_campaign_task_goes_before_other_selector_work`; `test_worker_serving.py::WorkerServingLeaseTests::test_requests_still_go_before_the_campaign`; `test_worker_serving.py::WorkerServingLeaseTests::test_an_empty_campaign_falls_back_to_the_selector`; `test_queue_fairness.py::FairShareTests::test_the_estimator_agrees_with_the_order_actually_served`. **Gaps:** `-backer_rank` as a tie-break between two accounts' rows tied on `lane_ahead` is pinned only through the estimator tests (Part 4).
- **Determinism hazards.** `Position.priority` is a stored float refreshed asynchronously by the selector (PRI-3); a lease reads whatever was last written. Float ties fall to `id`.
- **History.** USER band FIFO (human_rank) 2026-07-28 (d5965eb); fair share 2026-08-07 (PR #34); backer rank 2026-08-14 (abf61a0); served rank PR #199 (2026-11-09); campaign band PR #189.
- **Ordering.** After Q-2.15; consumed by Q-2.20/Q-2.21.

### Q-2.16.1 own_first values
- **Statement.** `own_first` is a per-worker integer, evaluated as a CASE in this order: if `serve_first` is non-empty and `serve_order` in {'others','own'} and the row is USER and matches `favoured` -> `listed_band` (2 with 'others', 1 with 'own'); else if the row is USER and matches `own_q` -> 1 for 'own', 0 for 'queue', -1 for 'others'; else 0. So: *Mine first* puts own/backed and listed accounts' requests ahead of other USER rows and interleaves them by fair share; *Queue order* gives no preference; *Others first* puts listed accounts first, then strangers, then own requests last (still before FILL/AUTO because `-source` comes first). A row both own and listed gets the listed value.
- **Inputs / outputs.** as Q-2.14.
- **Code.** `atomicdb/views.py:api_lease.choose_pending:1170-1178`.
- **Tests.** `test_queue_fairness.py::OwnRequestAffinityTests::test_a_workers_own_request_jumps_its_band_queue`; `test_queue_fairness.py::OwnRequestAffinityTests::test_after_its_own_the_worker_rejoins_the_fifo`; `test_serve_others_first.py::ServeOthersFirstLeaseTests::test_on_the_waiting_stranger_is_served_first`; `test_serve_others_first.py::ServeOthersFirstLeaseTests::test_on_an_older_own_request_still_waits_for_the_stranger`; `test_worker_serving.py::BackedCountsAsOwnTests::test_others_first_puts_the_backed_request_behind_the_rest`; `test_worker_serving.py::BackedCountsAsOwnTests::test_queue_order_gives_the_backed_request_no_preference`; `test_lease_heads.py::HeadLeaseEquivalenceTests::test_listed_accounts_with_others_first`. **Gaps:** none.
- **Determinism hazards.** none.
- **History.** affinity 2026-07; serve_others_first 2026-11-03; listed band 4e9292f (2026-11-05).
- **Ordering.** part of Q-2.16.

### Q-2.17 Named stratum and starvation
- **Statement.** `named_first = 1` for a USER row if `requested_by > ''` **or** `created < now - STARVED_AFTER` (`STARVED_AFTER = 24 hours`), else 0; always 0 outside USER (`band_only=True`). So a named request beats a fresh anonymous one, and an anonymous request older than 24 hours (measured from `created`, not `requested_at`) joins the named stratum and competes on fair share. The starved anonymous rows still share the anonymous account's fair-share partition (`lane_ahead` is computed over all anonymous rows).
- **Inputs / outputs.** `AnalysisTask.source/requested_by/created`.
- **Code.** `atomicdb/live_request.py:STARVED_AFTER:77`, `named_tier:121-131`, `named_first_rank:534-547`; `atomicdb/views.py:api_lease.choose_pending:1195`.
- **Tests.** `test_queue_fairness.py::NamedBeforeAnonymousTests::test_a_named_request_jumps_the_anonymous_flood`; `test_queue_fairness.py::StarvedRequestTests::test_a_starved_anonymous_click_beats_a_fresh_named_one`; `test_queue_fairness.py::StarvedRequestTests::test_the_fresh_anonymous_flood_still_waits_its_turn`; `test_queue_fairness.py::AnonymousBucketTests::test_a_starved_anonymous_click_still_reaches_the_front`; `test_queue_fairness.py::AnonymousBucketTests::test_the_starved_tier_never_inherits_the_deficit_of_the_fresh_one`; `test_lease_heads.py::HeadLeaseEquivalenceTests::test_starved_anonymous_rows`. **Gaps:** revived rows with an old `created` (Q-1.10).
- **Determinism hazards.** Wall clock: `now` is taken inside `named_first_rank` at each `choose_pending` call (up to four per lease), and `irregular_accounts` takes its own, later `now` (comment at `live_request.py:486-487`).
- **History.** 0005687 (PR #31, 2026-08-03).
- **Ordering.** Key 4 of Q-2.16.

### Q-2.18 Fair share (lane_ahead)
- **Statement.** For each candidate row, `lane_ahead` = (sum of `budget_nodes` over the rows of the same partition up to and including this row in window order) minus this row's `budget_nodes`, for USER rows; 0 for every other source. Partition: `(source, effective_account)` with `effective_account = lane_account if non-empty else requested_by` (anonymous = ''). Window order inside the partition: LEASED rows first, then PENDING; then `background` ascending (normal before background); then `queue_seq` ascending; then `id` ascending. The window is computed over the *whole* candidate set after the Q-2.15 filters, so filters change the sums (personal mode sums only what it can take; this affects only that worker's order). Effect: deficit round-robin by cost — the first waiting request of every account has `lane_ahead` equal to that account's running leases, a 10B request yields to 78 requests of 128M. COMPLETED rows never count.
- **Inputs / outputs.** `AnalysisTask.source/lane_account/requested_by/state/background/queue_seq/id/budget_nodes`.
- **Code.** `atomicdb/live_request.py:fair_share:146-250` (window at `:227-238`), `_effective_account:134-143`.
- **Tests.** `test_queue_fairness.py::FairShareTests::test_the_first_request_of_each_account_precedes_every_second_one`; `test_queue_fairness.py::FairShareTests::test_a_ten_billion_request_yields_its_turn_to_the_cheap_ones`; `test_queue_fairness.py::FairShareTests::test_the_burst_of_the_sixth_of_august_no_longer_blocks_a_newcomer`; `test_queue_fairness.py::FairShareTests::test_within_one_account_the_order_of_arrival_is_untouched`; `test_lanes.py::AccountShareTests::test_equal_accounts_alternate`; `test_lanes.py::AccountShareTests::test_an_account_with_nothing_queued_holds_nothing`; `test_lease_heads.py::HeadLeaseEquivalenceTests::test_background_rows_and_the_leased_ones_count_first`. **Gaps:** none.
- **Determinism hazards.** The sum includes LEASED rows read at statement time; a lease landing between statements changes sums (READ COMMITTED).
- **History.** PR #34 (2026-08-07, after the 1,562-request burst of 6-Aug); per-lane multiplier lived 10-16 Aug and was removed (fb2baa0); running-first and queue_seq abf61a0 (2026-08-14); background #316.
- **Ordering.** Key 5 of Q-2.16. Must use the same partition as `lanes`/`queue_ahead` (Part 4).

### Q-2.19 Least-recently-served tie-break (served_rank)
- **Statement.** For USER rows: 0 if the row's effective account has no *served stamp*, else the 1-based rank of its stamp among the distinct stamps of all present accounts, ascending (served longest ago = 1). The stamp of an account is the maximum `leased_at` among its USER rows (author rows with `lane_account=''` and rows with `lane_account = account`) that are `LEASED`, or `COMPLETED` with `completed >= cutoff`, and with `leased_at >= cutoff`, where `cutoff = now - SERVED_WINDOW` (1 hour). Only accounts that currently have USER rows in CHARGED are ranked. Equal stamps share a rank. 0 outside USER. It sits after `-backer_rank`, so it only orders rows otherwise tied.
- **Inputs / outputs.** `AnalysisTask.source/state/leased_at/completed/requested_by/lane_account`.
- **Code.** `atomicdb/live_request.py:SERVED_WINDOW:92`, `served_rank:264-283`, `_rank_case:286-295`, `_served_stamps:298-357`, `_queued_accounts_sql:373-403`.
- **Tests.** `test_queue_fairness.py::LeastRecentlyServedTests::test_a_single_worker_does_not_starve_the_small_account`; `test_queue_fairness.py::LeastRecentlyServedTests::test_a_lease_older_than_the_window_does_not_count`; `test_queue_fairness.py::LeastRecentlyServedTests::test_the_selector_bands_ignore_who_was_served`; `test_lease_paging.py::ServedRankEquivalenceTests::test_random_queues_rank_and_order_the_same`; `test_lease_paging.py::ServedRankEquivalenceTests::test_an_account_with_only_finished_work_is_left_out`. **Gaps:** none.
- **Determinism hazards.** Wall clock window; timestamps with microsecond ties; raw SQL datetime adaptation differs between SQLite (strings parsed back) and PostgreSQL.
- **History.** 820bf60 (PR #199, 2026-11-09; 29 leases to 3 in one morning); single-statement stamps 2026-11-25 (#317).
- **Ordering.** Key 7 of Q-2.16.

### Q-2.20 Two-step order then lock
- **Statement.** PostgreSQL forbids `FOR UPDATE` with window functions, so the lease (1) reads ordered ids without locks, (2) locks them in chunks of `LEASE_SCAN_CHUNK = 64` ids with `SELECT ... FOR UPDATE SKIP LOCKED OF task` filtered by `state='PENDING'` (position read, not locked), and yields, in the step-one order, each row that is present (not locked by another transaction, still PENDING) and, if `max_budget` is set, still has `budget_nodes <= max_budget` (a budget raised between the steps is re-checked). Step one comes in pages: first the head pages of Q-2.21 when applicable, then `_lease_order_pages`: the first `LEASE_PAGE_ROWS = 256` ids, then the next `2048`, then the *full* id list minus ids already handed out (not another OFFSET, because rows can shift between statements). A page is only fetched when everything before it yielded nothing usable. `max_budget` filters ids in Python after the ordered read (never in SQL, which would change the window sums). Every row locked in a chunk, including rows the caller then skips (Q-2.22..Q-2.27), stays locked until the lease transaction commits.
- **Inputs / outputs.** Locks `AnalysisTask` rows; reads `Position.status/visits/fen/priority`.
- **Code.** `atomicdb/views.py:LEASE_SCAN_CHUNK:133`, `LEASE_PAGE_ROWS:137`, `_lease_order:752-759`, `_lease_order_pages:762-800`, `_locked_in_order:894-958`.
- **Tests.** `test_lease_paging.py::PagedLeaseEquivalenceTests::test_random_queues_drained_lease_by_lease`; `test_lease_paging.py::PagedLeaseEquivalenceTests::test_rows_over_max_budget_ahead_of_one_that_fits`; `test_lease_paging.py::PagedLeaseEquivalenceTests::test_the_first_page_alone_is_read_when_it_decides`; `test_max_budget.py::CappedLeaseTests::test_a_budget_raised_after_the_ordering_is_skipped_under_the_lock`; `test_max_budget.py::UncappedLeaseTests::test_without_the_field_the_id_query_is_the_historic_one`; `test_lease_position_lock.py::LeaseWithThePositionLockedTests::test_a_request_is_leased_while_the_ingest_holds_its_position`; `test_lease_position_lock.py::LeaseWithThePositionLockedTests::test_a_task_another_transaction_holds_is_still_skipped`. **Gaps:** none for equivalence; the side effect "skipped rows stay locked and are invisible to concurrent leases" is not tested.
- **Determinism hazards.** `SKIP LOCKED` makes the chosen task depend on concurrent transactions; READ COMMITTED between step one and step two; rows locked-and-skipped hide from other concurrent leases until commit. In single-writer v2 the semantic is simply "first eligible row in total order".
- **History.** two-step since PR #34 (2026-08-07); `of=('self',)` PR #95 (2026-10-28, a 128M request waited 1,912 s while 60 later tasks were served); pages #317 (2026-11-25); max_budget #278.
- **Ordering.** Consumes Q-2.16 (and Q-2.21); yields to Q-2.22..Q-2.27.

### Q-2.21 Head-of-queue reading
- **Statement.** An optimisation that must return exactly the same ids in the same order as the full ordered list; used only outside the sweep pass and only when the worker follows no campaign. Accounts = every effective account with USER rows in CHARGED; *irregular* accounts = those with any live USER row that is a lane row of another author, an anonymous row older than `STARVED_AFTER`, or a row backed by any of `backed_names`; the rest are *regular*. For `rows` in (`LEASE_HEAD_ROWS = 64`, `512`): read for each regular account its first `rows + 1` waiting USER rows by `(background, queue_seq, id)` (author rows and lane rows merged); apply the worker's row filter (one row decides per account); accounts with more than `rows` waiting rows are *cut*. If no account is cut and the worker is not personal, stop and fall back to the paged full list. Otherwise read the ordered queryset restricted to {those head ids} ∪ {every LEASED USER row} ∪ {every USER row of each irregular account}; the result is exact up to and including the first "last row read" of any cut account, and is truncated there (truncated to empty if a cut account has no row in the result, or its last row has `budget_nodes <= 0`). Yield that prefix (filtered by `max_budget`); if no account was cut, the list is complete (personal workers then never read the full queue); otherwise try the larger `rows`, then the full pages minus ids already handed out.
- **Inputs / outputs.** as Q-2.18, plus raw SQL over the partial-index predicates (Q-1.13).
- **Code.** `atomicdb/views.py:LEASE_HEAD_ROWS:141`, `_lease_head_pages:803-891`; `api_lease.choose_pending:1297-1301`; `atomicdb/live_request.py:queued_accounts:414-420`, `queue_heads:423-459`, `irregular_accounts:462-506`.
- **Tests.** `test_lease_heads.py::HeadLeaseEquivalenceTests::test_random_queues_drained_lease_by_lease`; `test_lease_heads.py::HeadLeaseEquivalenceTests::test_an_account_whose_whole_head_is_over_max_budget`; `test_lease_heads.py::HeadLeaseEquivalenceTests::test_an_account_whose_whole_head_is_on_closed_positions`; `test_lease_heads.py::HeadLeaseEquivalenceTests::test_lanes_and_rows_the_worker_backed`; `test_lease_heads.py::HeadLeaseEquivalenceTests::test_house_account`; `test_lease_heads.py::HeadLeaseEquivalenceTests::test_personal_worker`; `test_lease_heads.py::HeadCostTests::test_a_personal_worker_with_nothing_to_take_reads_no_page`; `test_lease_heads.py::HeadsOffTests::test_no_heads`. **Gaps:** none.
- **Determinism hazards.** Several statements at different snapshots (READ COMMITTED); `irregular_accounts` uses a later `now`. A port that computes the full order in memory does not need this rule at all; it is listed so the porter knows it is an equivalence-preserving cache, not a semantic.
- **History.** 8fcb487 (#318, 2026-11-25).
- **Ordering.** First pages of Q-2.20.

### Q-2.22 Runnable gate: open position and current generation
- **Statement.** A locked candidate is skipped (scan continues) if its position's `status != 'UNKNOWN'` (re-read with the lock's snapshot) or `generation > position.visits` (a follow-up for a visit that has not landed yet). A follow-up whose previous visit closed the position is never run (it stays PENDING on a closed position until absorbed by a closure or `_absorb_requeued` does not apply since it never had a lease; see Part 5 `_absorb_shadowed`/CAS-7).
- **Inputs / outputs.** `Position.status`, `Position.visits`, `AnalysisTask.generation`.
- **Code.** `atomicdb/views.py:api_lease.choose_pending:1303-1309`.
- **Tests.** `tests.py::RequestTests::test_request_during_shallow_lease_preserves_deep_follow_up`; `tests.py::RequestTests::test_deep_follow_up_is_not_run_after_shallow_visit_solves_position`; `test_lease_heads.py::HeadLeaseEquivalenceTests::test_an_account_whose_whole_head_is_on_closed_positions`. **Gaps:** none.
- **Determinism hazards.** none beyond snapshot timing.
- **History.** 80b8b0c (2026-07-22).
- **Ordering.** First per-candidate check.

### Q-2.23 Legacy-build filter
- **Statement.** A worker that is not token-capable (Q-2.4) skips any candidate with `attempts > 0` (a task that has ever been leased) or `budget_nodes > LEGACY_MAX_BUDGET` (128,000,000 nodes). So old builds only get short first attempts; once recycled, a task can only go to a token-capable build (otherwise a dead tokenless process with the same machine name could submit against the new lease).
- **Inputs / outputs.** `AnalysisTask.attempts`, `.budget_nodes`.
- **Code.** `atomicdb/views.py:LEGACY_MAX_BUDGET:87`; `api_lease.choose_pending:1310-1318`.
- **Tests.** `tests.py::LeaseReclaimTests::test_recycled_task_waits_for_token_capable_worker`; `tests.py::LeaseReclaimTests::test_deep_first_attempt_waits_for_token_capable_worker`; `test_solve.py::SolveProtocolTests::test_an_old_worker_is_untouched_by_any_of_this`. **Gaps:** none.
- **Determinism hazards.** none.
- **History.** 80b8b0c (2026-07-22). Removable if D8 = drop.
- **Ordering.** After Q-2.22.

### Q-2.24 Deep-lease cap per account
- **Statement.** `deep_lease_cap = max(DEEP_LEASES_FLOOR=1, live // DEEP_LEASES_POOL_SHARE=3)` where `live` = number of `WorkerPing` rows (slots of any account) with `last_seen >= now - 180 s`. The *capped requesters* are the `requested_by` values (author, not lane account; '' = the anonymous tide as one account) holding `>= cap` rows that are `LEASED`, `source='USER'` and `budget_nodes >= DEEP_TASK_NODES` (10,000,000,000). In the first passes, a candidate with `budget_nodes >= 10B`, `source='USER'` and `requested_by` in that set is skipped (the scan continues). If nothing can be leased with the cap honoured, the final pass runs with the cap off (Q-2.28), so the cap reorders but never idles a slot. Computed once per `choose_pending` call, after recycling, in the lease transaction. The threshold stays at 10B although the request ladder ends at 2B (only old 10B requests and restored withdrawals are affected).
- **Inputs / outputs.** `WorkerPing.last_seen`; `AnalysisTask.state/source/budget_nodes/requested_by`.
- **Code.** `atomicdb/views.py:DEEP_TASK_NODES:128`, `DEEP_LEASES_POOL_SHARE:129`, `DEEP_LEASES_FLOOR:130`, `deep_lease_cap:716-727`, `_deep_capped_requesters:730-749`; `api_lease.choose_pending:1141-1142,1319-1328`; `atomicdb/metrics.py:LIVE_SECONDS:27`.
- **Tests.** `test_queue_fairness.py::DeepLeaseCapTests::test_one_account_cannot_hold_more_deep_leases_than_its_share`; `test_queue_fairness.py::DeepLeaseCapTests::test_without_the_cap_that_same_slot_would_have_gone_deep`; `test_queue_fairness.py::DeepLeaseCapTests::test_the_rest_are_served_as_soon_as_a_deep_lease_frees_up`; `test_queue_fairness.py::DeepLeaseCapTests::test_no_slot_is_left_idle_when_only_deep_work_remains`; `test_queue_fairness.py::DeepLeaseCapTests::test_the_cap_follows_the_pool_and_never_drops_below_one`; `test_queue_fairness.py::DeepLeaseCapTests::test_a_dead_slot_does_not_widen_anybody_quota`; `test_queue_fairness.py::DeepLeaseCapTests::test_the_cheap_rungs_are_not_capped`; `test_lease_heads.py::HeadLeaseEquivalenceTests::test_deep_capped_requester_ahead_of_the_rest`. **Gaps:** cap keyed by author while fair share is keyed by lane account; untested divergence for a backed 10B row.
- **Determinism hazards.** Wall clock (180 s liveness), and `WorkerPing.last_seen` is written by every worker call including this one's Q-2.3.
- **History.** 5733af2 (PR #34, 2026-08-07; soothdest holding 9 of 10 leases on 7-Aug).
- **Ordering.** After Q-2.23.

### Q-2.25 Idle-repair slot routing
- **Statement.** A candidate with `arm == 'repair'` and `source == 'AUTO'` is skipped unless the requesting slot reported `0 < threads <= REPAIR_MAX_THREADS` (8) in this request; any other task fits every slot. A repair promoted to USER by a visitor goes to anyone. The scan continues past a skipped repair. (Repair production is Part 6, Q-10.)
- **Inputs / outputs.** `AnalysisTask.arm/source`; POST `threads`.
- **Code.** `atomicdb/idle_repair.py:fits_slot:115-126`, `REPAIR_MAX_THREADS:102`; `atomicdb/views.py:api_lease.choose_pending:1329-1334`.
- **Tests.** `test_idle_repair.py::LeaseRoutingTests::test_a_narrow_slot_takes_the_repair`; `test_idle_repair.py::LeaseRoutingTests::test_a_wide_slot_is_given_something_else`; `test_idle_repair.py::LeaseRoutingTests::test_a_jobs_slot_speaks_for_its_own_engine`; `test_idle_repair.py::LeaseRoutingTests::test_a_slot_that_reports_no_threads_is_not_trusted_with_one`; `test_idle_repair.py::LeaseRoutingTests::test_a_repair_a_visitor_asked_for_goes_to_anybody`; `test_idle_repair.py::LeaseRoutingTests::test_the_wide_slot_does_not_stall_on_the_repair`; `test_idle_repair.py::ShallowRuleTests::test_only_a_server_picked_repair_is_routed`. **Gaps:** none.
- **Determinism hazards.** Depends on an unverifiable client-reported number.
- **History.** fc9f14b (PR #100, 2026-10-29).
- **Ordering.** After Q-2.24.

### Q-2.26 Positions the server's tables cover are never served
- **Statement.** If `ingest.server_tables_wdl(position.fen)` returns a value (not None), the candidate is skipped: its exact result will be applied by the ingest process (`ingest_queue.close_queued_by_tables`, TB-5, Part 5). `server_tables_wdl` returns None (and never touches disk) unless `logic.tb_applicable(fen, max_men)` holds — piece count `<= ATOMICDB_TB_SERVER_MAX_MEN` (setting, default 5), castling field `-` and en-passant field `-` — and returns None when the tablebase cannot be opened or the probe raises. Only a probe that answers skips the row.
- **Inputs / outputs.** `Position.fen`; Syzygy files at `settings.ATOMICDB_TB_PATHS`.
- **Code.** `atomicdb/ingest.py:tb_server_max_men:10301-10303`, `server_tables_wdl:10373-10384`; `atomicdb/logic.py:tb_applicable`; `atomicdb/tb.py:probe_wdl:92-113`; `atomicdb/views.py:api_lease.choose_pending:1335-1342`.
- **Tests.** `test_tb_on_pick.py::TablesSettleQueuedPositionsTests::test_a_covered_position_is_served_to_no_worker`; `test_tb_on_pick.py::TablesSettleQueuedPositionsTests::test_the_lease_goes_on_to_the_next_task`; `test_tb_on_pick.py::TablesSettleQueuedPositionsTests::test_a_probe_that_fails_leaves_the_task_served`; `test_tb_on_pick.py::TablesSettleQueuedPositionsTests::test_a_position_beyond_the_tables_never_reaches_the_disk`. **Gaps:** none.
- **Determinism hazards.** Files on disk and settings; module-level cached tablebase handle (a failed open is cached until process restart, `tb.py:_get_tablebase`); probe exceptions read as "not covered".
- **History.** 7aa36db (PR #116, 2026-11-03), 6-man tables 2026-11-02.
- **Ordering.** After Q-2.25, before Q-2.27.

### Q-2.27 Tablebase-capable routing and fallback
- **Statement.** The worker supports tables iff POST `tb == '1'`. The first candidate passing Q-2.22..Q-2.26 is remembered as `first`. A candidate is returned immediately if the worker supports tables or `logic.tb_applicable(fen)` is false (default `max_men = 6`, castling `-`, en-passant `-`). Otherwise (a TB-applicable position and a worker without tables) the scan continues over the *whole* queue; if it ends without a non-TB candidate, `first` (a TB-applicable task) is returned. So a non-TB worker never takes a TB position while any other eligible task exists.
- **Inputs / outputs.** POST `tb`; `Position.fen`.
- **Code.** `atomicdb/views.py:api_lease.choose_pending:1140,1343-1348`.
- **Tests.** `tests.py::TbRoutingTests::test_tb_task_waits_for_tb_worker`; `tests.py::TbRoutingTests::test_tb_task_served_when_nothing_else`; `tests.py::TbRoutingTests::test_non_tb_worker_scans_past_more_than_four_tb_tasks`; `test_lease_heads.py::HeadLeaseEquivalenceTests::test_tablebase_rows_ahead_of_a_worker_without_tables`; `test_lease_heads.py::HeadLeaseEquivalenceTests::test_only_tablebase_rows_falls_back_to_the_first`. **Gaps:** none.
- **Determinism hazards.** A non-TB worker that falls through scans and *locks* every PENDING candidate (Q-2.20), hiding them from concurrent leases until commit.
- **History.** Historic fallback since tier 1; whole-queue scan since July 2026.
- **Ordering.** Last per-candidate check.

### Q-2.28 Pass sequence
- **Statement.** When the slot holds nothing (Q-2.9..Q-2.12) and is under the machine cap (Q-2.13), the lease chooses at most one task with these passes, stopping at the first that returns a task: (1) if the worker is not personal and `sweep.take_share()` is true, `choose_pending(sweep_only=True)` (Q-2.28.1); if that returns nothing, `sweep.note_waiting(False)`; (2) `choose_pending()` (deep cap honoured); (3) if still nothing, the worker is not personal, and `max_budget is None or max_budget >= ingest.SERVER_PICKED_MIN_NODES` (= `REQUEST_BUDGET_LADDER[0]` = 128,000,000): call `ingest.next_tasks(TASK_REFILL_COUNT = 8)` (selector refill, PRI-4; creates AUTO tasks inside this lease transaction) and `choose_pending()` again; (4) if still nothing, `choose_pending(honour_deep_cap=False)`. Each `choose_pending` call recomputes the deep cap set and all annotations.
- **Inputs / outputs.** Creates `AnalysisTask` rows via PRI-4 in pass 3.
- **Code.** `atomicdb/views.py:TASK_REFILL_COUNT:85`; `api_lease:1392-1423`; `atomicdb/ingest.py:SERVER_PICKED_MIN_NODES:397`.
- **Tests.** `test_max_budget.py::RefillTests::test_a_worker_capped_below_server_picks_does_not_refill`; `test_max_budget.py::RefillTests::test_a_worker_capped_at_server_picks_refills_as_before`; `test_max_budget.py::RefillTests::test_an_uncapped_worker_refills_as_before`; `test_worker_serving.py::WorkerServingLeaseTests::test_personal_mode_is_idle_without_own_requests`; `test_idle_pool_budget.py::EmptyQueueLeaseTests::test_the_task_a_worker_gets_from_an_empty_queue_is_128m`; `test_selector_service.py::SelectorOutOfRequestTests::test_lease_does_not_run_the_global_pass`; `tests.py::RequestTests::test_user_requests_leased_first`; `test_queue_fairness.py::DeepLeaseCapTests::test_no_slot_is_left_idle_when_only_deep_work_remains`. **Gaps:** none.
- **Determinism hazards.** Pass 3 makes the lease a task *producer*, so which tasks exist depends on lease traffic; the refill runs inside the lease transaction and holds its locks.
- **History.** Refill inline since 2026-07-22; deep-cap second pass PR #34; sweep pass PR #224 (2026-11-11); max_budget gate PR #278.
- **Ordering.** After Q-2.13; before Q-2.29.

### Q-2.28.1 Sweep share pass (contract with Part 6)
- **Statement.** `sweep.take_share()` is false when the share `ATOMICDB_SWEEP16_SHARE` (Django setting, else environment variable, default 0.14, clamped to [0,1], NaN or unparsable -> default) is <= 0 or no sweep task is waiting (cached boolean `atomicdb:sweep16:waiting`, 60 s TTL, refreshed by `waiting_tasks().exists()` on a miss); true when share >= 1; otherwise it increments a cache counter `n` (`atomicdb:sweep16:leases`) and returns `floor(n*share) != floor((n-1)*share)` (exactly `share` of leases, evenly spread), falling back to `random.random() < share` if the cache fails. In the sweep pass the candidates are `sweep.waiting_tasks()` = `PENDING`, `arm='sweep16'`, `source='FILL'`, position `UNKNOWN`, ordered by `id` (oldest first), with no fair share or campaign keys and no house/personal filter, but through the same lock and per-candidate checks (Q-2.20, Q-2.22..Q-2.27). If it yields nothing (including because of `max_budget`, TB or other skips) the lease writes `note_waiting(False)` for 60 s, which turns the share off fleet-wide for that time.
- **Inputs / outputs.** Django cache keys above; `AnalysisTask.arm/source/state`.
- **Code.** `atomicdb/sweep.py:share:471-482`, `note_waiting:490-494`, `has_waiting:497-507`, `waiting_tasks:510-516`, `_next_lease_number:519-528`, `take_share:531-542`; `atomicdb/views.py:api_lease.choose_pending:1285-1288`, `api_lease:1394-1400`.
- **Tests.** `test_sweep.py::SweepShareRuleTests::test_the_fraction_is_exact_and_spread`; `test_sweep.py::SweepShareRuleTests::test_without_a_cache_it_falls_back_to_a_coin`; `test_sweep.py::SweepShareLeaseTests::test_a_share_of_one_serves_the_sweep_before_a_request`; `test_sweep.py::SweepShareLeaseTests::test_a_share_of_zero_keeps_the_old_order`; `test_sweep.py::SweepShareLeaseTests::test_a_stale_yes_is_corrected_by_the_lease`; `test_sweep.py::SweepShareLeaseTests::test_a_closed_position_is_not_served_by_the_share`; `test_sweep.py::SweepShareLeaseTests::test_over_many_leases_the_share_is_kept`; `test_max_budget.py::SweepShareTests::test_the_sweep_share_respects_the_cap_and_falls_through`. **Gaps:** the fleet-wide 60 s suppression caused by one capped or non-TB worker is untested (the test mocks `note_waiting`).
- **Determinism hazards.** Global cache counter shared by all web processes; random fallback; settings/env switch; 60 s cache TTL.
- **History.** 11657b8 (PR #224, 2026-11-11).
- **Ordering.** Pass 1 of Q-2.28.

### Q-2.29 Lease minting
- **Statement.** Unless replayed, the chosen task (if any) is written, in the same transaction, with `assigned_at = timezone.now()` (a second clock reading, later than the recycle `now`): `state='LEASED'`, `machine = slot`, `leased_at = assigned_at`, `lease_heartbeat_at = assigned_at`, `lease_token = secrets.token_urlsafe(32)` if token-capable else '', `lease_session = POST lease_session[:64]` if token-capable else '', `attempts += 1` (in Python from the locked row's value). No DBEvent. Then the transaction commits.
- **Inputs / outputs.** Writes `AnalysisTask.state/machine/leased_at/lease_heartbeat_at/lease_token/lease_session/attempts`.
- **Code.** `atomicdb/views.py:api_lease:1424-1436`.
- **Tests.** `test_lease_release.py::ReleaseOwnLeasesTests::test_a_released_task_goes_to_the_next_worker_that_asks`; `tests.py::LeaseReclaimTests::test_assignment_token_fences_stale_same_machine_process`; `tests.py::LeaseReclaimTests::test_postdeploy_tokenless_lease_recycles_after_one_hour`; `test_frontier.py::ExpansionRequestApiTests::test_an_expanded_child_is_immediately_leasable`. **Gaps:** none.
- **Determinism hazards.** Random token; wall clock.
- **History.** 80b8b0c.
- **Ordering.** Last write of the transaction; fencing by Part 2 reads these fields.

### Q-2.29.1 Lease response and searchmoves
- **Statement.** HTTP 200 `{"tasks": [...]}` with zero or one element `{id, fen (Position.fen), budget_nodes, multipv, searchmoves, lease_token, source, requested_by, line}`; `source`, `requested_by` and `line` are console-only additions old workers ignore. `searchmoves` (`_live_moves`): `[]` if the position is not expanded; otherwise the `move_uci` of every `Edge(parent=position)` whose child has `status == 'UNKNOWN'`, returned only if `0 < len(live) < len(edges)`, else `[]` (no restriction: either nothing is solved, or *everything* is solved). The list follows the unordered Edge query. The worker echoes the restriction back at submit and the ingest treats a restricted pass as unable to worsen `eval_cp` (EXP-2). `then_verify` is not sent.
- **Inputs / outputs.** `Position.fen/expanded`, `Edge.parent/move_uci/child`, child `Position.status`.
- **Code.** `atomicdb/views.py:_live_moves:605-627`; `api_lease:1460-1469`.
- **Tests.** `tests.py::SearchmovesTests::test_lease_restricts_to_unsolved_moves`; `tests.py::SearchmovesTests::test_no_restriction_when_nothing_solved`; `test_worker_console.py::LeaseLineTests::test_an_old_worker_reads_the_fields_it_always_read`. **Gaps:** the "all children closed -> unrestricted" case and the list order are untested.
- **Determinism hazards.** Edge query without `order_by` (row order); computed after commit from a fresh read.
- **History.** ba5c72a (2026-07-21, searchmoves); console fields d995849 (PR #214, 2026-11-09).
- **Ordering.** After commit of Q-2.29.

### Q-2.29.2 WorkerPing current task and console line
- **Statement.** After the transaction: with a task (new or replayed) set `WorkerPing.current_task_id = task.id`, `last_nps = 0`, `nps_updated = NULL`, `last_seen = now`; with a held live lease (Q-2.12) set `current_task_id = held id`, `last_seen = now`; with nothing set `current_task_id = NULL`, `last_nps = 0`, `nps_updated = NULL`, `last_seen = now`. The `line` field is the SAN line from the root taken only from caches (declared `route` label if it still holds, else the canonical lineage label); a missing label is computed in the background and left out ('' in the response); any exception yields ''. It can never fail the lease.
- **Inputs / outputs.** `WorkerPing.current_task_id/last_nps/nps_updated/last_seen`; view caches.
- **Code.** `atomicdb/views.py:api_lease:1438-1458`, `_lease_lines:1472-1505`.
- **Tests.** `test_worker_console.py::LeaseLineTests::test_a_request_says_whose_it_is_and_its_line`; `test_worker_console.py::LeaseLineTests::test_server_work_says_so`; `test_worker_console.py::LeaseLineTests::test_the_lease_never_walks_the_line_itself`; `test_worker_console.py::LeaseLineTests::test_a_broken_label_does_not_break_the_lease`; `tests.py::MachineVisibilityTests::test_lease_and_submit_update_ping`. **Gaps:** none.
- **Determinism hazards.** Cache contents decide whether `line` is present; background executor.
- **History.** d995849 (PR #214, 2026-11-09).
- **Ordering.** After Q-2.29.1 inputs are known; display only (WorkerPing feeds Q-2.24 `last_seen` only).

---

## Part 2: Heartbeat, submit, release, solve leases, engine gate

Scope: the worker protocol after a lease is handed out (`/atomicdb/api/heartbeat`, `/api/submit`, `/api/release`), the proof-lease protocol (`/api/solve/acquire`, `/api/solve/heartbeat`, `/api/solve/submit`), the stale solve-lease recycler, and the reference-engine gate. URL routes: `atomicdb/urls.py` (`api/heartbeat`, `api/submit`, `api/release`, `api/solve/acquire`, `api/solve/heartbeat`, `api/solve/submit`). All six views are `@csrf_exempt` form-POST endpoints that answer JSON.

Cross-references used below: Q-1.* (AnalysisTask model and states, part 1), Q-2.1..Q-2.29 (lease selection, analysis recycler in `api_lease`, part 1), Q-3.* (IngestJob lifecycle: `ingest_queue.enqueue/apply_job/process_job`, `then_verify` consumption, part 5), EXP-5 (`absorb_tasks`, `arbiter_budget`, `_absorb_shadowed`), SOL-1/SOL-2 (certificate verification and `apply_solve_result` closure effects), SOL-4 / Q-8 (who creates SolveTask rows: debt, autosolve95, fragile; part 6), TB-2 (server check of a submitted `tb_wdl`), PRI-6 (`refresh_selector` cycle, part 3).

Common constants (all in `atomicdb/views.py` unless noted):

| Constant | Value | Line |
|---|---|---|
| `LEASE_MINUTES` | 4 min (analysis lease silence window, used by part 1's recycler) | 72 |
| `RESTART_RECYCLE_MINUTES` | 3 min (restart relief, same machine identity, other session) | 79 |
| `LEASE_TOKEN_BUILD` | 2026072203 (first worker build that receives lease tokens) | 86 |
| `LEASES_PER_MACHINE` | 32 | 98 |
| `MAX_REPORTED_NPS` | 1_000_000_000_000 nodes/s | 142 |
| `MAX_SUBMIT_LINES_BYTES` | 512 * 1024 bytes (UTF-8 length of the `lines` field) | 193 |
| `MAX_SUBMIT_PV_PLIES` | 512 moves per PV | 194 |
| `AUTH_CACHE_SECONDS` | 300 s | 451 |
| `RELEASE_MAX_LEASES` | `LEASES_PER_MACHINE + 1` = 33 | 1796 |
| `SOLVE_LEASE_MINUTES` | 20 min (`ingest.py:8862`, re-exported `views.py:1908`) | |
| `MAX_SOLVE_CERTIFICATE_BYTES` | `solve.MAX_COMPRESSED_BYTES` | 1909 |

---

### Q-2.30 Worker authentication (enabled account, cached verification)
- **Statement.** Every worker endpoint in this part (heartbeat, submit, release, solve acquire/heartbeat/submit) authenticates with form fields `username` and `password`. A caller is accepted only if Django `authenticate()` succeeds AND an OpenBench `Profile` with `enabled=True` exists for that user; otherwise the endpoint answers `403 {"error": "bad credentials"}` (same body for a wrong password and a disabled account). A success is cached for 300 s under key `atomicdb:auth:` + HMAC-SHA256(SECRET_KEY, username + "\0" + password); the cached value is `(user.pk, sha256(user.password_hash))`. Every cache hit is re-validated with one query: the profile must still be enabled, `user.is_active` true, the username equal, and the stored password-hash fingerprint unchanged; any mismatch deletes the entry and falls back to the full path. Failures are never cached. Empty username or password never uses the cache. Authentication happens before any other validation except the method check (heartbeat and release check `POST` first: 405 `{"error": "POST required"}`; submit and solve endpoints have no method check, a GET simply fails auth because `request.POST` is empty).
- **Inputs / outputs.** Reads `auth.User`, `OpenBench.Profile.enabled`, `User.is_active`, `User.password`; Django cache. Writes only the cache entry. No DBEvent.
- **Code.** `atomicdb/views.py:_auth:407`, `_auth_pair:421`, `_auth_cache_key:455`, `_password_fingerprint:464`, `_auth_cache_hit:470`, `_auth_cache_store:501`.
- **Tests.** `test_fencing.py::ExpelledAccountTests::test_a_disabled_account_cannot_lease`, `test_fencing.py::ExpelledAccountTests::test_the_rest_of_the_protocol_is_closed_too` (heartbeat, submit, solve acquire/heartbeat/submit all 403), `test_lease_release.py::ReleaseRefusalTests::test_credentials_are_required`, `test_lease_release.py::ReleaseRefusalTests::test_the_refusal_without_credentials_is_the_heartbeats`, `test_lease_release.py::ReleaseRefusalTests::test_only_post_is_accepted`, `tests.py::MachineVisibilityTests::test_heartbeat_rejects_bad_credentials`, `test_solve.py::SolveProtocolTests::test_credentials_are_required`, `test_auth_cache.py::AuthCacheTests::*` (8 tests: no second hash, password never cached, password change / disabled profile / inactive user invalidate on hit, wrong password and disabled account never cached, empty credentials take the old path). **Gaps:** none material (the 300 s TTL value itself is not asserted).
- **Determinism hazards.** Django cache (process-shared, TTL wall clock, `views.py:505`); hits re-validated so the verdict does not depend on the cache, only the cost. `authenticate()` depends on Django auth backends configured in settings.
- **History.** Enabled-profile requirement: snapshot 53b5030 (2026-10-04, PR #48). Auth cache: c2c6f8c (2026-11-18, PR #279) after PBKDF2 hashing took 83 % of web CPU.
- **Ordering.** First step of every endpoint (after the method check where present). Precedes Q-2.31.

### Q-2.31 Capacity touch (WorkerPing upsert without touching leases)
- **Statement.** Heartbeat, solve acquire and solve heartbeat (and `api_lease`, part 1) call `_touch_worker` right after auth. It resolves the machine identity as `POST['machine']` defaulting to the authenticated username, truncated to 64 chars, and upserts the `WorkerPing` row keyed by `(machine, user=username)` with `threads` (`reported_threads`: int clamp to [0, 1024], unparsable = 0), `hash_mb` (max(0, int), unparsable = 0), `os` (first 64 chars), `last_seen = now`, and `worker_build` only when `reported_worker_build` gives a usable value (int in (0, 2_147_483_647]; otherwise the stored build is kept). An existing row is updated with a column-restricted `UPDATE` (never a full model save, so concurrent submit/heartbeat telemetry is not rolled back). It never changes, extends or reclaims a lease. Submit and release do NOT call it.
- **Inputs / outputs.** Writes `WorkerPing.threads, hash_mb, os, last_seen, worker_build` (create or update). Returns the ping, whose `machine` is the identity the caller's heartbeat is fenced against.
- **Code.** `atomicdb/views.py:_touch_worker:573`, `reported_threads:519`, `reported_worker_build:534`, `MAX_REPORTED_THREADS:516`, `MAX_WORKER_BUILD:531`.
- **Tests.** `tests.py::MachineVisibilityTests::test_capacity_touch_does_not_overwrite_concurrent_telemetry`, `tests.py::LeaseReclaimTests::test_request_without_build_keeps_the_stored_build`. **Gaps:** threads clamp at 1024 and `os` truncation not pinned here.
- **Determinism hazards.** `timezone.now()` (`views.py:581`); `get_or_create` under READ COMMITTED can race two first requests of the same machine (unique constraint on WorkerPing decides; telemetry only).
- **History.** Build column carried on the same write: snapshot 53b5030 (PR #48).
- **Ordering.** After Q-2.30, before the endpoint's own logic. The returned `ping.machine` is the identity used by Q-2.33 and Q-2.56..Q-2.59.

### Q-2.32 Heartbeat request validation
- **Statement.** `POST /api/heartbeat`. Order: method (405) -> auth (403) -> capacity touch (Q-2.31, already written even if the rest fails) -> `nps` = clamp(int(`nps` or 0), 0, 1e12); a non-integer `nps` answers `400 {"error": "invalid nps"}` -> `task_id`: when present and non-empty it must parse as int, else `400 {"error": "invalid task_id"}`. An absent/empty `task_id` is valid and means "idle".
- **Inputs / outputs.** Form fields `nps`, `task_id`, `lease_token`, `machine`, capacity fields.
- **Code.** `atomicdb/views.py:api_heartbeat:1509-1528`.
- **Tests.** `tests.py::MachineVisibilityTests::test_heartbeat_rejects_bad_credentials`. **Gaps:** invalid nps / invalid task_id responses and the nps clamp are not pinned.
- **Determinism hazards.** none.
- **History.** Separate heartbeat (keep only the authenticated assignment alive): 80b8b0c (2026-07-22, PR #13).
- **Ordering.** Before Q-2.33.

### Q-2.33 Heartbeat lease renewal (fencing)
- **Statement.** With a `task_id`, exactly one conditional `UPDATE AnalysisTask SET lease_heartbeat_at = now WHERE id = task_id AND state = 'LEASED' AND machine = ping.machine AND (lease_token = '' OR lease_token = POST['lease_token'])`. If it updates 1 row the lease is renewed; otherwise `409 {"error": "stale-lease"}` (covers: unknown id, not LEASED, other machine, wrong token). A tokenless lease (`lease_token=''`, only builds older than `LEASE_TOKEN_BUILD` hold one) is renewed with any token. Renewal never changes `leased_at`, `state`, `attempts` or the token. The account is not checked against the task (the machine name and token are the proof). Note the machine compared is `POST['machine']` defaulting to the username (Q-2.31), not the empty default used by submit.
- **Inputs / outputs.** Reads/writes `AnalysisTask.lease_heartbeat_at` (the value part 1's recycler compares against `LEASE_MINUTES`).
- **Code.** `atomicdb/views.py:api_heartbeat:1529-1538`.
- **Tests.** `tests.py::MachineVisibilityTests::test_heartbeat_tracks_current_task_and_keeps_original_lease_time`, `tests.py::LeaseReclaimTests::test_assignment_token_fences_stale_same_machine_process` (wrong token -> 409 stale-lease). **Gaps:** tokenless lease renewed with an arbitrary token; heartbeat for a task leased to another machine; heartbeat on a COMPLETED task (409).
- **Determinism hazards.** `timezone.now()` (`views.py:1529`) is the stored heartbeat; the recycler's verdict (part 1) depends on wall-clock distance to it.
- **History.** Token fencing: 80b8b0c (2026-07-22, PR #13).
- **Ordering.** After Q-2.32; before Q-2.34. Must precede part 1's recycler decisions for the same task to keep the lease alive.

### Q-2.34 Heartbeat live-view update
- **Statement.** After a successful renewal or an idle heartbeat, `WorkerPing` (the touched row) is updated: `current_task_id` = the renewed task id or NULL when idle, `last_seen = now`, `last_nps` = the clamped nps if a task is current else 0, `nps_updated = now` if a task is current else NULL. Response `200 {"ok": true, "machine": ping.machine, "current_task_id": id|null}`. A failed renewal (409) does not reach this step (WorkerPing keeps its previous `current_task_id`).
- **Inputs / outputs.** Writes `WorkerPing.current_task_id, last_seen, last_nps, nps_updated`.
- **Code.** `atomicdb/views.py:api_heartbeat:1539-1545`.
- **Tests.** `tests.py::MachineVisibilityTests::test_heartbeat_tracks_current_task_and_keeps_original_lease_time`. **Gaps:** idle heartbeat clearing `current_task_id` and zeroing `last_nps`.
- **Determinism hazards.** `timezone.now()` twice (`views.py:1540,1542`; may differ by microseconds). Telemetry only.
- **History.** 80b8b0c (PR #13).
- **Ordering.** After Q-2.33.

### Q-2.35 Submit payload validation (before any database read)
- **Statement.** `POST /api/submit` (no method check). After auth (403), the form is validated in this order, and the first failure answers `400 {"error": "malformed: <message>"}` without reading or claiming the task: `task_id` must exist and parse as int (missing -> message is the Python KeyError text `'task_id'`); `lines` must exist; its UTF-8 byte length must be <= 524,288 (`lines payload is too large`); it must parse as JSON (JSON error text); it must be a list of at most 32 entries (`lines must be a list with at most 32 entries`); each entry must be an object (`each line must be an object`); `move` must be a non-empty string of length <= 16 (`invalid root move`); `eval_cp` and `mate` must each be absent/null or a JSON integer that is not a boolean (`invalid eval_cp` / `invalid mate`); `pv` must be absent/null or a list of at most 512 strings each of length <= 16 (`invalid or excessively long PV`); `raw` must be absent/null or a string of length <= 65,536 (`raw line is too large`). Nothing else about the lines (legality, move belongs to the position) is checked here; that is ingest (Q-3/EXP-2).
- **Inputs / outputs.** Form fields `task_id`, `lines`. No writes.
- **Code.** `atomicdb/views.py:api_submit:1554-1584`, constants `MAX_SUBMIT_LINES_BYTES:193`, `MAX_SUBMIT_PV_PLIES:194`.
- **Tests.** `test_fencing.py::SubmitFencingTests::test_oversized_pv_is_rejected_before_claiming_the_task`. **Gaps:** the 32-line cap, the 512 KiB cap, move length 16, bool-as-int rejection, `raw` 64 KiB cap, and the exact error texts are not pinned (the worker treats any 4xx as definitive: `test_submit_timeout.py::WorkerSubmitRetryTests::test_does_not_retry_definitive_stale_lease`).
- **Determinism hazards.** Error text for JSON errors and KeyError comes from the Python runtime (`f'malformed: {e}'`, `views.py:1584`); a port cannot reproduce it byte for byte. Golden-file comparisons should match on prefix `malformed:`.
- **History.** Bounds added in 8c293ff (2026-07-22, PR #10, "Bound AtomicDB submit verification latency").
- **Ordering.** First after auth; before Q-2.36.

### Q-2.36 Submit scalar sanitation (elapsed, nodes, tb_wdl, tb_dtz)
- **Statement.** `elapsed` = float(`elapsed` or 0); non-finite or unparsable -> 0.0; otherwise clamped to [0.0, 86,400.0] seconds. `nodes` (searched) = max(0, int(`nodes` or 0)); unparsable (including `"1.5"`) -> 0. `tb_wdl`: absent or `""` -> None; otherwise must parse as int, else `400 {"error": "malformed: invalid tb_wdl"}` (the only rejection in this step; no range check here, TB-2 checks the value). `tb_dtz`: absent or `""` -> None; unparsable -> None (ignored, never rejected); |dtz| > 1024 -> None. These run after Q-2.35 and before any task read.
- **Inputs / outputs.** Form fields `elapsed`, `nodes`, `tb_wdl`, `tb_dtz`. Values flow into Q-2.40 (gate's `tablebase` flag = `tb_wdl is not None`), Q-2.42, Q-2.43, Q-2.44.
- **Code.** `atomicdb/views.py:api_submit:1586-1612`.
- **Tests.** `tests.py::MachineVisibilityTests::test_non_finite_elapsed_is_safely_ignored` (NaN -> 0.0, nps 0), `test_fencing.py::SubmitFencingTests::test_zero_reported_nodes_remain_zero`, `test_fencing.py::SubmitFencingTests::test_rejected_tb_does_not_account_elapsed_time` (elapsed 86400 accepted; TB rejection lands later in the job). **Gaps:** invalid `tb_wdl` 400; `tb_dtz` > 1024 dropped; negative nodes -> 0; elapsed > 86400 clamped.
- **Determinism hazards.** none (pure parsing). Float parsing follows Python `float()` (accepts `"inf"`, `"1e3"`, surrounding whitespace).
- **History.** `elapsed` clamp and finite check: 3960f81 (2026-07-21, PR #10). DTZ field added with clock slack (DTM-5), snapshot 53b5030 (PR #48).
- **Ordering.** After Q-2.35, before Q-2.37/Q-2.38.

### Q-2.37 Provenance sha sanitation (`_sha_field`)
- **Statement.** `engine_sha` and `net_sha` (and the sha form of `solver_build`, Q-2.61) are normalised by `_sha_field`: `str(raw or '').strip().lower()`; kept only if exactly 64 chars all in `0-9a-f`, else stored as `''`. Never raises, never rejects the submit. The cleaned `engine_sha` is what the engine gate (Q-9.4) sees, so a malformed sha reads as "missing".
- **Inputs / outputs.** Form fields `engine_sha`, `net_sha`. Written to `AnalysisTask.engine_sha`, `AnalysisTask.net_sha` in Q-2.44; `engine_sha` later copied into closure provenance by apply (Q-3).
- **Code.** `atomicdb/views.py:_sha_field:1772`, use at `api_submit:1615-1616`, `api_solve_submit:2093`.
- **Tests.** `test_proof_manager.py::SubmitProvenanceTests::test_provenance_is_persisted_when_present`, `::test_an_old_worker_submits_exactly_as_before`, `::test_garbage_provenance_is_dropped_not_stored`. **Gaps:** uppercase input lower-cased; whitespace stripped.
- **Determinism hazards.** none.
- **History.** 0de882a / 38f086a (2026-07-27, landed via PR #31).
- **Ordering.** Before Q-2.40 (the gate reads the cleaned value).

### Q-2.38 Submit ownership checks on the unlocked snapshot (error precedence)
- **Statement.** The task is read once without a lock (`AnalysisTask` by id). Precedence of answers: (1) no such task -> `400 {"error": "malformed: unknown task"}`; (2) `state` not in {LEASED, COMPLETED} (i.e. PENDING or CANCELLED) -> `400 {"error": "not-leased"}`; (3) `POST['machine']` empty (the default here is `''`, not the username) or different from `task.machine` -> `409 {"error": "not-your-lease"}`; (4) `task.lease_token` non-empty and not equal (constant-time compare) to `POST['lease_token']` -> `409 {"error": "stale-lease"}`; (5) `state == COMPLETED` -> `200 {"ok": true, "dup": true}` (Q-2.39). A tokenless lease (`lease_token=''`) skips check (4). The authenticated account is never compared with the task's lease holder; machine name plus token are the proof.
- **Inputs / outputs.** Reads `AnalysisTask.state, machine, lease_token, attempts, leased_at, position`. No writes.
- **Code.** `atomicdb/views.py:api_submit:1614-1631`.
- **Tests.** `test_fencing.py::SubmitFencingTests::test_pending_task_is_rejected`, `::test_other_machine_is_rejected`, `::test_machine_name_has_no_reserved_submit_prefix`, `tests.py::LeaseReclaimTests::test_assignment_token_fences_stale_same_machine_process`, `test_ingest_queue.py::SubmitEnqueueTests::test_the_definitive_rejections_are_unchanged`, `test_lease_release.py::ReleaseOwnLeasesTests::test_a_late_submit_of_a_released_task_is_refused`. **Gaps:** CANCELLED task -> not-leased; empty `machine` field -> not-your-lease; tokenless lease accepting any token.
- **Determinism hazards.** Unlocked read under READ COMMITTED; the snapshot's `attempts/leased_at/lease_token` become the fence of Q-2.41, so a concurrent re-lease between this read and the claim is detected there, not here.
- **History.** Token fencing with `secrets.compare_digest`: 80b8b0c (2026-07-22, PR #13).
- **Ordering.** After Q-2.36/Q-2.37; before Q-2.40 (gate) and Q-2.41 (claim).

### Q-2.39 Duplicate submit is idempotent
- **Statement.** A submit for a task already COMPLETED by the same machine with the same token (or a tokenless one) answers `200 {"ok": true, "dup": true}` and changes nothing: no second IngestJob (IngestJob.task is OneToOne and `enqueue` is `get_or_create`), no WorkerPing bump. This works because completion (Q-2.44) leaves `machine` and `lease_token` on the row. A COMPLETED row absorbed by the server (machine `''`, Q-2.50/EXP-5) answers `not-your-lease` to any submit. The same dup answer is produced when the claim CAS (Q-2.41) loses to a concurrent submit of the same task.
- **Inputs / outputs.** Reads `AnalysisTask.state, machine, lease_token`. No writes.
- **Code.** `atomicdb/views.py:api_submit:1630-1631, 1677-1678`; `atomicdb/ingest_queue.py:enqueue:68` (OneToOne `get_or_create`); `atomicdb/models.py:IngestJob.task` (OneToOneField, line 684).
- **Tests.** `test_fencing.py::SubmitFencingTests::test_duplicate_submit_is_idempotent`, `test_ingest_queue.py::SubmitEnqueueTests::test_a_replayed_submit_is_still_a_duplicate`, `test_submit_timeout.py::WorkerSubmitRetryTests::test_retries_exact_payload_after_read_timeout` (worker side: resubmits after a read timeout and accepts dup). **Gaps:** a resubmit after completion with a different payload (still dup, the first payload wins).
- **Determinism hazards.** Concurrent duplicate submits race on the CAS; exactly one wins (row-level lock of the UPDATE).
- **History.** Async ingest made "dup" the answer to a replay of an already-queued payload: bb61b6a (2026-07-27, via PR #31).
- **Ordering.** Part of Q-2.38 step (5) and of Q-2.41's loser classification.

### Q-2.40 Engine gate at submit: refuse, requeue, absorb
- **Statement.** After the ownership checks pass on a LEASED task (so no third party can push someone else's lease back with a bad sha), `engine_policy.rejects(engine_sha, tablebase = tb_wdl is not None)` (Q-9.4) is evaluated. If it rejects, in one transaction: conditional `UPDATE ... SET state='PENDING', machine='', lease_heartbeat_at=NULL, lease_token='', lease_session='' WHERE id=task_id AND state='LEASED' AND machine=POST.machine AND attempts=snapshot.attempts AND leased_at=snapshot.leased_at AND lease_token=snapshot.lease_token` (`leased_at` and `attempts` stay, as in Q-2.51). If that updated the row: the caller's `WorkerPing(machine, user)` gets `current_task_id = NULL`, and if the task's position is no longer `UNKNOWN` the now-PENDING task is absorbed (`absorb_tasks`: COMPLETED, `completed=now`, `nodes_searched=0`, machine stays `''`). The answer is `403 {"error": "engine-not-accepted", "detail": REJECTION_DETAIL}` whether or not the CAS matched (a concurrent change leaves the row as it is). No DBEvent (only a log line). A tablebase submission is never gated; it cannot land engine lines because apply takes the TB branch exclusively (Q-3, `ingest_queue.py:apply_job:260-267`).
- **Inputs / outputs.** Reads the cleaned `engine_sha`, `tb_wdl`. Writes `AnalysisTask.state, machine, lease_heartbeat_at, lease_token, lease_session` (and, when absorbed, `state=COMPLETED, completed, nodes_searched=0`); `WorkerPing.current_task_id`.
- **Code.** `atomicdb/views.py:api_submit:1633-1662`; `atomicdb/ingest.py:absorb_tasks:5770`; `atomicdb/engine_policy.py:rejects:150`, `REJECTION_DETAIL:35`.
- **Tests.** `test_engine_policy.py::EngineGateTests::test_a_foreign_engine_is_refused_and_the_task_goes_back`, `::test_a_missing_sha_is_refused_while_enforcing`, `::test_tablebase_results_are_never_gated`, `::test_another_machine_cannot_push_a_lease_back_with_a_bad_sha`, `::test_current_and_previous_reference_builds_are_accepted`, `::test_an_extra_engine_listed_in_the_policy_is_accepted`. **Gaps:** absorption of the refused task when its position closed during the search; the 403 when the CAS did not match; tokenless lease refused by the gate.
- **Determinism hazards.** The verdict depends on files on disk at request time (policy file, engines directory; Q-9.1/Q-9.2) and on per-process caches; not replayable from the payload alone. `timezone.now()` in absorption (`ingest.py:5786`).
- **History.** 67c1c56 (2026-10-15, PR #66), added after an engine not playing atomic chess fed 188 positions in October.
- **Ordering.** After Q-2.38; before Q-2.41. If it fires, nothing of Q-2.41..Q-2.46 runs.

### Q-2.41 Atomic claim of the task (compare-and-set) and loser classification
- **Statement.** In one transaction with the enqueue: `UPDATE AnalysisTask SET state='COMPLETED' WHERE id=task_id AND state='LEASED' AND machine=POST.machine AND attempts=snapshot.attempts AND leased_at=snapshot.leased_at AND lease_token=snapshot.lease_token`. If it updated exactly 1 row the submit owns the result. Otherwise the row is re-read and the answer is: COMPLETED -> `200 {"ok": true, "dup": true}`; not LEASED -> `400 {"error": "not-leased"}`; LEASED to the same machine but `attempts`, `leased_at` or `lease_token` differ from the snapshot (it was recycled and re-leased, even to the same machine) -> `409 {"error": "stale-lease"}`; otherwise -> `409 {"error": "not-your-lease"}`. A loser writes nothing (no IngestJob, task unchanged).
- **Inputs / outputs.** Reads/writes `AnalysisTask.state`; fence fields `machine, attempts, leased_at, lease_token`.
- **Code.** `atomicdb/views.py:api_submit:1668-1686`.
- **Tests.** `test_fencing.py::SubmitFencingTests::test_same_machine_release_during_the_claim_is_stale` (attempts/leased_at changed between read and claim -> 409 stale-lease, task stays LEASED, nothing enqueued), `test_ingest_queue.py::SubmitEnqueueTests::test_the_claim_and_the_enqueue_are_one_commit`. **Gaps:** loser -> not-leased (task recycled to PENDING between read and claim); loser -> not-your-lease (re-leased to another machine).
- **Determinism hazards.** READ COMMITTED race window between Q-2.38's read and this CAS is intentional; the CAS is the serialisation point. A port with a single writer reproduces it by evaluating the fence against the state at apply time.
- **History.** Fence on attempts/leased_at/token: 80b8b0c (2026-07-22, PR #13); claim and enqueue in one commit: bb61b6a (2026-07-27, via PR #31).
- **Ordering.** After Q-2.40; Q-2.42..Q-2.45 run inside the same transaction, in that order.

### Q-2.42 Reported nodes clamped to twice the authoritative budget
- **Statement.** After the claim, the task row is re-read under `SELECT ... FOR UPDATE OF self` (the task only; the position is not locked in this transaction) and `searched = min(searched, 2 * task.budget_nodes)` using the budget as it is now (not the snapshot's). Zero stays zero. This clamped value is what the IngestJob payload carries (`nodes`) and what `AnalysisTask.nodes_searched` stores; it is the "actual nodes" used by arbitration and `nodes_invested` downstream (EXP-2/EXP-5).
- **Inputs / outputs.** Reads `AnalysisTask.budget_nodes`; writes payload `nodes`, `AnalysisTask.nodes_searched`.
- **Code.** `atomicdb/views.py:api_submit:1690-1692`.
- **Tests.** `test_fencing.py::SubmitFencingTests::test_reported_nodes_are_clamped_to_twice_budget`, `::test_nodes_use_authoritative_budget_after_claim` (budget changed to 100 between read and claim; 999 reported -> 200 stored), `::test_zero_reported_nodes_remain_zero`, `tests.py::NodesAccountingTests::test_actual_nodes_recorded_not_budget` (12345 stored, not the budget), `test_mate_stop.py::SubmitTests::test_a_mate_stop_is_accepted_and_credited_with_its_nodes`. **Gaps:** none.
- **Determinism hazards.** Row lock `select_for_update(of=('self',))` (`views.py:1690`); budget may be raised concurrently by a request (part 4) between snapshot and claim, which is why the locked read is used.
- **History.** 3960f81 (2026-07-21, PR #10, "Harden AtomicDB closure and submit trust").
- **Ordering.** After Q-2.41; before Q-2.43.

### Q-2.43 IngestJob enqueued in the claim's commit
- **Statement.** `ingest_queue.enqueue(task, payload)` = `IngestJob.get_or_create(task=task, defaults={position_id: task.position_id, payload, next_attempt_at: now})`, state PENDING, inside the claim transaction (so a claimed task always has its job, and a rolled-back claim has none). Payload keys, exactly: `lines` (the validated list as parsed, not normalised), `nodes` (clamped, Q-2.42), `elapsed` (Q-2.36), `machine` (POST machine), `username` (authenticated account), `tb_wdl` (int or null), `tb_dtz` (int or null), `restricted` = `bool(POST['searchmoves'].split())` (true when the worker echoes a non-empty searchmoves restriction; absent = a complete pass). The submit does not touch the tree (no position or edge writes). `then_verify` is NOT read or written here: it was stamped on the task at request time (part 4) and is consumed by apply (Q-3).
- **Inputs / outputs.** Creates `IngestJob(task, position, payload, state=PENDING, next_attempt_at=now, attempts=0)`.
- **Code.** `atomicdb/views.py:api_submit:1693-1707`; `atomicdb/ingest_queue.py:enqueue:68`.
- **Tests.** `test_ingest_queue.py::SubmitEnqueueTests::test_a_submit_acknowledges_and_leaves_the_work_queued`, `::test_the_submit_itself_does_not_touch_the_tree`, `::test_the_claim_and_the_enqueue_are_one_commit`. **Gaps:** the `restricted` flag derived from `searchmoves` at the submit boundary (its effect is pinned at ingest level by `test_multipv_policy.py::RestrictedPassSemanticsTests::*`); payload key set not asserted as a whole.
- **Determinism hazards.** `timezone.now()` for `next_attempt_at` (`ingest_queue.py:73`); IngestJob ids (sequence) define claim order in Q-3 (`claim_next` orders by id).
- **History.** bb61b6a (2026-07-27, via PR #31, "Take the AtomicDB ingest out of the worker's request"); `restricted`: c024cea (2026-07-29).
- **Ordering.** After Q-2.42; before Q-2.44 (same transaction). Q-3 applies the job later (or Q-2.46 inline).

### Q-2.44 AnalysisTask completion fields
- **Statement.** In the same transaction the task row is saved with: `state='COMPLETED'`, `machine=POST.machine`, `completed=now`, `nodes_searched=searched` (clamped), `elapsed_seconds=elapsed`, `delivered_by=<authenticated username>` (not the machine's owner: two accounts may announce the same machine name), `engine_sha`, `net_sha` (cleaned, possibly `''`). Columns left untouched and therefore retained after completion: `leased_at`, `lease_heartbeat_at`, `lease_token`, `lease_session`, `attempts` (this retention is what makes Q-2.39 work). A delivered row always has a non-empty `machine`; that is what distinguishes it from an absorbed row (`ingest.is_absorbed`: COMPLETED, `nodes_searched` falsy, `machine` empty).
- **Inputs / outputs.** Writes `AnalysisTask.state, machine, completed, delivered_by, nodes_searched, elapsed_seconds, engine_sha, net_sha`.
- **Code.** `atomicdb/views.py:api_submit:1708-1725`; `atomicdb/ingest.py:is_absorbed:5789`.
- **Tests.** `test_lanes.py::DeliveryEvidenceTests::test_submitting_a_result_stamps_one` (delivered_by = authenticated account), `tests.py::NodesAccountingTests::test_actual_nodes_recorded_not_budget`, `test_proof_manager.py::SubmitProvenanceTests::*`, `tests.py::MachineVisibilityTests::test_lease_and_submit_update_ping` (elapsed stored). **Gaps:** retention of lease_token/lease_session/heartbeat after completion; a submit with `nodes=0` and `machine` set is "delivered", not "absorbed", by `is_absorbed` only because of the machine.
- **Determinism hazards.** `timezone.now()` for `completed` (`views.py:1709`); `completed` feeds time-windowed aggregates (lanes, contributor totals; part 4).
- **History.** `delivered_by`: fb2baa0 (2026-08-16, via PR #44) after a machine-name collision misattributed nodes; provenance shas: 38f086a (2026-07-27, via PR #31).
- **Ordering.** After Q-2.43; before Q-2.45.

### Q-2.45 WorkerPing delivery stamp
- **Statement.** Same transaction: `UPDATE WorkerPing WHERE machine=POST.machine AND user=<username>` sets `tasks_done = tasks_done + 1`, `last_seen=now`, `last_result_at=now` (the only "a result landed" evidence, used by contributor lanes, Q-5), `current_task_id=NULL`, and, only when `searched > 0 and elapsed > 0`, `last_nps = min(round(searched / elapsed), 1e12)` and `nps_updated=now`. If no WorkerPing row exists for that (machine, user) the update is a no-op (submit never creates one).
- **Inputs / outputs.** Writes `WorkerPing.tasks_done, last_seen, last_result_at, current_task_id, last_nps, nps_updated`.
- **Code.** `atomicdb/views.py:api_submit:1726-1745`.
- **Tests.** `tests.py::MachineVisibilityTests::test_lease_and_submit_update_ping` (nps = nodes/elapsed = 400), `tests.py::MachineVisibilityTests::test_non_finite_elapsed_is_safely_ignored` (nps 0), `test_lanes.py::DeliveryEvidenceTests::test_submitting_a_result_stamps_one`, `test_active_workers.py::SpeedTests::test_a_speed_is_capped_like_the_one_the_submit_keeps`. **Gaps:** no-op when the ping row is missing; ping of the same machine name under another account untouched.
- **Determinism hazards.** `timezone.now()` three times (`views.py:1728,1735,1741`); Python `round()` is half-to-even (port must match for golden equality of `last_nps`).
- **History.** `last_result_at`: 8ac9bc5 (2026-08-15, via PR #44).
- **Ordering.** Last step of the claim transaction (after Q-2.44).

### Q-2.46 Synchronous ingest switch and the submit reply
- **Statement.** After the claim transaction commits: if setting `ATOMICDB_SYNCHRONOUS_INGEST` is truthy, the job is applied in the request through the same code (`ingest_queue.process_job(job)` -> `apply_job` with failure backoff, Q-3), and the reply summary is the apply summary, or `{"ingest_job": id, "ingest_error": "retry"|"failed"}` on failure. Otherwise (production default False) the summary is `{"queued": true, "ingest_job": id}`. In both cases `summary.submit_timing_seconds = {"transaction": s, "apply": s, "total": s}` (rounded to 3 decimals, monotonic clock) is added, and the reply is `200 {"ok": true, "summary": summary}`. A TB result the server refuses is therefore still a 200 to the worker; the refusal lands in the job (`summary {"tb_rejected": true}`) and a `TB_REJECTED` DBEvent (TB-2).
- **Inputs / outputs.** Reads setting `ATOMICDB_SYNCHRONOUS_INGEST`. Writes (sync only) whatever Q-3 apply writes.
- **Code.** `atomicdb/views.py:api_submit:1746-1769`; `atomicdb/ingest_queue.py:synchronous_ingest_enabled:56`, `process_job:416`.
- **Tests.** `test_ingest_queue.py::SynchronousFallbackTests::test_the_switch_applies_before_answering`, `test_ingest_queue.py::SynchronousVsQueuedTests::test_both_paths_leave_the_same_tree`, `test_fencing.py::SubmitPreparationBoundaryTests::test_andor_preparation_runs_outside_atomic_block`, `::test_tb_probe_runs_outside_atomic_block`, `test_fencing.py::SubmitFencingTests::test_rejected_tb_does_not_account_elapsed_time`. **Gaps:** the exact reply shape (`queued`, `submit_timing_seconds`) is not asserted beyond `ok`.
- **Determinism hazards.** Settings switch; `time.monotonic()` timings in the reply (non-deterministic by nature; exclude from golden comparison). With the switch on, propagation is also inline (`propagation_queue.deferral_enabled` returns False, `propagation_queue.py:149-160`, Q-4).
- **History.** bb61b6a (2026-07-27, via PR #31).
- **Ordering.** After the commit of Q-2.41..Q-2.45. Q-3 (async) or this step (sync) applies the job.

### Q-2.47 Release request parsing (all-or-nothing)
- **Statement.** `POST /api/release`: method (405) -> auth (403) -> caller machine = `POST['machine']` defaulting to the username, first 64 chars -> `leases` = JSON list (absent = `[]`). Refusals, each `400 {"error": "malformed: <msg>"}` and nothing released: not JSON (`leases is not JSON`); not a list (`leases must be a list`); more than 33 entries (`at most 33 leases per call`); an entry not an object; `kind` not in {`analysis` (default), `solve`}; `task_id` not an int, a bool, or outside (0, 2^63) (`invalid task_id`); `lease_token` or `machine` not strings. Each entry becomes `(kind, task_id, token, owner[:64])` with owner defaulting to the caller machine. An empty list is valid (`released: 0`).
- **Inputs / outputs.** Form fields `machine`, `leases`. No writes.
- **Code.** `atomicdb/views.py:RELEASE_MAX_LEASES:1796`, `_release_entries:1799`, `api_release:1855-1864`.
- **Tests.** `test_lease_release.py::ReleaseRefusalTests::test_a_malformed_list_is_refused_whole`, `::test_one_call_can_name_every_lease_a_machine_can_hold`, `::test_only_post_is_accepted`, `test_lease_release.py::ReleaseOwnLeasesTests::test_nothing_named_is_an_answer_too`, `::test_the_worker_protocol_needs_no_csrf_token`. **Gaps:** bool task_id; task_id >= 2^63.
- **Determinism hazards.** none.
- **History.** 6f38398 (2026-10-27, PR #87, "Let a stopping worker hand its leases back").
- **Ordering.** Before Q-2.48.

### Q-2.48 Release ownership filter and lock order
- **Statement.** Of the parsed entries, only those with a non-empty token whose owner shares the caller's physical machine are kept: `machine_base(x) = x.split('#', 1)[0]`; kept iff `token != ''` and `machine_base(caller) != ''` and `machine_base(owner) == machine_base(caller)` (so `bob#3` may release `bob#0`'s lease, but `bob-pc2` is another machine). A tokenless lease is never released here (the worker chooses its own machine name; only the token proves possession). Kept entries are sorted by the tuple `(kind, task_id, token, owner)` (so all `analysis` before all `solve`, each by ascending id), which fixes the lock order. The account that holds a lease is not checked; machine base plus exact token is the proof. Dropped entries are silently ignored (they still count in the log's `named`).
- **Inputs / outputs.** No writes; selects which rows Q-2.49 touches.
- **Code.** `atomicdb/views.py:api_release:1865-1867`, `machine_base:689`.
- **Tests.** `test_lease_release.py::ReleaseRefusalTests::test_another_machines_lease_is_left_alone`, `::test_a_lookalike_machine_is_another_machine`, `::test_a_wrong_or_stale_token_releases_nothing`, `::test_a_tokenless_lease_is_never_released`, `::test_a_solve_lease_of_another_machine_or_token_stays`, `test_lease_release.py::ReleaseOwnLeasesTests::test_every_slot_and_the_solver_go_back_in_one_call`. **Gaps:** a caller whose machine field is empty (base `''`) releases nothing.
- **Determinism hazards.** none (explicit sort).
- **History.** 6f38398 (PR #87).
- **Ordering.** After Q-2.47; before Q-2.49.

### Q-2.49 Release requeue (same row shape as an expired lease)
- **Statement.** In one transaction, for each kept entry in order: `analysis` -> `_requeue_leases(AnalysisTask where id, machine=owner, lease_token=token)` (Q-2.51); `solve` -> `ingest.requeue_solve_leases(SolveTask where id, machine=owner, lease_token=token)` (SOL-5.2). Both only touch rows still LEASED, so a COMPLETED, CANCELLED, PENDING or FAILED task, a stale token, or a repeated call is a no-op. No heartbeat freshness check: a live lease is released immediately. `attempts` is not changed (it was counted at lease time). Reply `200 {"ok": true, "released": <analysis released + solve released>}`. No DBEvent (log line only).
- **Inputs / outputs.** Writes `AnalysisTask/SolveTask.state=PENDING, machine='', lease_heartbeat_at=NULL, lease_token='', lease_session=''`.
- **Code.** `atomicdb/views.py:api_release:1868-1878, 1888-1892`; `_requeue_leases:961`; `atomicdb/ingest.py:requeue_solve_leases:8865`.
- **Tests.** `test_lease_release.py::ReleaseOwnLeasesTests::test_a_released_lease_is_back_in_the_queue_at_once`, `::test_the_row_is_left_exactly_as_the_recycler_leaves_an_expired_one`, `::test_releasing_twice_changes_nothing_the_second_time`, `::test_a_released_task_goes_to_the_next_worker_that_asks`, `::test_a_late_submit_of_a_released_task_is_refused`, `test_lease_release.py::ReleaseRefusalTests::test_finished_and_waiting_tasks_are_left_alone`, `test_lease_release.py::SharedRecyclerTests::test_the_solve_recycler_and_a_release_leave_the_same_row`. **Gaps:** none.
- **Determinism hazards.** Row-level locks of the UPDATEs in sorted order; concurrent submit of the same task races on the LEASED predicate (whichever commits first wins; the loser gets dup/not-leased or `released` excludes it).
- **History.** 6f38398 (PR #87).
- **Ordering.** After Q-2.48; Q-2.50 runs in the same transaction after the loop.

### Q-2.50 Release absorption and live-view cleanup
- **Statement.** If at least one analysis lease was released: `_absorb_requeued()` runs, which absorbs EVERY AnalysisTask row (not only the ones just released) that is `PENDING`, `machine=''`, `lease_heartbeat_at IS NULL`, `leased_at IS NOT NULL` (i.e. it had a lease and was requeued) and whose position status is not `UNKNOWN`: `state=COMPLETED, completed=now, nodes_searched=0` (EXP-5 `absorb_tasks`; never CANCELLED, which is reserved for a requester's withdrawal). Then for each released (task_id, owner): `WorkerPing where machine=owner, user=<caller username>, current_task_id=task_id` gets `current_task_id=NULL, last_nps=0, nps_updated=NULL`. Solve releases trigger neither step.
- **Inputs / outputs.** Reads `Position.status`; writes `AnalysisTask.state, completed, nodes_searched`; `WorkerPing.current_task_id, last_nps, nps_updated`.
- **Code.** `atomicdb/views.py:api_release:1879-1887`, `_absorb_requeued:978`; `atomicdb/ingest.py:absorb_tasks:5770`.
- **Tests.** `test_lease_release.py::ReleaseOwnLeasesTests::test_a_task_on_a_closed_position_is_absorbed_as_the_recycler_does`, `::test_the_machine_stops_showing_the_task_it_gave_back`. **Gaps:** the global scope of `_absorb_requeued` (unrelated requeued rows on closed positions absorbed by someone else's release); WorkerPing of a slot whose ping belongs to another account is not cleared.
- **Determinism hazards.** Global unordered UPDATE over a predicate that depends on what other processes requeued (`views.py:990-992`); `timezone.now()` in absorption.
- **History.** 6f38398 (PR #87); `_absorb_requeued` shared with part 1's recycler.
- **Ordering.** After Q-2.49 inside the same transaction.

### Q-2.51 Analysis requeue field contract (shared helper)
- **Statement.** `_requeue_leases(qs)` = `qs.filter(state='LEASED').update(state='PENDING', machine='', lease_heartbeat_at=NULL, lease_token='', lease_session='')`, returns the count. `leased_at` and `attempts` are kept: a PENDING row with a non-null `leased_at` "has had a lease", which is how `_absorb_requeued` finds requeued rows and why such a task is never again leased to a tokenless build (part 1, `choose_pending`). It is the single way an analysis lease ends without a result, used by part 1's recycler (`api_lease`) and by Q-2.49; the engine-gate refusal (Q-2.40) writes the identical field set with its own CAS instead of calling the helper.
- **Inputs / outputs.** Writes `AnalysisTask.state, machine, lease_heartbeat_at, lease_token, lease_session`.
- **Code.** `atomicdb/views.py:_requeue_leases:961`.
- **Tests.** `test_lease_release.py::ReleaseOwnLeasesTests::test_the_row_is_left_exactly_as_the_recycler_leaves_an_expired_one`, `tests.py::LeaseReclaimTests::test_recycled_task_waits_for_token_capable_worker` (part 1). **Gaps:** none.
- **Determinism hazards.** none beyond the caller's.
- **History.** Extracted as a shared helper in 6f38398 (PR #87).
- **Ordering.** Called by Q-2.49 and by part 1's recycler (Q-2.1..29).

### Q-2.52 Solve acquire: request handling and max budget
- **Statement.** `POST /api/solve/acquire` (no method check). Auth (403) -> `max_budget` (`reported_max_budget`): absent or `""` -> None (no cap); present but not an int or <= 0 -> `400 {"error": "max_budget must be a positive whole number of nodes"}` (checked BEFORE the capacity touch) -> capacity touch (Q-2.31) -> machine = `ping.machine`; `lease_session` = first 64 chars of `POST['lease_session']` (may be empty). Everything else runs in one transaction (Q-2.53..Q-2.57).
- **Inputs / outputs.** Form fields `max_budget`, `machine`, `lease_session`, capacity fields.
- **Code.** `atomicdb/views.py:api_solve_acquire:1913-1931`, `reported_max_budget:551`, `BadMaxBudget:547`.
- **Tests.** `test_max_budget.py::SolveCapTests::test_an_unusable_cap_is_refused`. **Gaps:** none.
- **Determinism hazards.** none.
- **History.** Solve protocol: 38f086a (2026-07-27, via PR #31); `max_budget`: 8736cd0 (2026-11-17, PR #278).
- **Ordering.** Before Q-2.53.

### Q-2.53 Solve acquire: in-request stale sweep
- **Statement.** First statement of the acquire transaction: `now = timezone.now()`, then `ingest.recycle_stale_solve_leases(now=now)` (SOL-5.1..SOL-5.3) frees every solve lease, of any machine, silent for more than 20 minutes, and emits `SOLVE_LEASE_RECYCLED` if it freed any. The window `stale = now - 20 min` is also used by Q-2.54.
- **Inputs / outputs.** See SOL-5.1.
- **Code.** `atomicdb/views.py:api_solve_acquire:1931-1936`.
- **Tests.** Covered by SOL-5 tests; `test_lease_position_lock.py::LeaseWithThePositionLockedTests::test_a_proof_task_is_leased_while_the_ingest_holds_its_position`. **Gaps:** recycling triggered from the acquire path specifically.
- **Determinism hazards.** Wall clock (`views.py:1932`).
- **History.** e39a8dc (2026-08-29, PR #48) moved the sweep into `ingest` and added the periodic caller (SOL-5.4).
- **Ordering.** Before Q-2.54.

### Q-2.54 Solve acquire: replay of the live assignment (same session)
- **Statement.** The machine's live solve lease is `SolveTask where state='LEASED', machine=ping.machine, lease_heartbeat_at >= now - 20 min`, ordered by `(leased_at, id)`, first row, locked `FOR UPDATE OF self` (task only). If it exists and the request's `lease_session` is non-empty and equals the row's `lease_session` (constant-time), the same assignment is returned again, `200 {"tasks": [payload]}` with the same token, without incrementing `attempts`, without changing `leased_at` and without refreshing `lease_heartbeat_at` (covers a response lost after commit). An empty session never replays.
- **Inputs / outputs.** Reads `SolveTask.state, machine, lease_heartbeat_at, leased_at, lease_session`. No writes on replay.
- **Code.** `atomicdb/views.py:api_solve_acquire:1940-1950`.
- **Tests.** `test_solve.py::SolveProtocolTests::test_the_same_session_replays_its_assignment` (same task, same token, attempts stays 1), `test_lease_position_lock.py::LeaseWithThePositionLockedTests::test_a_replayed_lease_does_not_wait_for_its_position` (analysis analogue). **Gaps:** replay does not refresh the heartbeat.
- **Determinism hazards.** Wall clock window; `.first()` with explicit order (deterministic).
- **History.** 38f086a (2026-07-27, via PR #31).
- **Ordering.** After Q-2.53; Q-2.55 if the session differs.

### Q-2.55 Solve acquire: one live lease per machine identity, restart relief
- **Statement.** If the machine has a live solve lease (Q-2.54) and the session does not match (or is empty): let `beat = lease_heartbeat_at or leased_at`; if `beat` is null or `beat >= now - 3 min` (`RESTART_RECYCLE_MINUTES`), the request gets `200 {"tasks": []}` (a machine identity holds at most one live solve lease; `--jobs` slots `base#k` are distinct identities). Otherwise the holder is treated as a dead predecessor of a restarted worker: `UPDATE SolveTask SET state='PENDING', machine='', lease_heartbeat_at=NULL, lease_token='', lease_session='' WHERE id=active.id AND state='LEASED'` (`attempts` and `leased_at` kept; no DBEvent) and the same request falls through to selection (Q-2.56), where it may well receive that same task again (it still holds the row lock, so `skip_locked` does not skip it).
- **Inputs / outputs.** Writes `SolveTask.state, machine, lease_heartbeat_at, lease_token, lease_session` on relief.
- **Code.** `atomicdb/views.py:api_solve_acquire:1951-1965`, `RESTART_RECYCLE_MINUTES:79`.
- **Tests.** `test_bug_hunt_findings.py::SolveLeaseRestartTests::test_a_restarted_solver_is_relieved_like_an_analysis_worker`. **Gaps:** fresh holder + other session -> empty list; relief returning the same task.
- **Determinism hazards.** Wall clock (3-minute threshold against the stored heartbeat).
- **History.** cf6ce53 / 0ca4fb8 (2026-07-29, via PR #31), mirroring the analysis restart relief after the t24 incident.
- **Ordering.** After Q-2.54; before Q-2.56.

### Q-2.56 Solve acquire: selection order
- **Statement.** Candidates: `SolveTask where state='PENDING'` and, if `max_budget` is given, `budget_nodes <= max_budget`. Locked with `SELECT ... FOR UPDATE SKIP LOCKED OF self`. Order: `is_debt` ascending (1 when `arm == ingest.DEBT_ARM` = `'debt'`, else 0: everything that is not certification debt first), then `budget_nodes` descending, then `id` ascending; first row. No filter on the position's status (debt tasks are on closed positions by design; a solve task on a position closed meanwhile is still served), no attempts cap, no per-account fairness. No candidate -> `200 {"tasks": []}`.
- **Inputs / outputs.** Reads `SolveTask.state, budget_nodes, arm, id`.
- **Code.** `atomicdb/views.py:api_solve_acquire:1967-1982`.
- **Tests.** `test_engine_debt.py::DebtIsServedLastTests::test_a_request_outranks_the_debt_even_though_it_is_bigger`, `::test_the_debt_is_served_once_nothing_else_is_waiting`, `test_max_budget.py::SolveCapTests::test_a_capped_solver_takes_the_largest_proof_that_fits`, `::test_with_no_proof_that_fits_the_solver_gets_none`, `::test_an_uncapped_solver_takes_the_largest_as_before`, `test_lease_position_lock.py::LeaseWithThePositionLockedTests::test_a_proof_task_is_leased_while_the_ingest_holds_its_position`. **Gaps:** tie-break by id; serving a task whose position closed.
- **Determinism hazards.** `skip_locked` makes the pick depend on concurrent transactions (`views.py:1975`); otherwise fully ordered.
- **History.** Debt last: 204d17e (2026-07-28, via PR #31); cap filter: 8736cd0 (PR #278).
- **Ordering.** After Q-2.55; before Q-2.57.

### Q-2.57 Solve acquire: assignment and payload
- **Statement.** The picked row is saved with `state='LEASED'`, `machine=ping.machine`, `leased_at = lease_heartbeat_at = now` (one timestamp), `lease_token = secrets.token_urlsafe(32)` (43 URL-safe chars), `lease_session = <request session>`, `attempts = attempts + 1`. Reply `200 {"tasks": [{"id", "fen": position.fen, "goal", "stage": budget_stage, "budget_nodes", "ruleset": logic.RULESET_ID, "lease_token"}]}`. Exactly one task per acquire.
- **Inputs / outputs.** Writes `SolveTask.state, machine, leased_at, lease_heartbeat_at, lease_token, lease_session, attempts`.
- **Code.** `atomicdb/views.py:api_solve_acquire:1983-1992`, `_solve_payload:1995`.
- **Tests.** `test_solve.py::SolveProtocolTests::test_acquire_leases_and_submit_closes` (goal, ruleset), `test_engine_debt.py::DebtIsServedLastTests::test_the_debt_is_served_once_nothing_else_is_waiting` (stage F0). **Gaps:** attempts increment on a fresh lease (only replay asserts attempts 1).
- **Determinism hazards.** Randomness of the token (`views.py:1986`); wall clock.
- **History.** 38f086a (via PR #31).
- **Ordering.** Last step of acquire.

### Q-2.58 Solve heartbeat (strict token)
- **Statement.** `POST /api/solve/heartbeat`: method (405) -> auth (403) -> capacity touch -> `task_id` int else `400 {"error": "invalid task_id"}` -> `UPDATE SolveTask SET lease_heartbeat_at=now WHERE id=task_id AND state='LEASED' AND machine=ping.machine AND lease_token=POST['lease_token']` (exact equality: unlike Q-2.33 there is no tokenless allowance; an empty token only matches an empty one). 1 row -> `200 {"ok": true, "current_task_id": task_id}`; else `409 {"error": "stale-lease"}`. WorkerPing's `current_task_id`/nps are not touched.
- **Inputs / outputs.** Writes `SolveTask.lease_heartbeat_at`.
- **Code.** `atomicdb/views.py:api_solve_heartbeat:2008-2025`.
- **Tests.** `test_solve.py::SolveProtocolTests::test_heartbeat_keeps_the_lease_and_rejects_a_stranger`, `test_solve.py::SolveWorkerLeaseTests::test_the_solve_thread_beats_for_the_task_it_is_proving` (worker side). **Gaps:** invalid task_id 400.
- **Determinism hazards.** Wall clock (`views.py:2022`).
- **History.** 38f086a (via PR #31).
- **Ordering.** Independent; keeps the lease out of SOL-5.1 and Q-2.55.

### Q-2.59 Solve submit: validation and fencing precedence
- **Statement.** `POST /api/solve/submit` (no method check). Auth (403) -> `task_id` int else `400 {"error": "malformed: task_id"}` -> `outcome` must be one of `PROVED`, `DISPROVED`, `UNKNOWN` else `400 {"error": "malformed: outcome"}` (so `DISPROVED_WHITE_WIN`, which `apply_solve_result` understands, cannot arrive through this endpoint) -> unlocked read: unknown -> `400 {"error": "malformed: unknown task"}`; `state == COMPLETED` -> `200 {"ok": true, "dup": true}` BEFORE any machine/token check (any authenticated account gets dup for any completed id); state not LEASED (PENDING, FAILED) -> `400 {"error": "not-leased"}`; `POST['machine']` empty or != `task.machine` -> `409 {"error": "not-your-lease"}`; `task.lease_token` non-empty and != `POST['lease_token']` (constant-time) -> `409 {"error": "stale-lease"}`. These checks are NOT repeated under a lock (see Q-2.61).
- **Inputs / outputs.** Reads `SolveTask.state, machine, lease_token`.
- **Code.** `atomicdb/views.py:api_solve_submit:2029-2056`.
- **Tests.** `test_solve.py::SolveProtocolTests::test_a_stale_token_cannot_submit`, `::test_another_machine_cannot_submit`, `::test_a_second_submit_is_a_duplicate_not_a_reclosure`. **Gaps:** FAILED task -> not-leased; invalid outcome 400; dup returned to a foreign account.
- **Determinism hazards.** Unlocked read under READ COMMITTED (`views.py:2045`).
- **History.** 38f086a (via PR #31).
- **Ordering.** Before Q-2.60.

### Q-2.60 Solve submit: certificate cap, telemetry and advisory fields
- **Statement.** The uploaded file `certificate` is read up to `MAX_SOLVE_CERTIFICATE_BYTES + 1` bytes; more than the cap -> `413 {"error": "certificate too large"}` before any parsing. Fortress telemetry: for each name in (`tt_hit`, `quiet_scc`, `reset_rate`, `stagnation`, `score`) the field `fortress_<name>`, if present and non-empty, is parsed as float and clamped with `max(0.0, min(1e9, x))` (unparsable entries are skipped; `inf` -> 1e9; NaN -> 1e9 under Python's min/max semantics); an empty dict becomes None. `pn`, `dn`, `nodes` parsed as int or None (`searched_nodes = nodes or 0`); `elapsed` passed raw (clamped to [0, 86400] by `apply_solve_result`). `trusted_submitter = user.is_staff or username in settings.ATOMICDB_TB_TRUSTED`. `solver_build = _sha_field(solver_build) or solver_build[:64]` (any string up to 64 chars is kept). Telemetry and advisory numbers never change a verdict (scheduling hints only, SOL-3).
- **Inputs / outputs.** Form fields `certificate` (file), `fortress_*`, `pn`, `dn`, `nodes`, `elapsed`, `solver_build`. Passed to `apply_solve_result`.
- **Code.** `atomicdb/views.py:api_solve_submit:2058-2094`, `MAX_SOLVE_CERTIFICATE_BYTES:1909`.
- **Tests.** `test_solve.py::SolveProtocolTests::test_an_oversized_certificate_is_refused_before_parsing`, `test_engine_debt.py::TelemetryIsAdvisoryTests::test_the_fortress_indicators_are_stored`, `::test_garbage_telemetry_is_dropped_not_stored`, `::test_a_perfect_fortress_score_still_closes_nothing`, `test_survive_fleet.py::FortressClassifierTests::test_it_fires_on_telemetry_that_travelled_the_real_wire`. **Gaps:** NaN/inf handling; `trusted_submitter` via `ATOMICDB_TB_TRUSTED` at this boundary; non-sha `solver_build` kept.
- **Determinism hazards.** Settings `ATOMICDB_TB_TRUSTED`; float edge semantics.
- **History.** Telemetry: 204d17e (2026-07-28, via PR #31).
- **Ordering.** After Q-2.59; before Q-2.61.

### Q-2.61 Solve submit: task state transitions (apply_solve_result boundary)
- **Statement.** `ingest.apply_solve_result` (SOL-2) verifies the certificate outside the transaction (only when outcome is PROVED, or the unreachable-here DISPROVED_WHITE_WIN), then in one transaction locks the row `SELECT ... FOR UPDATE` and: if `state == COMPLETED` -> returns `{"dup": true}` (reply 200 with that summary); otherwise writes `outcome, advisory_pn, advisory_dn, searched_nodes=max(0,int), elapsed_seconds (clamped [0,86400]), solver_build[:64], telemetry (if any), completed=now`, and: PROVED without a verifying certificate (missing blob or verifier error) -> `state='FAILED'`, `verified=False`, `reject_reason=reason[:2000]`, DBEvent `SOLVE_REJECTED {task, key, reason[:500], machine}`, reply summary `{"rejected": true, "reason": reason[:200]}`; otherwise -> `state='COMPLETED'`, `verified`, `reject_reason=''` and, if verified, the certificate columns; closure/upgrade/dispute effects are SOL-2/REV-4. The locked re-check covers only COMPLETED: it does not re-check `state == LEASED`, `machine` or `lease_token`, so a submitter that passed Q-2.59 on a stale snapshot still completes a task that was recycled (PENDING) or re-leased to another machine meanwhile (the row's `machine` is not rewritten; the new holder later gets `dup`). FAILED and COMPLETED are terminal: nothing in this part moves them back to PENDING. Lease columns are not cleared on completion.
- **Inputs / outputs.** Writes `SolveTask.state, outcome, verified, reject_reason, advisory_pn, advisory_dn, searched_nodes, elapsed_seconds, solver_build, telemetry, completed, certificate*`; DBEvents `SOLVE_REJECTED`, `SOLVE_VERIFIED`, `SURVIVE_VERIFIED` (SOL-2).
- **Code.** `atomicdb/views.py:api_solve_submit:2087-2095`; `atomicdb/ingest.py:apply_solve_result:8723` (lock 8766, dup 8767, FAILED 8778-8786, COMPLETED 8788-8801).
- **Tests.** `test_solve.py::SolveProtocolTests::test_acquire_leases_and_submit_closes`, `::test_a_second_submit_is_a_duplicate_not_a_reclosure`. **Gaps:** FAILED transition through the endpoint; submit racing a recycle/re-lease (no fence under lock).
- **Determinism hazards.** Verifier wall time measured with `time.monotonic()` (event payload only); unlocked pre-checks vs locked apply (READ COMMITTED race, `views.py:2045` vs `ingest.py:8766`); `timezone.now()` for `completed`.
- **History.** 38f086a (2026-07-27, via PR #31).
- **Ordering.** After Q-2.60. Closure effects (SOL-2) then `backup_cascade` / `backup_backed_evals` on the position when verified or disputed.

### SOL-5.1 Stale solve-lease predicate and window
- **Statement.** `recycle_stale_solve_leases(now=None, minutes=20)`: `stale = (now or timezone.now()) - 20 min`; a SolveTask is stale iff `state='LEASED'` (applied inside the helper) and `leased_at < stale` and (`lease_heartbeat_at IS NULL` or `lease_heartbeat_at < stale`). Both conditions are strict `<`. Those rows are requeued with SOL-5.2 across all machines. Returns the count. Distinct from the analysis window (`LEASE_MINUTES` = 4, part 1).
- **Inputs / outputs.** Reads `SolveTask.state, leased_at, lease_heartbeat_at`; writes via SOL-5.2.
- **Code.** `atomicdb/ingest.py:SOLVE_LEASE_MINUTES:8862`, `recycle_stale_solve_leases:8878`.
- **Tests.** `test_engine_debt.py::RecycleStaleSolveLeasesTests::test_a_lease_with_a_dead_heartbeat_goes_back_to_the_queue`, `::test_a_live_lease_is_left_alone`, `::test_the_recycled_row_is_servable_again`. **Gaps:** a lease leased > 20 min ago with a fresh heartbeat is kept (implied by "live lease" test only if it sets leased_at old; boundary equality not pinned).
- **Determinism hazards.** Wall clock (`ingest.py:8903`); unordered bulk UPDATE.
- **History.** Moved out of `api_solve_acquire` into `ingest` in e39a8dc (2026-08-29, PR #48) after a lease sat LEASED for 22 days with no solver polling.
- **Ordering.** Before SOL-5.3.

### SOL-5.2 Solve requeue field contract
- **Statement.** `requeue_solve_leases(qs)` = `qs.filter(state='LEASED').update(state='PENDING', machine='', lease_heartbeat_at=NULL, lease_token='', lease_session='')`, returns the count. `attempts`, `leased_at`, `budget_*`, `arm` stay. The single way a proof lease ends without a result (sweep and release); the restart relief (Q-2.55) writes the same field set inline.
- **Inputs / outputs.** Writes `SolveTask.state, machine, lease_heartbeat_at, lease_token, lease_session`.
- **Code.** `atomicdb/ingest.py:requeue_solve_leases:8865`.
- **Tests.** `test_lease_release.py::SharedRecyclerTests::test_the_solve_recycler_and_a_release_leave_the_same_row`. **Gaps:** none.
- **Determinism hazards.** none.
- **History.** 6f38398 (PR #87) extracted it for `api_release`.
- **Ordering.** Called by SOL-5.1 and Q-2.49.

### SOL-5.3 SOLVE_LEASE_RECYCLED event
- **Statement.** When SOL-5.1 freed at least one row it creates DBEvent `SOLVE_LEASE_RECYCLED` with payload `{"freed": <count>, "minutes": int(minutes)}`; a pass that frees nothing writes no event.
- **Inputs / outputs.** DBEvent `SOLVE_LEASE_RECYCLED`.
- **Code.** `atomicdb/ingest.py:recycle_stale_solve_leases:8908-8910`.
- **Tests.** `test_engine_debt.py::RecycleStaleSolveLeasesTests::test_a_clean_pass_writes_no_event`, `::test_recycling_says_so_in_the_diary`. **Gaps:** none.
- **Determinism hazards.** DBEvent `ts` is `auto_now_add` (wall clock).
- **History.** e39a8dc (PR #48).
- **Ordering.** After SOL-5.1 in the same call.

### SOL-5.4 Who runs the solve recycler, and when
- **Statement.** Two callers: (a) every `/api/solve/acquire` at the start of its transaction (Q-2.53); (b) the periodic selector cycle `refresh_selector`, step `solve-leases`, which runs after the `priorities` step and BEFORE the arms that count their pending SolveTask quota (`debt`, `coverage`, adversarial arms; part 6), so a frozen lease does not occupy quota. With no solver polling, (b) is the only clock that frees leases.
- **Inputs / outputs.** As SOL-5.1..3.
- **Code.** `atomicdb/management/commands/refresh_selector.py:302` (`step('solve-leases', ingest.recycle_stale_solve_leases)`); `atomicdb/views.py:api_solve_acquire:1936`.
- **Tests.** `test_selector_service.py::RefreshSelectorCommandTests::test_stale_solve_leases_are_recycled_before_queue_caps`. **Gaps:** none.
- **Determinism hazards.** Cadence is the service interval (wall clock); results depend on when the cycle runs relative to heartbeats.
- **History.** e39a8dc (2026-08-29, PR #48).
- **Ordering.** Before Q-8 debt/coverage/dn/fragile enqueue in the same cycle (part 6); after PRI-3 refresh (part 3).

### Q-9.1 Engine policy file: mode and extra engines (fail-open)
- **Statement.** The policy is read from settings `ATOMICDB_ENGINE_POLICY_FILE` (default `/etc/openbench/atomicdb-engine-policy.json`). Default policy `{"mode": "off", "extra_engines": {}}`. Any of: empty path, missing file, unreadable file, invalid JSON -> `off`. If the JSON is an object: `mode` becomes `enforce` only if exactly the string `"enforce"` (anything else, including unknown modes, stays `off`); `extra_engines`, if an object, becomes `{sha.lower(): str(note)}` keeping only keys that are 64-char lowercase hex after lowering. Parsed policies are cached per process by path with stamp `(st_size, st_mtime_ns)`; a changed stamp re-reads the file. A JSON array or other non-object is `off`.
- **Inputs / outputs.** Reads the policy file and settings. No DB writes.
- **Code.** `atomicdb/engine_policy.py:DEFAULT_POLICY_FILE:31`, `_policy_path:47`, `_is_sha256:57`, `load_policy:62`, `_policies:44`.
- **Tests.** `test_engine_policy.py::EngineGateTests::test_the_gate_is_off_without_a_policy_file`, `::test_a_broken_or_unknown_policy_reads_as_off`, `::test_an_extra_engine_listed_in_the_policy_is_accepted`. **Gaps:** stamp-based reload of an edited policy file; uppercase sha keys normalised.
- **Determinism hazards.** File on disk; per-process module cache keyed on mtime (a rewrite within the same mtime granularity and same size is not seen); every web process decides independently.
- **History.** 67c1c56 (2026-10-15, PR #66).
- **Ordering.** Read by Q-9.3/Q-9.4 at each submit.

### Q-9.2 Reference engines discovery
- **Statement.** `reference_engines()` scans settings `ATOMICDB_ENGINES_DIR` (default `/var/www/atomicdb-engines`), non-recursively; every entry whose name starts with `Atomic-Stockfish-` and is a regular file contributes `{sha256(file contents): file name}`. Unreadable directory or entries -> skipped (empty map if the directory cannot be listed). Digests are cached per process by `(path, size, mtime_ns)`; a replaced binary is hashed again. Retiring a build = deleting its file.
- **Inputs / outputs.** Reads the engines directory. No DB writes.
- **Code.** `atomicdb/engine_policy.py:DEFAULT_ENGINES_DIR:32`, `REFERENCE_PREFIX:33`, `_engines_dir:52`, `_file_digest:99`, `reference_engines:114`, `_digests:42`.
- **Tests.** `test_engine_policy.py::EngineGateTests::test_current_and_previous_reference_builds_are_accepted`, `::test_other_files_in_the_engines_directory_are_not_engines`, `::test_a_replaced_binary_is_hashed_again`. **Gaps:** symlinked binaries (`is_file()` follows symlinks).
- **Determinism hazards.** Filesystem contents and `os.scandir` order (order irrelevant: result is a map; duplicate digests keep the last name scanned, description text only).
- **History.** 67c1c56 (PR #66).
- **Ordering.** Used by Q-9.3 and Q-9.4.

### Q-9.3 Accepted engine set
- **Statement.** `accepted_engines(policy)` = `{sha: "reference: <file name>"}` for every reference binary, then `extra_engines` entries added with `"extra: <note>"` only where the sha is not already present (reference description wins). The management command `engine_gate_report` prints this set and marks which recent submissions the gate would refuse (reporting only, no writes).
- **Inputs / outputs.** None written.
- **Code.** `atomicdb/engine_policy.py:accepted_engines:136`; `atomicdb/management/commands/engine_gate_report.py:28-31`.
- **Tests.** `test_engine_policy.py::EngineGateTests::test_the_report_marks_what_the_gate_would_refuse`, `::test_an_extra_engine_listed_in_the_policy_is_accepted`. **Gaps:** none.
- **Determinism hazards.** As Q-9.1/Q-9.2.
- **History.** 67c1c56 (PR #66).
- **Ordering.** Used by Q-9.4.

### Q-9.4 Gate decision (`rejects`)
- **Statement.** `rejects(engine_sha, tablebase)` returns, in this order: False if `tablebase` (a submit carrying `tb_wdl`); False if the policy mode is not `enforce`; False if no reference binary can be found (directory missing or empty: fail-open); otherwise True iff the (cleaned, Q-2.37) `engine_sha` is not in the accepted set. An empty or malformed sha is therefore refused while enforcing. `enforcing()` = mode is `enforce`. While off, nothing is hashed. The gate applies only to analysis submits (Q-2.40); solve submits, heartbeats and leases are never gated.
- **Inputs / outputs.** Boolean consumed by Q-2.40.
- **Code.** `atomicdb/engine_policy.py:rejects:150`, `enforcing:146`.
- **Tests.** `test_engine_policy.py::EngineGateTests::test_a_missing_sha_is_refused_while_enforcing`, `::test_tablebase_results_are_never_gated`, `::test_no_reference_binary_lets_everything_through`, `::test_a_foreign_engine_is_refused_and_the_task_goes_back`, `::test_another_machine_cannot_push_a_lease_back_with_a_bad_sha`. **Gaps:** none.
- **Determinism hazards.** Files on disk and per-process caches (Q-9.1/Q-9.2); the verdict for the same payload can differ between processes and over time.
- **History.** 67c1c56 (PR #66).
- **Ordering.** Inside Q-2.40, after Q-2.38.

---

## Part 5: Ingest queue and propagation queue

Scope: `atomicdb/ingest_queue.py`, `atomicdb/propagation_queue.py`, `ingest.absorb_tasks` / `ingest.is_absorbed`, the
`IngestJob` and `PropagationSeed` models and their indexes, and the commands `process_ingest_queue`,
`process_propagation`, `absorb_shadowed_tasks`. All paths are relative to the repository root. Line numbers
are those of the checkout on 2026-11-28.

Context a porter needs first. A worker result reaches the tree in up to three stages:

1. **Submit** (`views.api_submit`, part 2, Q-2.3x): compare-and-swap of the task `LEASED -> COMPLETED`, stamping of the
   task, and `ingest_queue.enqueue` of the raw payload, all in ONE transaction.
2. **Stage (a), the job** (`ingest_queue.apply_job`, run by `process_ingest_queue`, 5 processes in production): the
   result is applied to its own position (`ingest.ingest_analysis`, EXP-2) and the job goes `DONE` in the same
   transaction.
3. **Stage (b), the climb** (`ingest.propagate_landings`, EXP-6): inline inside stage (a) when deferral is off; with
   `ATOMICDB_DEFER_PROPAGATION` on, stage (a) writes a `PropagationSeed` and ONE drainer (`process_propagation`) climbs
   batches of seeds.

v2 (plan section 2.2) folds all three into one writer batch; the rules below are what that batch must reproduce
(values, closures, task states, notifications, summaries), plus the v1 mechanics that the shadow comparison will see.

---

### Q-3.1 IngestJob record, states and the one-job-per-task invariant
- **Statement.** An `IngestJob` is the durable raw result of one delivered analysis task. There is at most one job per
  `AnalysisTask` (OneToOne `task`, enforced by the database unique constraint of the one-to-one column). A job has
  exactly one of four states: `PENDING` (waiting, possibly in backoff), `PROCESSING` (claimed by a processor),
  `DONE` (applied; terminal), `FAILED` (5 failed attempts; parked until a manual requeue). `payload` is the raw JSON as
  the submit built it and is never modified after creation; `summary` is written when the job is applied and later
  merged into (keys `propagated`, `carried`, `then_verify`). Deleting the task or the position deletes the job
  (`on_delete=CASCADE` on both FKs). Jobs are never deleted by the queue: `DONE` rows (6.2M in production) are the
  history that the v2 whole-history replay reads (plan section 7.3).
- **Inputs / outputs.** `IngestJob.task` (OneToOne AnalysisTask, related_name `ingest_job`), `position` (FK Position),
  `payload` (JSON), `state` (char 10, default `PENDING`, db_index), `attempts` (int, default 0), `next_attempt_at`
  (datetime NULL, db_index), `claimed_at` (datetime NULL), `last_error` (text, default ''), `summary` (JSON NULL),
  `created` (auto_now_add, db_index), `updated` (auto_now). Partial index `atomic_ingest_open_by_id` on `(id)` WHERE
  `state IN ('PENDING','PROCESSING')`.
- **Code.** `atomicdb/models.py:IngestJob:665-713` (index 702-710); `atomicdb/migrations/0016_ingest_queue.py`
  (table, original index `atomic_ingest_ready (state, next_attempt_at, id)`);
  `atomicdb/migrations/0073_ingestjob_open_by_id.py` (partial index, `CREATE INDEX CONCURRENTLY IF NOT EXISTS`,
  `atomic = False`); `atomicdb/migrations/0075_drop_unused_indexes.py:60,117` (drops `atomic_ingest_ready`).
- **Tests.** `test_ingest_queue.py::OpenJobsIndexTests::test_index_covers_exactly_the_states_the_claim_reads`,
  `test_ingest_queue.py::OpenJobsIndexTests::test_migration_creates_the_index`,
  `test_ingest_queue.py::SubmitEnqueueTests::test_a_submit_acknowledges_and_leaves_the_work_queued`.
  **Gaps:** no test pins the CASCADE deletion of a job with its task or position; no test asserts that `payload` is
  immutable after a successful apply (only after failures: `test_a_failure_retries_with_backoff_and_then_parks`).
- **Determinism hazards.** `created`/`updated` are wall-clock stamps (not read by any value rule; `created` feeds the
  submit-to-climbed latency metric only). The index is a performance artefact, not semantics.
- **History.** Created by bb61b6a "Take the AtomicDB ingest out of the worker's request" (2026-07-27). Partial index
  added by hand in production on 23-Nov-2026 (claims took 4.6-30 s over 4.9M rows), then as migration 0073; the old
  composite index dropped in 0075.
- **Ordering.** Created by Q-3.2 (inside the submit transaction, part 2). Consumed by Q-3.3/Q-3.4.

### Q-3.2 Enqueue in the submit transaction
- **Statement.** `enqueue(task, payload)` does `get_or_create(task=task, defaults={position_id: task.position_id,
  payload, next_attempt_at: now})`. The caller owns the transaction: the submit runs it inside the same `atomic()`
  that moved the task `LEASED -> COMPLETED` (compare-and-swap on id, state, machine, attempts, leased_at,
  lease_token), so either the task is claimed AND the payload is durable, or neither. Because a `COMPLETED` task is
  never leased again, no lease can expire while its result waits. Since the CAS succeeds at most once per lease,
  `created` is always True in practice; a replayed submit is answered `{'ok': True, 'dup': True}` by the CAS branch
  before reaching `enqueue` (part 2). Payload written by the submit: `lines` (list of line dicts as received),
  `nodes` (int, already clamped to `min(reported, 2 * task.budget_nodes)`), `elapsed` (float seconds),
  `machine`, `username` (authenticated account), `tb_wdl` (int or None), `tb_dtz` (int or None), `restricted`
  (bool: the request carried a non-empty `searchmoves`).
- **Inputs / outputs.** Reads `AnalysisTask.position_id`. Writes one `IngestJob` row (state `PENDING`, attempts 0,
  `next_attempt_at = now`). No DBEvent.
- **Code.** `atomicdb/ingest_queue.py:enqueue:68-74`; caller `atomicdb/views.py:api_submit:1669-1707` (CAS 1670-1686,
  clamp 1692, enqueue 1693-1707).
- **Tests.** `test_ingest_queue.py::SubmitEnqueueTests::test_a_submit_acknowledges_and_leaves_the_work_queued`,
  `test_ingest_queue.py::SubmitEnqueueTests::test_the_submit_itself_does_not_touch_the_tree`,
  `test_ingest_queue.py::SubmitEnqueueTests::test_the_claim_and_the_enqueue_are_one_commit`,
  `test_ingest_queue.py::SubmitEnqueueTests::test_a_replayed_submit_is_still_a_duplicate`,
  `test_fencing.py::SubmitFencingTests::test_reported_nodes_are_clamped_to_twice_budget`,
  `test_fencing.py::SubmitFencingTests::test_nodes_use_authoritative_budget_after_claim`.
  **Gaps:** the exact payload key set (`restricted`, `tb_dtz`) is not asserted as a whole by any test.
- **Determinism hazards.** `next_attempt_at = timezone.now()` (line 73). The payload's `elapsed` is worker-reported
  wall time (only accounting, Q-3.15).
- **History.** bb61b6a (2026-07-27). `restricted` added with the searchmoves pass (c024cea); `tb_dtz` with the DTZ
  protocol; the 2x clamp is part 2's.
- **Ordering.** After the submit's CAS and validation (part 2, Q-2.3x, including the engine gate Q-9); before Q-3.4.
  With the synchronous switch (Q-3.22) Q-3.7 runs right after the commit in the same request.

### Q-3.3 Ready set of the ingest queue
- **Statement.** A job is ready when (`state = PENDING` and `next_attempt_at <= now`) or (`state = PENDING` and
  `next_attempt_at IS NULL`) or (`state = PROCESSING` and `claimed_at < now - 900 s`). Ready jobs are ordered by `id`
  ascending (arrival order). `CLAIM_STALE_SECONDS = 900` (15 minutes): a processor that died mid-job leaves its row
  `PROCESSING`; because its apply transaction rolled back, nothing is half-written and the row may be taken again.
  `FAILED` and `DONE` rows are never ready.
- **Inputs / outputs.** Reads `IngestJob.state`, `next_attempt_at`, `claimed_at`, `id`. Writes nothing.
- **Code.** `atomicdb/ingest_queue.py:ready_jobs:77-87`; constant `CLAIM_STALE_SECONDS:53`.
- **Tests.** `test_ingest_queue.py::ProcessorTests::test_a_backoff_keeps_a_job_out_of_the_queue_until_it_is_due`,
  `test_ingest_queue.py::ProcessorTests::test_a_stale_claim_is_recovered`,
  `test_queue_control.py::DuplicateNotificationTests::test_a_second_pass_does_not_duplicate_anybody`.
  **Gaps:** the `next_attempt_at IS NULL` branch (a PENDING row with no due time) is not tested on its own.
- **Determinism hazards.** Wall clock: `now = timezone.now()` and the 900 s window (lines 78-79). A job whose
  expensive preparation (Q-3.8, outside any lock) runs longer than 900 s can be claimed by a second processor while the
  first still works on it; both then apply, and Q-3.10 makes the second a no-op.
- **History.** bb61b6a (2026-07-27).
- **Ordering.** Read by Q-3.4.

### Q-3.4 Claiming a job
- **Statement.** `claim_next()` opens a transaction, takes the first ready job (Q-3.3) with `SELECT ... FOR UPDATE SKIP
  LOCKED` (`.first()` on the id-ordered set), sets `state = PROCESSING`, `claimed_at = now`, saves
  (`state, claimed_at, updated`) and commits. Returns the job or None. Reclaiming a stale `PROCESSING` row does NOT
  increment `attempts` (only Q-3.5 does). Several processors may run (production: five); each claim skips rows another
  processor holds locked, so jobs are applied concurrently and NOT in id order across processors.
- **Inputs / outputs.** `IngestJob.state` `PENDING|PROCESSING(stale) -> PROCESSING`, `claimed_at`, `updated`.
- **Code.** `atomicdb/ingest_queue.py:claim_next:90-99`.
- **Tests.** `test_ingest_queue.py::ProcessorTests::test_a_stale_claim_is_recovered`,
  `test_ingest_queue.py::ProcessorTests::test_a_failure_retries_with_backoff_and_then_parks`.
  **Gaps:** no test runs two processors concurrently; no test asserts `attempts` is unchanged by a stale reclaim.
- **Determinism hazards.** `skip_locked=True` (line 93): which job a processor gets depends on concurrent locks.
  Multi-writer concurrency means the global application order is a race (documented in `propagation_queue` docstring
  lines 7-10, 68-73). `claimed_at` is wall clock (line 97). A job whose apply crashes the PROCESS (OOM, kill) rather
  than raising is reclaimed every 15 minutes for ever: no attempt is counted.
- **History.** bb61b6a (2026-07-27).
- **Ordering.** After Q-3.2 commit; before Q-3.7.

### Q-3.5 Failure, backoff and parking of a job
- **Statement.** Any exception raised by `apply_job` (Q-3.7) rolls its transaction back entirely and is recorded
  outside it: `attempts := attempts + 1`; if `attempts >= MAX_ATTEMPTS` (5) then `state = FAILED`,
  `next_attempt_at = NULL`; otherwise `state = PENDING`, `next_attempt_at = now + BACKOFF_SECONDS[min(max(attempts-1,0),
  3)]` with `BACKOFF_SECONDS = (5, 30, 120, 600)` seconds (so after attempt 1 wait 5 s, after 2 wait 30 s, after 3
  wait 120 s, after 4 wait 600 s; attempt 5 parks). Total patience before parking is 755 s (the comment's "half an
  hour" is wrong). Always `claimed_at = NULL`, `last_error = str(error)[:2000]`, `updated = now`. A FAILED job leaves its
  task `COMPLETED` (claimed at submit) and the position without the result: `visits` does not advance, nothing is
  requeued automatically. `process_job` returns `(None, 'failed')` when parked, `(None, 'retry')` otherwise.
- **Inputs / outputs.** `IngestJob.attempts`, `state` (`PROCESSING|PENDING -> PENDING|FAILED`), `next_attempt_at`,
  `claimed_at`, `last_error`, `updated`. Log warning only; no DBEvent.
- **Code.** `atomicdb/ingest_queue.py:MAX_ATTEMPTS:46`, `BACKOFF_SECONDS:49`, `_backoff_seconds:166-168`,
  `_mark_failure:171-187`, `process_job:416-429`.
- **Tests.** `test_ingest_queue.py::ProcessorTests::test_a_failure_retries_with_backoff_and_then_parks`,
  `test_ingest_queue.py::ProcessorTests::test_a_backoff_keeps_a_job_out_of_the_queue_until_it_is_due`.
  **Gaps:** the exact backoff values per attempt are not asserted (only "in the future"); truncation at 2000 chars is
  not tested.
- **Determinism hazards.** Wall clock for `next_attempt_at` (lines 181-183). Which errors occur ("database is locked",
  deadlocks) depends on concurrency; v2 has no such failures, so a v1 job that FAILED for a transient reason is a job
  v2 applies.
- **History.** bb61b6a (2026-07-27).
- **Ordering.** After a failed Q-3.7; the row re-enters Q-3.3 when due. Manual exit from FAILED is Q-3.6.

### Q-3.6 Manual requeue of failed jobs
- **Statement.** `retry_failed()` moves every `FAILED` job to `PENDING` with `attempts = 0`, `claimed_at = NULL`,
  `next_attempt_at = now`, `updated = now`, payload intact, and returns the count. Exposed as
  `process_ingest_queue --retry-failed`, which prints `{"requeued": n}` and then continues into the normal loop (with
  `--once` it drains once and exits).
- **Inputs / outputs.** `IngestJob.state FAILED -> PENDING`, `attempts`, `claimed_at`, `next_attempt_at`, `updated`.
- **Code.** `atomicdb/ingest_queue.py:retry_failed:506-510`; `atomicdb/management/commands/process_ingest_queue.py:48-52`.
- **Tests.** `test_ingest_queue.py::ProcessorTests::test_a_failed_job_can_be_requeued_and_then_succeeds`.
  **Gaps:** the command flag itself is not tested.
- **Determinism hazards.** Operator-triggered; wall clock for `next_attempt_at`.
- **History.** bb61b6a (2026-07-27).
- **Ordering.** Manual; afterwards Q-3.3.

### Q-3.7 Applying one job: process_job and drain
- **Statement.** `process_job(job)` calls `apply_job(job)` (Q-3.8 .. Q-3.21); on any exception it calls Q-3.5 and
  returns `(None, 'failed'|'retry')`; on success returns `(summary, None)`. `drain(limit=None)` loops: `claim_next()`;
  stop when it returns None or when `done + failed >= limit`; count successes and failures; return
  `{'done': d, 'failed': f}`. Each job is claimed and applied in its own transactions; there is no batching in stage
  (a).
- **Inputs / outputs.** As Q-3.4, Q-3.5 and Q-3.8 onward.
- **Code.** `atomicdb/ingest_queue.py:process_job:416-429`, `drain:432-445`.
- **Tests.** `test_ingest_queue.py::ProcessorTests::test_the_processor_applies_what_the_submit_queued`,
  `test_ingest_queue.py::ProcessorTests::test_processing_twice_changes_nothing`,
  `test_parent_recheck.py::SafetyTests::test_a_recheck_that_fails_leaves_the_landing_applied`.
  **Gaps:** the `limit` argument is not tested.
- **Determinism hazards.** `time.monotonic()` only for the log line (418, 428).
- **History.** bb61b6a (2026-07-27).
- **Ordering.** Q-3.4 then Q-3.8 .. Q-3.21 in the order listed in Q-3.8.

### Q-3.8 apply_job: payload parsing and the preparation outside the transaction
- **Statement.** From the payload: `lines = payload.lines or []`, `searched = int(payload.nodes or 0)`,
  `elapsed = float(payload.elapsed or 0.0)`, `machine = payload.machine or ''`, `tb_wdl`, `tb_dtz`,
  `restricted = bool(payload.restricted)` (absent = full pass). Then, BEFORE opening the write transaction, exactly
  one preparation branch runs:
  1. `tb_wdl is not None` (the worker probed its own tables): `tb_prepared = ingest.prepare_tb_closure(key, tb_wdl,
     dtz=tb_dtz, user=User(payload.username) or None)` (TB-2); `rejected = tb_prepared is None`. A rejection is final:
     the job will still go DONE (retrying would give the same rejection).
  2. otherwise: read `Position.fen` (raises if the position vanished, which fails the job, Q-3.5);
     `tb_prepared = ingest.prepare_server_tb_closure(key, fen)` (the server's own tables cover it: closes on their exact
     result whatever tables the worker had; TB-2); if that is None, `lines = score_bands.normalize_lines(lines,
     stm_white)` (EXP-3; idempotent, `ingest_analysis` repeats it) and `mate_proofs = ingest.prepare_mate_proofs(fen,
     lines)` (MPV-2, the online AND/OR proof with its wall-clock budget).
  `prepare_tb_closure` returns None without a `TB_REJECTED` event when the position is no longer `UNKNOWN` or not
  `tb_applicable`; it emits `TB_REJECTED` (`invalid-wdl`, `wdl-mismatch`, `server-probe-unavailable`,
  `untrusted-six-piece`) for the other rejections.
- **Inputs / outputs.** Reads `IngestJob.payload`, `Position.fen`, `Position.status`, the server's Syzygy files,
  settings `ATOMICDB_TB_SERVER_MAX_MEN` (default 5), `ATOMICDB_TB_TRUSTED`. May write DBEvent `TB_REJECTED` (outside the
  job's transaction: it survives a later rollback of the job).
- **Code.** `atomicdb/ingest_queue.py:apply_job:247-277`; `atomicdb/ingest.py:prepare_tb_closure:10306-10355`,
  `prepare_server_tb_closure:10358-10370`, `server_tables_wdl:10373-10384`.
- **Tests.** `test_fencing.py::SubmitPreparationBoundaryTests::test_andor_preparation_runs_outside_atomic_block`,
  `test_fencing.py::SubmitPreparationBoundaryTests::test_tb_probe_runs_outside_atomic_block`,
  `test_fencing.py::SubmitFencingTests::test_rejected_tb_does_not_account_elapsed_time`.
  **Gaps:** the server-tables branch inside `apply_job` (a plain search on a covered position closing by TB) is pinned
  only indirectly by `test_tb_on_pick.py` (which covers the lease skip and the sweep, Q-3.23); a `TB_REJECTED` event
  that survives a rolled-back job is not tested.
- **Determinism hazards.** `prepare_mate_proofs` has a wall-clock budget (MPV-2, `ONLINE_MATE_PROOF_SECONDS`) and reads
  the tree outside the transaction (READ COMMITTED: what it read may change before the write transaction opens).
  TB probes read files on disk and depend on settings. The `TB_REJECTED` event is written even if the job later rolls
  back and retries, so a job retried k times can emit k events.
- **History.** bb61b6a; server-table closure 37c7f03 (2026-11-02) and 7aa36db (#116, 2026-11-03); DTZ / distance
  #312 (7a6f5d3, 2026-11-25).
- **Ordering.** First step of `apply_job`, before Q-3.9 and the transaction of Q-3.10.

### Q-3.9 apply_job: task context, attribution and trust
- **Statement.** Before the transaction, the job's task row is read once (`filter(pk=job.task_id).values(source,
  requested_by, also_requested_by, budget_nodes, route, then_verify, delivered_by, engine_sha).first()`; None if the
  task row is gone). `source` (USER/FILL/AUTO) is the closure source of every closure this application causes, however
  far the cascade climbs (CAS-4; an unknown or missing source becomes `NONE`). `account = task.delivered_by or
  payload.username or ''`. Provenance `{task: job.task_id, account, engine_sha: task.engine_sha or ''}` is attached to
  every closure (`ClosureProvenance`, CAS-4). `trusted = ingest.account_trusted(account)` (REV-2) is passed to
  `ingest_analysis` and decides whether an unproven mate line closes or becomes a `PendingClaim` (CLM-4).
- **Inputs / outputs.** Reads `AnalysisTask.source, requested_by, also_requested_by, budget_nodes, route, then_verify,
  delivered_by, engine_sha`; `TrustedAccount`; setting `ATOMICDB_CLOSURE_TRUST_GATE`. Sets context variables
  `_closure_source`, `_closure_provenance` for the duration of the transaction.
- **Code.** `atomicdb/ingest_queue.py:apply_job:286-299,312-313`; `atomicdb/ingest.py:closure_attribution:5652-5670`,
  `account_trusted:5705`.
- **Tests.** `test_adversarial_explorer.py::ClosureAttributionTests::test_a_selector_closure_is_stamped_auto`,
  `test_adversarial_explorer.py::ClosureAttributionTests::test_a_coverage_closure_is_stamped_fill`,
  `test_adversarial_explorer.py::ClosureAttributionTests::test_a_visitor_closure_is_stamped_user`,
  `test_closure_trust.py::CooperativeMateTests::test_every_closure_records_task_account_and_engine`,
  `test_closure_trust.py::CooperativeMateTests::test_an_untrusted_account_does_not_close_the_start_position`,
  `test_closure_trust.py::CooperativeMateTests::test_a_trusted_account_closes_as_before`,
  `test_closure_trust.py::CooperativeMateTests::test_the_gate_can_be_switched_off`,
  `test_closure_trust.py::ProvenClosuresTests::test_a_proven_mate_closes_for_any_account`.
  **Gaps:** the fallback `account = payload.username` when `delivered_by` is empty is not tested.
- **Determinism hazards.** The task row is read outside the transaction (it may be edited concurrently, e.g. a
  requester added; the notification set of Q-3.17 is the one read here). Trust is a mutable table and a settings switch.
- **History.** Attribution 0de882a/8c04741; provenance and trust gate 2d065be (#223, 2026-11-11).
- **Ordering.** After Q-3.8, before Q-3.10.

### Q-3.10 apply_job: idempotence under the job lock
- **Statement.** The write transaction opens with `ingest.carried_cuts()` (collects ply-guard frontiers, BKD-9),
  `atomic()` and `closure_attribution(source, provenance)`, and re-reads the job with `SELECT ... FOR UPDATE`. If its
  state is already `DONE`, it returns the stored `summary or {}` and does nothing else (no tree write, no absorption,
  no notification, no time accounting; and none of the post-commit steps Q-3.18 .. Q-3.20 run either, because the
  function returns). This makes a second application of the same job (stale reclaim, synchronous plus async path) a
  no-op.
- **Inputs / outputs.** Reads `IngestJob.state`, `summary`. Row lock on the job.
- **Code.** `atomicdb/ingest_queue.py:apply_job:312-316`.
- **Tests.** `test_ingest_queue.py::ProcessorTests::test_processing_twice_changes_nothing`,
  `test_queue_control.py::DuplicateNotificationTests::test_a_second_pass_does_not_duplicate_anybody`.
  **Gaps:** none significant.
- **Determinism hazards.** Relies on the row lock (`select_for_update`) for exclusion between processors.
- **History.** bb61b6a (2026-07-27).
- **Ordering.** First thing inside the transaction; then Q-3.11.

### Q-3.11 apply_job: the three landing branches
- **Statement.** Inside the transaction, exactly one branch:
  1. `rejected` (worker WDL rejected in Q-3.8): `summary = {'tb_rejected': True}`; the tree is untouched.
  2. `tb_prepared` not None (worker WDL accepted, or server tables cover the position): `closed =
     ingest._apply_prepared_tb(key, tb_prepared)` (TB-2): re-checks under a row lock that the position is still
     `UNKNOWN`, has the same FEN and is still `tb_applicable`; sets `status = wdl_to_status(wdl, stm)`, `closure = 'TB'`,
     `clock_slack = None if DRAW else slack_from_dtz(dtz)`; emits closure events (`NODE_CLOSED` etc., CAS-7, which also
     absorbs the position's PENDING tasks, Q-3.14); then runs `backup_cascade([key])` and `backup_backed_evals([key])`
     INLINE and schedules the post-commit recheck (CAS-6). `summary = {'tb_closed': True}` if it closed, else
     `{'tb_rejected': True}`. A TB landing is NEVER deferred, even with deferral on.
  3. otherwise (an engine search): `arbiter = arbiter_budget(lines, searched, task.budget_nodes)` (Q-3.12);
     `summary = ingest.ingest_analysis(key, lines, searched, machine=machine, mate_proofs=mate_proofs,
     restricted=restricted, defer_propagation=deferral, trusted=trusted, arbiter_budget=arbiter)` (EXP-2..EXP-6; with
     `deferral` not None it ends by writing a PropagationSeed, Q-4.3, and returns `summary['propagation'] =
     'deferred'`); then `absorbed = _absorb_shadowed(job, arbiter)` (Q-3.13) and `summary['absorbed'] = absorbed` when
     non-zero.
  No branch advances `visits` for a TB result. A rejected TB result does not absorb anything and does not requeue the
  task (it stays COMPLETED with the delivered nodes).
- **Inputs / outputs.** See EXP-2, TB-2. Summary keys produced here: `tb_rejected`, `tb_closed`, everything
  `ingest_analysis` returns (`closed_children`, `propagation`, `revoked`, `certified`, and with an inline climb
  `backed_up`, `backed_evals`, `uncertainty_expanded`, `witness_refuted`, `cascaded`, `repaired`, `autosolved`),
  `absorbed`.
- **Code.** `atomicdb/ingest_queue.py:apply_job:317-336`; `atomicdb/ingest.py:_apply_prepared_tb:10387-10415`,
  `ingest_analysis:985-1315` (deferral hand-off 1303-1310).
- **Tests.** `test_ingest_queue.py::ProcessorTests::test_the_processor_applies_what_the_submit_queued`,
  `test_fencing.py::SubmitFencingTests::test_rejected_tb_does_not_account_elapsed_time`,
  `test_tb_on_pick.py::TablesSettleQueuedPositionsTests::test_the_sweep_closes_it_and_tells_the_requester`,
  `test_ingest_queue.py::SynchronousVsQueuedTests::test_both_paths_leave_the_same_tree`.
  **Gaps:** no test pins that a TB landing climbs inline when deferral is on (no PropagationSeed for TB landings); no
  test pins that a rejected worker WDL leaves the task COMPLETED without requeue.
- **Determinism hazards.** As EXP-2/TB-2 (cross-ref). The TB branch's inline cascade under deferral means two
  different climb schedules coexist in one deployment.
- **History.** TB landing in the queue since bb61b6a; server tables 7aa36db (#116); arbiter d6b5359 (#225,
  2026-11-11); deferral 5671bcf (#188, 2026-11-08).
- **Ordering.** After Q-3.10; then Q-3.15, Q-3.16, Q-3.17 inside the same transaction.

### Q-3.12 Arbiter budget of a mate-stopped search (EXP-5)
- **Statement.** `arbiter_budget(lines, searched, budget)` returns the budget with which a pass arbitrates the showcase
  (EXP-2) and absorbs requests (Q-3.13). It returns `budget` (the task's `budget_nodes`) iff ALL hold: `lines` is
  non-empty, `lines[0]` is a dict, `lines[0]['mate'] is not None` (either sign: the side to move mates or is mated),
  `budget` is a Python `int`, and `budget > searched`. Otherwise it returns `searched` (the nodes actually run, already
  clamped to 2x budget at submit). Only the FIRST line counts: a mate in a later line is not a mate stop. Credit,
  nodes per second and `nodes_invested` still use `searched`. Rationale: since worker build 2026111101 a search ends at
  the first completed iteration whose best line is a mate (`go nodes N mate 250`).
- **Inputs / outputs.** Pure function of the payload lines, payload nodes and `AnalysisTask.budget_nodes`.
- **Code.** `atomicdb/ingest_queue.py:arbiter_budget:190-208`; call `apply_job:324-325`.
- **Tests.** `test_mate_stop.py::ArbiterBudgetTests::test_a_mate_stop_arbitrates_with_its_budget`,
  `test_mate_stop.py::ArbiterBudgetTests::test_a_mated_side_counts_too`,
  `test_mate_stop.py::ArbiterBudgetTests::test_a_pass_without_a_mate_arbitrates_with_its_nodes`,
  `test_mate_stop.py::ArbiterBudgetTests::test_a_mate_in_a_later_line_is_not_a_mate_stop`,
  `test_mate_stop.py::ArbiterBudgetTests::test_a_full_search_is_unchanged`,
  `test_mate_stop.py::ShowcaseTests::test_a_mate_stop_takes_the_showcase`,
  `test_mate_stop.py::SubmitTests::test_a_mate_stop_is_accepted_and_credited_with_its_nodes`,
  `test_mate_stop.py::SubmitTests::test_a_short_pass_without_a_mate_absorbs_only_what_it_ran`.
  **Gaps:** `budget` not an int (None when the task row is gone) is not tested; a mate line from an UNTRUSTED account
  (stored only as a PendingClaim) still arbitrates and absorbs with the full budget, untested.
- **Determinism hazards.** none (pure). Note the `isinstance(budget, int)` test: a budget read back as another numeric
  type would silently fall back to `searched`.
- **History.** d6b5359 "Stop AtomicDB searches at the first mate and keep only exact lines" (#225, 2026-11-11).
- **Ordering.** Inside Q-3.11 branch 3, before `ingest_analysis` and Q-3.13.

### Q-3.13 Live absorption of shadowed requests (EXP-5)
- **Statement.** After an engine landing, in the same transaction, every OTHER task on the same position with
  `budget_nodes <= arbiter` (Q-3.12) and `state = PENDING` is absorbed (Q-3.14). Tasks asking for more than the pass ran
  stay PENDING. `LEASED` tasks are never touched (they will deliver their own result). There is no creation-time cut:
  a PENDING task created after this search was leased (e.g. a +16 sweep re-request) is absorbed too, unlike the backfill
  command (Q-3.25). If the job rolls back, the absorbed tasks come back with it. The count goes into
  `summary['absorbed']` when non-zero.
- **Inputs / outputs.** Reads `AnalysisTask.position_id, budget_nodes, state, id`. Writes per Q-3.14. No DBEvent.
- **Code.** `atomicdb/ingest_queue.py:_absorb_shadowed:211-237`; call `apply_job:334-336`.
- **Tests.** `test_ingest_queue.py::AbsorptionTests::test_the_pass_absorbs_what_it_left_without_work`,
  `test_ingest_queue.py::AbsorptionTests::test_an_absorbed_row_claims_no_nodes_and_no_machine`,
  `test_ingest_queue.py::AbsorptionTests::test_another_position_is_none_of_its_business`,
  `test_queue_control.py::AbsorptionStillWorksTests::test_a_cheaper_pending_request_is_absorbed_by_a_deeper_analysis`,
  `test_queue_control.py::AbsorptionStillWorksTests::test_a_deeper_pending_request_survives_a_cheaper_analysis`,
  `test_sweep.py::SweepDripTests::test_a_search_that_lands_while_it_waits_still_absorbs_it`,
  `test_idle_repair.py::ShallowLandingTests::test_a_repair_absorbed_unsearched_does_not_count`.
  **Gaps:** equality `budget_nodes == arbiter` is absorbed (`__lte`), not pinned by a dedicated test.
- **Determinism hazards.** Concurrency: a task created by another transaction after this transaction's snapshot is
  not seen (READ COMMITTED: the UPDATE sees rows committed before it starts); in v2 the writer's order decides.
- **History.** d07b172 "a landed analysis closes the requests it just made pointless" (2026-08-03); cut changed from
  `searched` to `arbiter` by d6b5359 (#225).
- **Ordering.** After `ingest_analysis` inside Q-3.11 branch 3; before Q-3.15. Related: closure-time absorption
  (CAS-7 via `_emit_closure_events`, `ingest.py:5827`) and the recycler's `_absorb_requeued` (part 1).

### Q-3.14 The absorption idiom (absorb_tasks / is_absorbed)
- **Statement.** `absorb_tasks(queryset)` is the only way a task is absorbed: restrict the queryset to
  `state = PENDING` and update `state = COMPLETED`, `completed = now`, `nodes_searched = 0`; returns the row count.
  `machine` is not written: a PENDING row always has `machine = ''` (the recycler and release clear it, part 1), so an
  absorbed row is COMPLETED without nodes and without machine, and per-machine aggregates (contributors, leaderboards)
  exclude it automatically. `LEASED`, `CANCELLED`, `COMPLETED` rows are never changed. `is_absorbed(task)` is
  `state == COMPLETED and not nodes_searched and not machine` (a delivered row always carries its machine). Callers:
  Q-3.13, `_emit_closure_events` (every closure absorbs all PENDING tasks of the closed position), the recycler's
  `views._absorb_requeued`, the backfill Q-3.25, the +16 sweep revive logic (part 6).
- **Inputs / outputs.** `AnalysisTask.state PENDING -> COMPLETED`, `completed`, `nodes_searched`. `requested_by`,
  `budget_nodes`, `priority`, `route` untouched.
- **Code.** `atomicdb/ingest.py:absorb_tasks:5770-5786`, `is_absorbed:5789-5797`, `_emit_closure_events:5827`;
  `atomicdb/views.py:_absorb_requeued:978-992`.
- **Tests.** `test_ingest_queue.py::AbsorptionTests::test_an_absorbed_row_claims_no_nodes_and_no_machine`,
  `test_ingest_queue.py::AbsorptionTests::test_the_completed_ladder_reads_the_same_as_before`,
  `test_ingest_queue.py::ClosureAbsorbsItsQuestionsTests::test_a_mate_witness_closes_the_children_it_leaves_without_work`,
  `test_lease_release.py::ReleaseOwnLeasesTests::test_a_task_on_a_closed_position_is_absorbed_as_the_recycler_does`,
  `test_queue_control.py::AbsorptionStillWorksTests::test_the_position_can_still_be_requested_after_an_absorption`,
  `test_queue_control.py::AbsorptionStillWorksTests::test_a_delivered_search_still_to_be_applied_is_not_a_dead_row`,
  `test_public_api.py::OnlyIfMissingOnAnAbsorbedSlotTests::test_a_search_that_ran_answers_and_the_absorbed_row_stays_dead`,
  `test_search_depth_column.py::DepthColumnTests::test_an_absorbed_row_is_not_a_search`,
  `test_sweep.py::SweepDripTests::test_another_arms_absorbed_task_is_left_alone`.
  **Gaps:** a delivered COMPLETED row with `nodes_searched = 0` and a machine (zero-node submit) is "not absorbed"
  by the predicate; pinned only by `test_fencing.py::SubmitFencingTests::test_zero_reported_nodes_remain_zero`
  indirectly.
- **Determinism hazards.** `completed = timezone.now()` (5786). The predicate's reliance on `machine = ''` assumes
  Q-1/part-1 invariants hold.
- **History.** Factored as the single idiom with d0070d6 (2026-08-03).
- **Ordering.** Inside the caller's transaction.

### Q-3.15 Engine time accounting
- **Statement.** Inside the transaction, if `elapsed` is truthy (non-zero) and the summary is not `tb_rejected`,
  `Position.time_invested := time_invested + elapsed` (atomic `F()` update). A rejected WDL buys nothing and costs no
  clock. An accepted TB closure and every engine landing (including one whose `ingest_analysis` stored nothing new)
  add the time.
- **Inputs / outputs.** `IngestJob.payload.elapsed`; writes `Position.time_invested`.
- **Code.** `atomicdb/ingest_queue.py:apply_job:339-341`.
- **Tests.** `test_fencing.py::SubmitFencingTests::test_rejected_tb_does_not_account_elapsed_time`.
  **Gaps:** accepted-TB accounting not tested.
- **Determinism hazards.** `elapsed` is the worker's wall-clock report (a fact of the payload, deterministic on replay).
- **History.** bb61b6a.
- **Ordering.** After Q-3.11, before Q-3.16.

### Q-3.16 Job completion (DONE)
- **Statement.** Still inside the transaction: `state = DONE`, `summary = <summary>`, `claimed_at = NULL`,
  `next_attempt_at = NULL`, `last_error = ''`, saved with `updated`. `attempts` keeps its value. The tree writes, the
  absorption, the time accounting, the DONE mark and the notifications (Q-3.17) commit together or not at all.
- **Inputs / outputs.** `IngestJob.state PROCESSING|PENDING -> DONE` (PENDING when applied synchronously, Q-3.22),
  `summary`, `claimed_at`, `next_attempt_at`, `last_error`, `updated`.
- **Code.** `atomicdb/ingest_queue.py:apply_job:342-349`.
- **Tests.** `test_ingest_queue.py::ProcessorTests::test_the_processor_applies_what_the_submit_queued`,
  `test_ingest_queue.py::ProcessorTests::test_a_failed_job_can_be_requeued_and_then_succeeds`.
  **Gaps:** none.
- **Determinism hazards.** `updated` wall clock.
- **History.** bb61b6a.
- **Ordering.** After Q-3.15; Q-3.17 follows in the same transaction; then commit; then Q-3.18 .. Q-3.20.

### Q-3.17 "Analysis ready" notification to the requesters
- **Statement.** In the job's transaction (also for a `tb_rejected` result: the call is unconditional), using the task
  dict read in Q-3.9: build `wanted` = `[task.requested_by] + task.also_requested_by`, dropping empty names and
  duplicates, keeping first-appearance order. Return nothing if `wanted` is empty, or if
  `notification_deserved(task.source, task.budget_nodes)` is false: deserved iff `source == 'USER'` or `budget_nodes >=
  NOTIFY_MIN_BUDGET` (`= REQUEST_BUDGET_LADDER[0] = 128,000,000` nodes). Keep only names that exist as OpenBench
  `auth_user.username` (one query), minus those with `ContributorPref.notify_ready = False`
  (`preferences.ready_notice_muted`; no row = not muted). For each remaining name, in `wanted` order,
  `RequestNotification.get_or_create(task_id=job.task_id, username=name, defaults={position_id: job.position_id, route:
  task.route or ''})`. The unique constraint on `(task, username)` (conditional on task not NULL) dedupes. All
  requesters share the FIRST requester's route. A task deleted before the apply (task dict None) notifies nobody.
- **Inputs / outputs.** Reads `AnalysisTask.requested_by, also_requested_by, source, budget_nodes, route`,
  `auth.User.username`, `ContributorPref.notify_ready`. Writes `RequestNotification(task, username, position, route)`.
- **Code.** `atomicdb/ingest_queue.py:_notify_requester:108-163`, call `apply_job:350`; `atomicdb/ingest.py:
  notification_deserved:8666-8687`, `NOTIFY_MIN_BUDGET:64`; `atomicdb/preferences.py:ready_notice_muted`.
- **Tests.** `test_notifications.py::NotificationCreationTests::test_a_served_request_notifies_the_account_that_asked`,
  `test_notifications.py::NotificationCreationTests::test_an_anonymous_request_notifies_nobody`,
  `test_notifications.py::NotificationCreationTests::test_a_name_without_an_account_notifies_nobody`,
  `test_notifications.py::NotificationCreationTests::test_one_task_notifies_exactly_once`,
  `test_notifications.py::NotificationCreationTests::test_a_small_coverage_fill_does_not_notify`,
  `test_notifications.py::NotificationCreationTests::test_an_expansion_notifies_through_each_child_it_bought`,
  `test_notification_settings.py::ReadyNoticeSettingTests::test_the_notice_is_on_without_a_row`,
  `test_notification_settings.py::ReadyNoticeSettingTests::test_turned_off_no_notification_is_created`,
  `test_notification_settings.py::ReadyNoticeSettingTests::test_the_other_requesters_of_the_task_still_get_theirs`,
  `test_queue_control.py::DuplicateNotificationTests::test_every_requester_gets_the_notification`,
  `test_queue_control.py::DuplicateNotificationTests::test_a_second_pass_does_not_duplicate_anybody`,
  `test_queue_control.py::DuplicateNotificationTests::test_a_name_without_an_account_notifies_nobody`,
  `test_queue_control.py::EndToEndClickTests::test_two_visitors_share_one_task_and_both_are_notified`.
  **Gaps:** notification on a `tb_rejected` result (a requester is told "ready" for a result that changed nothing)
  is not tested; the 128M floor for a non-USER task carrying a requester is pinned only via the FILL case.
- **Determinism hazards.** Cross-database read of OpenBench's `auth_user` (account existence can change); mutable
  preference table. Under deferral the notice is written at stage (a), BEFORE the climb has updated the ancestors.
- **History.** 1c64e73 (2026-07-29); route of the requester 9a76ba8; all requesters abf61a0 (2026-08-14, via #44);
  mute setting b2145b3.
- **Ordering.** Last statement inside the job transaction (after Q-3.16). Q-3.23 reuses it for TB-swept tasks.

### Q-3.18 Proof numbers of the landed position after the job
- **Statement.** After the commit, if the climb is NOT pending (`summary.get('propagation') != 'deferred'`: deferral
  off, a TB branch, or an early return of `ingest_analysis` that did not defer), call
  `ingest.proof.refresh_proof_numbers([job.position_id])` (PN-4/PN-8). It guarantees every applied job touches the
  pn/dn of its position even when nothing cascaded (rejected WDL, already closed). With a pending climb, Q-4.16 does it
  after the climb. This call is NOT wrapped: an exception here propagates to `process_job`, whose `_mark_failure`
  overwrites the already-committed `DONE` with `PENDING` (attempts + 1, backoff) or `FAILED`; the next claim then finds
  `state != DONE` and Q-3.10 does not stop it, so the whole landing is applied a SECOND time (visits, nodes_invested,
  time_invested, absorption, notification attempt). The same holds for any non-`DatabaseError` exception escaping
  Q-3.19/Q-3.20. A porter must not reproduce this; it is listed as an open question.
- **Inputs / outputs.** pn/dn columns (PN family). No DBEvent.
- **Code.** `atomicdb/ingest_queue.py:apply_job:354-360`.
- **Tests.** `test_deferred_propagation.py::DurableDeferralTests::test_the_job_commits_its_result_and_leaves_its_climb`
  (asserts the deferred path leaves pn work to the climb); `test_proof_manager.py::IncrementalMaintenanceTests::
  test_backup_cascade_maintains_the_numbers` (cascade side). **Gaps:** the "rejected WDL still refreshes pn/dn" case is
  untested; the failure-after-commit path (exception in the refresh flips a DONE job back to PENDING and re-applies
  it) is untested.
- **Determinism hazards.** Runs outside the transaction, after other writers may have committed (READ COMMITTED).
  pn/dn only order the selector (PRI); values and closures never read them.
- **History.** 0de882a "Give AtomicDB a proof manager".
- **Ordering.** After commit of Q-3.16; before Q-3.19.

### Q-3.19 Carrying a cut ascent after the job commits
- **Statement.** If the job's ascent was cut by the ply guard (frontier keys collected by `carried_cuts`, BKD-9), call
  `ingest.carry_cut_ascents(cut)` after the commit, in short transactions of its own. If it returns non-None, the job's
  `summary` becomes `{**summary, 'carried': carried}` (UPDATE of the DONE row). A frontier wider than
  `CARRY_MAX_KEYS = 64` keys is left to `sweep_stale_backed` (BKD-12). A `DatabaseError` is logged and swallowed: the
  job stays applied and the cut stays on record (`BACKED_GUARD` event) for the sweep.
- **Inputs / outputs.** Backed columns of ancestors (BKD-7); `IngestJob.summary.carried`.
- **Code.** `atomicdb/ingest_queue.py:apply_job:366-374`; `atomicdb/ingest.py:CARRY_MAX_KEYS:4449`,
  `carried_cuts:4454-4465`, `carry_cut_ascents:4803`.
- **Tests.** `test_stale_backed_sweep.py::IngestCarryTests::test_an_ingest_carries_its_cut_to_the_top`,
  `test_stale_backed_sweep.py::IngestCarryTests::test_a_wide_frontier_is_left_to_the_sweep`,
  `test_stale_backed_sweep.py::IngestCarryTests::test_a_carry_that_fails_leaves_the_job_applied`.
  **Gaps:** none in scope.
- **Determinism hazards.** Runs after commit, interleaved with other writers; the carry is a v1 repair mechanism that
  v2 replaces by running every ascent to rest (plan 2.2.3).
- **History.** b344e4f "Carry an ingest's cut ascent on as soon as it commits" and 1ead68e (summary record), #89
  (2026-10-27).
- **Ordering.** After Q-3.18; before Q-3.20. Under deferral the cut of the job is empty (the climb runs in the drainer,
  Q-4.16 carries there).

### Q-3.20 Request + verify PV ("then_verify") after the job
- **Statement.** If `task.then_verify` and the summary is not `tb_rejected` and the climb is not pending, call
  `_verify_after(job, task)` after the commit; when it returns non-None, the job's `summary` becomes
  `{**summary, 'then_verify': n}`. `_verify_after`: (1) if the requester's queue is full
  (`views._account_queue_full(requested_by)`, part 4, `REQUEST_QUEUE_MAX = 10,000`) return None (nothing attempted);
  (2) in its own transaction, lock the position row; if missing or `status != 'UNKNOWN'` return None; (3) `outcome =
  ingest.enqueue_pv_verification(pos, requested_by, route=task.route or '')` (MPV-6, the Verify PV button's purchase);
  return `int(outcome)` (number of searches queued, 0 allowed). A `DatabaseError` is logged and gives None. Under
  deferral the same request rides in the seed payload as `verify = {requested_by, route}` and runs in Q-4.16.
- **Inputs / outputs.** `AnalysisTask.then_verify, requested_by, route`; creates verification `AnalysisTask` rows
  (MPV-6); `IngestJob.summary.then_verify`.
- **Code.** `atomicdb/ingest_queue.py:apply_job:375-380`, `_verify_after:384-413`.
- **Tests.** `test_then_verify.py::ThenVerifyIngestTests::test_a_flagged_request_verifies_its_line_when_it_lands`,
  `test_then_verify.py::ThenVerifyIngestTests::test_an_unflagged_request_buys_nothing_more`,
  `test_deferred_propagation.py::DurableDeferralTests::test_the_requested_verification_waits_for_the_climb`.
  **Gaps:** the queue-full refusal and the closed-position refusal are untested.
- **Determinism hazards.** Queue-full check reads outside any lock (a concurrent request can fill the queue between
  the check and the purchase). Runs after other writers may have changed the position.
- **History.** be5b897 "request an analysis and verify its line in one click" (#163, 2026-11-05); deferred variant
  5671bcf (#188).
- **Ordering.** Last step of `apply_job`. Requires the job committed (the walk must read the new line).

### Q-3.21 Hand-off of the climb to the propagation queue
- **Statement.** Before the transaction, if `propagation_queue.deferral_enabled()` (Q-4.2), `deferral = {'job':
  job.pk}`, plus `deferral['verify'] = {'requested_by': task.requested_by, 'route': task.route}` when
  `task.then_verify`. It is passed as `defer_propagation` to `ingest_analysis`, which after stage (a) builds the
  `Landing` and calls `propagation_queue.defer(landing, extra=deferral)` instead of `propagate_landings([landing])`
  (Q-4.3), and returns `summary['propagation'] = 'deferred'`. `climb_pending` (line 354) is that marker; it disables
  Q-3.18 and Q-3.20 in `apply_job` (Q-4.16 does them after the climb).
- **Inputs / outputs.** Setting `ATOMICDB_DEFER_PROPAGATION`; writes a `PropagationSeed` inside the job's transaction.
- **Code.** `atomicdb/ingest_queue.py:apply_job:305-310,354`; `atomicdb/ingest.py:ingest_analysis:1303-1310`.
- **Tests.** `test_deferred_propagation.py::DurableDeferralTests::test_the_job_commits_its_result_and_leaves_its_climb`,
  `test_deferred_propagation.py::StageATests::test_the_search_is_stored_and_nothing_above_it_moves`,
  `test_deferred_propagation.py::StageATests::test_a_closure_of_its_own_child_is_stage_a_and_its_parents_are_b`.
  **Gaps:** none.
- **Determinism hazards.** Settings switch decides the schedule (and thus, through the noise cut, possibly values;
  Q-4.21).
- **History.** 5671bcf (#188, 2026-11-08).
- **Ordering.** Decided before Q-3.10; the seed is written inside Q-3.11 branch 3.

### Q-3.22 Synchronous ingest switch (emergency path)
- **Statement.** With `ATOMICDB_SYNCHRONOUS_INGEST = True`, the submit, after committing its claim + enqueue (Q-3.2),
  calls `ingest_queue.process_job(job)` in the same request (without `claim_next`: the job goes `PENDING -> DONE`
  directly, or through Q-3.5 to `PENDING` with backoff / `FAILED`, after which the async processor, if running, picks
  it up). The worker still gets `{'ok': True, 'summary': ...}`; on error the summary is `{'ingest_job': id,
  'ingest_error': 'retry'|'failed'}`. With the switch on, deferral is forced off (`deferral_enabled()` is false), so the
  synchronous path always climbs inline.
- **Inputs / outputs.** As Q-3.7..Q-3.20.
- **Code.** `atomicdb/ingest_queue.py:synchronous_ingest_enabled:56-65`; `atomicdb/views.py:api_submit:1750-1756`;
  `atomicdb/propagation_queue.py:deferral_enabled:149-160`.
- **Tests.** `test_ingest_queue.py::SynchronousFallbackTests::test_the_switch_applies_before_answering`,
  `test_ingest_queue.py::SynchronousVsQueuedTests::test_both_paths_leave_the_same_tree`,
  `test_deferred_propagation.py::DurableDeferralTests::test_the_synchronous_ingest_still_climbs_inline`.
  **Gaps:** the error summary shape is not tested.
- **Determinism hazards.** Settings switch; concurrent with the async processors (a job applied synchronously while a
  processor claims it is serialized by Q-3.10).
- **History.** bb61b6a (2026-07-27).
- **Ordering.** Right after the submit transaction commits. v2: not needed (one writer).

### Q-3.23 Tablebase sweep of queued positions (TB-5)
- **Statement.** `close_queued_by_tables(limit=TB_SWEEP_LIMIT=200)` closes, from the server's own tables, positions that
  have PENDING tasks (the lease skips such tasks, part 1, so no worker is ever served them). Scan all
  `AnalysisTask(state=PENDING, position.status='UNKNOWN')` ordered by task `id`; skip rows whose FEN is not
  `logic.tb_applicable(fen, tb_server_max_men())` (piece count and no castling/en passant, from the FEN, no disk);
  group rows by position in first-seen order; once 200 distinct positions are collected, rows of NEW positions are
  skipped but rows of already-collected positions are still appended. For each position in that order:
  `prepared = prepare_server_tb_closure(key, fen)` (probe; None = tables cannot answer, skip); then in one transaction
  with `closure_attribution(first task's source)` (no provenance): `_apply_prepared_tb(key, prepared)` (Q-3.11 branch 2
  semantics, including closure events that absorb the PENDING tasks, Q-3.14); if it did not close, skip; else
  `_notify_requester(_TaskJob(task.id, key), task)` for each collected task (Q-3.17 rules, with the task dict from the
  scan). Returns the number of positions closed. `_TaskJob` carries only `task_id` and `position_id`.
- **Inputs / outputs.** Reads `AnalysisTask` (PENDING), `Position.fen/status`, Syzygy files, setting
  `ATOMICDB_TB_SERVER_MAX_MEN` (default 5). Writes `Position.status, closure='TB', clock_slack`, DBEvents `NODE_CLOSED`
  (and `TB_REJECTED` on probe disagreement), `ClosureProvenance`, absorbed tasks, `RequestNotification`s, the inline
  cascade and backed ascent.
- **Code.** `atomicdb/ingest_queue.py:TB_SWEEP_LIMIT:451`, `TB_SWEEP_SECONDS:452`, `close_queued_by_tables:455-493`,
  `_TaskJob:496-503`; scheduled by `process_ingest_queue.py:69-82`.
- **Tests.** `test_tb_on_pick.py::TablesSettleQueuedPositionsTests::test_the_sweep_closes_it_and_tells_the_requester`,
  `test_tb_on_pick.py::TablesSettleQueuedPositionsTests::test_the_sweep_leaves_other_positions_alone`,
  `test_tb_on_pick.py::TablesSettleQueuedPositionsTests::test_the_sweep_closes_nothing_the_tables_cannot_answer`,
  `test_tb_on_pick.py::TablesSettleQueuedPositionsTests::test_the_processor_runs_the_sweep`, and the lease-side
  `test_a_covered_position_is_served_to_no_worker`, `test_the_lease_goes_on_to_the_next_task`,
  `test_a_probe_that_fails_leaves_the_task_served`, `test_a_position_beyond_the_tables_never_reaches_the_disk` (same
  class). **Gaps:** the 200-position limit and the "keep appending tasks of collected positions" rule are untested.
- **Determinism hazards.** Disk (Syzygy files), settings; scan order by task id (deterministic); runs every 30 s of
  `time.monotonic()` in EACH ingest processor (5 concurrent sweeps in production; idempotent through the row lock and
  status re-check in `_apply_prepared_tb`, notifications deduped by the unique constraint, but two sweeps can race on
  the same position and one may fail with an IntegrityError, caught as `DatabaseError` by the command).
- **History.** 7aa36db "positions the server's tables cover are closed from them, never served" (#116, 2026-11-03);
  37c7f03 (6-piece tables, 2026-11-02).
- **Ordering.** Inside the processor loop after each `drain` (Q-3.24), at most every 30 s.

### Q-3.24 The ingest processor loop (process_ingest_queue)
- **Statement.** `manage.py process_ingest_queue [--once] [--limit N] [--idle-seconds S=2.0] [--retry-failed]
  [--status]`. `--status` prints `queue_depth()` (count by state, JSON, sorted keys) and exits. Otherwise (after the
  optional Q-3.6) it loops until SIGINT/SIGTERM: (1) `ingest_queue.drain(limit)`; (2) if `monotonic() >=
  next_tb_sweep` (initially 0, so the first turn always sweeps): set `next_tb_sweep = now + 30 s`, run Q-3.23 (a
  `DatabaseError` is logged), and, only when deferral is OFF, `propagation_queue.drain(max_batches=1)` to settle
  PropagationSeed rows left from a period with deferral on (Q-4.19; `DatabaseError` logged); (3) with `--once`, stop;
  (4) if the drain did nothing (`done == 0 and failed == 0`), sleep `max(0.1, idle_seconds)` in 0.1 s steps,
  interruptible. Prints the totals JSON `{done, failed, tb_closed[, propagation_settled]}` at exit. Several processes
  may run (production runs five, called "the five ingest writers"); each runs its own TB sweep timer.
- **Inputs / outputs.** As the called rules.
- **Code.** `atomicdb/management/commands/process_ingest_queue.py:Command.handle:43-107`;
  `atomicdb/ingest_queue.py:queue_depth:513-516`.
- **Tests.** `test_ingest_queue.py::ProcessorTests::test_the_management_command_drains_and_reports`,
  `test_ingest_queue.py::ProcessorTests::test_the_management_command_reports_the_queue_depth`,
  `test_tb_on_pick.py::TablesSettleQueuedPositionsTests::test_the_processor_runs_the_sweep`,
  `test_deferred_propagation.py::DurableDeferralTests::test_the_writers_take_what_is_left_once_deferral_is_off`.
  **Gaps:** signal handling and the idle sleep are untested (v1-specific).
- **Determinism hazards.** `time.monotonic()` timers (75-76, 103-106); number of processes is deployment config;
  SIGTERM between jobs only (a job in flight finishes or rolls back).
- **History.** bb61b6a; TB sweep 7aa36db (#116); leftover propagation 5671bcf (#188).
- **Ordering.** Service loop; v2 replaces it by the writer's input loop.

### Q-3.25 Backfill of zombie requests (absorb_shadowed_tasks)
- **Statement.** `manage.py absorb_shadowed_tasks [--dry-run]` (run hourly by cron in production; not in the repo's
  services) absorbs two DISJOINT classes of PENDING tasks with `absorb_tasks` (Q-3.14): (a) **closed**: the task's
  position is not `UNKNOWN`; (b) **shadowed**: the position is `UNKNOWN` and there EXISTS a `COMPLETED` task on the same
  position with `budget_nodes >= this.budget_nodes` and `completed >= this.created` (a search that finished while this
  task was waiting). A task created after the covering search is a deliberate re-request (e.g. the +16 sweep re-buying
  128M on a position searched at 128M), not a duplicate, and is kept. Note the covering COMPLETED task may itself be an
  absorbed row (nodes 0); the query does not exclude absorbed rows. Tombstoned positions (`priority <= DEAD/2`) are NOT
  touched: their tasks are still leased (the lease does not look at priority). `--dry-run` prints the two counts and
  writes nothing. Idempotent: a second pass finds nothing. Order of the two writes is irrelevant (disjoint on status).
- **Inputs / outputs.** `AnalysisTask.state, position_id, budget_nodes, completed, created`, `Position.status`. Writes
  per Q-3.14.
- **Code.** `atomicdb/management/commands/absorb_shadowed_tasks.py:Command.handle:64-93`.
- **Tests.** `test_ingest_queue.py::ShadowBackfillTests::test_the_dry_run_counts_without_touching_a_single_row`,
  `test_ingest_queue.py::ShadowBackfillTests::test_the_pass_absorbs_the_shadowed_and_leaves_the_deeper_alone`,
  `test_ingest_queue.py::ShadowBackfillTests::test_the_pass_absorbs_the_pending_on_a_closed_position`,
  `test_ingest_queue.py::ShadowBackfillTests::test_a_leased_row_on_a_closed_position_keeps_running`,
  `test_ingest_queue.py::ShadowBackfillTests::test_a_task_made_after_the_search_is_not_a_duplicate`,
  `test_ingest_queue.py::ShadowBackfillTests::test_a_second_pass_finds_nothing_left`,
  `test_ingest_queue.py::ShadowBackfillTests::test_a_tombstoned_position_is_not_a_zombie_and_is_still_served`,
  `test_sweep.py::SweepDripTests::test_the_hourly_absorb_pass_leaves_its_tasks_alone`.
  **Gaps:** an absorbed COMPLETED row acting as the shadowing row is untested.
- **Determinism hazards.** Cron schedule (wall clock); compares wall-clock timestamps `completed` vs `created`.
- **History.** d07b172 / d0070d6 (2026-08-03); the `completed >= created` cut by 405a42b "Stop the hourly absorb pass
  from emptying the +16 sweep's queue" (2026-11-13; 8,860 sweep tasks absorbed 11-13 Nov).
- **Ordering.** Independent periodic pass. In v2 it should be unnecessary if Q-3.13 and closure absorption are exact,
  but note the live and backfill rules differ (no creation cut live).

---

### Q-4.1 PropagationSeed record
- **Statement.** A `PropagationSeed` is the climb a landed search still owes its ancestors. It is written in the SAME
  transaction as the landing's stage (a) and deleted in the transaction that climbs it; its existence is the only
  claim/lock state (no state column). Several rows may exist for the same position (no unique constraint): two
  searches of it, or a job applied twice. `payload` holds the seed lists exactly as the inline climb would use them:
  `cascade` (`[key, *closed, *others]`), `backed` (`[key, *parents, *others, *audience]`), `closed`, `others`,
  `parents`, `audience`, plus `job` (IngestJob id) and optional `verify` (`{requested_by, route}`). `source` is the
  landing's closure source label (max 16 chars, '' allowed). Deleting the position deletes its seeds (CASCADE).
- **Inputs / outputs.** `PropagationSeed.position` (FK), `source`, `payload` (JSON), `attempts` (int 0), `next_attempt_at`
  (datetime NULL), `last_error` (text ''), `created` (auto_now_add, db_index). Index `atomic_propagation_ready` on
  `(next_attempt_at, id)`.
- **Code.** `atomicdb/models.py:PropagationSeed:716-749`; `atomicdb/migrations/0058_propagationseed.py`;
  `atomicdb/ingest.py:Landing:1318-1394` (`to_payload:1381-1385`).
- **Tests.** `test_deferred_propagation.py::DurableDeferralTests::test_the_job_commits_its_result_and_leaves_its_climb`,
  `test_deferred_propagation.py::DeduplicationTests::test_two_landings_on_one_position_are_one_climb`.
  **Gaps:** the exact payload key set is not asserted.
- **Determinism hazards.** `created` wall clock (used by `backlog()` age only).
- **History.** 5671bcf "commit a landing at once, climb its ancestors from a durable queue" (#188, 2026-11-08;
  proposed by BManBJ, approved by the owner).
- **Ordering.** Written by Q-4.3, consumed by Q-4.5 .. Q-4.11.

### Q-4.2 Deferral switch
- **Statement.** `deferral_enabled()` is true iff setting `ATOMICDB_DEFER_PROPAGATION` is truthy (default False) AND the
  synchronous ingest switch (Q-3.22) is off. With it off, the landing climbs inline inside the job's transaction
  (`propagate_landings([landing])`), and TB landings always climb inline (Q-3.11).
- **Inputs / outputs.** Settings only.
- **Code.** `atomicdb/propagation_queue.py:deferral_enabled:149-160`.
- **Tests.** `test_deferred_propagation.py::DurableDeferralTests::test_the_synchronous_ingest_still_climbs_inline`,
  `test_deferred_propagation.py::DurableDeferralTests::test_the_writers_take_what_is_left_once_deferral_is_off`.
  **Gaps:** none.
- **Determinism hazards.** Settings switch (deployment); production has it on.
- **History.** 5671bcf (#188).
- **Ordering.** Read by Q-3.21 (per job, before its transaction) and Q-3.24.

### Q-4.3 Writing a seed (defer)
- **Statement.** `defer(landing, extra)`: if `extra`, merge it into `landing.extra` (keys `job`, `verify`); create
  `PropagationSeed(position_id=landing.key, source=landing.source or '', payload=landing.to_payload(),
  next_attempt_at=now)`. The caller's transaction (the job's) owns the commit: the seed commits with stage (a) or not at
  all; a process dying between (a) and (b) leaves the row for the next drain. `landing.source` is the closure source
  current when the Landing was built (`current_closure_source()`, i.e. the task's source label from Q-3.9).
- **Inputs / outputs.** Writes one `PropagationSeed`.
- **Code.** `atomicdb/propagation_queue.py:defer:163-170`; `atomicdb/ingest.py:Landing.of:1346-1362`,
  `ingest_analysis:1303-1310`.
- **Tests.** `test_deferred_propagation.py::StageATests::test_the_search_is_stored_and_nothing_above_it_moves`,
  `test_deferred_propagation.py::DurableDeferralTests::test_the_job_commits_its_result_and_leaves_its_climb`.
  **Gaps:** none.
- **Determinism hazards.** `next_attempt_at = now` (line 170). The lists `parents`, `others`, `audience` are read from
  edges inside stage (a) (`Edge.objects.filter(child_id=...)` without `order_by`, `ingest.py:1358-1359`): their ORDER
  is database row order; for a single landing that order is the order of the inline climb's seeds.
- **History.** 5671bcf (#188).
- **Ordering.** End of stage (a) inside Q-3.11 branch 3.

### Q-4.4 Ready set of the propagation queue
- **Statement.** A seed is ready iff `next_attempt_at <= now`; ready seeds are ordered by `id` ascending. A row with
  `next_attempt_at = NULL` is NEVER ready (`defer` always sets it, so this does not happen in practice).
- **Inputs / outputs.** Reads `next_attempt_at`, `id`.
- **Code.** `atomicdb/propagation_queue.py:ready_seeds:173-176`.
- **Tests.** `test_deferred_propagation.py::DurableDeferralTests::test_a_climb_that_fails_keeps_its_rows_and_retries`.
  **Gaps:** NULL never ready is untested.
- **Determinism hazards.** Wall clock `now` (174).
- **History.** 5671bcf (#188).
- **Ordering.** Read by Q-4.5, Q-4.6.

### Q-4.5 Claim size: a row that failed before climbs alone
- **Statement.** `_claim_size(limit)`: read the `attempts` of the FIRST ready seed (Q-4.4, by id, ignoring locks). None
  ready: 0 (nothing to do). If that first row has `attempts > 0`, the batch size is 1; otherwise it is `limit`
  (default `BATCH_SIZE = 32`, the command's `--batch`, minimum 1). Only the first ready row is inspected: a failed row
  that is ready but not first rides in a full batch.
- **Inputs / outputs.** Reads `PropagationSeed.attempts`.
- **Code.** `atomicdb/propagation_queue.py:BATCH_SIZE:106`, `_claim_size:389-395`.
- **Tests.** `test_deferred_propagation.py::DurableDeferralTests::test_a_row_that_failed_climbs_alone`.
  **Gaps:** the "failed row not first" case is untested.
- **Determinism hazards.** Read outside the claim transaction; the row inspected may be locked/taken by then.
- **History.** 5671bcf (#188). BATCH_SIZE lowered from 128 to 32 by #322 (comment lines 102-117: 128-row batches lost
  4-6 batches an hour to deadlocks).
- **Ordering.** First step of Q-4.6.

### Q-4.6 Claiming a batch
- **Statement.** `drain_batch(limit)`: `size = _claim_size(limit)`; 0 returns 0. Then, in a transaction (inside a
  `carried_cuts()` block): take `ready_seeds()[:size]` with `SELECT ... FOR UPDATE SKIP LOCKED` (on a retry after a
  transient error, restricted to `pk IN ids` of the failed attempt, no others). No rows: return 0 (also on a retry
  whose rows vanished, which ends `drain`). `ids` = the claimed ids in id order. The row lock is the only claim; the
  rows are deleted at the end of the same transaction (Q-4.11).
- **Inputs / outputs.** Row locks on `PropagationSeed`.
- **Code.** `atomicdb/propagation_queue.py:drain_batch:398-422`.
- **Tests.** `test_deferred_propagation.py::EquivalenceTests::test_one_batch_per_landing_writes_the_same_as_one_merged_batch`,
  `test_propagation_throughput.py::DeadlockRetryTests::test_a_deadlock_climbs_the_same_rows_again_as_one_batch`.
  **Gaps:** concurrent drainers (skip_locked) untested.
- **Determinism hazards.** `skip_locked` (418); batch composition depends on what is ready at that instant (arrival
  timing of submits, backoff clocks) and on `--batch`: the same inputs can be climbed in different groupings, and the
  grouping can change values through the noise cut (Q-4.21).
- **History.** 5671bcf (#188); retry restriction #322 (525b862, 2026-11-26).
- **Ordering.** After Q-4.5; before Q-4.14 (registration), Q-4.7.

### Q-4.7 One climb per closure source
- **Statement.** Inside the batch transaction, the claimed rows (id order) are grouped by `row.source or ''`; groups are
  processed in the order their source first appears among the id-ordered rows. For each group, under
  `closure_attribution(source or None)` (empty means `NONE`), `_merge(group)` (Q-4.8) then
  `ingest.propagate_landings(merged)` (EXP-6); its per-landing summaries are collected (`summaries.update`, keyed by
  position key: a position present in two groups gets the later group's summary), the merged landings appended in group
  order, and the job payloads gathered per position. Closures written by the climb carry the group's source (and no
  provenance: provenance is not stored in the seed).
- **Inputs / outputs.** As EXP-6; closure source on `ClosureProvenance`/`NODE_CLOSED`.
- **Code.** `atomicdb/propagation_queue.py:drain_batch:443-453`.
- **Tests.** `test_deferred_propagation.py::EquivalenceTests::test_a_line_from_the_start_position`,
  `test_deferred_propagation.py::EquivalenceTests::test_a_transposition`,
  `test_deferred_propagation.py::EquivalenceTests::test_a_closure_that_reaches_another_parent`.
  **Gaps:** a batch mixing sources (two groups) is untested; the missing provenance (task/account/engine) on closures
  made by a deferred climb is untested.
- **Determinism hazards.** Group order follows id order (deterministic given the claim); a position landing twice in
  one batch with different sources is climbed twice, once per group.
- **History.** 5671bcf (#188).
- **Ordering.** After Q-4.6; Q-4.8 and EXP-6 per group; then Q-4.10.

### Q-4.8 Merging seeds of one position (_merge)
- **Statement.** `_merge(rows)`: read the current `Position` rows of all distinct positions in one `in_bulk` (inside the
  batch transaction, before the climb). For rows in order: build `Landing.from_payload(position_id, payload,
  source=row.source, pos=snapshot)`: it reads only `closed`, `others`, `parents`, `audience` (and `job`, `verify` into
  `extra`); the stored `cascade` and `backed` lists are IGNORED and recomputed by `cascade_seeds()` / `backed_seeds()`.
  The first row of a position gives its Landing; each later row of the same position extends the four lists with keys
  not already present (first-appearance order kept); its `extra` is not merged (the job payloads are kept separately).
  For every row, `jobs[position_id]` gets the row's full payload appended. Returns the landings whose position still
  exists (`pos is not None`), in first-arrival order, and `jobs`. The arms of a merged position run once, on the row as
  it stands at merge time.
- **Inputs / outputs.** Reads `Position` (full rows), seed payloads.
- **Code.** `atomicdb/propagation_queue.py:_merge:197-221`; `atomicdb/ingest.py:Landing.from_payload:1387-1394`,
  `cascade_seeds:1364-1375`, `backed_seeds:1377-1379`.
- **Tests.** `test_deferred_propagation.py::DeduplicationTests::test_two_landings_on_one_position_are_one_climb`,
  `test_deferred_propagation.py::DeduplicationTests::test_siblings_share_their_parent_in_one_climb`.
  **Gaps:** a seed whose position was deleted (landing dropped, but its rows still deleted and its job's `propagated`
  summary not written) is untested.
- **Determinism hazards.** The snapshot is read under READ COMMITTED while stage (a) writers commit concurrently: the
  arms see the position as of the merge, which may already include a later stage (a) of the same position whose seed
  is not in this batch.
- **History.** 5671bcf (#188).
- **Ordering.** Inside Q-4.7, before `propagate_landings`.

### Q-4.9 Batch seeds and per-landing arms (contract with EXP-6)
- **Statement.** For ONE landing, `propagate_landings` uses its seed lists in their natural order (the inline climb
  exactly). For SEVERAL, `cascade_seeds = sorted(set(all cascade seeds, empty keys dropped))` and `backed_seeds =
  sorted(set(all backed seeds))` (key order, i.e. sha256 hex order), ONE `backup_cascade` and ONE
  `backup_backed_evals`, then for each landing in order: `_uncertainty_expand([key, *parents])`,
  `_witness_refuted_revisit(pos, parents)`, `_queue_value_cascade(pos)`, `idle_repair.queue_from_landing(pos,
  parents)`, `autosolve.queue_from_landing(pos)` (arms, parts 3/4/6), `_recheck_once_committed` with skip set =
  all landed and closed keys of the batch (single landing: `{key, *closed}`), and finally one
  `_recheck_meeting_points_once_committed(landings[0].key, climb)`. Summaries share the batch-wide cascade/backed counts.
  The docstring claims the result equals a sequence of inline climbs once at rest, except the noise cut, the guards and
  seed order (Q-4.21).
- **Inputs / outputs.** See EXP-6, CAS-1, BKD-7, CAS-6 and the arms.
- **Code.** `atomicdb/ingest.py:propagate_landings:1397-1485`.
- **Tests.** `test_deferred_propagation.py::EquivalenceTests::test_one_batch_per_landing_writes_the_same_as_one_merged_batch`,
  `test_lost_wake.py::MeetingClimbTests::test_deferred_climb_that_wrote_last_reads_the_row_again`,
  `test_lost_wake.py::MeetingClimbTests::test_deferred_climb_that_rested_last_reads_the_row_again`,
  `test_lost_wake.py::MeetingClimbTests::test_one_climb_after_the_other_needs_no_repair`,
  `test_cascade_bulk_reads.py::EquivalenceTests::*` (8 tests driving `propagation_queue.drain`).
  **Gaps:** equivalence is tested on small lines; no test where the merged noise cut changes a value.
- **Determinism hazards.** Single vs batch seed order differ; the `len(landings) == 1` switch makes behaviour depend on
  batch size (claimed harmless; owner decision D2/D3 for v2).
- **History.** 5671bcf (#188).
- **Ordering.** Inside Q-4.7; the rechecks run in commit hooks (after Q-4.14's proof hook, which is registered first).

### Q-4.10 Recording the climb on its jobs (_record)
- **Statement.** Still inside the batch transaction: for each merged landing and each job payload of its position that
  has a `job` id, `by_job[job] = {**summaries[landing.key], 'batch': len(rows)}` (`batch` = number of seed rows claimed
  in the WHOLE batch, all groups). Each such `IngestJob.summary` becomes `{**summary, 'propagated': by_job[id]}`
  (bulk_update). Jobs of a landing dropped by `_merge` (deleted position) get nothing.
- **Inputs / outputs.** `IngestJob.summary.propagated`.
- **Code.** `atomicdb/propagation_queue.py:_record:224-238`; call `drain_batch:454`.
- **Tests.** `test_deferred_propagation.py::DurableDeferralTests::test_the_job_commits_its_result_and_leaves_its_climb`.
  **Gaps:** the `batch` count semantics are untested.
- **Determinism hazards.** `batch` depends on grouping (Q-4.6).
- **History.** 5671bcf (#188).
- **Ordering.** After all groups' climbs; before Q-4.11.

### Q-4.11 Settling: deleting the climbed rows
- **Statement.** `PropagationSeed.objects.filter(pk__in=ids).delete()` in the same transaction as the climb and the
  record; the commit settles them. A drainer that dies mid-climb rolls back and leaves the rows untouched (attempts not
  incremented). `drain_batch` returns `len(ids)` after Q-4.16.
- **Inputs / outputs.** Deletes `PropagationSeed` rows.
- **Code.** `atomicdb/propagation_queue.py:drain_batch:455,478`.
- **Tests.** `test_deferred_propagation.py::DurableDeferralTests::test_the_command_drains_and_reports_the_backlog`,
  `test_deferred_propagation.py::DurableDeferralTests::test_a_climb_that_fails_keeps_its_rows_and_retries`.
  **Gaps:** none.
- **Determinism hazards.** none beyond the transaction.
- **History.** 5671bcf (#188).
- **Ordering.** Last write of the batch transaction; then commit hooks (Q-4.14, then CAS-6 rechecks), then Q-4.16.

### Q-4.12 Transient retry of a whole batch
- **Statement.** If the batch raises and `ids` is non-empty and fewer than `TRANSIENT_RETRIES = 2` retries were made and
  the error is transient, sleep `TRANSIENT_RETRY_SLEEP * attempt` = 0.5 s, then 1.0 s, and climb the SAME rows again as
  one batch (claim restricted to `ids`, Q-4.6), with a fresh `carried_cuts` block and fresh summaries. Transient means:
  walking `error`, then `__cause__` or `__context__` repeatedly (cycle-safe by `id`), some exception has `pgcode` or
  `sqlstate` in `{'40P01' (deadlock_detected), '40001' (serialization_failure)}`. Nothing is written between attempts,
  so the values are the batch's.
- **Inputs / outputs.** None beyond the retried batch.
- **Code.** `atomicdb/propagation_queue.py:TRANSIENT_RETRIES:126`, `TRANSIENT_RETRY_SLEEP:127`,
  `_TRANSIENT_SQLSTATES:128-131`, `_is_transient:134-146`, `drain_batch:456-464`.
- **Tests.** `test_propagation_throughput.py::TransientTests::test_what_counts_as_transient`,
  `test_propagation_throughput.py::DeadlockRetryTests::test_a_deadlock_climbs_the_same_rows_again_as_one_batch`,
  `test_propagation_throughput.py::DeadlockRetryTests::test_the_retried_batch_writes_what_a_clean_batch_writes`,
  `test_propagation_throughput.py::DeadlockRetryTests::test_a_deadlock_that_keeps_coming_back_takes_the_old_path`,
  `test_propagation_throughput.py::DeadlockRetryTests::test_an_error_that_is_not_transient_is_not_retried`.
  **Gaps:** none.
- **Determinism hazards.** Exists only because of concurrent writers (Postgres); `time.sleep` (463). v2: not
  applicable.
- **History.** #322 (525b862, 2026-11-26): 9-13% of drainer time was lost to deadlocks taking the slow path.
- **Ordering.** Wraps Q-4.6 .. Q-4.11.

### Q-4.13 Failure path of a batch: per-row backoff, never parked
- **Statement.** On a non-transient error, or a transient one after 2 retries: if `ids` is non-empty,
  `_mark_failure(ids, error)`: for each row, `attempts := attempts + 1`, `last_error = str(error)[:2000]`,
  `next_attempt_at = now + BACKOFF_SECONDS[min(max(attempts-1,0),3)]` with `BACKOFF_SECONDS = (5, 30, 120, 600)` s
  (a failure to record the failure is logged). If the claim itself failed (`ids` empty), only log. Return -1. There is
  NO maximum: a seed retries for ever, at most every 600 s, and once due again it climbs alone if it is the first ready
  row (Q-4.5).
- **Inputs / outputs.** `PropagationSeed.attempts`, `last_error`, `next_attempt_at`.
- **Code.** `atomicdb/propagation_queue.py:BACKOFF_SECONDS:107`, `_backoff_seconds:179-181`, `_mark_failure:184-194`,
  `drain_batch:465-474`.
- **Tests.** `test_deferred_propagation.py::DurableDeferralTests::test_a_climb_that_fails_keeps_its_rows_and_retries`,
  `test_deferred_propagation.py::DurableDeferralTests::test_a_row_that_failed_climbs_alone`.
  **Gaps:** exact backoff values and the absence of a cap are untested.
- **Determinism hazards.** Wall clock (186, 191-192).
- **History.** 5671bcf (#188).
- **Ordering.** After a failed Q-4.6 .. Q-4.11 (all rolled back).

### Q-4.14 Proof numbers after the batch commit
- **Statement.** `proof_later = proof_after_commit_enabled()` = setting `ATOMICDB_PROOF_AFTER_COMMIT` (default True)
  AND the drainer is not already inside an atomic block (a test or shell draining inside its own transaction refreshes
  inside, as before). When true, before any climb the batch enters `ingest.proof_refresh_after_commit()`, a context
  that makes every `backup_cascade` inside append its proof-refresh seed list (`seed_keys + changed_keys`) to `calls`
  instead of refreshing (`ingest.py:1895-1900`), and registers `transaction.on_commit(refresh_proof, robust=True)` FIRST
  (so it runs before the rechecks' commit hooks). After the commit, `refresh_proof` hands `calls` to the pipeline
  (Q-4.15) if one is open, else runs `refresh_collected_proof_numbers(calls)` (each seed list, in call order, through
  `proof.refresh_proof_numbers`) on the drainer's thread; a `DatabaseError` is logged. When false, each cascade refreshes
  inside the transaction and Q-4.16 refreshes the landed keys after the commit.
- **Inputs / outputs.** pn/dn (PN-4, PN-8).
- **Code.** `atomicdb/propagation_queue.py:proof_after_commit_enabled:262-272`,
  `_refresh_proof_after_commit:275-281`, `_hand_over_proof:380-386`, `drain_batch:409,423-439`;
  `atomicdb/ingest.py:proof_refresh_after_commit:1929-1942`, `refresh_collected_proof_numbers:1945-1948`.
- **Tests.** `test_proof_after_commit.py::SameProofNumbersTests::test_a_line_from_the_start_position`,
  `test_proof_after_commit.py::SameProofNumbersTests::test_a_transposition`,
  `test_proof_after_commit.py::SameProofNumbersTests::test_a_closure_that_reaches_another_parent`,
  `test_proof_after_commit.py::SameProofNumbersTests::test_a_repetition_cycle`,
  `test_proof_after_commit.py::WhenTheRefreshRunsTests::test_the_refresh_waits_for_the_commit`,
  `test_proof_after_commit.py::WhenTheRefreshRunsTests::test_inside_another_transaction_it_refreshes_as_before`,
  `test_proof_after_commit.py::WhenTheRefreshRunsTests::test_the_setting_turns_it_off`,
  `test_propagation_throughput.py::HandOverTests::test_a_transposition`,
  `test_propagation_throughput.py::HandOverTests::test_a_repetition_cycle`.
  **Gaps:** none in scope.
- **Determinism hazards.** Outside the transaction a row another transaction holds is no longer skipped by
  `_refresh_campaign` (PN-4), so the refresh "only reaches further": the pn/dn written depend on timing. Settings
  switch; `connection.in_atomic_block`.
- **History.** 1e6c60f "a drained batch refreshes its proof numbers once, after its commit" (#240, 2026-11-13: pn/dn
  refresh was 65% of drainer time).
- **Ordering.** Registered at the start of the batch transaction; runs first after the commit, before the CAS-6
  rechecks' hooks and before Q-4.16.

### Q-4.15 Proof refresh pipeline thread
- **Statement.** While `proof_pipeline()` is open (the drainer service opens one unless `--proof-inline`, and only if
  setting `ATOMICDB_PROOF_PIPELINE` is truthy, default True, and no pipeline is already open in the process), each
  committed batch's `calls` are put on a FIFO queue of size `max(1, PIPELINE_DEPTH=1)` consumed by ONE daemon thread
  (`atomicdb-proof-refresh`) with its own DB connection, which runs them in commit order while the next batch climbs.
  `submit` blocks when the queue is full (the drainer waits after its commit, holding no lock). Exceptions in a refresh
  are logged and the thread continues. `close()` enqueues a sentinel and joins: leaving the pipeline (including on
  SIGTERM) runs every refresh already handed over; a process that dies loses the queued ones (pn/dn order a little worse
  until the next refresh reaching them). The thread closes its connections when it ends, if it ran anything.
- **Inputs / outputs.** pn/dn.
- **Code.** `atomicdb/propagation_queue.py:PIPELINE_DEPTH:313`, `ProofPipeline:316-354`, `_pipeline:357`,
  `proof_pipeline:360-377`; `atomicdb/management/commands/process_propagation.py:72-74`.
- **Tests.** `test_propagation_throughput.py::PipelineTests::test_refreshes_run_in_commit_order_and_close_waits`,
  `test_propagation_throughput.py::PipelineTests::test_a_drainer_too_far_ahead_waits`,
  `test_propagation_throughput.py::PipelineTests::test_a_failing_refresh_does_not_stop_the_pipeline`,
  `test_propagation_throughput.py::PipelineTests::test_the_setting_keeps_the_refresh_inline`,
  `test_propagation_throughput.py::PipelineTests::test_an_open_pipeline_is_used_and_closed`.
  **Gaps:** none (v1-specific mechanism).
- **Determinism hazards.** A second thread writes pn/dn concurrently with the next climb (which reads status/values and
  commits new statuses the refresh may or may not see). Module-level mutable `_pipeline` dict (357). Thread scheduling.
- **History.** #322 (525b862, 2026-11-26: the refresh was 39% of drainer time).
- **Ordering.** Consumes Q-4.14 hand-overs in commit order.

### Q-4.16 After the batch commit: landed pn/dn, carried cuts, deferred verifications
- **Statement.** After the commit (rows already gone, nothing here puts them back): (1) if the batch did not refresh
  after commit (`proof_later` false), `refresh_proof_numbers([landing.key for landing in landings])`; with
  `proof_later` true the landed keys were seeds of the batch's own refresh and are not refreshed twice;
  (2) if the batch's ascents left a cut frontier, `carry_cut_ascents(cut)` (BKD-9) — unlike Q-3.19, the result is not
  recorded in any job summary; (3) for each landing in order and each of its job payloads in arrival order that has a
  `verify` dict: `_verify_after(_Job(job_id, key), verify)` (Q-3.20 rules: queue cap, lock, `UNKNOWN` check,
  `enqueue_pv_verification`); if it returned non-None and `job_id` is set, the job's `summary.then_verify` is set
  (read-modify-write of the summary). Every step logs and swallows `DatabaseError`.
- **Inputs / outputs.** pn/dn, backed columns of ancestors, verification tasks, `IngestJob.summary.then_verify`.
- **Code.** `atomicdb/propagation_queue.py:_after_commit:481-515`, `_verify_requests:241-249`, `_Job:252-259`;
  call `drain_batch:477`.
- **Tests.** `test_deferred_propagation.py::DurableDeferralTests::test_the_requested_verification_waits_for_the_climb`.
  **Gaps:** carry after a deferred climb is untested; a verify for a job whose seed position was deleted is untested.
- **Determinism hazards.** Runs outside any transaction after other writers commit; concurrent with the pipeline thread.
- **History.** 5671bcf (#188); `proof_refreshed` flag 1e6c60f (#240).
- **Ordering.** After commit and its hooks (Q-4.14 hand-over, CAS-6 rechecks); last step of `drain_batch`.

### Q-4.17 Draining loop
- **Statement.** `drain(max_batches=None, limit=BATCH_SIZE)`: repeat `drain_batch(limit)` while `batches < max_batches`
  (or unbounded): result 0 stops; result < 0 counts a failed batch and continues (the failed rows wait for their
  backoff, others go on); result > 0 adds to `settled`. Returns `{settled, failed_batches, batches}` (failed batches are
  counted in `batches`).
- **Inputs / outputs.** As Q-4.5 .. Q-4.16.
- **Code.** `atomicdb/propagation_queue.py:drain:518-531`.
- **Tests.** `test_deferred_propagation.py::*` (all classes drive `drain`).
  **Gaps:** a claim failure (`ids` empty, -1) repeating for ever inside an unbounded `drain` is untested (it would spin).
- **Determinism hazards.** none beyond the batches.
- **History.** 5671bcf (#188).
- **Ordering.** Called by Q-4.18 and Q-4.19.

### Q-4.18 The propagation drainer service (process_propagation)
- **Statement.** `manage.py process_propagation [--once] [--batch N=32] [--idle-seconds S=1.0] [--proof-inline]
  [--status]`. ONE instance is the intended shape (a second corrupts nothing but brings deadlocks back). `--status`
  prints `backlog()` (`{waiting: count, oldest: ISO time, oldest_seconds}`) and exits. Otherwise, inside a proof
  pipeline (Q-4.15) unless `--proof-inline`, loop until SIGINT/SIGTERM: `drain(max_batches=1, limit=max(1, batch))` (one
  batch per turn, so a SIGTERM waits for one climb at most); with `--once`, stop when a turn did no batch or settled
  nothing and failed; without `--once`, when the turn did no batch or settled 0, `wait_idle(idle_seconds)` (sleep in
  0.1 s steps, minimum 0.1 s, cut by the stop flag). At exit (after the pipeline drained) prints totals JSON.
- **Inputs / outputs.** As the called rules.
- **Code.** `atomicdb/management/commands/process_propagation.py:Command.handle:46-91`;
  `atomicdb/propagation_queue.py:backlog:534-546`, `wait_idle:555-559`.
- **Tests.** `test_deferred_propagation.py::DurableDeferralTests::test_the_command_drains_and_reports_the_backlog`.
  **Gaps:** signal handling untested.
- **Determinism hazards.** `time.monotonic()` sleeps; deployment decides the instance count and `--batch`.
- **History.** 5671bcf (#188); pipeline and `--proof-inline` #322.
- **Ordering.** Service loop; v2: absent (climb runs to rest in the writer's batch).

### Q-4.19 Leftover seeds drained by the ingest writers when deferral is off
- **Statement.** When deferral is OFF (e.g. rolled back after a period on, or with the drainer stopped), each ingest
  processor, on its 30 s TB-sweep tick (Q-3.24), runs `propagation_queue.drain(max_batches=1)` (default limit 32) so no
  seed is stranded. With deferral ON the seeds belong to the drainer alone.
- **Inputs / outputs.** As Q-4.6 .. Q-4.16.
- **Code.** `atomicdb/management/commands/process_ingest_queue.py:83-97`.
- **Tests.** `test_deferred_propagation.py::DurableDeferralTests::test_the_writers_take_what_is_left_once_deferral_is_off`.
  **Gaps:** none.
- **Determinism hazards.** Up to five processors draining concurrently (skip_locked), on wall-clock ticks.
- **History.** 5671bcf (#188).
- **Ordering.** Inside Q-3.24 after Q-3.23.

### Q-4.20 Seeds in flight hold off the stale-backed repair
- **Statement.** `pending_positions(keys)` returns the subset of `keys` that have at least one `PropagationSeed` row
  (ready or in backoff). `sweep_stale_backed` (BKD-12) excludes from its repair every position that has an open
  `IngestJob` (`PENDING`/`PROCESSING`) or a pending seed: the landing's own climb is about to recompute that
  neighbourhood, and repairing it now would race with it. `backlog()` is read-only reporting (queue table in
  `lanes.queue_table`, the `--status` flag).
- **Inputs / outputs.** Reads `PropagationSeed.position_id`, `IngestJob.state/position_id`.
- **Code.** `atomicdb/propagation_queue.py:pending_positions:549-552`;
  `atomicdb/management/commands/sweep_stale_backed.py:688-703`; `atomicdb/lanes.py:313-326`.
- **Tests.** `test_stale_backed_sweep.py::SettleTests::test_a_position_with_its_own_result_landing_waits_for_it`.
  **Gaps:** the seed half (as opposed to the open-job half) is not tested separately.
- **Determinism hazards.** Timing: what is "in flight" depends on queue state at the sweep's read.
- **History.** 5671bcf (#188).
- **Ordering.** Read by BKD-12's sweep.

### Q-4.21 Batch-dependence of the deferred climb (documented semantics)
- **Statement.** The module docstring states what a batch climb does differently from the same landings climbed one
  after another, and claims none changes a value once the climb has come to rest: (1) the noise cut
  `BACKED_EPSILON_CP` (BKD-8): two 8 cp moves each stop below the cut inline but, merged, move 16 cp and wake the
  parents; the merged result is the value `sweep_stale_backed` would repair the sequential one to (same rest point,
  reached sooner) — i.e. BEFORE the sweep, sequential and batched states differ; (2) the size guards (ply, node budget,
  revisit cap, BKD-9) cut a climb by its size, and the frontier is carried on after the commit; (3) seed order (batch
  passes keys sorted; the cascade pops from a set). Not the same as any sequence: a later landing's stage (a) running
  before an earlier landing's climb (the child seed guard `_seed_child_eval`, CLM-1, reads `backed_nodes`, which only a
  climb writes); the docstring says the five writers already produced that schedule and deferral makes it more frequent.
  The arms and rechecks run per landing after the merged climb.
- **Inputs / outputs.** None of its own (semantic note binding Q-4.6 .. Q-4.9).
- **Code.** `atomicdb/propagation_queue.py` module docstring lines 40-81.
- **Tests.** `test_deferred_propagation.py::EquivalenceTests::test_one_batch_per_landing_writes_the_same_as_one_merged_batch`
  (equivalence on a small case). **Gaps:** no test shows the noise-cut divergence, nor the CLM-1 schedule dependence.
- **Determinism hazards.** This IS the main hazard: final stored values (before a sweep) depend on batch composition,
  which depends on arrival timing, backoff clocks, `--batch`, deadlocks, and concurrent stage (a) writers. Plan
  section 2.2.6 / owner decision D2.
- **History.** 5671bcf (#188); BATCH_SIZE 128 -> 32 in #322.
- **Ordering.** Applies to every deferred climb.

---

## Part 3: Priority, selector and budgets

Scope: `atomicdb/ingest.py` lines 30-460 (budget ladders, MultiPV, server-picked floor, interior cap), 5835-7180 (priority formula, regret, refresh v1/v2/delta, next_tasks, pool, proof/value selector, value cascade, bootstrap), the rung helpers it calls, the `Position.priority` tombstone lifecycle, campaign tagging as a priority input, and the selector/planner services (`refresh_selector`, `run_planner`, `selector_shadow`, `calibrate_planner`, `planner_status`, `atomicdb/planner/*`) at the level of what they write.

Units used throughout: **nodes** = engine nodes (budget of a search); **cp** = centipawns, White's perspective unless said otherwise; **units** = priority units (dimensionless float). `M` = 10^6, `B` = 10^9.

Production configuration observed in the repo (needed to know which branches are live):
- `ATOMICDB_SELECTOR=pn` in the selector's systemd unit (`docs/solver-allocation.md:15`), so `next_tasks` takes the proof/value path (PRI-4.4) and never reads `Position.priority` to choose work.
- `refresh_selector` runs with `--loop --interval 120 --no-priorities --no-coverage --no-adversarial --autosolve-cap 0 --pool-target 64` (`docs/server/atomicdb-selector.service.d/70-reserva-acotada.conf`, emergency valve of 11-Oct-2026, kept in tree 13-Oct by `d3443f4`). `--no-priorities` has been production since 29-Aug-2026 (`refresh_selector.py` help text). So in production **the priority column is not recomputed at all**; it only carries tombstones (PRI-1.7) and whatever an old pass left.
- `ATOMICDB_SELECTOR_V2` default off, `ATOMICDB_SELECTOR_DELTA` default on, `ATOMICDB_DESCENT` default `proof` (the production value of `ATOMICDB_DESCENT` is not recorded in the repo: see open questions).

---

### PRI-1.1 Priority constants
- **Statement.** The selector formula uses these named constants (all floats unless noted): `DEAD = -1e9` (tombstone value); `REGRET_WEIGHT = 3.0` units per 100 cp of regret; `DISCONNECTED_REGRET = 5` (regret **units**, not cp, charged to a position with no path from the root); `CAMPAIGN_BONUS = 40.0` units per `ln(1+votes)`; `EVAL_PROXIMITY_CAP = 1500` cp (int; caps the closeness term at 15 units); `MATE_BAND_BONUS = 50.0`; `UNEXPANDED_BONUS = 2.0`; `VISIT_WEIGHT = 1.5` units per visit; `REGRET_CAP = 3000` cp (int; saturates regret at 30 units); `FAR_REGRET = REGRET_CAP / 100.0 = 30.0` units (price of a connected node outside the bounded ball); `MATE_BAND = score_bands.BIG_LOW = 9000` cp. Live-row boundary everywhere is `priority > DEAD / 2` (i.e. > -5e8).
- **Inputs / outputs.** Constants only.
- **Code.** `atomicdb/ingest.py:DEAD..FAR_REGRET:5835-5862`; `atomicdb/ingest.py:MATE_BAND:94`.
- **Tests.** `test_selector_v2.py::AnchorTests::test_the_refuted_subtree_saturates_the_regret`, `test_selector_v2.py::AnchorTests::test_the_orphan_keeps_the_disconnected_price`, `test_selector_v2.py::HorizonTests::test_the_two_prices_are_not_the_same_number`. **Gaps:** none of the individual weights (1.5, 2.0, 50) is pinned by a value assertion except through ordering tests (`tests.py::SelectorTests::*`).
- **Determinism hazards.** none.
- **History.** Named constants introduced with the bounded selector, `53567d5` (2026-08-03, landed via PR #31); formula itself from `441724d` (2026-07-21, chessdb-style regret) and `5ef54c1` (2026-07-21, mate-band fast-track, dead-branch pruning).
- **Ordering.** Read by PRI-1.3..PRI-1.6, PRI-2.*, PRI-3.*.

### PRI-1.2 Value fed to the selector (best-known precedence)
- **Statement.** The value of a position for regret and for the closeness/mate terms is `known_eval_of(status, backed_eval, eval_cp)`: if `status` is WHITE_WIN -> +10000, BLACK_WIN -> -10000, DRAW -> 0 (`_status_eval`); else `backed_eval` if not NULL; else `eval_cp` (may be NULL). In the priority pass the rows are filtered to `status='UNKNOWN'`, so for scored rows it is `backed_eval ?? eval_cp`. In the regret Dijkstra it is used for every node, closed ones included (closed children still create gaps for their siblings). NULL means "no information".
- **Inputs / outputs.** Reads `Position.status`, `Position.backed_eval`, `Position.eval_cp`. Writes nothing.
- **Code.** `atomicdb/ingest.py:known_eval_of:5135`, `best_known_eval:5152`, `_status_eval:1984`; used at `_regret_from_root:6025`, `_BoundedNode.__init__:6104`, `refresh_priorities_v1:6311`, `refresh_priorities_v2:6719`.
- **Tests.** `test_selector_leak.py::RegretUsesBestKnownEvalTests::test_the_blind_parent_now_contributes_its_gap`, `::test_the_mate_band_bonus_sees_a_backed_only_node`, `::test_a_closed_sibling_still_creates_the_gap_it_should`. **Gaps:** none notable.
- **Determinism hazards.** none (pure function of columns). Note `budget_for` (PRI-5.3) uses raw `eval_cp`, not this value.
- **History.** "Fuga del 28-jul": regret used raw `eval_cp`, so a parent never searched directly gave a zero-gap back door into a refuted subtree; fixed `6c54e2f` (2026-07-27, "Stop the selector entering a refuted subtree by the blind door").
- **Ordering.** Before PRI-2.1/2.2 (value map) and PRI-1.5.

### PRI-1.3 Regret units and out-of-ball prices
- **Statement.** `_runits(regret)`: if `regret == +inf` (no path from the root found by the global Dijkstra) return `DISCONNECTED_REGRET = 5` units; else `min(regret, 3000) / 100.0` units (0..30). In the bounded engine (v2) a scored row with no finalized regret (outside the ball, PRI-2.3) is priced `FAR_REGRET = 30.0` if `Position.reachable` is true, else `DISCONNECTED_REGRET = 5`. The "far" price punishes, the "loose" price forgives (a hand-pasted FEN box deserves a look).
- **Inputs / outputs.** Regret (cp float or inf), `Position.reachable`.
- **Code.** `atomicdb/ingest.py:_runits:5891`; out-of-ball pricing `refresh_priorities_v2:6716-6718`.
- **Tests.** `test_selector_v2.py::AnchorTests::test_the_orphan_keeps_the_disconnected_price`, `test_selector_v2.py::HorizonTests::test_a_connected_node_beyond_the_horizon_pays_the_saturated_regret`, `::test_a_disconnected_node_beyond_the_horizon_pays_the_loose_price`, `test_selector_v2.py::BehindAClosedWallTests::test_both_engines_pay_the_loose_price`. **Gaps:** none.
- **Determinism hazards.** `Position.reachable` is a stored column with history: `expand` marks children reachable one ply when the parent is reachable (`ingest.py:755-760`), `backfill_reachable` recomputes it and stops at closed nodes. A child marked reachable before its only parent closed costs 30 in v2 and 5 in v1 (75-unit swing on a formula whose ceiling is ~67+bonus). v2 prices depend on when `backfill_reachable` last ran.
- **History.** `reachable` column + FAR_REGRET with the bounded selector `53567d5` (2026-08-03); the wall rule ("the column answers the Dijkstra's question") documented in the v2 docstring.
- **Ordering.** After PRI-2.*; feeds PRI-1.5.

### PRI-1.4 Campaign bonus
- **Statement.** Once per pass, `_active_campaign_votes()` loads `{Campaign.id: Campaign.votes}` for campaigns with `state = ACTIVE` (one query). `_campaign_bonus(campaign_id, votes)`: 0.0 if `campaign_id` is NULL or not in the dict (PROPOSED, PAUSED, DONE campaigns score nothing); else `40.0 * ln(1 + max(0, votes))`. An ACTIVE campaign with 0 votes scores 0 here (unlike the descent weight, PRI-4.4, which floors votes at 1).
- **Inputs / outputs.** Reads `Campaign.state`, `Campaign.votes`, `Position.campaign_id`.
- **Code.** `atomicdb/ingest.py:_active_campaign_votes:5865`, `_campaign_bonus:5876`.
- **Tests.** `test_campaigns.py::SelectorBonusTests::test_an_active_campaign_lifts_its_positions_over_identical_ones`, `::test_more_votes_weigh_more_but_with_diminishing_returns`, `::test_a_campaign_that_is_only_proposed_weighs_nothing`, `::test_a_paused_campaign_stops_weighing_without_losing_its_tags`, `test_selector_v2.py::AnchorTests::test_the_campaign_bonus_survives_the_new_engine`. **Gaps:** votes NULL would raise (`max(0, None)`); not tested (column assumed non-null).
- **Determinism hazards.** Votes read once at pass start; a vote landing mid-pass is seen next pass. `math.log1p` float.
- **History.** `4001003` (2026-07-29, "campanas con estado, votos y peso en el selector"). With `ATOMICDB_SELECTOR=pn` the bonus is inert (documented in `docs/solver-allocation.md` §1); campaigns reach the pn path through PRI-4.4 instead.
- **Ordering.** Computed before PRI-1.5 in each pass; `_priority_ceiling` (PRI-1.6) uses the max votes.

### PRI-1.5 The priority formula
- **Statement.** For one live position: `e = abs(known)` if `known` is not NULL else 0 (int cp). `priority = min(e, 1500)/100.0 + (50.0 if e >= 9000 else 0.0) + (0.0 if expanded else 2.0) - 3.0 * runits - 1.5 * visits + campaign_bonus`, evaluated left to right in that order (float). `known` per PRI-1.2, `runits` per PRI-1.3, `campaign_bonus` per PRI-1.4. Range without bonus: at most 15+50+2 = 67, regret subtracts up to 90 (30 units x 3), visits subtract without bound. Both engines (v1 and v2) call this single function so they give the same number for the same inputs.
- **Inputs / outputs.** Reads `known`, `Position.expanded`, `Position.visits`, `Position.campaign_id`, regret, campaign votes. Returns a float; written to `Position.priority` by PRI-3.3/3.4.
- **Code.** `atomicdb/ingest.py:priority_of:5904`.
- **Tests.** `tests.py::SelectorTests::test_global_best_first_by_eval`, `::test_priority_prefers_root_relevant_lines`, `::test_mate_band_boost_and_budget_jump`, `test_selector_v2.py::AnchorTests::test_computed_priorities_match_position_by_position`, `test_selector_v2.py::DeltaPassTests::test_a_touched_row_is_repriced`, `test_selector_service.py::RefreshSelectorCommandTests::test_command_writes_real_priorities`. **Gaps:** no test pins exact float values for a full formula instance (all assertions are orderings or v1==v2 equality).
- **Determinism hazards.** Float summation order matters for the "unchanged" check of PRI-3.4 (exact equality); the port must evaluate in the same order with IEEE doubles to reproduce write decisions (values would be equal anyway).
- **History.** `441724d` (2026-07-21); centralised in one function `53567d5` (2026-08-03).
- **Ordering.** After PRI-1.2..1.4 and PRI-2.*; before writes PRI-3.3/3.4.

### PRI-1.6 Priority ceiling (horizon bound)
- **Statement.** `_priority_ceiling(votes) = 1500/100 + 50 + 2 + 40 * ln(1 + max(0, max(votes.values(), default=0)))`. It is an upper bound of everything the formula can add to any node; the subtractive terms are excluded (visits >= 0). Used only by the bounded Dijkstra's stop test (PRI-2.3).
- **Inputs / outputs.** ACTIVE campaign votes.
- **Code.** `atomicdb/ingest.py:_priority_ceiling:5924`.
- **Tests.** `test_selector_v2.py::HorizonTests::test_the_horizon_stops_the_walk` (indirect). **Gaps:** no direct value test.
- **Determinism hazards.** none.
- **History.** `53567d5` (2026-08-03).
- **Ordering.** Computed at the start of PRI-2.2.

### PRI-1.7 Tombstones (DEAD) and their revival
- **Statement.** A tombstone is `Position.priority = DEAD (-1e9)`. It is **set** only by the minting selectors when a candidate fails `_still_reachable` (PRI-1.8): `next_tasks` regret path and `_next_tasks_by_proof` both do `pos.priority = DEAD; pos.save(update_fields=['priority'])` and skip it (no task). Live rows are `priority > DEAD/2`; every priority pass filters `status='UNKNOWN' AND priority > DEAD/2`, so a tombstone is **never rescored** and no number of campaign votes resurrects it. Tombstones are **revived** (priority set to the sentinel `0.0`, a high value next to rows carrying regret) by other families: (a) `expand` when a new edge reaches a tombstoned child (`priority=0.0`, `updated` NOT touched); (b) `link_existing_children` likewise (`updated` not touched); (c) `_revive_tombstones(keys)` on revocation: the keys and their direct children only (one level), with `updated=now`; (d) `_walk_pv_frontier` (Verify PV) and `views.goto` (explorer click) on the child they step into (`save(update_fields=['priority'])`, `updated` not touched). Other consumers treat `priority > DEAD/2` as "open branch" (idle repair, autosolve, backlog filters). `next_tasks` (regret mode) does **not** filter tombstones in its query (PRI-4.2).
- **Inputs / outputs.** Writes `Position.priority`. No event.
- **Code.** set: `atomicdb/ingest.py:next_tasks:6850-6853`, `_next_tasks_by_proof:7113-7116`; respected: `refresh_priorities_v1:6305-6306`, `_priority_rows:6619`, `_regret_from_root_bounded:6193`; revived: `ingest.py:link_existing_children:666-671`, `expand:747-754`, `_revive_tombstones:5474`, `_walk_pv_frontier:10101-10103`, `views.py:goto:4400-4402`.
- **Tests.** `tests.py::SelectorTests::test_dead_branch_not_selected`, `::test_tombstones_survive_refresh_no_starvation`, `::test_new_edge_revives_tombstoned_position`, `test_campaigns.py::SelectorBonusTests::test_a_tombstone_is_not_resurrected_by_any_number_of_votes`, `test_selector_v2.py::AnchorTests::test_tombstones_are_not_resurrected`, `test_selector_v2.py::MarkingWhatMovedTests::test_reviving_a_tombstone_marks_the_row`, `::test_reviving_marks_the_direct_children_too`, `test_ingest_queue.py::ShadowBackfillTests::test_a_tombstoned_position_is_not_a_zombie_and_is_still_served`, `test_idle_repair.py::ConnectorTests::test_a_dead_branch_is_not_bought`. **Gaps:** revival through `link_existing_children`, `_walk_pv_frontier` and `goto` is not pinned here; no test that a tombstone is never re-set when it becomes reachable again without a revival path (it simply stays dead).
- **Determinism hazards.** Revivals (a), (b), (d) do not set `updated`, so a delta pass (PRI-3.6) does not see the row move; a revived row outside the ball keeps the provisional 0.0 until something else touches it. Tombstoning happens inside the lease HTTP path (`views.api_lease` -> `next_tasks`), so its timing depends on worker polls.
- **History.** `5ef54c1` (2026-07-21, dead-branch pruning); "las ramas muertas no resucitan" in the v1 docstring; `_revive_tombstones` marks `updated` since the delta mode (`d519cfd`, 2026-08-04).
- **Ordering.** Set during PRI-4.2/4.4; respected by PRI-3.*; revived by EXP-1/POS-3/REV-1/MPV-6 (other parts).

### PRI-1.8 `_still_reachable` (does analysing it still matter upward)
- **Statement.** `_still_reachable(pos)`: if the position has no incoming edge (`Edge.child = pos`), return True (root or loose seed). Else return True iff at least one parent has `status = 'UNKNOWN'`. A position all of whose parents are closed is "useless": the minting selectors tombstone it (PRI-1.7). Accepts a Position or a key (callers in `backlog.py` and `sweep.py` pass keys/rows).
- **Inputs / outputs.** Reads `Edge.child_id`, parent `Position.status`. Two queries (`exists()`).
- **Code.** `atomicdb/ingest.py:_still_reachable:6770`; callers `next_tasks:6850`, `_next_tasks_by_proof:7113`, `backlog.py:498,507,521`, `sweep.py:641`.
- **Tests.** `tests.py::SelectorTests::test_dead_branch_not_selected`. **Gaps:** the "no parents = reachable" branch and multi-parent mixes are not pinned directly.
- **Determinism hazards.** READ COMMITTED: a parent closing or reopening concurrently changes the answer; no locking.
- **History.** `5ef54c1` (2026-07-21).
- **Ordering.** Called just before minting in PRI-4.2/4.4.

### PRI-1.9 Campaign ownership by inheritance (priority input)
- **Statement.** `Position.campaign_id` (the input of PRI-1.4) is set at creation only: a child created by `expand`/`positions_for` inherits the parent's `campaign_id`; `get_or_create_position(fen, campaign_id)` passes it only in `defaults`, so an existing position keeps its owner (or none). An untagged parent produces untagged children. Positions born from an explorer click inherit too.
- **Inputs / outputs.** Writes `Position.campaign_id` on insert.
- **Code.** `atomicdb/ingest.py:get_or_create_position:473`, `_get_or_create_position:492-498`, `create_positions:568-576`, `expand:737`.
- **Tests.** `test_campaigns.py::InheritanceTests::test_a_child_born_under_a_tagged_parent_inherits_the_tag`, `::test_a_child_that_already_existed_keeps_having_no_owner`, `::test_an_untagged_parent_does_not_invent_an_owner`, `::test_a_node_born_from_a_click_in_the_explorer_inherits_too`. **Gaps:** none.
- **Determinism hazards.** First creator wins under transpositions: which campaign owns a transposed node depends on which parent created it first (log order).
- **History.** `4001003` (2026-07-29).
- **Ordering.** POS-2 (creation); cross-ref to the campaign-ownership rules owned elsewhere (community_campaign rotate is not in this part).

### PRI-1.10 Campaign subtree adoption on activation
- **Statement.** When staff POSTs `action=activate` to `/atomicdb/campaign/<id>/state`, inside one transaction the campaign goes ACTIVE and `_tag_campaign_subtree` runs a BFS from `campaign.root_id` over `Edge` (children read in batches of `CAMPAIGN_TAG_BATCH = 500` parents), stopping when `len(seen) >= CAMPAIGN_TAG_MAX_NODES = 50_000` (the root counts). Every visited key with `campaign IS NULL` is updated to `campaign=<id>, updated=now()` (queryset update in batches of 500). Positions owned by another campaign are not stolen. `pause`/`done` never untag. Returns `(tagged, visited)` in the reply.
- **Inputs / outputs.** Reads `Edge`; writes `Position.campaign_id`, `Position.updated`; `Campaign.state` via `apply_state`.
- **Code.** `atomicdb/views.py:_tag_campaign_subtree:8572`, constants `views.py:364-365`, call `views.py:campaign_state:8821-8823`.
- **Tests.** `test_campaigns.py::ActivationTaggingTests::test_activating_tags_the_root_and_the_subtree_below_it`, `::test_the_answer_says_how_much_it_adopted`, `::test_a_position_another_campaign_already_owns_is_not_stolen`, `::test_the_walk_stops_at_its_cap`, `::test_pausing_does_not_hand_the_subtree_back`, `test_selector_v2.py::MarkingWhatMovedTests::test_adopting_a_subtree_into_a_campaign_marks_the_rows`. **Gaps:** which 50,000 nodes are adopted when the cap bites is not pinned.
- **Determinism hazards.** `Edge.objects.filter(parent_id__in=...)` has no `order_by`: when the cap bites, the adopted set depends on DB row order; `updated=timezone.now()` wall clock.
- **History.** `4001003` (2026-07-29); `updated` marking added with delta mode (2026-08-04).
- **Ordering.** After `Campaign.apply_state(ACTIVE)`, same transaction; before the next priority pass.

---

### PRI-2.1 Global regret Dijkstra (v1)
- **Statement.** `_regret_from_root()` loads **all** positions (`key, fen, eval_cp, backed_eval, status`) into `val[key] = known` (PRI-1.2), `white_stm[key] = (fen side field == 'w')`, `settled = {key: status != UNKNOWN}`, and all edges into `children[parent] = [child...]`. `regret` starts at +inf for every loaded key; `regret[root] = 0` if the root key (`root_key()`, sha256 of the canonical start FEN) exists; heap `[(0.0, root)]`. Pop `(r, k)`; skip if `r > regret[k]`. If `k` is settled, do **not** relax its children (a closed node is not a motorway into a solved subtree). If `white_stm` has no entry for `k` (row born between the two snapshots), skip it. Otherwise with kids = children[k]: `known` = values of kids whose value is not NULL; `best = max(known)` if White to move else `min(known)`, None if no known kids. For each kid: `gap = (best - v)` (White to move) or `(v - best)` (Black to move) when both `v` and `best` are not NULL, else 0 (unvalued children inherit the parent's regret: optimism). `nr = r + gap`; if `nr < regret.get(kid, inf)`, set and push. Returns `{key: regret}` with inf for unreached keys (including kids reached only via edges whose row was missing from the snapshot, which get a finite regret but are not in `val`). Transpositions take the minimum route. Gaps are >= 0 so Dijkstra is exact.
- **Inputs / outputs.** Reads all `Position` rows (5 columns) and all `Edge(parent_id, child_id)`. Returns dict; writes nothing.
- **Code.** `atomicdb/ingest.py:_regret_from_root:5999`.
- **Tests.** `tests.py::SelectorTests::test_priority_prefers_root_relevant_lines`, `test_selector_leak.py::RegretUsesBestKnownEvalTests::test_a_closed_parent_no_longer_hands_out_a_zero_gap_path`, `::test_a_materialised_won_line_is_not_a_motorway`, `::test_a_closed_sibling_still_creates_the_gap_it_should`, `test_selector_v2.py::MissingRowTests::test_v1_skips_the_newborn`, `test_selector_v2.py::BehindAClosedWallTests::test_the_walk_stops_at_the_wall_and_so_does_the_column`. **Gaps:** root key absent (empty DB) behaviour (heap still starts with the root key; `regret.get(root)` is inf so it is skipped unless present) not pinned.
- **Determinism hazards.** Two unsynchronised snapshots (positions, then edges) in autocommit under READ COMMITTED: rows/edges born between them produce the "newborn" case (skipped). Result values are order-independent (minimum over paths); heap ties break by key string. O(graph) memory: 2 dicts of the whole DB.
- **History.** `441724d` (2026-07-21); closed nodes stop relaxing `1b15ab5` (2026-07-27, "close the second regret leak", fuga #2); best-known values `6c54e2f` (2026-07-27); newborn skip after a KeyError that killed the shadow (4-Aug).
- **Ordering.** First step of PRI-3.3.

### PRI-2.2 Bounded regret Dijkstra (v2)
- **Statement.** `_regret_from_root_bounded(campaign_votes, top_n, batch_size=500)` has the same gap semantics as PRI-2.1 but reads the graph lazily. State: `node` cache (`_BoundedNode`: value=known, white_stm, settled, expanded, visits, campaign_id, stored priority), `regret` (best known incl. frontier), `done` (finalized). Start `regret[root]=0`, heap `[(0, root)]`. Loop: pop up to `batch_size` entries, discarding entries whose key is already done or whose `r > regret[key]`; if the batch is empty continue. **Horizon test** (PRI-2.3) on the first (smallest) entry, before any query; break if it fails. Load the batch's rows (`_load_bounded_nodes`: keys not cached, sorted, `IN` chunks of 500, columns `key, fen, eval_cp, backed_eval, status, expanded, visits, campaign_id, priority`). For each batch entry: `done[key] = r`; if the row is missing skip (never KeyError); if not settled and `priority > DEAD/2`, offer it to the top-K with `priority_of(value, expanded, visits, campaign_id, _runits(r), votes)`. Then read edges `parent_id IN (unsettled batch keys)`, load all those children, and relax exactly as PRI-2.1 except: children missing from the cache are skipped (no regret), and children already in `done` are skipped. Returns `done` = `{key: finalized regret}` of everything popped (including settled and missing-row keys). Keys not in the result are "outside the ball" and priced by PRI-1.3.
- **Inputs / outputs.** Reads `Position` (9 columns, by key batches) and `Edge` (by parent batches). Returns dict.
- **Code.** `atomicdb/ingest.py:_BoundedNode:6090`, `_regret_from_root_bounded:6113`, `_load_bounded_nodes:6237`, `BOUNDED_BATCH:5996`.
- **Tests.** `test_selector_v2.py::AnchorTests::test_the_ball_covers_the_whole_graph`, `::test_computed_priorities_match_position_by_position`, `::test_the_refuted_subtree_saturates_the_regret`, `test_selector_v2.py::MissingRowTests::test_v2_finishes_the_pass`, `test_selector_v2.py::BehindAClosedWallTests::test_the_walk_stops_at_the_wall_and_so_does_the_column`. **Gaps:** see PRI-2.4 (batch-boundary approximation) - untested.
- **Determinism hazards.** Reads are not a snapshot (each batch is its own statement under READ COMMITTED); a row not yet visible is skipped. Heap ties by key string. Result depends on `batch_size` (PRI-2.4).
- **History.** `53567d5` (2026-08-03, "the selector walks the ball that matters, not the whole graph"), landed via PR #31. Off by default (`ATOMICDB_SELECTOR_V2`).
- **Ordering.** First step of PRI-3.4.

### PRI-2.3 Horizon cut and top-K
- **Statement.** `selector_horizon_width() = max(4 * views.TASK_REFILL_COUNT, SELECTOR_HORIZON_FLOOR) = max(4*8, 1000) = 1000`. The bounded Dijkstra keeps a `_TopK(top_n)` min-heap of `(priority, key)` of offered candidates (only unsettled rows with `priority > DEAD/2`). `cut` = priority of the K-th best offered so far, `-inf` while fewer than K offered. Stop rule, evaluated on the first entry of each batch before loading: `ceiling - 3.0 * min(r_min, 3000) / 100.0 < cut` -> stop the walk (nothing below can reach the top-K because regret only grows). The cut only rises, so pruning errs on the side of exploring more.
- **Inputs / outputs.** None stored.
- **Code.** `atomicdb/ingest.py:_TopK:5938`, `SELECTOR_HORIZON_FLOOR:5976`, `selector_horizon_width:5979`, stop test `6179-6181`, offer `6193-6196`.
- **Tests.** `test_selector_v2.py::HorizonTests::test_the_horizon_stops_the_walk`, `test_selector_v2.py::SelectorSwitchTests::test_the_horizon_follows_the_lease_batch`, `test_selector_v2.py::DeltaPassTests::test_the_fixture_really_leaves_nodes_outside_the_ball`. **Gaps:** `_TopK` tie behaviour.
- **Determinism hazards.** `_TopK.offer` replaces only on strictly greater priority; heap ordering ties compare keys (string), so which equal-priority key stays in the top-K depends on offer order. Offer order follows heap pop order (deterministic given data).
- **History.** `53567d5` (2026-08-03).
- **Ordering.** Inside PRI-2.2.

### PRI-2.4 Batch-boundary approximation of the bounded Dijkstra
- **Statement.** Because v2 finalizes **all** entries of a popped batch (`done[key] = r`) before relaxing any of them, a node B in the same batch as a node A keeps its popped regret even if A -> B would give a smaller one (`nr < r_B`), since relaxation skips `child in done`. v1 (PRI-2.1) would give the smaller value. The result therefore depends on `BOUNDED_BATCH = 500` and is an over-estimate in that case. Equality v1 == v2 holds when no batch contains such a pair (all current tests).
- **Inputs / outputs.** As PRI-2.2.
- **Code.** `atomicdb/ingest.py:_regret_from_root_bounded:6185-6186` (finalize) and `6224` (skip done).
- **Tests.** none. **Gaps:** untested divergence.
- **Determinism hazards.** Batch size and heap order dependent.
- **History.** Present since `53567d5`.
- **Ordering.** Inside PRI-2.2.

---

### PRI-3.1 `refresh_priorities` dispatcher and engine switch
- **Statement.** `refresh_priorities(force=False, top_k=None, delta=None)` calls `refresh_priorities_v2(force, top_k, delta)` if `settings.ATOMICDB_SELECTOR_V2` is truthy (env `ATOMICDB_SELECTOR_V2` in `1/true/yes`, default False), else `refresh_priorities_v1(force, top_k)` (`delta` ignored). Required deployment order for v2: migration 0036, `backfill_reachable`, `selector_shadow`, then the switch.
- **Inputs / outputs.** Settings only.
- **Code.** `atomicdb/ingest.py:refresh_priorities:6751`, `selector_v2_enabled:6813`; `OpenSite/settings.py:151-152`.
- **Tests.** `test_selector_v2.py::SelectorSwitchTests::test_the_switch_defaults_to_off`, `::test_the_switch_routes_to_the_bounded_engine`, `::test_the_service_pass_goes_through_the_switch`. **Gaps:** none.
- **Determinism hazards.** Settings/env switch.
- **History.** `53567d5` (2026-08-03).
- **Ordering.** Called by PRI-6.1 (service) and PRI-3.9 (inline).

### PRI-3.2 Refresh throttle, `force` and compute-only mode
- **Statement.** Both engines keep a process-local `_priority_refresh_cache = {'at': monotonic seconds}`. Unless `force` or `top_k` is given, a call within `PRIORITY_REFRESH_SECONDS = 30.0` s of the last completed write pass returns False without doing anything. `top_k` (compute-only, "shadow") never writes, never reads/updates the cache or the delta state, never uses delta, and returns `{key: priority}` of the top `top_k` by priority (`_TopK`). A completed write pass sets `cache['at']` to the **start** time of that pass and returns True.
- **Inputs / outputs.** Process memory only.
- **Code.** `atomicdb/ingest.py:PRIORITY_REFRESH_SECONDS:156`, `_priority_refresh_cache:176`, `refresh_priorities_v1:6288-6292,6302,6322-6326`, `refresh_priorities_v2:6693-6697,6706-6707,6740-6744`.
- **Tests.** `test_selector_service.py::RefreshSelectorCommandTests::test_command_ignores_the_process_local_cache`, `test_selector_v2.py::AnchorTests::test_compute_only_mode_caps_the_head_and_writes_nothing`, `test_selector_v2.py::DeltaPassTests::test_the_shadow_never_runs_incremental`, `test_selector_v2.py::WriteVolumeTests::test_the_shadow_does_not_disturb_the_last_real_pass`, `test_performance_guards.py::PriorityRefreshCacheTests::test_inline_fallback_still_refreshes_only_once`. **Gaps:** none.
- **Determinism hazards.** `time.monotonic()` wall clock; per-process cache (each gunicorn worker has its own).
- **History.** Throttle from the inline era; `force` used by the service so its interval is the only clock (`refresh_selector.py:288-292`).
- **Ordering.** First check of PRI-3.3/3.4.

### PRI-3.3 Full pass, v1 engine
- **Statement.** `refresh_priorities_v1`: take `delivered_at = timezone.now()` before reading; compute `regret = _regret_from_root()` (PRI-2.1) and campaign votes; iterate `Position.filter(status='UNKNOWN', priority > DEAD/2)` as full instances (`iterator(chunk_size=2000)`); for each compute `priority_of(best_known_eval(pos), pos.expanded, pos.visits, pos.campaign_id, _runits(regret.get(key, inf)), votes)`. If it differs (`!=`) from the stored value, collect; after the loop `bulk_update(['priority'])` in batches of 500 (does not touch `updated`). Record the pass as mode `full` with `rows = scored`, write counters 0 (not instrumented), and persist `delivered_at` as the delta marker (PRI-3.7), so a later v2 delta pass can follow a v1 pass.
- **Inputs / outputs.** Reads all UNKNOWN live positions; writes `Position.priority`.
- **Code.** `atomicdb/ingest.py:refresh_priorities_v1:6248`.
- **Tests.** `test_selector_service.py::RefreshSelectorCommandTests::test_command_writes_real_priorities`, `test_selector_v2.py::AnchorTests::test_written_priorities_match_too`, `tests.py::SelectorTests::test_tombstones_survive_refresh_no_starvation`, `test_campaigns.py::SelectorBonusTests::test_a_closed_position_does_not_come_back_because_it_was_voted`. **Gaps:** none notable.
- **Determinism hazards.** Not a snapshot: regret is computed from snapshots taken before the scoring scan; rows changed meanwhile are scored with fresh columns but stale regret. `timezone.now()`. Unordered iteration (harmless: per-row function).
- **History.** Moved out of the lease path into the `refresh_selector` service (P0d); inline fallback kept (PRI-3.9).
- **Ordering.** After PRI-2.1; before PRI-3.7.

### PRI-3.4 Pass, v2 engine (full or delta)
- **Statement.** `refresh_priorities_v2`: `delivered_at = timezone.now()` first; votes; `regret = _regret_from_root_bounded(votes)` (PRI-2.2); `since = _delta_since(delivered_at, delta)` (PRI-3.5) unless compute-only. Rows from `_priority_rows(since, regret)` (PRI-3.6) as column tuples `(key, eval_cp, backed_eval, expanded, visits, campaign_id, priority, reachable)`. Per row: `runits = _runits(r)` if the key has a finalized regret else `30.0` if `reachable` else `5`; `prio = priority_of(known_eval_of('UNKNOWN', backed_eval, eval_cp), expanded, visits, campaign_id, runits, votes)`. If `priority == prio` (exact float equality) count `unchanged` and skip; else `written += 1`, add `|prio - priority|` to the histogram (PRI-3.7) when the old priority is not NULL, append `Position(key=key, priority=prio)` and `bulk_update(['priority'])` every `PRIORITY_WRITE_BATCH = 500` rows (writes interleaved with the read). Record mode `delta` if `since` else `full`, `rows`, `written`, `unchanged`, `ball = len(regret)`, histogram; persist the marker.
- **Inputs / outputs.** Reads `Position` columns; writes `Position.priority` only (not `updated`).
- **Code.** `atomicdb/ingest.py:refresh_priorities_v2:6630`, `PRIORITY_WRITE_BATCH:6331`, `_PRIORITY_COLUMNS:6510`.
- **Tests.** `test_selector_v2.py::AnchorTests::test_written_priorities_match_too`, `test_selector_v2.py::WriteVolumeTests::test_a_rewritten_row_is_counted_and_its_change_is_sized`, `::test_a_second_pass_over_a_quiet_graph_writes_nothing`, `::test_the_ball_is_reported_and_fits_inside_what_was_scored`, `test_selector_v2.py::DeltaPassTests::test_the_priority_write_does_not_dirty_the_marker`, `test_selector_v2.py::DeltaAgreesWithTheCompletePassTests::test_delta_writes_what_a_complete_pass_would_write`. **Gaps:** no write epsilon exists yet (the histogram was added to choose one; never chosen).
- **Determinism hazards.** Writing while reading: if the DB scans by the `priority` index, a row can be seen twice (same number) or missed (keeps last pass's value). READ COMMITTED; `timezone.now()`; exact float equality.
- **History.** `53567d5` (2026-08-03); write counters/histogram `62409d5` (2026-08-05, after 5-Aug measured ~5M rows rewritten per pass at 704 UPDATE/s from micro-drift).
- **Ordering.** After PRI-2.2 and PRI-3.5; before PRI-3.7.

### PRI-3.5 Delta-mode selection (`_delta_since`)
- **Statement.** Returns the lower bound `since` of a delta pass, or None for a full pass. None if `delta is False`, or `delta is None` and `settings.ATOMICDB_SELECTOR_DELTA` is falsy (env `ATOMICDB_SELECTOR_DELTA` in `0/false/no` turns it off; anything else, including empty, keeps it on; default True). `delta=True` requests delta even with the setting off. Marker: in-process `_selector_delta_state['at']`; if None and the disk has not been checked in this process, read once from `<ATOMICDB_STATE_DIR>/selector_delta.json` (`{"at": ISO-8601}`; missing file, bad JSON, or naive timestamp -> None). None -> full (first pass of a history). If `now - last > SELECTOR_DELTA_MAX_GAP = 12 h` -> full. Else `since = last` (the **start** time of the last completed pass, so writes during that pass are seen by the next).
- **Inputs / outputs.** Reads settings, the state file; writes `_selector_delta_state['at']`, `['disk_checked']`.
- **Code.** `atomicdb/ingest.py:SELECTOR_DELTA_MAX_GAP:6394`, `_selector_delta_state:6455`, `_delta_state_path:6460`, `_load_delta_state:6467`, `selector_delta_enabled:6514`, `_delta_since:6582`; `OpenSite/settings.py:162-169`.
- **Tests.** `test_selector_v2.py::SelectorDeltaSwitchTests::test_an_empty_environment_leaves_the_incremental_pass_on`, `::test_the_environment_can_bring_the_complete_pass_back`, `::test_anything_that_is_not_a_no_keeps_it_on`, `test_selector_v2.py::DeltaPassTests::test_the_first_pass_of_a_process_is_complete`, `::test_the_second_one_is_incremental`, `::test_a_long_silence_falls_back_to_a_complete_pass`, `::test_the_switch_brings_the_complete_pass_back`, `test_selector_v2.py::DeltaMarkerPersistenceTests::*` (8 tests: gap outlives a pass, marker on disk, restart resumes, reset forgets disk, broken marker, naive timestamp, stale marker, unwritable dir). **Gaps:** none.
- **Determinism hazards.** Wall clock (`timezone.now()` vs a persisted timestamp), a file on disk, process-local state, env switch.
- **History.** `d519cfd` (2026-08-04); gap raised from 10 min to 12 h and disk marker `3722e1a` (2026-08-05: with 10 min the delta was unreachable by construction, six full passes of 24 min-2.2 h on 4-Aug).
- **Ordering.** Inside PRI-3.4 after regret; before PRI-3.6.

### PRI-3.6 Delta key set
- **Statement.** A delta pass rescores (filter `status='UNKNOWN' AND priority > DEAD/2` unchanged) only the keys in: (1) the whole ball (every key returned by PRI-2.2, finalized); (2) `touched` = keys with `Position.updated > since` (sorted); (3) direct parents of touched keys (`Edge.child_id IN touched` in chunks of 500 -> `parent_id`). The parents ply covers the backed cascade, which writes `backed_eval` with `bulk_update` that does not set `updated`. The union is sorted and read in `IN` chunks of 500. A full pass reads every live row. Rows outside the ball that were not touched keep the previous pass's priority (stale tail tolerated).
- **Inputs / outputs.** Reads `Position.updated` (index `atomic_pos_updated`), `Edge`.
- **Code.** `atomicdb/ingest.py:_delta_keys:6598`, `_priority_rows:6612`; index `atomicdb/models.py:164`.
- **Tests.** `test_selector_v2.py::DeltaPassTests::test_a_touched_row_is_repriced`, `::test_the_direct_parent_of_a_touched_row_is_repriced_too`, `::test_an_untouched_row_keeps_its_priority`, `::test_the_ball_is_repriced_every_pass_whatever_happened`, `::test_a_delta_pass_reprices_fewer_rows_than_a_full_one`. **Gaps:** grandparents of a touched row whose ascent changed `backed_eval` two plies up are assumed to be inside the ball (not tested).
- **Determinism hazards.** Correctness depends on every writer of a formula term setting `updated` (queryset `update()` and `bulk_update` do not, unless explicit): known non-markers are tombstone revivals in `expand`/`link_existing_children`/`goto`/`_walk_pv_frontier` (PRI-1.7). Wall-clock comparison `updated > since`.
- **History.** `d519cfd` (2026-08-04).
- **Ordering.** Inside PRI-3.4.

### PRI-3.7 Pass record, marker persistence and write histogram
- **Statement.** `_record_pass(mode, rows, started, delivered_at, written, unchanged, ball, hist)` stores into `_selector_delta_state`: mode (`full`/`delta`), rows, seconds (`round(monotonic - started, 3)`), written, unchanged, ball, hist; if `delivered_at` is given it becomes `at` and is persisted atomically (`selector_delta.json.tmp` then `os.replace`) to `ATOMICDB_STATE_DIR`; any OSError is ignored. Only completed write passes record (a killed pass leaves the previous marker, so the next delta covers from the last complete one). Histogram buckets of `|new - old|`: `le-3` (<= 1e-3), `le-2` (<= 1e-2), `le-1` (<= 1e-1), `le0` (<= 1), `le+1` (<= 10), `gt+1` (> 10); all six always present. `selector_pass_report()` returns a copy for the service's JSON log. `reset_selector_delta_state()` clears memory and deletes the file.
- **Inputs / outputs.** Writes the state file; no DB writes.
- **Code.** `atomicdb/ingest.py:_DELTA_BUCKETS:6418`, `_new_delta_hist:6423`, `_delta_bucket:6436`, `_persist_delta_state:6488`, `_forget_delta_state_on_disk:6501`, `reset_selector_delta_state:6527`, `selector_pass_report:6543`, `_record_pass:6560`.
- **Tests.** `test_selector_v2.py::WriteVolumeTests::*`, `test_selector_v2.py::DeltaMarkerPersistenceTests::test_a_completed_pass_leaves_the_marker_on_disk`, `::test_reset_forgets_the_disk_too`, `::test_an_unwritable_state_dir_does_not_kill_the_pass`. **Gaps:** none.
- **Determinism hazards.** File on disk; monotonic clock (reporting only).
- **History.** `62409d5`, `3722e1a` (2026-08-05).
- **Ordering.** Last step of PRI-3.3/3.4.

### PRI-3.8 Inline emergency refresh
- **Statement.** If `settings.ATOMICDB_INLINE_SELECTOR` is truthy (env in `1/true/yes`, default False), `next_tasks` (regret mode only) first calls `refresh_priorities()` (not forced, so throttled to once per 30 s per process, PRI-3.2) inside the lease request. Without it, nothing in the HTTP path recomputes priorities.
- **Inputs / outputs.** As PRI-3.1.
- **Code.** `atomicdb/ingest.py:inline_selector_enabled:6801`, `next_tasks:6843-6844`; `OpenSite/settings.py:141-142`.
- **Tests.** `test_selector_service.py::SelectorOutOfRequestTests::test_next_tasks_does_not_run_the_global_pass`, `::test_lease_does_not_run_the_global_pass`, `::test_emergency_flag_restores_the_inline_pass`, `::test_flag_defaults_to_off`, `test_performance_guards.py::PriorityRefreshCacheTests::test_task_requests_no_longer_refresh_at_all`, `::test_inline_fallback_still_refreshes_only_once`. **Gaps:** none.
- **Determinism hazards.** Runs in whichever gunicorn process serves a lease; process-local throttle; env switch.
- **History.** P0d (selector moved out of the request); flag kept as rollback.
- **Ordering.** Before PRI-4.2 when on.

---

### PRI-4.1 `next_tasks(n)` dispatch
- **Statement.** `next_tasks(n)`: if `proof.selector_mode() == 'pn'` (`settings.ATOMICDB_SELECTOR`, lower-cased, default `regret`) return `_next_tasks_by_proof(n)` (PRI-4.4) and never touch priorities or regret; else optional inline refresh (PRI-3.8) and the regret path (PRI-4.2). Contract for callers: it **creates** missing AnalysisTask rows and returns a list of PENDING tasks (new or pre-existing) of length <= n; callers that need a task re-run their own lease choice (`views.api_lease` calls `ingest.next_tasks(TASK_REFILL_COUNT=8)` only when nothing is leasable, the worker is not `personal`, and its `max_budget` is None or >= `SERVER_PICKED_MIN_NODES`; then calls `choose_pending()` again; the return value is ignored there). `top_up_analysis_pool` uses `len()` of the result.
- **Inputs / outputs.** Settings; see PRI-4.2/4.4.
- **Code.** `atomicdb/ingest.py:next_tasks:6825`; `atomicdb/proof.py:selector_mode:208`; call site `atomicdb/views.py:api_lease:1407-1412` (Q-2, part 1).
- **Tests.** `test_proof_manager.py::SelectorFlagTests::test_default_selector_is_still_regret`, `::test_pn_selector_produces_tasks_from_the_descent`, `::test_pn_selector_does_not_run_the_global_dijkstra`, `::test_pn_selector_without_campaigns_returns_nothing`, `test_selector_leak.py::ProofDescentIsImmuneTests::test_the_descent_never_consults_the_regret_map`, `test_max_budget.py::RefillTests::test_a_worker_capped_below_server_picks_does_not_refill`, `::test_a_worker_capped_at_server_picks_refills_as_before`, `::test_an_uncapped_worker_refills_as_before`, `test_idle_pool_budget.py::EmptyQueueLeaseTests::test_the_task_a_worker_gets_from_an_empty_queue_is_128m`. **Gaps:** none.
- **Determinism hazards.** Called inside the HTTP lease path: when tasks get minted depends on worker poll timing; concurrent leases mint the same rows (deduped by the unique constraint, PRI-4.11).
- **History.** `ATOMICDB_SELECTOR=pn` switch with the proof manager (July 2026); production runs `pn`.
- **Ordering.** Called by Q-2 lease (part 1) and PRI-4.3.

### PRI-4.2 Regret-mode minting
- **Statement.** Query `Position.filter(status='UNKNOWN').order_by('-priority')[:4*n]` (no tombstone filter, no tie-break). For each row while `len(tasks) < n`: if not `_still_reachable` -> tombstone (PRI-1.7) and continue. `clamp = _short_mate_clamp(pos)` (PRI-5.4); `budget = server_picked_budget(budget_for(pos), clamp, pos=pos)` (PRI-5.3, 5.6, 5.7). `get_or_create(position=pos, generation=pos.visits, defaults={budget_nodes: budget, multipv: multipv_for(pos.visits, budget, clamp=clamp)})` (source defaults to AUTO, arm ''). Append the task iff its state is PENDING (a pre-existing PENDING row of any source/budget is returned as is; LEASED/COMPLETED/CANCELLED rows at that generation block the position for this call).
- **Inputs / outputs.** Reads `Position.priority`, `status`, `visits`, `eval_cp`, `mate_in`, `last_analysis`, `backed_move`, `fen`; creates `AnalysisTask(position, generation=visits, budget_nodes, multipv, source=AUTO, arm='')`; writes `Position.priority=DEAD` for unreachable ones.
- **Code.** `atomicdb/ingest.py:next_tasks:6845-6863`.
- **Tests.** `tests.py::SelectorTests::test_global_best_first_by_eval`, `::test_dead_branch_not_selected`, `::test_mate_band_boost_and_budget_jump`, `test_idle_pool_budget.py::RegretSelectorTests::*` (6 tests: floor, visit ladder above floor, short-mate check, ladder outbids clamp, pool counts, rollback 0), `test_mate_clamp.py::SelectorTaskTests::*` (3), `test_multipv_policy.py::MultipvAtCreationSitesTests::test_the_auto_selector_follows_the_budget_it_chose`, `test_sweep.py::InteriorRungPolicyTests::test_the_selector_buys_an_interior_position_at_128m`, `::test_the_selector_still_climbs_on_a_leaf`, `test_selector_service.py::RefreshSelectorCommandTests::test_stale_priorities_still_produce_tasks`, `test_queue_control.py::BackersStayInTheVisitorBandTests::test_a_degraded_request_does_not_outrank_position_priority`. **Gaps:** equal-priority tie order; tombstones entering the 4n window.
- **Determinism hazards.** `order_by('-priority')` without a secondary key: ties (e.g. many rows at the default 0.0 or revived 0.0) come in DB order. Tombstoned rows (-1e9) are in the window only if fewer than 4n live rows exist; a tombstone that became reachable again would then be minted. Concurrent mint race resolved by `uniq_task_per_generation` + `get_or_create` retry. READ COMMITTED.
- **History.** `5ef54c1` (2026-07-21, global best-first); floor `417d2c3` (2026-10-18, PR #68); interior cap `1495ea4` (2026-11-11, PR #224).
- **Ordering.** After PRI-3.* (whatever the column holds); budget rules PRI-5.* inside.

### PRI-4.3 Analysis pool top-up
- **Statement.** `top_up_analysis_pool(target=None)`: `target` defaults to `ANALYSIS_POOL_TARGET = 512` (production passes `--pool-target 64`). `pending = count(AnalysisTask state=PENDING, source=AUTO, arm != 'repair')` (idle-repair tasks excluded; FILL and USER tasks are not counted). If `pending >= target` return 0; else return `len(next_tasks(target - pending))`. Runs last among the queueing steps of the selector service (PRI-6.1), never rate-limited by arm budgets.
- **Inputs / outputs.** Reads AnalysisTask counts; creates tasks via PRI-4.2/4.4.
- **Code.** `atomicdb/ingest.py:ANALYSIS_POOL_TARGET:6876`, `top_up_analysis_pool:6879`.
- **Tests.** `tests.py::SelectorTests::test_pool_top_up_keeps_the_queue_fed`, `test_idle_pool_budget.py::RegretSelectorTests::test_the_pool_top_up_mints_at_the_floor_and_counts_tasks`, `test_idle_pool_budget.py::ProofSelectorTests::test_a_pool_of_several_is_all_at_the_floor`, `test_idle_repair.py::PoolTests::test_waiting_repairs_do_not_shrink_the_pool`, `test_sweep.py::SweepDripTests::test_the_pool_does_not_count_it`, `test_arm_rate_budget.py::DescendRateTests::test_the_top_up_still_fills_the_pool_with_the_arm_spent`, `test_arm_rate_budget.py::UnbudgetedDoorsTests::test_the_top_up_is_never_rate_limited`, `test_selector_service.py::RefreshSelectorCommandTests::test_no_priorities_keeps_the_other_arms_and_default_pool`. **Gaps:** the returned count includes pre-existing PENDING tasks that `next_tasks` returned (not only newly minted ones); not pinned.
- **Determinism hazards.** Counts under READ COMMITTED while leases run concurrently.
- **History.** `4798fe1` (2026-07-29, pool maintained by the selector); target 64 -> 512 by community poll P6 (3-0, 28-Aug-2026, `e39a8dc`, PR #48); production valve `--pool-target 64` since 11-Oct (memory: ratified 13-Oct, PR #61); repair arm excluded `fc9f14b` (2026-10-29, PR #100).
- **Ordering.** After all capped arms in PRI-6.1.

### PRI-4.4 Proof/value selector loop (`_next_tasks_by_proof`)
- **Statement.** `campaigns = proof.active_campaigns()` (`ProofCampaign.active=True` ordered by id); if empty return []. `by_value` per PRI-4.5. `roots = proof.campaign_roots()` (community `Campaign` ACTIVE with open root, ordered by id, weight `ln(1+max(1, votes))`; [] if `ATOMICDB_CAMPAIGN_DESCENT` is off). `base = _task_counter()` = highest `AnalysisTask.id` (0 if none). `attempt_budget = 4 * max(1, n)`. Loop while `len(tasks) < n` and `attempts < attempt_budget`: `campaign = campaigns[attempts % len(campaigns)]`, `counter = base + attempts`, `start = proof.campaign_start(counter, roots)` (deterministic draw `sha256("campaign:" + counter)`; each root gets `0.35 * weight/total` of the unit interval, None = global root); descend: value walker `proof.descend_value(campaign, counter, avoid=seen, start)` if `by_value` else df-pn `proof.descend(campaign, counter, avoid=seen, start)` (PN-6/PN-9, out of scope). `attempts += 1`. Skip if no position or already in `seen`; add to `seen`; if not `_still_reachable` -> tombstone and continue. Budget per PRI-4.6; arm per PRI-4.7; `get_or_create(position=pos, generation=pos.visits, defaults={budget_nodes, arm, multipv})`; append iff PENDING.
- **Inputs / outputs.** Reads ProofCampaign, Campaign, proof numbers (via descend), Position; creates AnalysisTask (source AUTO); writes `Position.priority=DEAD`.
- **Code.** `atomicdb/ingest.py:_task_counter:7035`, `_next_tasks_by_proof:7046`; `atomicdb/proof.py:active_campaigns:414`, `CAMPAIGN_DESCENT_SHARE:966`, `CAMPAIGN_ACTIVATION_VOTES:971`, `campaign_descent_enabled:974`, `campaign_roots:979`, `campaign_start:997`, `_point:918`.
- **Tests.** `test_bug_hunt_findings.py::ProofSelectorAttemptCapTests::test_the_descent_loop_stops_at_its_attempt_cap`, `test_solver_allocation.py::CampaignMintingTests::test_with_no_campaigns_every_descent_starts_at_the_global_root`, `::test_a_campaign_descent_marks_its_tasks`, `::test_a_normal_descent_is_not_marked`, `test_idle_pool_budget.py::ProofSelectorTests::test_the_df_pn_descent_mints_at_the_floor`, `test_value_descent.py::ReservationTests::test_the_pool_never_hands_the_same_position_twice`, `test_value_descent.py::PoolVarietyTests::*`, `test_shared_alt.py::SameAnswerTests::test_the_minted_tasks_are_the_same`, `test_proof_manager.py::SelectorFlagTests::test_pn_selector_without_campaigns_returns_nothing`. **Gaps:** dependence on `base` (max task id) not pinned.
- **Determinism hazards.** `counter` = max AnalysisTask id + attempt: concurrent task creation by other processes (requests, arms, other leases) changes `base` and therefore campaign starts and repertoire buckets; the "same state, same queue" claim only holds for the task table's max id. READ COMMITTED reads in the descent. Inside the HTTP lease path.
- **History.** Attempt cap restored `e504c10` (2026-07-29: the node budget variable had overwritten the attempt bound inside /api/lease); campaign starts `b57bc07` (2026-08-05).
- **Ordering.** Called by PRI-4.1; budget PRI-4.6; after PN-4 refresh (proof numbers stored).

### PRI-4.5 Descent mode and the walker's hourly gate
- **Statement.** `by_value = (proof.descent_mode() == 'value') and _arm_budget_room('descend') > 0`, checked **once per call** (a gate, not a clip). `descent_mode()` is `value` iff `settings.ATOMICDB_DESCENT` lower-cased equals exactly `value`; anything else is `proof`. When the `descend` arm has used its hourly rate (`ATOMICDB_ARM_RATE_DESCEND`, default 120 tasks/hour over a rolling 1 h window, Q-8 part 6), the call falls back to df-pn for this whole batch; it never leaves the batch empty because of the budget.
- **Inputs / outputs.** Settings; counts `AnalysisTask(arm='descend', created >= now-1h)`; may emit `ARM_RATE_BUDGET` event (Q-8).
- **Code.** `atomicdb/ingest.py:_next_tasks_by_proof:7086-7087`; `atomicdb/proof.py:descent_mode:1328`; `atomicdb/ingest.py:_arm_budget_room:9143`, `ARM_RATE_SETTING:9110`.
- **Tests.** `test_arm_rate_budget.py::DescendRateTests::test_a_spent_hour_falls_back_to_the_proof_descent`, `::test_a_rate_of_zero_leaves_the_descent_of_always`, `test_value_descent.py::ProductionRootTests::test_with_the_switch_off_the_queue_is_the_one_from_yesterday`. **Gaps:** none.
- **Determinism hazards.** Rolling wall-clock window (`timezone.now()`), env switch.
- **History.** Value walker `b3087dc` (2026-08-12); hourly gate `c76fcbe` (2026-08-17, after 4,551 cascade purchases in one hour on 15-Aug).
- **Ordering.** Start of PRI-4.4.

### PRI-4.6 Budget composition in the proof/value selector
- **Statement.** For the chosen `pos`: `clamp = _short_mate_clamp(pos)`. `budget = budget_for(pos)` if the descent was df-pn (`target is None`) **or** a short-mate clamp applies; else `value_budget(target)` (PRI-4.8). Then `or_clamp = proof.proved_or_clamp(pos.key, campaigns)` (PN-7: `(OR_CLAMP_NODES=250_000, OR_CLAMP_MULTIPV=1)` when every active campaign has every UNKNOWN parent settled by a proved sibling under an OR node; None if `ATOMICDB_OR_CLAMP` off, no parents, or > 32 parents); if not None: `budget = min(budget, 250_000)`, `clamp = or_clamp` (the OR clamp beats the mate carve-out). Finally `budget = server_picked_budget(budget, clamp, pos=pos)` and `multipv = multipv_for(pos.visits, budget, clamp=clamp)`.
- **Inputs / outputs.** As PRI-5.*; reads parents/edges for the OR clamp.
- **Code.** `atomicdb/ingest.py:_next_tasks_by_proof:7117-7141`; `atomicdb/proof.py:OR_CLAMP_NODES:1035`, `OR_CLAMP_MAX_PARENTS:1041`, `or_clamp_enabled:1044`, `proved_or_clamp:1068`.
- **Tests.** `test_solver_allocation.py::OrClampMintingTests::test_the_minted_task_carries_the_minimum`, `::test_the_clamp_beats_the_ladder_and_the_mate_carve_out`, `::test_without_a_proven_sibling_the_ladder_still_rules`, `test_idle_pool_budget.py::ProofSelectorTests::test_the_proved_or_clamp_still_buys_its_cheap_look`, `::test_a_short_mate_check_stays_a_cheap_single_line`, `::test_once_the_ladder_outbids_the_clamp_it_is_floored`, `::test_zero_gives_back_the_old_price`, `test_value_descent.py::ClampsStillWinTests::test_a_short_mate_claim_buys_its_verification_not_the_excavation`, `::test_the_proved_or_clamp_still_cheapens_a_settled_sibling`, `test_idle_pool_budget.py::ValueWalkerTests::*` (5). **Gaps:** none.
- **Determinism hazards.** `proved_or_clamp` reads parents with `.distinct()[:33]` without order (only the count matters); READ COMMITTED.
- **History.** OR clamp `b57bc07` (2026-08-05); floor `417d2c3` (PR #68); interior `1495ea4` (PR #224).
- **Ordering.** Inside PRI-4.4 before minting.

### PRI-4.7 Arm marking of selector tasks
- **Statement.** The `arm` default of a task minted by `_next_tasks_by_proof` is `'campaign'` (`CAMPAIGN_ARM`) if the descent started at a community campaign root (`start is not None`), else `'descend'` (`VALUE_DESCEND_ARM`) if `by_value`, else `''`. A campaign-started descent keeps `'campaign'` even when the value walker chose it (so it is not charged to the `descend` hourly rate). Regret-mode tasks are `''`. Source is always AUTO (no privilege).
- **Inputs / outputs.** `AnalysisTask.arm` on creation only (get_or_create defaults; an existing row keeps its arm).
- **Code.** `atomicdb/ingest.py:CAMPAIGN_ARM:6907`, `VALUE_DESCEND_ARM:6917`, `_next_tasks_by_proof:7138-7139`.
- **Tests.** `test_solver_allocation.py::CampaignMintingTests::test_a_campaign_descent_marks_its_tasks`, `::test_a_normal_descent_is_not_marked`, `test_arm_rate_budget.py::DescendRateTests::test_the_walker_marks_what_it_buys`. **Gaps:** none.
- **Determinism hazards.** none beyond PRI-4.4.
- **History.** `b57bc07` (2026-08-05), `c76fcbe` (2026-08-17).
- **Ordering.** At creation in PRI-4.4.

### PRI-4.8 `value_budget(target)`
- **Statement.** Price the walker quotes by arm (`target.arm`, `target.floor_nodes`): `rung` -> `_next_rung(floor_nodes)` on the request ladder (None -> 128M; first rung > floor; else 2B); `cycle` -> `_rung_at_least(floor_nodes)` on the exploration ladder (first rung >= floor; else 2B); `backup` -> `VALUE_BACKUP_NODES = BUDGET_LADDER[1] = 32M`; `explore` -> `VALUE_EXPLORE_NODES = BUDGET_LADDER[0] = 8M`; anything else (`spine`, `bottleneck`) -> `VALUE_SPINE_NODES = REQUEST_BUDGET_LADDER[0] = 128M`. The server floor (PRI-5.6) then lifts 8M/32M to 128M.
- **Inputs / outputs.** `proof.ValueTarget(position, plies, arm, floor_nodes, line_plies)`.
- **Code.** `atomicdb/ingest.py:VALUE_SPINE_NODES..VALUE_EXPLORE_NODES:6939-6941`, `value_budget:6963`; `atomicdb/proof.py:VALUE_ARM_*:1234-1239`.
- **Tests.** `test_value_descent.py::SpineWalkTests::test_the_tip_is_bought_at_the_request_rung_not_the_explore_one`, `test_value_descent.py::SaturatedTipTests::test_the_target_climbs_to_the_node_with_unjudged_replies`, `::test_a_spine_without_bottlenecks_buys_the_next_rung`, `test_value_descent.py::CyclingSpineTests::test_the_pre_cycle_node_buys_a_deeper_pass`, `test_value_descent.py::ReservationTests::test_the_backup_bucket_spends_on_the_alternative`, `::test_the_explore_bucket_buys_the_best_seeded_virgin`, `test_idle_pool_budget.py::ValueWalkerTests::test_the_next_rung_above_the_floor_is_untouched`. **Gaps:** `cycle` with `floor_nodes=None` would raise TypeError in `_rung_at_least` (not tested; assumed never None).
- **Determinism hazards.** none.
- **History.** `b3087dc` (2026-08-12).
- **Ordering.** Inside PRI-4.6.

### PRI-4.9 Value cascade after a landing
- **Statement.** `_queue_value_cascade(pos, cap=16)` runs once per landing in `propagate_landings` after the status cascade and the backed ascent. Returns 0 unless `descent_mode() == 'value'` and `pos.status == 'UNKNOWN'` (status as the landing's stage (a) left it, read before the cascade). `hourly = _arm_budget_room('cascade')` (default 240/hour, `ATOMICDB_ARM_RATE_CASCADE`); 0 -> return 0. `pending = count(PENDING, arm='cascade')`. `room = min(cap, hourly, max(0, VALUE_CASCADE_CAP(16) - pending))`; <= 0 -> 0. `budget = server_picked_budget(VALUE_CASCADE_NODES = 8M)` = 128M (no `pos`, so no interior cap). For each child in `unjudged_children(pos)` (edges of `pos` ordered by `Edge.id`, child UNKNOWN with `eval_cp` NULL and `backed_eval` NULL) until `made == room`: `get_or_create(position=child, generation=child.visits, defaults={budget, multipv_for(child.visits, budget), source AUTO, arm 'cascade'})`; count only newly created rows. If `made > 0` emit `DBEvent(kind='VALUE_CASCADE', payload={'key': pos.key, 'queued': made})`.
- **Inputs / outputs.** Reads Edge/Position children, AnalysisTask counts; creates AnalysisTask; emits `VALUE_CASCADE`, possibly `ARM_RATE_BUDGET`.
- **Code.** `atomicdb/ingest.py:VALUE_CASCADE_NODES/CAP/ARM:6956-6958`, `_queue_value_cascade:6984`, call `propagate_landings:1449`; `unjudged_children:9634`, `is_unjudged:9619`.
- **Tests.** `test_value_descent.py::ValueCascadeTests::test_the_cascade_is_capped_floored_and_marked`, `::test_the_quota_is_its_own_and_does_not_refill_while_it_is_full`, `::test_a_seeded_reply_is_not_bought_again`, `::test_the_proof_descent_does_not_cascade`, `test_idle_pool_budget.py::ValueWalkerTests::test_the_cascade_is_floored`, `test_arm_rate_budget.py::CascadeRateTests::*` (6), `test_arm_rate_budget.py::ArmRateReceiptTests::*` (4). **Gaps:** a position closed by the cascade of its own landing still cascades (stale `pos.status`); not tested. The `cap` argument does not change the 16-pending brake (constant used).
- **Determinism hazards.** Rolling wall-clock hourly window; pending count races with leases; uses the pre-cascade `pos` object; in a drainer batch the cascade of landing i sees tasks created by landing i-1 (landing order).
- **History.** `b3087dc` (2026-08-12); hourly ceiling `c76fcbe` (2026-08-17).
- **Ordering.** After CAS-1 and BKD-7 in `propagate_landings` (EXP-6), after `_uncertainty_expand` and `_witness_refuted_revisit`, before idle repair and autosolve (part 6).

### PRI-4.10 `bootstrap_root`
- **Statement.** `bootstrap_root(budget=None)`: `budget` defaults to `BUDGET_LADDER[-1] = 2B` (docstring says 512M; code is 2B). Get or create the start position, `expand` it, then for each edge of the root (no explicit order, `select_related('child')`) whose child is UNKNOWN: if the child has a PENDING task (`.first()`, no order), set `budget_nodes = max(old, budget)`, `source = 'USER'`, stamp `requested_at = now` if it was not USER already, save; else find the first free generation `g >= child.visits` (loop on existence) and create `AnalysisTask(position=child, generation=g, budget_nodes=budget, source='USER', requested_at=now, multipv=5)` (seeding width). Returns the number of children handled. Not rate-limited. Idempotent (promotes, never duplicates). No production caller in the code (management/tests only).
- **Inputs / outputs.** Creates/updates AnalysisTask (`budget_nodes`, `source`, `requested_at`); creates Positions/Edges via `expand`.
- **Code.** `atomicdb/ingest.py:bootstrap_root:7147`, `_stamp_request:7599`.
- **Tests.** `tests.py::BootstrapTests::test_bootstrap_root_deep_pass`, `test_multipv_policy.py::MultipvAtCreationSitesTests::test_bootstrapping_the_root_stays_wide`, `test_arm_rate_budget.py::UnbudgetedDoorsTests::test_the_root_seeding_is_never_rate_limited`. **Gaps:** the multiple-PENDING `.first()` choice.
- **Determinism hazards.** `.first()` without `order_by` (orders by pk in Django only when the model has default ordering; AnalysisTask has none -> DB order); `timezone.now()`.
- **History.** From the July 2026 seed of the tree.
- **Ordering.** Standalone.

### PRI-4.11 Task identity at minting (dedupe by position and generation)
- **Statement.** Every server-minted analysis task in this part is created with `get_or_create(position=pos, generation=pos.visits, defaults=...)` under the unique constraint `uniq_task_per_generation(position, generation)`. Consequences a porter must keep: (1) at most one task per (position, visit number); (2) defaults (budget, multipv, arm, source) apply only on creation, an existing row is never re-priced by these paths; (3) an existing non-PENDING row at that generation (LEASED, COMPLETED absorbed with 0 nodes, CANCELLED) blocks minting for that position until `visits` changes; (4) a pre-existing PENDING row (even USER) is returned as if minted.
- **Inputs / outputs.** `AnalysisTask` rows.
- **Code.** `atomicdb/models.py:AnalysisTask.Meta:546-548`; `atomicdb/ingest.py:next_tasks:6856`, `_queue_value_cascade:7021`, `_next_tasks_by_proof:7135`.
- **Tests.** `tests.py::BootstrapTests::test_bootstrap_root_deep_pass` (no duplicates), `test_value_descent.py::ReservationTests::test_the_pool_never_hands_the_same_position_twice`. **Gaps:** the CANCELLED/absorbed blocking case for server minting.
- **Determinism hazards.** Concurrent creators race; the loser of an IntegrityError re-reads (Django `get_or_create`), so which defaults win depends on timing.
- **History.** Constraint from the initial model; CANCELLED state (Q-1, part 1) added later and blocks the slot.
- **Ordering.** Q-1 state machine (part 1).

---

### PRI-5.1 Exploration ladder (`BUDGET_LADDER`)
- **Statement.** `BUDGET_LADDER = [8_000_000, 32_000_000, 128_000_000, 512_000_000, 2_000_000_000]` nodes, indexed by visit count (`min(visits, 4)`). Derived constants used elsewhere: `VALUE_EXPLORE_NODES = 8M`, `VALUE_BACKUP_NODES = 32M`, `VALUE_CASCADE_NODES = 8M`, idle repair `REPAIR_MIN_NODES = BUDGET_LADDER[2]`, `REPAIR_MAX_RUNG = BUDGET_LADDER[3]`.
- **Inputs / outputs.** Constant.
- **Code.** `atomicdb/ingest.py:BUDGET_LADDER:35`.
- **Tests.** `test_idle_pool_budget.py::RegretSelectorTests::test_the_visit_ladder_above_the_floor_still_climbs`, `test_max_budget.py::CappedLeaseTests::test_a_lower_rung_of_the_ladder_is_still_served`. **Gaps:** none.
- **Determinism hazards.** none.
- **History.** Original design (chessdb-style deep probes).
- **Ordering.** Read by PRI-5.3, PRI-4.8, PRI-5.8.

### PRI-5.2 Request ladder and derived request constants
- **Statement.** `REQUEST_BUDGET_LADDER = [128_000_000, 512_000_000, 2_000_000_000]` nodes (first rung follows donated capacity; it was 512M on 28-Jul and back to 128M on 29-Jul). Derived: `NOTIFY_MIN_BUDGET = 128M` (floor for notifying a requester); `SUBTREE_REQUEST_NODES = 10_000_000_000` (wire value only, the last stop of the depth slider and the API `budget`; never a task size); `SUBTREE_SEARCH_NODES = 128M`; `SUBTREE_TASK_CAP = 10B // 128M = 78`; `FRONTIER_OR_WIDTH = 3`, `FRONTIER_AND_CAP = 64`, `FRONTIER_BLIND_WIDTH = 8`, `FRONTIER_CLICK_CAP = 64`, `FRONTIER_DESCENT_MAX_PLIES = 32`, `SUBTREE_SCAN_CAP = 4096` child rows, `SUBTREE_CHUNK = 64` parents per statement; `SERVER_PICKED_MIN_NODES = 128M`; `AUTO_INTERIOR_MAX_NODES = 128M`; `VALUE_SPINE_NODES = 128M`. Behaviour of requests using them: part 4 (Q-6/Q-7).
- **Inputs / outputs.** Constants.
- **Code.** `atomicdb/ingest.py:44,56-58,64,72-75,82,90-91,397,423,6939`.
- **Tests.** `tests.py::RequestTests::test_reanalysis_uses_128m_512m_2b_staircase`, `test_idle_pool_budget.py::RuleTests::test_the_floor_is_the_first_request_rung`, `test_depth_slider.py::SpentLadderButtonTests::test_the_button_names_the_searches_below`. **Gaps:** none.
- **Determinism hazards.** none.
- **History.** 128M/512M/2B since `80b8b0c` (2026-07-22, PR #13); 10B rung removed and replaced by up to 78 searches of 128M below the position `737d288` (2026-10-29, PR #99).
- **Ordering.** Read by PRI-5.6/5.7/5.8 and Q-6/Q-7.

### PRI-5.3 `budget_for(pos)` (visit ladder + mate band jump)
- **Statement.** `budget = BUDGET_LADDER[min(pos.visits, 4)]`. If `_short_mate_clamp(pos)` is not None return `max(budget, clamp_budget)` (the ladder still wins when it asks for more). Else if `abs(pos.eval_cp or 0) >= 9000` (MATE_BAND, raw own eval, not backed) `budget = max(budget, 128M)` ("extract the whole PV"). Return budget. Callers then apply PRI-5.6.
- **Inputs / outputs.** Reads `Position.visits`, `eval_cp`, plus PRI-5.4 inputs.
- **Code.** `atomicdb/ingest.py:budget_for:6778`.
- **Tests.** `tests.py::SelectorTests::test_mate_band_boost_and_budget_jump`, `test_mate_clamp.py::BudgetForCarveOutTests::test_a_short_claim_replaces_the_band_jump`, `::test_a_long_claim_keeps_the_band_jump`, `::test_the_band_without_distance_keeps_the_band_jump`, `::test_the_visit_ladder_takes_over_when_it_asks_for_more`, `::test_nothing_changes_outside_the_band`, `test_mate_clamp.py::MateDistanceUnitTests::test_a_proven_win_without_a_stated_distance_is_not_a_claim`. **Gaps:** none.
- **Determinism hazards.** none.
- **History.** Mate-band jump `5ef54c1` (2026-07-21); short-mate carve-out `b69ab74` (2026-07-29).
- **Ordering.** Before PRI-5.6 at each mint site (PRI-4.2, PRI-4.6; also coverage, dn repair, unexplored, witness-refuted, verification sites in other parts).

### PRI-5.4 Claimed mate distance and the short-mate clamp (also EXP-4)
- **Statement.** `claimed_mate_plies(pos)` (plies, None if unknown): (1) `pos.mate_in` if not NULL -> `max(0, mate_in)`; (2) else the first dict line of `last_analysis` that is not `prior_pass`: if it has a non-zero `mate` field (UCI `score mate N`, moves) -> `_mate_moves_to_plies(|N|, N>0, stm_white)` = `2N-1` if the winner is the side to move else `2N` (0 for N <= 0); if that first current line has no mate, stop (no distance); (3) else if `9500 < |eval_cp| < 10000` (above the tablebase band, below a proven win without distance) -> `_mate_moves_to_plies(10000 - |eval_cp|, eval_cp > 0, stm_white)`; else None. `_short_mate_clamp(pos)`: None if plies is None or `> MATE_CLAMP_PLIES = 6`; else `(min(32_000_000, max(2_000_000, 4_000_000 * plies)), 1)` = `(budget, multipv)`. Short mate = M<=3 counting the defender's ply.
- **Inputs / outputs.** Reads `Position.mate_in`, `last_analysis`, `eval_cp`, `fen`.
- **Code.** `atomicdb/ingest.py:MATE_CLAMP_*:126-130`, `TB_CLAMP_CEILING:134`, `_mate_moves_to_plies:262`, `claimed_mate_plies:275`, `_short_mate_clamp:318`.
- **Tests.** `test_mate_clamp.py::ShortMateClampShapeTests::*` (6), `test_mate_clamp.py::MateDistanceUnitTests::test_a_proven_win_without_a_stated_distance_is_not_a_claim`, `test_mate_clamp.py::SelectorTaskTests::*` (3), `test_mate_clamp.py::UnexploredChildrenTests::test_a_child_that_already_claims_a_short_mate_is_verified`. **Gaps:** none.
- **Determinism hazards.** none (pure).
- **History.** `b69ab74` (2026-07-29); TB band redefined as 9500 - plies to draw on 24-Nov (score_bands) so TB cps never pass the threshold.
- **Ordering.** Before PRI-5.3, PRI-5.5, PRI-5.6.

### PRI-5.5 `multipv_for(visits, budget_nodes=None, seeding=False, clamp=None)`
- **Statement.** In order: `seeding` -> 5; `clamp` given and `budget_nodes` given and `budget_nodes <= clamp[0]` -> `clamp[1]` (1 for both the mate clamp and the OR clamp); `budget_nodes` given and `visits >= 1` and `budget_nodes >= DEPTH_BUDGET_THRESHOLD (512M)` -> `DEPTH_MULTIPV = 2`; else `5 if visits < 3 else 3`. A first visit (visits 0) is always 5 wide regardless of budget.
- **Inputs / outputs.** Pure; result stored in `AnalysisTask.multipv` at creation.
- **Code.** `atomicdb/ingest.py:DEPTH_BUDGET_THRESHOLD:258`, `DEPTH_MULTIPV:259`, `multipv_for:334`.
- **Tests.** `test_multipv_policy.py::MultipvPolicyTests::*` (4), `test_multipv_policy.py::MultipvAtCreationSitesTests::*` (2), `test_mate_clamp.py::MultipvPolicyTests::*` (4), `test_idle_pool_budget.py::RuleTests::test_a_ladder_asking_more_than_the_clamp_is_an_ordinary_look`, `test_backlog.py::Adopting::test_shallow_positions_get_the_house_width_of_a_revisit`. **Gaps:** none.
- **Determinism hazards.** none.
- **History.** Depth policy `6c54e2f` (2026-07-27, community case: MultiPV 5 reached depth 18 and said -89, MultiPV 1 reached 23 and said -901); first visit always 5 after the 512M first rung (28-Jul); clamp `b69ab74`.
- **Ordering.** After the final budget at each mint site.

### PRI-5.6 Server-picked floor (`server_picked_budget`)
- **Statement.** `server_picked_budget(budget, clamp=None, pos=None)`: if `clamp` is given and `budget <= clamp[0]`, return `budget` unchanged (a clamp that is the purchase keeps its price). Else if `caps_interior(pos, budget)` (PRI-5.7) set `budget = 128M`. Return `max(budget, SERVER_PICKED_MIN_NODES = 128M)`. Applies to every task the server mints on its own (AUTO or FILL): selector (PRI-4.2/4.6), value cascade (PRI-4.9), coverage, dn repair, idle repair, unexplored/witness/uncertainty arms (other parts). Never applied to human requests (`_request_rung`, frontier button, Verify PV), to `_queue_disputed_reanalysis` or `_queue_cycle_disambiguation`. Rollback is setting the constant to 0 (code change, no env).
- **Inputs / outputs.** Pure + PRI-5.7 inputs.
- **Code.** `atomicdb/ingest.py:SERVER_PICKED_MIN_NODES:397`, `server_picked_budget:446`.
- **Tests.** `test_idle_pool_budget.py::RuleTests::*` (6), `test_idle_pool_budget.py::RegretSelectorTests::test_a_first_look_is_minted_at_the_floor`, `::test_zero_gives_back_the_old_price`, `test_idle_pool_budget.py::VisitorRequestTests::test_the_same_child_keeps_its_visitor_price`, `test_sweep.py::InteriorRungRuleTests::test_the_floor_still_applies`, `::test_a_clamp_keeps_its_price`. **Gaps:** none.
- **Determinism hazards.** none (env via PRI-5.7).
- **History.** `417d2c3` (2026-10-18, PR #68; community poll closed 5-0, after the 8/11-Oct write floods of 8M probes).
- **Ordering.** Last budget step before `multipv_for` at each server mint site.

### PRI-5.7 No deep rungs on interior positions (`caps_interior`)
- **Statement.** `is_interior(pos) = bool(pos.backed_move)` (its published value stands on a child). `caps_interior(pos, budget)` is true iff `pos is not None` and `budget > AUTO_INTERIOR_MAX_NODES (128M)` and `is_interior(pos)` and not `auto_interior_deep_rungs()`. `auto_interior_deep_rungs()`: `settings.ATOMICDB_AUTO_INTERIOR_DEEP_RUNGS` if defined else env of the same name, true iff in `1/true/yes` (rollback restores 512M/2B on interior positions). Only effective when the caller passes `pos` to `server_picked_budget` (the value cascade does not; regret/proof selectors, coverage, dn, unexplored do).
- **Inputs / outputs.** Reads `Position.backed_move`; settings/env.
- **Code.** `atomicdb/ingest.py:AUTO_INTERIOR_MAX_NODES:423`, `AUTO_INTERIOR_DEEP_RUNGS_SETTING:424`, `auto_interior_deep_rungs:427`, `is_interior:435`, `caps_interior:440`; also called directly at `ingest.py:4274` (quality arm, part 6).
- **Tests.** `test_sweep.py::InteriorRungRuleTests::test_an_interior_position_is_held_at_the_first_rung`, `::test_a_leaf_keeps_every_rung`, `::test_without_the_position_nothing_changes`, `::test_the_setting_restores_the_old_prices`, `test_sweep.py::InteriorRungPolicyTests::*` (2). **Gaps:** none.
- **Determinism hazards.** Settings/env read at call time (not cached).
- **History.** `1495ea4` (2026-11-11, PR #224; audit: ~10T nodes/week, 16% of the fleet, spent on 512M/2B of interior positions).
- **Ordering.** Inside PRI-5.6.

### PRI-5.8 Rung helpers
- **Statement.** `_next_rung(completed_max)`: None -> `REQUEST_BUDGET_LADDER[0]` (128M); else first request rung strictly greater than `completed_max`; else the last (2B). `_rung_at_least(nodes)`: first `BUDGET_LADDER` rung `>= nodes`; else 2B. `_rung_matching(nodes)`: first `BUDGET_LADDER` rung with `nodes <= rung * 1.01` (`RUNG_OVERSHOOT_TOLERANCE = 0.01`, because a search stops a few nodes past its budget, e.g. 512,000,099); else 2B. Request-side semantics (`_completed_max_budget`, ladder state) are in part 4.
- **Inputs / outputs.** Pure.
- **Code.** `atomicdb/ingest.py:_rung_at_least:4130`, `RUNG_OVERSHOOT_TOLERANCE:4140`, `_rung_matching:4143`, `_next_rung:7554`.
- **Tests.** `test_value_descent.py::SaturatedTipTests::test_a_spine_without_bottlenecks_buys_the_next_rung`, `test_value_descent.py::CyclingSpineTests::test_the_pre_cycle_node_buys_a_deeper_pass`, `test_backlog.py::Adopting::test_the_price_beats_the_search_it_redoes`. **Gaps:** boundary at exactly `rung * 1.01`.
- **Determinism hazards.** Float multiply in `_rung_matching` (`rung * (1 + 0.01)`): reproduce with doubles.
- **History.** `_rung_matching` added for the quality arm (BKD-11) after `_rung_at_least` rounded 512,000,099 up to 2B.
- **Ordering.** Used by PRI-4.8, BKD-11, Q-6.

---

### PRI-6.1 Selector service cycle (`refresh_selector`)
- **Statement.** One process (`manage.py refresh_selector --loop --interval N`, default 60 s) runs passes. Order inside a pass, each step isolated (an exception is logged, recorded in `failed_steps`, and the next step runs): (0) drop dead DB connections; (1) `public-counters-pre` only if counters are published and priorities are on; (2) `priorities`: `refresh_priorities(force=True)` unless `--no-priorities`; (3) `solve-leases`: `recycle_stale_solve_leases` (SOL-5); (4) `debt`: `enqueue_engine_debt(cap)` unless `--no-debt`; (5) `coverage` unless `--no-coverage`; (6) adversarial flag (`--adversarial`/`--no-adversarial`, else `ATOMICDB_ADVERSARIAL`): `dn-repair` then `fragile`; (7) `autosolve95` `drip_backlog(target=--autosolve-cap)` if cap > 0; (8) `repair-backlog` `idle_repair.drip_backlog` unless `--no-repair-backlog`; (9) `sweep16` `sweep.drip(cap, user_backlog_max)` if cap > 0; (10) `pool`: `top_up_analysis_pool(--pool-target)` (PRI-4.3); (11) `public-counters` unless `--no-public-counters`. Then one JSON line (pass, seconds, selector mode/rows/seconds/written/unchanged/hist/ball, per-step counts, failed_steps). Next pass starts `max(0, interval - elapsed)` s after the end (SIGINT/SIGTERM stop between sleeps). `--status` prints live/tombstoned counts and exits. SQLite: `busy_timeout = 30000`.
- **Inputs / outputs.** Writes `Position.priority` (step 2) and tasks via the arms (parts 4-6 and PRI-4.3); public snapshot cache.
- **Code.** `atomicdb/management/commands/refresh_selector.py:handle:212`, steps `283-365`, sleep `407`.
- **Tests.** `test_selector_service.py::RefreshSelectorCommandTests::test_command_runs_the_pass_once`, `::test_command_ignores_the_process_local_cache`, `::test_no_priorities_keeps_the_other_arms_and_default_pool`, `::test_stale_solve_leases_are_recycled_before_queue_caps`, `test_selector_service.py::SelectorCycleResilienceTests::*` (4), `test_idle_repair.py::SelectorStepTests::test_the_selector_walks_the_backlog_and_says_so`, `::test_the_walk_can_be_left_out`, `test_sweep.py::SweepDripTests::test_the_selector_service_runs_the_step`. **Gaps:** the full step order (debt before coverage before adversarial before autosolve before repair before sweep before pool) is pinned only pairwise (solve leases before caps; pool last via behaviour).
- **Determinism hazards.** Wall-clock interval; the order of arms within a pass determines who takes the shared PENDING room; interleaving with concurrent leases/ingest (different processes) is unordered.
- **History.** Service created to take the Dijkstra out of `/api/lease` (P0d); `--no-priorities` 29-Aug-2026 (`e39a8dc`, PR #48) after a full pass held >5.6 GB RSS at 36M positions; step isolation after a crash loop that never reached the pool; dead-connection drop after the 10-Oct PostgreSQL restart (22 h of failed passes); solve-lease recycling moved here (only clock running when nobody asks SOLVE).
- **Ordering.** Independent process; its steps follow the order above.

### PRI-6.2 Production selector configuration
- **Statement.** Production overrides the unit's `ExecStart` with `refresh_selector --loop --interval 120 --no-priorities --no-coverage --no-adversarial --autosolve-cap 0 --pool-target 64`, i.e. per pass: no priority refresh, no coverage, no adversarial arms, no autosolve backlog walk; solve-lease recycle, debt top-up (cap 500), repair backlog, sweep16 drip (cap 256 unless overridden) and a pool of 64 AUTO tasks. With `ATOMICDB_SELECTOR=pn`, the pool is filled by PRI-4.4.
- **Inputs / outputs.** Config.
- **Code.** `docs/server/atomicdb-selector.service.d/70-reserva-acotada.conf`; `Documentation/atomicdb-selector.service`.
- **Tests.** none (deployment). **Gaps:** the production env (`ATOMICDB_DESCENT`, arm rates) is not in the repo.
- **Determinism hazards.** Configuration lives outside the code; a v2 golden test must record it.
- **History.** Emergency valve 11-Oct-2026 (512 AUTO 8M tasks per pass were flooding the ingest); kept in tree `d3443f4` (2026-10-13).
- **Ordering.** n/a.

### PRI-6.3 Proof-directed planner (shadow only)
- **Statement.** `run_planner --mode shadow` (mode from `--mode`, else env `ATOMICDB_PLANNER`, else `shadow`; `off` does nothing; `fill/auto/idle` are refused as phase 2) holds an exclusive lock file `<ATOMICDB_STATE_DIR or tmp>/atomicdb-planner.lock`, loads the searched subgraph in memory, and every `--interval` (default 300 s) runs a cycle: incremental refresh by `updated`, pn/dn recompute (full every 6 h or when needed), virtual loss on every PENDING/LEASED AnalysisTask's position, `select_batch` (default 512 picks; stops when no White move within 4x of the best pn, the root is flagged, or the descent hits in-flight/proven work), and records one `PlannerShadowCycle` (started, finished, mode='shadow', model_version, root_key, root_pn, root_dn, fleet_nodes_per_day, picks, metrics JSON) plus `PlannerShadowPick` rows (rank, kind NODE/LEAF/REST, position_key, parent_key, move_uci, ply, pn, dn, eval_cp; unique (cycle, rank); transposed duplicates dropped). Hourly it deletes cycles older than `--retention-days` (7). Exits after `--max-hours` (72) or RSS > `--max-rss-mb` (4096, exit code 3); 5 consecutive cycle failures re-raise. **It serves nothing**: no AnalysisTask is created and `Position.priority` is not touched.
- **Inputs / outputs.** Reads Position/Edge/AnalysisTask; writes only `PlannerShadowCycle`, `PlannerShadowPick`, and the lock file.
- **Code.** `atomicdb/planner/__init__.py:MODES:22`, `atomicdb/management/commands/run_planner.py:handle:96`, `atomicdb/planner/shadow.py:ShadowRunner.cycle:192`, prune `295-298`, `atomicdb/planner/select.py:select_batch:68`, `atomicdb/models.py:PlannerShadowCycle:1038`, `PlannerShadowPick:1064`.
- **Tests.** `test_planner_shadow.py::ShadowCycleTest::*` (3), `test_planner_shadow.py::AutocommitShadowTest::test_cycle_outside_a_transaction`, `test_planner_shadow.py::PlannerCommandsTest::*` (5), `test_planner_algebra.py::*` (20). **Gaps:** none relevant to served values.
- **Determinism hazards.** Wall-clock intervals/retention; in-memory graph refresh by `updated` with an overlap window; irrelevant to served state.
- **History.** Shadow planner Nov-2026 (migration 0063; prototype cost model of 11-Nov-2026). Unit marked NOT INSTALLED in `Documentation/atomicdb-planner.service`.
- **Ordering.** Independent.

### PRI-6.4 Planner cost model file
- **Statement.** `calibrate_planner` (read-only on the DB) fits a censored lognormal cost model and writes `<ATOMICDB_STATE_DIR>/planner_costmodel.json` (schema `atomicdb.planner.costmodel.v1`, atomic tmp+replace) plus a report JSON; `--dry-run`-style flag writes nothing. `load_model()` reads that file or falls back to `PROTOTYPE_MODEL`. Only the shadow planner reads it.
- **Inputs / outputs.** Writes a JSON file; no DB writes.
- **Code.** `atomicdb/planner/costmodel.py:MODEL_FILENAME:45`, `load_model:224`, `save_model:241`; `atomicdb/management/commands/calibrate_planner.py`.
- **Tests.** `test_planner_costmodel.py::*` (7), incl. `CalibrationDatabaseTest::test_command_writes_only_its_json`, `::test_export_reads_only`. **Gaps:** none.
- **Determinism hazards.** File on disk.
- **History.** Nov-2026.
- **Ordering.** Before PRI-6.3 (optional).

### PRI-6.5 Selector shadow comparison
- **Statement.** `selector_shadow` runs `refresh_priorities_v1(force=True, top_k=K)` then `refresh_priorities_v2(force=True, top_k=K)` (v1 first, for the RSS peak reading), compares the two heads (Jaccard, Kendall tau; pass thresholds `MIN_JACCARD = 0.95`, `MIN_TAU = 0.90`), prints JSON and optionally dumps a CSV. Writes nothing to the DB and does not disturb the delta state (compute-only mode).
- **Inputs / outputs.** Reads the graph; optional CSV file.
- **Code.** `atomicdb/management/commands/selector_shadow.py:MIN_JACCARD:51`, calls `94-95`.
- **Tests.** `test_selector_shadow.py::ShadowCommandTests::*` (3), `test_selector_shadow.py::WalledSubtreeShadowTests::*` (5). **Gaps:** none.
- **Determinism hazards.** none for stored state.
- **History.** With the bounded selector (Aug-2026).
- **Ordering.** Gate before switching PRI-3.1 to v2.

### PRI-6.6 Switches that change selection (summary)
- **Statement.** All settings/env that change which tasks are created or which priorities are written in this part: `ATOMICDB_SELECTOR` (`regret` default / `pn`), `ATOMICDB_SELECTOR_V2` (off), `ATOMICDB_SELECTOR_DELTA` (on; `0/false/no` off), `ATOMICDB_INLINE_SELECTOR` (off), `ATOMICDB_STATE_DIR` (delta marker, planner files), `ATOMICDB_DESCENT` (`proof` default / `value`), `ATOMICDB_CAMPAIGN_DESCENT` (on), `ATOMICDB_OR_CLAMP` (on), `ATOMICDB_AUTO_INTERIOR_DEEP_RUNGS` (off), `ATOMICDB_ARM_RATE_DESCEND` (120/h) and `ATOMICDB_ARM_RATE_CASCADE` (240/h), `ATOMICDB_PLANNER` (shadow/off; no effect on serving), plus `refresh_selector` flags (PRI-6.1/6.2). Code constants that are documented rollbacks: `SERVER_PICKED_MIN_NODES` (0 = old prices).
- **Inputs / outputs.** Settings.
- **Code.** `OpenSite/settings.py:138-246`; functions cited in the rules above.
- **Tests.** Per rule above. **Gaps:** none.
- **Determinism hazards.** All read from the process environment; different processes (web, selector, ingest) can disagree if their environments differ.
- **History.** See individual rules.
- **Ordering.** n/a.

---

## Part 4: Requests, frontier and lanes

Scope: how a person's request becomes (or joins, raises, revives, withdraws) an `AnalysisTask`; the public request doors and their contract; the walks below a position (frontier descent, subtree request, breadth swap, uncertainty and witness-refuted arms); lane account and the fair share; per-account caps; community backlog adoption. The lease side that CONSUMES `lane_account`, `backers`, `queue_seq`, `background`, `requested_by` is Q-2.1..29 (part 1); this part defines how those fields are WRITTEN and the ordering keys they feed.

Conventions used below. Paths are `atomicdb/<file>.py:<function>:<line>` against `origin/spell-runner` `f59413e`. "USER band" = `AnalysisTask.source == 'USER'`. "Live" = state in (PENDING, LEASED). "Slot" = the unique `(position, generation)` pair (`uniq_task_per_generation`, `models.py:547`). Constants (all in nodes unless stated): `REQUEST_BUDGET_LADDER = [128_000_000, 512_000_000, 2_000_000_000]` (`ingest.py:44`), `BUDGET_LADDER = [8M, 32M, 128M, 512M, 2B]` (`ingest.py:35`), `SUBTREE_REQUEST_NODES = 10_000_000_000` (`ingest.py:56`, a wire value, never a task budget), `SUBTREE_SEARCH_NODES = 128_000_000` (`:57`), `SUBTREE_TASK_CAP = 78` (`:58`), `NOTIFY_MIN_BUDGET = 128_000_000` (`:64`), `FRONTIER_OR_WIDTH = 3`, `FRONTIER_AND_CAP = 64`, `FRONTIER_BLIND_WIDTH = 8`, `FRONTIER_CLICK_CAP = 64` (`:72-75`), `FRONTIER_DESCENT_MAX_PLIES = 32` (`:82`), `SUBTREE_SCAN_CAP = 4_096` child rows, `SUBTREE_CHUNK = 64` parents (`:90-91`), `MATE_BAND = 9000` cp (`:94`). Budget constants and `budget_for`/`server_picked_budget`/`multipv_for`/`_short_mate_clamp` are PRI-5/EXP-4 (part 3); they are called here as black boxes.

---

## Q-5 Lanes and fairness

### Q-5.1 Contributor delivery window
- **Statement.** An account "ran a worker" iff some `WorkerPing` row for that user has `last_result_at >= now - 7 days` (`CONTRIBUTOR_WINDOW_DAYS = 7`). `last_seen` (auto_now on any authenticated call) is deliberately NOT used: polling without delivering earns nothing. `contributor_accounts()` additionally intersects with OpenBench `Profile.enabled=True` (two queries, intersection in Python, because the DB router forbids cross-DB joins). `ran_a_worker(username)` does NOT check `enabled` (one row lookup); `''` → False. This predicate buys no queue position (lanes were removed 16-Aug); it only gates the right to choose a rung (Q-5.2).
- **Inputs / outputs.** Reads `WorkerPing.user`, `WorkerPing.last_result_at`, `Profile.enabled`. Writes nothing.
- **Code.** `atomicdb/lanes.py:window_start:54`, `:_recent_worker_accounts:59`, `:_enabled:76`, `:contributor_accounts:98`, `:ran_a_worker:103`.
- **Tests.** `test_lanes.py::ContributorPredicateTests::test_a_recent_delivery_counts`, `::test_a_delivery_older_than_the_window_does_not`, `::test_saying_hello_forever_earns_nothing`, `::test_a_delivery_inside_the_window_counts_when_switched_off`, `::test_a_revoked_account_loses_the_privilege`, `::test_staff_alone_earns_nothing`, `::test_an_account_that_never_delivered_is_not_contributing`, `::test_the_rung_selector_reads_the_same_predicate`; `test_lanes.py::AccountShareTests::test_running_a_worker_buys_no_queue_position`. **Gaps:** boundary exactly at 7 days (`>=`) not pinned.
- **Determinism hazards.** Wall clock `timezone.now()` (`lanes.py:56`). Depends on a second database (OpenBench `Profile`).
- **History.** Contributor lanes created 10-Aug and removed 16-Aug (PR #44, "lanes decide the order, and a backed request rides the lightest one"); the window survived only for the rung selector.
- **Ordering.** Read by Q-5.2 at request time; independent of everything else.

### Q-5.2 Right to choose a rung, and what a chosen budget may be
- **Statement.** `may_choose(user)`: False for None/anonymous; True for `is_staff`; else requires `Profile.enabled=True` AND `ran_a_worker(username)`. `chosen_rung_for(raw, user)`: empty/whitespace `raw` → `(None, False)` (no choice); non-integer → `(None, True)` (invalid, request refused 400 `bad-budget`/refused); integer not in `REQUEST_BUDGET_LADDER` and `!= SUBTREE_REQUEST_NODES` → `(None, True)`; valid value but `not may_choose(user)` → `(None, False)` (choice silently DROPPED, the request proceeds as the default button; never 403, to avoid an oracle of who may choose); else `(budget, False)`. `asks_for_subtree(raw)` is True iff `int(raw.strip()) == 10_000_000_000`, regardless of who asks (used for refusals that must not reveal rights).
- **Inputs / outputs.** POST field `budget`; `User.is_staff`, `Profile.enabled`, `WorkerPing`. Output `(chosen, invalid)`.
- **Code.** `atomicdb/depth.py:may_choose:64`, `:chosen_rung:86`, `:chosen_rung_for:111`, `:asks_for_subtree:135`.
- **Tests.** `test_depth_slider.py::ChosenRungTests::*` (8 tests, e.g. `test_a_contributor_can_jump_straight_to_the_top_rung`, `test_the_owner_can_too`), `test_depth_slider.py::ForgedChoiceTests::test_an_anonymous_post_cannot_buy_the_top_rung`, `::test_an_account_without_a_recent_worker_cannot_either`, `::test_a_worker_that_went_quiet_a_week_ago_cannot_either`, `::test_a_budget_outside_the_ladder_is_refused_and_nothing_is_queued`, `::test_a_budget_above_the_ladder_is_refused_too`, `::test_a_budget_that_is_not_even_a_number_is_refused`; `test_subtree_request.py::SliderTests::test_an_anonymous_choice_of_the_last_stop_is_dropped`, `::test_so_is_the_choice_of_an_account_that_may_not_choose`. **Gaps:** a `budget` with leading `+` or spaces inside (Python `int()` accepts `" 128000000 "` after strip and `"+128000000"`) not pinned.
- **Determinism hazards.** Wall clock via Q-5.1. Cross-DB read.
- **History.** Rung selector introduced with the depth slider; 10B top rung replaced by the subtree stop 29-Oct (PR #99).
- **Ordering.** Evaluated by each door before `_queue_request` (Q-6.24..26).

### Q-5.3 Effective (lane) account of a request
- **Statement.** `effective_account(author, backers, loads)`: candidates = `[author] + backers` with empty names removed, order preserved. If fewer than 2 candidates → `''`. Else pick the candidate minimising `(loads.get(name, 0), candidates.index(name))` (tie → earliest in list, i.e. the author first, then backers in join order). Return `''` if the winner is the author (empty means "the author"), else the winner's name.
- **Inputs / outputs.** Pure function. Output written to `AnalysisTask.lane_account` by Q-5.5.
- **Code.** `atomicdb/lanes.py:effective_account:117`.
- **Tests.** `test_lanes.py::BackedRequestLaneTests::test_a_backed_request_rides_the_backers_empty_queue`, `::test_the_author_keeps_the_request`, `::test_a_backer_with_the_heavier_queue_lends_nothing`, `::test_an_unbacked_request_is_untouched`; `test_lanes.py::HandoverLaneTests::test_a_handover_to_the_queue_lender_drops_the_loan`. **Gaps:** a name appearing twice (author also in backers cannot happen by Q-6.15, so `index` is safe); tie among two backers not pinned.
- **Determinism hazards.** none (pure; tie by list position).
- **History.** PR #44 (16-Aug), after Eclipsia's report of a backed request buried ~2,690 rows deep in its author's queue.
- **Ordering.** Needs Q-5.4 loads.

### Q-5.4 Charged loads
- **Statement.** `charged_loads(names)` = for each name, `SUM(budget_nodes)` over tasks with `source='USER'`, `requested_by = name`, state in (PENDING, LEASED) (`CHARGED`) and `position.status='UNKNOWN'` (`SERVEABLE`). Keyed by AUTHOR (`requested_by`), not by lane: rows a name lends its lane to do not count for that name, and rows it rides on another's lane still count for the author. Missing names are absent (treated as 0). Note: the task being backed is itself included in its author's load (it is PENDING USER), which is why a backer with an equal-sized queue does not win (`test_a_backer_with_the_heavier_queue_lends_nothing`).
- **Inputs / outputs.** Reads `AnalysisTask.source/requested_by/state/budget_nodes`, `Position.status`.
- **Code.** `atomicdb/lanes.py:charged_loads:143`; predicates `atomicdb/live_request.py:SERVEABLE:82`, `:CHARGED:89`.
- **Tests.** `test_lanes.py::AccountShareTests::test_equal_accounts_alternate`, `test_lanes.py::CancelledWorkCostsNothingTests::test_cancelled_rows_are_not_charged_to_an_account`. **Gaps:** that lane-borrowed rows are charged to the author (not the lender) is not pinned.
- **Determinism hazards.** READ COMMITTED: computed inside the request transaction without locking other accounts' rows; concurrent leases/landings change it (`lanes.py:152`).
- **History.** PR #44.
- **Ordering.** Before Q-5.3; called from Q-5.5.

### Q-5.5 Lane resolution when the requester set changes
- **Statement.** `_resolve_lane_account(task)` recomputes `lane_account = effective_account(requested_by, also_requested_by, charged_loads([requested_by]+also_requested_by minus empties))` and saves only if it changed. It runs ONLY when the set of requesters changes: after a new backer is recorded (`add_requester`, Q-6.16) and after a hand-over (`withdraw_requester`, Q-6.19). It is a snapshot: never recomputed when loads later change. It is NOT run on adoption (anonymous → named, Q-6.15), on `_revive_withdrawn` (Q-6.8, which does not touch `lane_account`), on `clear_own_queue` bulk cancel, or on `restore_cancelled`.
- **Inputs / outputs.** Writes `AnalysisTask.lane_account`.
- **Code.** `atomicdb/ingest.py:_resolve_lane_account:7519`; callers `:add_requester:7380`, `:withdraw_requester:7438`.
- **Tests.** `test_lanes.py::BackedRequestLaneTests::*`, `test_lanes.py::HandoverLaneTests::test_a_handover_to_the_queue_lender_drops_the_loan`; `test_queue_control.py::BumpReadsTheLaneItActuallyRidesTests::*` (consumers). **Gaps:** stale `lane_account` surviving a revival of an absorbed row that had backers (see Q-6.8 and open question) is unpinned.
- **Determinism hazards.** Depends on loads read at that instant (Q-5.4); result depends on transaction interleaving.
- **History.** PR #44 (16-Aug). Hand-over re-resolution added with withdraw (PR #44).
- **Ordering.** After Q-6.15 records the backer; before Q-6.17 computes the front (front_of_own_queue reads `lane_account`).

### Q-5.6 Fair share: partition and `lane_ahead`
- **Statement.** Over a queryset that MUST contain all CHARGED rows (PENDING and LEASED; a window only sees surviving rows), annotate `_fair_cumulative = SUM(budget_nodes) OVER (PARTITION BY source, effective_account ORDER BY running_first ASC, background ASC, queue_seq ASC, id ASC)` with inclusive frame, where `effective_account = lane_account if lane_account != '' else requested_by` and `running_first = 0 for LEASED else 1`. `lane_ahead = _fair_cumulative - budget_nodes` for USER rows (virtual START time: excludes the row itself), `0` for non-USER rows. Consequences a porter must reproduce: every account's first waiting row has `lane_ahead` = nodes already LEASED for that account; leased work counts first so bumping cannot erase running debt; background rows of an account sort after all its normal rows; anonymous rows (`requested_by=''`, no lane) form ONE partition per source; AUTO/FILL rows are all 0 (position priority decides there). `backer_rank = backers` for USER rows else 0. COMPLETED rows never count (debt does not accumulate forever).
- **Inputs / outputs.** Reads `source, lane_account, requested_by, state, background, queue_seq, id, budget_nodes, backers`. Annotations only; consumed by lease selection (Q-2.x, part 1) and Q-5.8.
- **Code.** `atomicdb/live_request.py:_effective_account:134`, `:fair_share:146` (window at `:229-238`, backer_rank `:247`).
- **Tests.** `test_queue_fairness.py::FairShareTests::test_the_first_request_of_each_account_precedes_every_second_one`, `::test_a_ten_billion_request_yields_its_turn_to_the_cheap_ones`, `::test_the_burst_of_the_sixth_of_august_no_longer_blocks_a_newcomer`, `::test_within_one_account_the_order_of_arrival_is_untouched`, `::test_the_estimator_agrees_with_the_order_actually_served`; `test_queue_fairness.py::AnonymousBucketTests::*`; `test_queue_control.py::OwnQueueBumpTests::test_a_bump_does_not_erase_the_debt_of_your_running_work`; `test_request_priority.py::OrderTests::*`; `test_queue_control.py::BackersStayInTheVisitorBandTests::test_a_degraded_request_does_not_outrank_position_priority`. **Gaps:** none major.
- **Determinism hazards.** Window ordering tie-break is `id` (stable). Row set depends on READ COMMITTED snapshot at lease time.
- **History.** Deficit round-robin by cost introduced 7-Aug (PR #34) after the 6-Aug burst of 1,562 requests; contributor-lane multiplier added 10-Aug and removed 16-Aug (PR #44); `running_first` key added with the bump (PR #44/#48); `background` key 25-Nov (#316 → PR #331).
- **Ordering.** Read-time ordering; depends on the fields written by Q-5.5, Q-6.13, Q-6.17, Q-6.18, Q-6.19.

### Q-5.7 Service keys after the fair share (named tier, backers, least-recently-served)
- **Statement.** The USER band is ordered by `SERVICE_KEYS = ('-named_first', 'lane_ahead', '-backer_rank', 'served_rank')` then `id`. `named_first = 1` iff `requested_by > ''` OR `created < now - 24 h` (`STARVED_AFTER`; note: `created`, not `requested_at`), else 0 — anonymous fresh rows wait behind named ones until a day old. `served_rank`: for each account (by effective account, '' = anonymous tide) that has USER rows in CHARGED, stamp = max `leased_at` among its USER rows that are LEASED, or COMPLETED with `completed >= now-1h`, with `leased_at >= now-1h`; rank = 1-based position of the stamp among the distinct stamps (oldest first); accounts without a stamp → 0 (go first). Equal stamps share a rank. Outside USER → 0.
- **Inputs / outputs.** Reads `requested_by, created, leased_at, completed, state, lane_account`. Annotations only.
- **Code.** `atomicdb/live_request.py:named_tier:121`, `:served_rank:264`, `:_rank_case:286`, `:_served_stamps:298`, `:SERVICE_KEYS:531`, `:named_first_rank:534`, `:service_order:550`.
- **Tests.** `test_queue_fairness.py::StarvedRequestTests::test_a_starved_anonymous_click_beats_a_fresh_named_one`, `::test_the_fresh_anonymous_flood_still_waits_its_turn`, `::test_the_receipt_counts_the_starved_tier_ahead`; `test_queue_fairness.py::LeastRecentlyServedTests::*`; `test_lease_paging.py::ServedRankEquivalenceTests::*`; `test_queue_control.py::DuplicateRequestTests::test_more_people_win_the_tie_between_two_first_requests`. **Gaps:** starvation aged by `created` while the display ages by `requested_at` (revived rows are "old" for starvation) unpinned.
- **Determinism hazards.** Wall clock in three places (`:131`, `:283`, served window); raw SQL (`:338`) vendor-dependent datetime parsing (`:353`).
- **History.** Named tier + 24 h starvation 3-Aug (PR #31); `served_rank` 9-Nov (PR #199, 29 leases to 3 in one morning); `_served_stamps` rewritten as one statement 25-Nov.
- **Ordering.** After Q-5.6; consumed by part 1 lease order and Q-5.8.

### Q-5.8 Place in the queue (`ahead`) served to the requester
- **Statement.** `queue_ahead(task)` = number of PENDING USER rows (over CHARGED ∩ SERVEABLE, i.e. live USER rows on UNKNOWN positions) that come before `task` in `service_order`, counting only PENDING rows (leased ones are not "queue"). Own-first worker affinity and the deep-lease cap are deliberately ignored (the estimate can only err long). Only PENDING USER tasks get a place; others → None. Cached per task id for 20 s under key `atomicdb.qplace.<id>` storing `(ahead, ahead_of_same_account)`, with sentinel `(-1,-1)` = "not in band". `invalidate_queue_ahead(task_id)` is called after a successful bump (web and script). The request payload's `ahead` (Q-6.26) uses `_user_queue_ahead(pos)` = the place of the lowest-id PENDING USER task of the position.
- **Inputs / outputs.** Reads the band; writes the cache only. Output `payload['ahead']`.
- **Code.** `atomicdb/live_request.py:queue_ahead:574`, `:queue_ahead_pair:610` (20 s at `:639`), `:queue_ahead_map:643`, `:invalidate_queue_ahead:568`; `atomicdb/views.py:_user_queue_ahead:3010`.
- **Tests.** `test_queue_fairness.py::NamedBeforeAnonymousTests::test_the_click_receipt_tells_how_many_are_ahead`, `::test_an_anonymous_click_counts_the_whole_named_tier_ahead`; `test_queue_fairness.py::AheadCountTruthTests::test_a_fresh_request_does_not_count_the_zombies_ahead_of_it`; `test_queue_control.py::ScriptedQueueDoorTests::test_a_scripted_bump_forgets_the_cached_place`; `test_live_request.py::WaitingTests::test_a_queue_in_front_is_counted`; `test_multi_request.py::RequestMovesTests::test_the_place_in_the_queue_is_not_worked_out_per_move`. **Gaps:** cache staleness after withdraw/join (not invalidated) unpinned.
- **Determinism hazards.** 20 s shared cache (`live_request.py:639`); wall clock through Q-5.7; not invalidated on cancel/join/lifo.
- **History.** Receipt count added 31-Jul (Lesha); 20 s cache 17-Aug; pair shape later.
- **Ordering.** After the request transaction commits (computed after `request_analysis`).

### Q-5.9 Queue table (served values of /queue/)
- **Statement.** `measure_queue(now)`: one row per AUTHOR (`requested_by`), not per lane account (unlike Q-5.6). For USER rows in CHARGED ∩ SERVEABLE: `waiting` = count PENDING, `running` = count LEASED, `nodes` = SUM budget_nodes (both states), `oldest` = MIN(COALESCE(requested_at, created)) over PENDING only. `served` = SUM `nodes_searched` of USER COMPLETED rows with `completed >= now-1h` (`SERVED_WINDOW`). `share` = round(100*served/total_served, 1) or 0.0. Rows sorted by `(-waiting, -nodes, account or '~')` (anonymous `''` sorts last among ties). Plus `waiting_writes`/`oldest_write` (IngestJob PENDING count/min created) and `waiting_propagation`/`oldest_propagation` (Q-4). Published through `metrics.shared_snapshot` with 60 s freshness (`QUEUE_CACHE_SECONDS`). `fleet_nodes_last_hour` = SUM nodes_searched of all COMPLETED in last hour, cached 60 s.
- **Inputs / outputs.** Read-only; cache keys `atomicdb.queue.table.v1`, `atomicdb.queue.fleet_hour_nodes.v1`.
- **Code.** `atomicdb/lanes.py:fleet_nodes_last_hour:173`, `:_charged_by_requester:195`, `:_served_by_requester:233`, `:measure_queue:249`, `:_write_backlog:297`, `:_propagation_backlog:313`, `:queue_table:328`.
- **Tests.** `test_lanes.py::QueuePageTests::*` (7), `test_lanes.py::CancelledWorkCostsNothingTests::test_cancelled_rows_do_not_show_on_the_queue_page`, `test_request_time.py::RequestTimeTests::*`, `test_queue_live.py::*`. **Gaps:** sort tie-break by `'~'` unpinned.
- **Determinism hazards.** Wall clock; 60 s cached snapshot; set iteration `set(charged)|set(served)` (`:262`) is re-sorted, so harmless.
- **History.** Lane table → account table 16-Aug (PR #44); `requested_at` aging 8-Nov (PR #190); fleet line 13-Nov.
- **Ordering.** Display only; no state transition.

### Q-5.10 Per-account cap of waiting requests
- **Statement.** `_account_queue_full(requested_by)` is True iff count of tasks with `state='PENDING'`, `source='USER'`, `position.status='UNKNOWN'`, `requested_by = requested_by` is `>= REQUEST_QUEUE_MAX = 10_000`. LEASED rows, rows on closed positions, CANCELLED rows and rows the account only BACKS do not count. The anonymous tide (`''`) shares one allowance. Only person-initiated doors check it: single request (Q-6.26), multi-select (Q-6.32, checked once before the batch and again per move via `_queue_request`), request-children (Q-6.33), unexplored button (Q-6.34), verify-PV door (Q-6.36), backlog adopt (Q-6.42, as room = MAX − waiting). `restore_cancelled` (undo) does NOT check it. Server arms never check it.
- **Inputs / outputs.** Read-only count.
- **Code.** `atomicdb/views.py:REQUEST_QUEUE_MAX:174`, `:_account_queue_full:630`; `atomicdb/backlog.py:_queue_room:681`.
- **Tests.** `test_lanes.py::AccountQueueCapTests::test_a_full_account_is_refused`, `::test_a_full_account_blocks_nobody_else`, `::test_the_anonymous_tide_shares_one_allowance`, `::test_running_work_does_not_spend_the_allowance`, `::test_requests_on_solved_positions_do_not_spend_it_either`, `::test_the_bulk_button_respects_the_same_allowance`; `test_lanes.py::CancelledWorkCostsNothingTests::test_cancelled_rows_do_not_spend_the_allowance`, `::test_clearing_your_queue_gives_the_allowance_back`; `tests.py::RequestTests::test_request_queue_full_keeps_structured_status`, `::test_the_queue_ceiling_is_a_real_number`; `test_backlog.py::Caps::test_a_full_queue_refuses_the_batch_with_the_usual_receipt`. **Gaps:** undo bypassing the cap unpinned.
- **Determinism hazards.** Count is not locked: two concurrent requests of one account can both pass at 9,999 (READ COMMITTED race, `views.py:644`).
- **History.** Global ceiling replaced by per-account 16-Aug (PR #44); raised to 10,000 on 5-Nov (PR #178; was 5,000).
- **Ordering.** Checked before the purchase transaction; Q-6.24 only-if-missing pre-answer and the ip dedup (Q-6.25) run BEFORE it.

### Q-5.11 Queue-full refusal, receipt and HTTP contract
- **Statement.** Refusal body `{'status': 'queue-full-account'}` with HTTP 429 (`QUEUE_FULL_STATUS`) and header `Retry-After: 300` (`QUEUE_FULL_RETRY_AFTER`, seconds) on whole-response doors (`_with_queue_full_retry` adds the header whenever the status code is 429). A `DBEvent(kind='LANE_CAP_HIT', payload={'account': requested_by or '(anonymous)', 'waiting': 10000})` is written unless one with the same `payload.account` exists with `ts >= now - 1 minute` (`LANE_CAP_RECEIPT_WINDOW`). API doors add `reason` text from `_REQUEST_REFUSALS['queue-full-account']` = "you already have the maximum number of requests waiting; let some land or clear your queue". In batches the per-move status is `queue-full` (multi-select) or `queue-full-account` (request-children skipped reason) and the batch stops there (Q-6.32/33). Backlog adopt answers `queue-full` (JSON 429).
- **Inputs / outputs.** DBEvent `LANE_CAP_HIT` (hidden from public feed).
- **Code.** `atomicdb/views.py:_queue_full_payload:650`, `:_with_queue_full_retry:676`, `:_queue_full_response:683`, `:_REQUEST_REFUSALS:3557`.
- **Tests.** `test_lanes.py::AccountQueueCapTests::test_the_refusal_leaves_a_receipt`, `::test_one_receipt_per_account_and_minute`, `::test_a_new_minute_writes_a_new_receipt`, `::test_another_accounts_receipt_does_not_count`, `::test_the_receipt_stays_off_the_home_feed`; `test_api_request_children.py::RequestChildrenApiTests::test_a_full_queue_refuses_like_api_request`. **Gaps:** the exact `Retry-After` value is not asserted in every door.
- **Determinism hazards.** Wall-clock 1-minute window (`views.py:666`); check-then-insert race can write two receipts.
- **History.** Receipt 16-Aug (PR #44); one per account-minute 2-Nov (PR #110); 503 → 429 + Retry-After 25-Nov (#313 → PR #315).
- **Ordering.** Immediately after Q-5.10 returns True.

---

## Q-6 Requests

### Q-6.1 Request outcome vocabulary
- **Statement.** `RequestOutcome` is a `str` subclass carrying a `detail` dict merged into the JSON payload. Statuses that leave `request_analysis`: `queued`, `already-queued` (optionally `detail={'adopted': True}`), `already-solved`, `already-analyzed` (only with `only_if_missing` or `only_unexplored`; `detail={'completed_budget': n}`), `expanded`, `saturated` (walks; with counters), `ladder-spent` (`LADDER_SPENT`, only with `one_search`). `_LADDER_EXHAUSTED = 'ladder-exhausted'` is an internal sentinel and must never be returned to a caller. Door-level statuses added by views: `already-requested` (ip dedup), `queue-full-account`, `unknown-position` (404), `bad-budget` (400), `confirm-10b` (400), `busy` (503, Retry-After 2), `top-rung-single` (400), `no-moves`/`too-many` (400), `login-required` (403), `nothing-to-do`, `done`/`partial`, `refused` (+`reason`).
- **Inputs / outputs.** Wire contract.
- **Code.** `atomicdb/ingest.py:_LADDER_EXHAUSTED:7180`, `:LADDER_SPENT:7185`, `:RequestOutcome:7188`.
- **Tests.** `test_frontier.py::UnchangedLadderTests::test_ordinary_status_carries_status_and_queue_receipt`; `test_adopted_request.py::AdoptedReceiptTests::*`; `test_multi_request.py::SpentLadderTests::test_a_move_searched_to_the_top_buys_nothing_and_says_so`. **Gaps:** none.
- **Determinism hazards.** none.
- **History.** `LADDER_SPENT` 29-Oct (PR #99); `adopted` detail 3-Nov (PR #152).
- **Ordering.** n/a.

### Q-6.2 Bought rung vs searched budget
- **Statement.** Two distinct readings of a position's history. `_completed_max_budget(pos)` = MAX `budget_nodes` over its COMPLETED tasks, INCLUDING absorbed rows (COMPLETED, `nodes_searched=0`, `machine=''`): "which rung has been bought". `searched_budget(pos)` = MAX `budget_nodes` over COMPLETED tasks with `nodes_searched > 0`: "has an engine looked this deep". `searched_budgets(keys)` = the same as one GROUP BY; positions with no search are absent (not 0). Values are the BOUGHT budget (`budget_nodes`), not reported nodes. `is_absorbed(task)` = COMPLETED and `nodes_searched` falsy and `machine` falsy. CANCELLED rows count for neither.
- **Inputs / outputs.** Reads `AnalysisTask.state, budget_nodes, nodes_searched, machine`.
- **Code.** `atomicdb/ingest.py:_completed_max_budget:7204`, `:_searches_that_ran:7211`, `:searched_budget:7222`, `:searched_budgets:7238`, `:is_absorbed:5789`.
- **Tests.** `test_search_depth_column.py::DepthColumnTests::test_a_searched_move_shows_its_deepest_search`, `::test_it_is_the_line_searched_budget_draws`, `::test_an_absorbed_row_is_not_a_search`, `::test_the_api_carries_it_beside_the_visits`; `test_ingest_queue.py::AbsorptionTests::test_the_completed_ladder_reads_the_same_as_before`; `test_public_api.py::OnlyIfMissingTests::test_a_rung_bought_but_never_searched_does_not_count`. **Gaps:** none.
- **Determinism hazards.** none (aggregates).
- **History.** `searched_budget` added 11-Oct with `only_if_missing` (PR #62); `is_absorbed` 11-Oct (PR #62).
- **Ordering.** Read under the position lock in Q-6.4..6.

### Q-6.3 Next rung, ladder state, ladder exhausted
- **Statement.** `_next_rung(completed_max)`: None → 128M; else the first rung of `REQUEST_BUDGET_LADDER` strictly greater than `completed_max`; if none, the last rung (2B). `request_ladder_state(pos)` = `(completed_max, _next_rung(completed_max))` (used by the page and by the `expect` check, Q-6.27). `ladder_exhausted(pos)` = `pos.status=='UNKNOWN'` and `completed_max >= 2B` (read-only; used by the ip dedup, Q-6.25). Note completed_max includes absorbed rows (Q-6.2).
- **Inputs / outputs.** Read-only.
- **Code.** `atomicdb/ingest.py:ladder_exhausted:7540`, `:_next_rung:7554`, `:request_ladder_state:7565`.
- **Tests.** `tests.py::RequestTests::test_reanalysis_uses_128m_512m_2b_staircase`; `test_frontier.py::LadderExhaustionTests::test_ladder_exhausted_reports_only_the_spent_top_rung`; `test_depth_slider.py::OfferedRungTests::test_a_spent_ladder_offers_nothing`; `test_frontier.py::UnchangedLadderTests::test_middle_rung_still_escalates_on_the_position_itself`. **Gaps:** a completed_max between rungs (e.g. 200M from an AUTO task) → 512M not pinned explicitly.
- **Determinism hazards.** none.
- **History.** Ladder 128M→512M→2B since 29-Jul (first rung temporarily 512M on 28-Jul); 10B rung removed 29-Oct (PR #99).
- **Ordering.** Inside Q-6.6.

### Q-6.4 Purchase: lock and closed check
- **Statement.** `_request_rung` first re-reads the position with `SELECT ... FOR UPDATE` (row lock on Position) so all later decisions use fresh `visits`/`status`. If `status != 'UNKNOWN'` → `already-solved` (nothing written). The caller owns the transaction (`request_analysis` wraps in `atomic()`; `_queue_request` wraps again).
- **Inputs / outputs.** Locks `Position`.
- **Code.** `atomicdb/ingest.py:_request_rung:7724-7726`.
- **Tests.** `tests.py::RequestTests::test_reanalysis_refreshes_stale_position_before_selecting_rung`, `::test_request_on_solved_position`; `test_public_api.py::RequestApiTests::test_a_decided_position_buys_nothing`. **Gaps:** none.
- **Determinism hazards.** DB locking (`select_for_update`, `:7724`). Other task creators (selector, arms, `enqueue_unexplored_children`) do NOT take this lock, so the later `get_or_create` can still race (Q-6.7).
- **History.** Stale-instance refresh added early (test name).
- **Ordering.** First step of every purchase.

### Q-6.5 Purchase gates: `only_unexplored` and `only_if_missing`
- **Statement.** After the lock: if `only_unexplored` and not `is_unexplored(pos)` (Q-6.35 predicate) → `RequestOutcome('already-analyzed', completed_budget=searched_budget(pos))` (may be None). If `only_if_missing`: `target = budget_floor or 128M`; if `searched_budget(pos) >= target` → `already-analyzed` with that budget. Otherwise the purchase proceeds with `target` set (Q-6.6 caps the floor at it). With `target` set the `_LADDER_EXHAUSTED` early exit is skipped (a target never reports a spent ladder).
- **Inputs / outputs.** Reads tasks via Q-6.2; `Position.nodes_invested/backed_nodes/status`.
- **Code.** `atomicdb/ingest.py:_request_rung:7727-7740`.
- **Tests.** `test_public_api.py::OnlyIfMissingTests::*` (9), `test_public_api.py::OnlyIfMissingOnAnAbsorbedSlotTests::*` (6); `test_api_request_children.py::RequestChildrenApiTests::test_a_search_landing_during_the_batch_is_not_deepened`; `test_api_request_children.py::RequestSelectedChildrenTests::test_a_search_landing_during_the_batch_covers_the_target`. **Gaps:** `only_unexplored` and `only_if_missing` together (children door without select + budget) answering order not pinned.
- **Determinism hazards.** none beyond the lock.
- **History.** `only_if_missing` 11-Oct (PR #62, Torom); `only_unexplored` 23-Nov (#300 → PR #315).
- **Ordering.** After Q-6.4, before Q-6.6.

### Q-6.6 Purchase floor
- **Statement.** If no target and `completed_max >= 2B` → return `_LADDER_EXHAUSTED` (nothing written). Else `floor = _next_rung(completed_max)`; `clamp = _short_mate_clamp(pos)` (PRI-5/EXP-4); if `clamp` and `clamp[0] > (completed_max or 0)` then `floor = clamp[0]` (a short mate with known distance is verified cheaply, may go BELOW 128M); then `floor = max(floor, budget_for(pos))`; then if `budget_floor` (validated chosen rung) `floor = max(floor, budget_floor)` (so a chosen rung beats the mate carve-out); finally if `target` is set `floor = min(floor, target)`. Order of these five steps is significant.
- **Inputs / outputs.** Reads `Position.visits, eval_cp, last_analysis, mate fields` through helpers.
- **Code.** `atomicdb/ingest.py:_request_rung:7737-7758`.
- **Tests.** `test_mate_clamp.py::RequestedTaskTests::test_a_click_on_a_short_mate_verifies_instead_of_digging`, `::test_a_click_on_the_band_without_distance_keeps_the_floor`, `::test_a_cheap_seed_does_not_force_the_request_floor`, `::test_the_ladder_recovers_control_once_the_check_ran`; `test_depth_slider.py::ChosenRungTests::test_a_contributor_can_jump_straight_to_the_top_rung`; `test_subtree_request.py::WalkBoundTests::test_no_search_is_bought_above_128m`; `test_sweep.py::InteriorRungPolicyTests::test_a_request_still_climbs_on_an_interior_position`. **Gaps:** `budget_for` exceeding the request rung (visits ≥ 4 → 2B via BUDGET_LADDER) combined with a 128M target not pinned.
- **Determinism hazards.** none.
- **History.** Short-mate carve-out (MATE_CLAMP_*) added around 30-Jul; target cap 11-Oct (PR #62).
- **Ordering.** After Q-6.5; before Q-6.7.

### Q-6.7 Purchase: new slot
- **Statement.** `get_or_create(position=pos, generation=pos.visits, defaults={budget_nodes: floor, source: 'USER', requested_by, route, requested_at: now, background, multipv: multipv_for(pos.visits, floor, clamp=clamp)})`. Fields not set keep model defaults (`arm=''`, `queue_seq=0`, `backers=0`, `also_requested_by=[]`, `lane_account=''`, `then_verify=False`, state PENDING). If created → apply LIFO preference (Q-6.18) → `queued`.
- **Inputs / outputs.** Creates `AnalysisTask` (USER, arm '').
- **Code.** `atomicdb/ingest.py:_request_rung:7759-7767`.
- **Tests.** `tests.py::RequestTests::test_request_queues_user_task_with_floor`; `test_frontier.py::UnchangedLadderTests::test_fresh_request_keeps_the_classic_single_task`; `test_multipv_policy.py::MultipvAtCreationSitesTests::test_a_visitor_request_at_the_floor_stays_wide`, `::test_a_visitor_request_on_a_high_rung_goes_deep`; `test_request_time.py::RequestTimeTests::test_a_new_request_is_stamped_when_it_is_made`. **Gaps:** IntegrityError from a concurrent insert by a producer that does not lock the Position is not caught here (500 to the user).
- **Determinism hazards.** `timezone.now()` stored in `requested_at` (display only). Unique-constraint race with non-locking producers (`:7759`).
- **History.** Original request path; `requested_at` 8-Nov (PR #190); `background` 25-Nov (PR #331).
- **Ordering.** After Q-6.6.

### Q-6.8 Purchase: revive a withdrawn or absorbed slot
- **Statement.** If the existing slot row is CANCELLED, or `is_absorbed(task)`, it is revived in place by `_revive_withdrawn(task, floor, multipv_for(pos.visits, floor, clamp), requested_by, route, background=background)`, then LIFO (Q-6.18) → `queued`. `_revive_withdrawn` sets: `state=PENDING`, `completed=None`, `budget_nodes=budget` (the NEW budget, never max with the old one), `source` (default USER; caller-supplied in Q-6.35), `requested_by`, `also_requested_by=[]`, `backers=0`, `queue_seq=0`, `route`, `multipv`, `background`, `requested_at=now`. It does NOT reset `lane_account`, `then_verify`, `arm`, `attempts`, `machine`, `delivered_by`, `engine_sha`, `net_sha`, `nodes_searched`, `elapsed_seconds`, `leased_at`, `lease_*`, `created`, `generation`.
- **Inputs / outputs.** Updates the slot row.
- **Code.** `atomicdb/ingest.py:_request_rung:7768-7799`, `:_revive_withdrawn:7614`.
- **Tests.** `test_queue_control.py::WithdrawCostsNothingTests::test_asking_again_revives_the_same_row`, `::test_asking_again_does_not_inherit_the_withdrawn_rung`, `::test_a_deeper_rung_can_still_be_picked_on_the_way_back`; `test_queue_control.py::AbsorptionStillWorksTests::test_the_position_can_still_be_requested_after_an_absorption`; `test_public_api.py::OnlyIfMissingOnAnAbsorbedSlotTests::test_a_target_above_what_ran_brings_the_row_back_at_the_target`, `::test_a_rung_bought_and_never_searched_comes_back_at_the_first_rung`; `test_request_priority.py::UpgradeTests::test_a_withdrawn_api_row_comes_back_at_the_level_asked_now`; `test_request_time.py::RequestTimeTests::test_a_withdrawn_request_asked_again_is_a_fresh_wait`. **Gaps:** surviving `lane_account`/`then_verify`/`arm` on a revived absorbed row unpinned (see open questions).
- **Determinism hazards.** `timezone.now()` (display).
- **History.** Revive withdrawn 16-Aug (PR #44); "budget asked now, not the larger" 11-Oct (PR #55, 7,255 stuck positions); absorbed rows revived 11-Oct (PR #62, 97 stuck open positions).
- **Ordering.** Alternative branch after Q-6.7 finds an existing row.

### Q-6.9 Purchase: existing PENDING row (raise, promote, adopt, join)
- **Statement.** On a PENDING slot row: `adopted = requested_by != '' and task.requested_by == '' and task.source == 'USER'`; `promoted = task.source != 'USER'`. Set `budget_nodes = max(budget_nodes, floor)` (never lowered; may exceed an `only_if_missing` target: a live row is joined as it stands); `source='USER'`; `requested_by` filled only if empty; `route` filled only if empty; `background` merged by Q-6.13 with `fresh=promoted`; if promoted or adopted, `requested_at=now`. `multipv` is NOT recomputed when the budget rises. Save fields `source, budget_nodes, requested_by, route` (+background, +requested_at). Then `add_requester(task, requested_by)` (Q-6.16; no-op for the author or anonymous). Return `queued` if promoted, `RequestOutcome('already-queued', adopted=True)` if adopted, else `already-queued`.
- **Inputs / outputs.** Updates the row; may add a backer.
- **Code.** `atomicdb/ingest.py:_request_rung:7800-7830`.
- **Tests.** `tests.py::RequestTests::test_request_promotes_existing_auto_task`; `test_queue_control.py::DuplicateRequestTests::*`; `test_adopted_request.py::AdoptedReceiptTests::test_taking_an_anonymous_request_says_it_is_yours_now`, `::test_joining_someone_elses_request_is_not_adopting_it`; `test_mate_clamp.py::RequestedTaskTests::test_a_pending_request_for_more_is_never_lowered`; `test_depth_slider.py::ChosenRungTests::test_a_deeper_choice_lifts_a_request_that_is_still_waiting`; `test_public_api.py::OnlyIfMissingTests::test_a_live_deeper_request_of_somebody_else_is_joined_as_it_stands`; `test_request_time.py::RequestTimeTests::test_server_work_promoted_to_a_request_is_a_fresh_wait`, `::test_an_adopted_anonymous_request_is_a_fresh_wait_for_its_new_owner`, `::test_joining_a_request_keeps_its_authors_wait`. **Gaps:** stale `multipv` after a raise unpinned.
- **Determinism hazards.** none beyond `now`.
- **History.** Promotion is original; backers 14-Aug (PR #44); adopted receipt 3-Nov (PR #152).
- **Ordering.** After Q-6.7; calls Q-6.13, Q-6.14, Q-6.16.

### Q-6.10 Purchase: LEASED row too shallow → follow-up generation
- **Statement.** If the slot row is LEASED and `budget_nodes < floor`: find `follow_up` = PENDING task of the position with `generation > task.generation`, lowest generation first. If none: `generation = max(pos.visits + 1, task.generation + 1)`, incremented while any task of the position has it; create `AnalysisTask(position, generation, budget_nodes=floor, source='USER', requested_by, route, requested_at=now, background, multipv=multipv_for(generation, floor, clamp))` (note: generation used as the visits argument); NO LIFO, NO add_requester. If found: `budget_nodes=max(.,floor)`; background merged (`fresh = follow_up.source != 'USER'`); stamp `requested_at` if it was not USER or it gains a name; `source='USER'`; fill empty `requested_by`/`route`; save. The requester is NOT recorded as a backer of an existing follow-up owned by someone else. Returns `queued`.
- **Inputs / outputs.** Creates or updates a PENDING task at a later generation.
- **Code.** `atomicdb/ingest.py:_request_rung:7831-7866`.
- **Tests.** `tests.py::RequestTests::test_request_during_shallow_lease_preserves_deep_follow_up`, `::test_deep_follow_up_is_not_run_after_shallow_visit_solves_position`; `test_api_request_children.py::RequestChildrenApiTests::test_a_deeper_follow_up_behind_a_running_search_is_reported`. **Gaps:** a second person's deeper click while a follow-up of another person exists (backer lost) unpinned.
- **Determinism hazards.** `.first()` ordered by generation (deterministic). The generation-probe loop is not locked against concurrent producers.
- **History.** Original (pre-August).
- **Ordering.** Alternative branch after Q-6.7.

### Q-6.11 Purchase: LEASED row that already covers the rung
- **Statement.** If LEASED and `budget_nodes >= floor`: merge background (fresh if not USER); if `source != 'USER'` set `source='USER'` and stamp `requested_at`; save if anything changed; then `add_requester(task, requested_by)`. Return `already-queued`. The running search is not reordered.
- **Inputs / outputs.** Updates the leased row.
- **Code.** `atomicdb/ingest.py:_request_rung:7867-7883`.
- **Tests.** `tests.py::RequestTests::test_sufficient_auto_lease_is_promoted_and_deduplicated`. **Gaps:** backer added to a LEASED row (notification) only indirectly pinned.
- **Determinism hazards.** none.
- **History.** Original; add_requester on leased rows 14-Aug (PR #44).
- **Ordering.** Alternative branch after Q-6.7.

### Q-6.12 Purchase: delivered-but-unapplied COMPLETED row on the slot
- **Statement.** If the slot row is COMPLETED with a machine or nodes (a delivered search whose ingest job has not yet advanced `visits`), none of the branches apply and `_request_rung` returns `already-queued` without writing. (CANCELLED and absorbed are handled by Q-6.8.)
- **Inputs / outputs.** none.
- **Code.** `atomicdb/ingest.py:_request_rung:7883` (fall-through).
- **Tests.** `test_queue_control.py::AbsorptionStillWorksTests::test_a_delivered_search_still_to_be_applied_is_not_a_dead_row`. **Gaps:** none.
- **Determinism hazards.** Depends on ingest-queue timing (Q-3): the same click yields `already-queued` or a new rung depending on whether the IngestJob has been applied.
- **History.** Clarified with the absorbed-row fix (PR #62).
- **Ordering.** n/a.

### Q-6.13 Request priority merge (`background`)
- **Statement.** `_merge_priority(task, background, fresh)`: `wanted = background if fresh else (task.background and background)`; returns `['background']` and sets it if it changed, else `[]`. Meaning: a row becoming a request now (`fresh`, i.e. promoted from AUTO/FILL) takes the request's priority as is (so an API request can make server work a background request); on a row that already is a request, a normal request clears background and a background request never sets it. Also: `_queue_request`'s dedup path and `request_subtree`'s 'mine' path clear `background` on live USER rows of the position (Q-6.25, Q-7.10). Script doors default to background (Q-6.29); page doors are normal.
- **Inputs / outputs.** Writes `AnalysisTask.background`.
- **Code.** `atomicdb/ingest.py:_merge_priority:7661`.
- **Tests.** `test_request_priority.py::UpgradeTests::test_a_click_from_another_address_lifts_the_api_row`, `::test_a_click_from_the_same_address_lifts_it_too`, `::test_priority_normal_on_the_api_lifts_it`, `::test_a_background_request_never_lowers_a_normal_row`, `::test_a_server_row_promoted_by_the_api_becomes_background`; `test_request_priority.py::DoorTests::test_an_existing_row_is_normal`. **Gaps:** none.
- **Determinism hazards.** none.
- **History.** 25-Nov (#316 → PR #331).
- **Ordering.** Inside Q-6.9/6.10/6.11.

### Q-6.14 Request time stamp
- **Statement.** `requested_at` is set to `now` when a request row is created (Q-6.7, Q-6.10, Q-6.35 USER, Q-6.44), revived (Q-6.8), promoted from AUTO/FILL to USER (Q-6.9/6.10/6.11, Q-6.35 source rewrite to USER), or adopted by a name (Q-6.9, Q-6.15). NOT on join, bump, LIFO, hand-over, or undo. `request_time = requested_at or created`. Display only: the service order never reads it (starvation uses `created`, Q-5.7). Note `_revive_withdrawn` stamps even when the caller passes `source=AUTO`.
- **Inputs / outputs.** Writes `AnalysisTask.requested_at`.
- **Code.** `atomicdb/ingest.py:_stamp_request:7599`; `atomicdb/models.py:request_time:542`.
- **Tests.** `test_request_time.py::RequestTimeTests::*` (8). **Gaps:** hand-over keeps the old author's time (unpinned).
- **Determinism hazards.** Wall clock (display only).
- **History.** 8-Nov (PR #190, Torom "Oldest wait").
- **Ordering.** Wherever listed.

### Q-6.15 Record a requester (join / adopt)
- **Statement.** `_record_requester(task, username)`: returns False and writes nothing if `username == ''` or task not live. If `task.requested_by == ''` (anonymous request): ADOPT — set `requested_by=username`, stamp `requested_at`, save; return False (adopting is not joining: no backer, no lane, no front move). If `username == requested_by` or already in `also_requested_by` → False. Else append to `also_requested_by` (arrival order), set `backers = len(also_requested_by)`, save; return True. Anonymous clicks never add precedence.
- **Inputs / outputs.** Writes `requested_by, requested_at` or `also_requested_by, backers`.
- **Code.** `atomicdb/ingest.py:_record_requester:7316`.
- **Tests.** `test_queue_control.py::DuplicateRequestTests::test_the_second_person_is_written_down_on_the_task`, `::test_asking_twice_yourself_is_still_one_person`, `::test_a_click_without_a_session_adds_no_precedence`, `::test_an_anonymous_request_is_adopted_by_the_first_name`; `test_lanes.py::BackedRequestLaneTests::test_a_named_backer_adopts_an_anonymous_request`; `test_public_api.py::RequestApiTests::test_a_second_account_backs_the_request_instead_of_duplicating_it`, `::test_the_author_of_a_live_request_is_not_a_backer_of_it`. **Gaps:** adoption of a LEASED anonymous row (allowed by the state check) unpinned.
- **Determinism hazards.** none; JSON list order is arrival order (deterministic given serialized writers; concurrent joins on the same row are serialized only by the caller's Position lock where one exists — `enqueue_unexplored_children` locks the parent, not the child).
- **History.** 14-Aug (PR #44, Eclipsia's "more precedence" report).
- **Ordering.** Called by Q-6.16.

### Q-6.16 add_requester: join, lend lane, move to front
- **Statement.** `add_requester(task, username)`: if `_record_requester` returns False → return False. Else `_resolve_lane_account(task)` (Q-5.5); if task is not PENDING return True; else `seq = front_of_own_queue(task)` (Q-6.17) and, if not None, write `queue_seq = seq`. Return True. Callers: Q-6.9, Q-6.11, Q-6.35.
- **Inputs / outputs.** Writes `lane_account`, `queue_seq`.
- **Code.** `atomicdb/ingest.py:add_requester:7355`.
- **Tests.** `test_queue_control.py::DuplicateRequestTests::test_joining_moves_the_request_to_the_front_of_its_own_queue`; `test_lanes.py::BackedRequestLaneTests::*`; `test_queue_control.py::BumpOnThePositionTests::test_joining_somebody_elses_request_does_not_hand_you_its_place`. **Gaps:** none.
- **Determinism hazards.** Q-5.4 load race.
- **History.** PR #44.
- **Ordering.** Q-6.15 → Q-5.5 → Q-6.17.

### Q-6.17 Front of own queue (bump floor)
- **Statement.** `front_of_own_queue(task)`: `account = task.lane_account or task.requested_by`; '' → None. Consider PENDING USER rows other than `task` whose effective account is `account` (`lane_account == account` OR (`lane_account == ''` AND `requested_by == account`)) AND with the SAME `background` value. `front` = the one with minimal `(queue_seq, id)`. None if no such row, or if `(task.queue_seq, task.id) < front` (already first). Else return `front.queue_seq - 1` (values may go negative; repeated bumps keep decreasing). LEASED rows are excluded (they lead the partition anyway, Q-5.6).
- **Inputs / outputs.** Read-only; caller writes `queue_seq`.
- **Code.** `atomicdb/ingest.py:front_of_own_queue:7262`.
- **Tests.** `test_queue_control.py::BumpReadsTheLaneItActuallyRidesTests::*` (5), `test_queue_control.py::OwnQueueBumpTests::test_a_bumped_request_leads_the_queue_of_its_own_account`, `::test_a_bump_never_takes_a_turn_away_from_another_account`, `::test_the_first_of_your_own_has_nowhere_to_move`, `::test_a_second_bump_overtakes_the_first_one`; `test_request_priority.py::OrderTests::test_moving_to_the_front_stays_inside_the_level`. **Gaps:** none.
- **Determinism hazards.** `.first()` with explicit `order_by('queue_seq','id')` (deterministic). No lock on the other rows: two concurrent bumps can write the same `queue_seq` (ties then broken by id).
- **History.** 14-Aug (PR #44, Wolfram); partition by effective account (lane) instead of author 29-Aug (PR #48); same-background filter 25-Nov (PR #331).
- **Ordering.** After Q-5.5 (reads `lane_account`).

### Q-6.18 Newest-first (LIFO) preference
- **Statement.** `_apply_lifo_preference(task, requested_by)`: if `requested_by` non-empty and `ContributorPref(account=requested_by, lifo_queue=True)` exists, set `queue_seq = front_of_own_queue(task)` when not None. Applied ONLY after `_request_rung` creates a slot (Q-6.7) or revives one (Q-6.8), and by `_buy_verification` (MPV-6). Not applied to promotion/adoption/join, the LEASED follow-up (Q-6.10), `enqueue_unexplored_children` (Q-6.35), or backlog `_mint` (Q-6.44). Because `request_subtree` buys through `_request_rung`, a LIFO account's subtree children end up in reverse walk order.
- **Inputs / outputs.** Reads `ContributorPref.lifo_queue`; writes `queue_seq`.
- **Code.** `atomicdb/ingest.py:_apply_lifo_preference:7578`; toggle `atomicdb/views.py:api_pref_lifo:2433`.
- **Tests.** `test_queue_control.py::QueueApiAndLifoTests::test_lifo_places_each_new_request_at_the_front_of_only_its_queue`, `::test_switching_lifo_off_leaves_the_next_request_at_the_tail`, `::test_lifo_endpoint_is_post_only_and_requires_login`; `test_then_verify.py::VerifyHonoursNewestFirstTests::test_a_bought_verification_is_offered_to_newest_first`. **Gaps:** subtree reversal under LIFO unpinned.
- **Determinism hazards.** none.
- **History.** Community vote P5 28-Aug; code reconciled 29-Aug (PR #48).
- **Ordering.** After Q-6.7/6.8 within the same transaction.

### Q-6.19 Withdraw a request (author only)
- **Statement.** `withdraw_requester(task, now)` (caller has checked: USER, PENDING, `requested_by == account`, row locked). If `also_requested_by` non-empty: HAND OVER — `requested_by = backers[0]`, `also_requested_by = backers[1:]`, `backers = len(backers)-1`, `queue_seq = 0`; save; `_resolve_lane_account`; return `'handed-over'` (state stays PENDING; budget, generation, route, background, requested_at unchanged). Else: UPDATE state=CANCELLED, `completed = now or timezone.now()`; return `'cancelled'`. LEASED is never withdrawn.
- **Inputs / outputs.** AnalysisTask PENDING → CANCELLED, or ownership change.
- **Code.** `atomicdb/ingest.py:withdraw_requester:7392`.
- **Tests.** `test_queue_control.py::WithdrawOwnRequestTests::*` (7); `test_queue_control.py::WithdrawKeepsOtherBackersTests::test_the_request_survives_under_the_first_of_the_others`, `::test_it_still_gets_served_and_to_its_new_owner`, `::test_the_place_it_held_in_the_old_queue_does_not_travel`, `::test_the_last_person_waiting_closes_it`; `test_queue_control.py::WithdrawCostsNothingTests::test_the_ladder_does_not_advance`, `::test_a_withdrawn_request_is_not_a_served_one`; `test_lanes.py::HandoverLaneTests::*`. **Gaps:** `backers` counter out of sync with the list (handled in clear_own_queue) unpinned here.
- **Determinism hazards.** `completed` wall clock (not read by rules; CANCELLED rows are excluded everywhere by positive state filters).
- **History.** 16-Aug (PR #44, asfault).
- **Ordering.** Called from Q-6.20, Q-6.38.

### Q-6.20 Clear own queue
- **Statement.** `clear_own_queue(username, now)`: '' → (0,0). `mine` = USER, `requested_by=username`, PENDING (any position status). First, for each row with `backers > 0` (unordered iteration), call `withdraw_requester`; count `handed` for 'handed-over' and `withdrawn_by_hand` otherwise. Then bulk UPDATE the rows still matching `mine` with `backers = 0` to CANCELLED, `completed=now` (no lane re-resolution). Returns `(cancelled + withdrawn_by_hand, handed)`. Idempotent. Rows the user only backs are untouched.
- **Inputs / outputs.** PENDING → CANCELLED (bulk) and hand-overs.
- **Code.** `atomicdb/ingest.py:clear_own_queue:7445`; door `atomicdb/views.py:api_queue_clear:8280`.
- **Tests.** `test_queue_control.py::ClearOwnQueueTests::*` (8); `test_lanes.py::CancelledWorkCostsNothingTests::test_clearing_your_queue_gives_the_allowance_back`. **Gaps:** order of hand-overs when several rows have backers (irrelevant to outcome) unpinned.
- **Determinism hazards.** Unordered iteration (`:7480`) — outcome independent of order. Wall clock `completed`.
- **History.** 16-Aug (PR #44, Eclipsia).
- **Ordering.** Inside the door's transaction.

### Q-6.21 Undo a withdrawal
- **Statement.** `restore_cancelled(task)`: False if state != CANCELLED, or `task.position.status != 'UNKNOWN'`. Else conditional UPDATE (`pk`, state=CANCELLED) → state=PENDING, `completed=NULL`; True. Everything else (budget, generation, route, queue_seq, requested_at, background, backers=0, lane_account) is restored as it was. Not checked: the per-account cap (Q-5.10), whether `position.visits` has moved past `generation`, whether another live task now exists for the position.
- **Inputs / outputs.** CANCELLED → PENDING.
- **Code.** `atomicdb/ingest.py:restore_cancelled:7496`.
- **Tests.** `test_queue_control.py::WithdrawCostsNothingTests::test_undo_puts_it_back_exactly_where_it_was`, `::test_undo_is_refused_once_the_position_has_a_verdict`; `test_queue_control.py::ScriptedQueueDoorTests::test_a_scripted_cancel_withdraws_and_undo_restores_the_same_row`. **Gaps:** undo after the slot generation became stale; undo over the cap.
- **Determinism hazards.** The web door locks the task row with `select_related('position')` but the position status is read from that join without a Position lock (comment at `views.py:8252` claims the position is locked; it is only the task row).
- **History.** 16-Aug (PR #44).
- **Ordering.** Door-driven.

### Q-6.22 request_analysis dispatch
- **Statement.** `request_analysis(pos, requested_by, route, budget_floor, only_if_missing, one_search, only_unexplored, background)`: (1) if `budget_floor == 10B`: raise ValueError if any of only_if_missing/one_search/only_unexplored; else return `request_subtree(...)` (Q-7.10). (2) In `atomic()`: if `budget_floor is None` and none of the three flags and `_breadth_swap_eligible(pos)` (Q-7.6): `outcome = _descend_frontier(...)` (Q-7.5); if outcome != 'saturated', write `DBEvent('BREADTH_SWAP', {'key', 'outcome', plus int/str detail items})` and return it. (3) `outcome = _request_rung(...)`; if it is a RequestOutcome return it; if not `_LADDER_EXHAUSTED` return `RequestOutcome(outcome)`. (4) Exhausted: if the swap ran → `saturated`; elif `one_search` → `ladder-spent`; else → `request_subtree(...)`.
- **Inputs / outputs.** As the callees; DBEvent `BREADTH_SWAP`.
- **Code.** `atomicdb/ingest.py:request_analysis:8572`.
- **Tests.** `test_frontier.py::LadderExhaustionTests::test_exhausted_ladder_no_longer_requeues_the_parent`, `::test_solved_position_never_expands`; `test_frontier.py::BreadthSwapTests::test_a_deep_revisit_request_expands_instead_of_deepening`, `::test_the_first_deep_look_is_never_swapped`, `::test_the_seeding_pass_is_never_swapped`, `::test_a_mate_band_node_still_buys_depth`; `test_subtree_request.py::PlainButtonTests::test_the_click_after_2b_buys_the_searches_below`; `test_subtree_request.py::ApiTests::test_the_ingest_door_will_not_mix_the_two_either`; `test_multi_request.py::SpentLadderTests::test_nor_does_the_breadth_swap_buy_replies_in_a_batch`; `test_depth_slider.py::ChosenRungTests::test_an_explicit_choice_buys_depth_even_with_the_swap_on`; `tests.py::RequestTests::*`. **Gaps:** none major.
- **Determinism hazards.** Settings switch `ATOMICDB_BREADTH_SWAP` (env, default off; `OpenSite/settings.py:185`).
- **History.** Swap 27-29 Jul (PR #31); subtree past the ladder 29-Oct (PR #99); `one_search` 29-Oct (PR #99).
- **Ordering.** Called by `_queue_request` (Q-6.26).

### Q-6.23 Which served tasks notify their requester
- **Statement.** `notification_deserved(source, budget_nodes)` = `source == 'USER' or (budget_nodes or 0) >= 128M`. The OR covers USER rows demoted to AUTO by `enqueue_unexplored_children` (Q-6.35) that keep `requested_by`. FILL coverage seeds (8M) and autonomous selector work do not notify. Creation of the notification itself is in the ingest queue (part 5).
- **Inputs / outputs.** Pure predicate.
- **Code.** `atomicdb/ingest.py:notification_deserved:8666`.
- **Tests.** `test_notifications.py::NotificationCreationTests::test_a_served_request_notifies_the_account_that_asked`, `::test_an_anonymous_request_notifies_nobody`, `::test_a_name_without_an_account_notifies_nobody`, `::test_one_task_notifies_exactly_once`, `::test_a_small_coverage_fill_does_not_notify`, `::test_the_criterion_is_the_band_or_the_request_floor`, `::test_an_expansion_notifies_through_each_child_it_bought`. **Gaps:** none.
- **Determinism hazards.** none.
- **History.** With notifications (Aug).
- **Ordering.** Read at landing (Q-3, part 5).

### Q-6.24 `_queue_request`: only-if-missing pre-answer
- **Statement.** Before the dedup and the cap: if `only_if_missing` and `pos.status == 'UNKNOWN'` and `searched_budget(pos) >= (chosen or 128M)` → return `({'status': 'already-analyzed', 'completed_budget': searched}, 200)` without lock, receipt or RequestLog. The purchase re-asks under the lock (Q-6.5).
- **Inputs / outputs.** none written.
- **Code.** `atomicdb/views.py:_queue_request:2911-2921`.
- **Tests.** `test_public_api.py::OnlyIfMissingTests::test_with_the_flag_the_same_call_buys_nothing`, `::test_nothing_bought_leaves_no_click_receipt`, `::test_a_deeper_search_covers_the_first_rung`. **Gaps:** none.
- **Determinism hazards.** none.
- **History.** PR #62.
- **Ordering.** First step of `_queue_request`.

### Q-6.25 `_queue_request`: ip + position dedup
- **Statement.** `recent_same_position` = a `RequestLog(ip, position)` with `created >= now - 1 h` exists. `active_user_request` = a live USER task exists on the position. If `recent_same_position` and not active and `ladder_exhausted(pos)`: active := a live USER task exists on any CHILD (via `edges_in.parent = pos`). If `recent_same_position and active_user_request and chosen is None`: if not `background`, UPDATE live USER rows of the position with `background=True` → False (any author); return `({'status': 'already-requested'}, 200)`. A chosen rung bypasses the dedup. There is no hourly rate limit beyond this. `ip` = last comma-separated entry of `X-Forwarded-For`, else `REMOTE_ADDR`, else `'0.0.0.0'`.
- **Inputs / outputs.** Reads `RequestLog`; may clear `background`.
- **Code.** `atomicdb/views.py:_client_ip:2693`, `:_queue_request:2922-2968`.
- **Tests.** `tests.py::RequestTests::test_request_dedup_same_ip_position`, `::test_completed_recent_request_can_queue_next_rung`, `::test_fen_creation_log_does_not_block_first_analysis_request`, `::test_receipts_no_longer_rate_limit_an_ip`; `test_public_api.py::RequestApiTests::test_asking_twice_from_one_address_is_the_same_click_twice`, `::test_the_door_has_no_hourly_gate`; `test_subtree_request.py::PlainButtonTests::test_a_double_click_does_not_buy_twice`, `::test_nor_when_the_searches_sit_further_down`; `test_depth_slider.py::ChosenRungTests::test_a_repeated_click_without_a_choice_still_deduplicates`; `test_request_priority.py::UpgradeTests::test_a_click_from_the_same_address_lifts_it_too`. **Gaps:** dedup counts ANY live USER task on the position (someone else's) as "still being served" — unpinned.
- **Determinism hazards.** Wall clock 1-h window (`:2922`); depends on the client IP header.
- **History.** Hourly limit removed 28-Jul; dedup scoped to live tasks; child check 29-Oct (PR #99); background lift 25-Nov (PR #331).
- **Ordering.** After Q-6.24, before Q-5.10.

### Q-6.26 `_queue_request`: cap, route, purchase, receipt, payload
- **Statement.** After the dedup: Q-5.10/5.11 cap check. Route = `checked_route` if given else `_validated_route_for(raw_route, pos.key)` (full replay against the rules; any failure → '' and the request proceeds). In `atomic()`: `outcome = ingest.request_analysis(...)`; if outcome in (`queued`, `already-queued`, `expanded`, `saturated`) create `RequestLog(ip, position=pos)` (same transaction: a lock error rolls back the task too). Payload = `{'status': str(outcome)} ∪ outcome.detail`; if `detail.subtree`, add `message` (server-composed sentence); if `with_place` and status in (queued, already-queued) add `ahead` (Q-5.8) when not None. Returns HTTP 200.
- **Inputs / outputs.** `RequestLog` row; tasks via Q-6.22.
- **Code.** `atomicdb/views.py:_queue_request:2969-3007`, `:_validated_route_for:2787`.
- **Tests.** `tests.py::RequestTests::test_request_log_lock_rolls_back_new_task`, `::test_request_lock_is_reported_as_retryable_busy`; `test_queue_fairness.py::OwnRequestAffinityTests::test_a_declared_route_is_stored_and_wins_the_label`, `::test_a_broken_route_is_ignored_but_the_click_still_lands`, `::test_the_request_endpoint_records_the_logged_in_requester`; `test_frontier.py::DescentRequestApiTests::test_a_saturated_click_still_leaves_one_receipt`; `test_subtree_request.py::PlainButtonTests::test_nothing_left_to_buy_is_said_and_still_leaves_one_receipt`. **Gaps:** none.
- **Determinism hazards.** none beyond callees.
- **History.** Common path for click and API (Aug, Wolfram).
- **Ordering.** After Q-6.25; calls Q-6.22.

### Q-6.27 Explorer single-request door
- **Statement.** `POST /atomicdb/request/<key>/` (CSRF required; GET → 405 `{'error':'POST only'}`). Unknown key → 404 `{'status':'unknown-position'}`. `chosen, invalid = depth.chosen_rung(request)`; invalid → 400 `bad-budget`. If `chosen == 10B` and POST `confirm != '1'` → 400 `confirm-10b`. `target = chosen` if chosen and != 10B else `_expected_budget(POST.expect)`; if target and `completed_max (bought, incl. absorbed) >= target` → 200 `{'status':'already-analyzed','completed_budget': completed_max}` (no receipt). Then `_queue_request(pos, ip, requested_by=session user or '', chosen, raw_route=POST.route)`. If POST `then_verify == '1'` and status in (queued, already-queued, already-requested): UPDATE all live USER tasks of the position (any author) `then_verify=True`; if any updated, payload `then_verify: true`. On `OperationalError` containing "database is locked"/"database table is locked" → 503 `{'status':'busy'}` with `Retry-After: 2`; other errors re-raised.
- **Inputs / outputs.** Writes `then_verify`; tasks via Q-6.26.
- **Code.** `atomicdb/views.py:_api_request_once:2699`, `:api_request:3032`.
- **Tests.** `test_depth_slider.py::ExpectedBudgetTests::*` (5), `test_depth_slider.py::ChosenRungTests::test_the_last_stop_requires_an_explicit_confirmation`, `::test_the_top_rung_no_longer_asks_for_one`, `::test_a_choice_already_searched_buys_nothing`; `test_then_verify.py::ThenVerifyRequestTests::test_the_box_marks_the_live_request`, `::test_without_the_box_nothing_is_marked`; `tests.py::ExplorerRequestCsrfTests::*`; `tests.py::RequestTests::test_request_lock_is_reported_as_retryable_busy`, `::test_non_lock_database_error_is_not_hidden_or_retried`. **Gaps:** `then_verify` marking another person's live request unpinned.
- **Determinism hazards.** The `expect` check uses BOUGHT rungs (absorbed included) while only_if_missing uses SEARCHED rungs — two different answers for the same position (`views.py:2750` vs Q-6.24).
- **History.** confirm 10B 28-Aug (P2); `expect` 3-Nov (PR #151); then_verify 5-Nov (PR #163).
- **Ordering.** Door → Q-6.24..26.

### Q-6.28 `expect` field
- **Statement.** `_expected_budget(raw)`: `int(raw)` if it parses and is in `REQUEST_BUDGET_LADDER`, else None (ignored, never refused).
- **Inputs / outputs.** Pure.
- **Code.** `atomicdb/views.py:_expected_budget:2774`.
- **Tests.** `test_depth_slider.py::ExpectedBudgetTests::test_no_expect_or_a_bad_one_behaves_as_before`, `::test_a_stale_expect_is_refused_and_buys_nothing`, `::test_a_current_expect_buys_exactly_that_rung`. **Gaps:** none.
- **Determinism hazards.** none.
- **History.** PR #151.
- **Ordering.** Inside Q-6.27.

### Q-6.29 Script identity and priority
- **Statement.** `_api_request_identity`: no `username` and no `password` in POST → anonymous `(None, False)`; otherwise `_auth(request)` must succeed (worker credentials, enabled profile) else `(None, True)` → 403 `{'status':'refused','reason':'bad credentials'}` with no receipt. The session cookie is never read by script doors. `_request_priority`: `priority` field (default `'low'`), case-insensitive, stripped; `low` → background=True, `normal` → False; anything else → 400 `{'status':'refused','reason':'priority must be low (the default) or normal'}`. `_priority_word(task)` = 'low' if `background` else 'normal'.
- **Inputs / outputs.** none written.
- **Code.** `atomicdb/views.py:_api_request_identity:3251`, `:API_PRIORITIES:3280`, `:_request_priority:3284`, `:_priority_word:3302`.
- **Tests.** `test_request_priority.py::DoorTests::*` (7); `test_public_api.py::RequestApiTests::test_wrong_credentials_are_not_quietly_downgraded_to_anonymous`, `::test_without_credentials_it_is_served_as_an_anonymous_click`, `::test_a_browser_session_does_not_authenticate_this_endpoint`. **Gaps:** none.
- **Determinism hazards.** `_auth` has a 5-minute credential cache (out of scope, views).
- **History.** Priority 25-Nov (PR #331).
- **Ordering.** Before any write in Q-6.30/6.33.

### Q-6.30 Public request API (`/atomicdb/api/request`)
- **Statement.** CSRF-exempt POST (else 405 `{'status':'refused','reason':'POST only'}`). Order: identity (Q-6.29) → FEN parse `_parse_public_fen` (PublicFenError → 400 with its text; any other exception → 400 'that FEN could not be read') → `chosen_rung_for(budget, user)` (invalid → 400 with the reason listing the rungs and the 10B stop) → priority → `only_if_missing` (`'1'` or `'true'`) together with `asks_for_subtree(budget)` → 400 refused (for everybody). In `atomic()`: if the FEN's key is unknown, `get_or_create_position_linked(fen)` (POS-3), `DBEvent('SEEDED', {key, fen, ip, via:'api'})`, `seeded=True`; always `ApiRequestLog(account=username, ip)`. After commit, `settle_linked_children(pos)` if the seed linked closed children. Then `moves` label (Q-6.31), then `_queue_request(pos, ip, requested_by=username, chosen, only_if_missing, checked_route, background)`. Payload adds `key, fen, seeded, task (id), budget_nodes, task_state ('searching' if LEASED else 'waiting'), priority, backed` from `_api_request_task(pos, username)` = lowest-id live USER task of the position (`backed` = username in its `also_requested_by`); if status in `_REQUEST_REFUSALS`, add `reason`. Busy → 503 refused 'server busy, retry', Retry-After 2.
- **Inputs / outputs.** Position seed, DBEvent `SEEDED`, `ApiRequestLog`, tasks via Q-6.26.
- **Code.** `atomicdb/views.py:api_public_request:3333`, `:_api_request_task:3307`, `:_REQUEST_REFUSALS:3557`.
- **Tests.** `test_public_api.py::RequestApiTests::*` (17); `test_subtree_request.py::ApiTests::*`; `test_request_priority.py::DoorTests::test_an_api_request_is_background_by_default`, `::test_priority_normal_puts_an_api_request_at_the_click_level`, `::test_an_unknown_priority_is_refused_before_anything_happens`. **Gaps:** `task` reported is the lowest-id live row, which after a LEASED follow-up (Q-6.10) is the running one, not the one just queued (request-children uses `newest=True`, this door does not).
- **Determinism hazards.** none beyond callees.
- **History.** API added Aug (Wolfram); seeding; `task_state` 11-Oct; `backed` Aug; priority 25-Nov (PR #331).
- **Ordering.** Door → Q-6.24..26.

### Q-6.31 `moves` label on API requests
- **Statement.** `_api_moves_route(raw, target_key)`: strip; longer than `API_MOVES_MAX_CHARS = 64*8 = 512` chars → ignored ("the line is longer than 64 plies"); split on commas/whitespace, lowercase; empty → `('', '')` (no line); more than 64 plies → ignored; replay from the start FEN: each token must match `^[a-h][1-8][a-h][1-8][qrbn]?$` and be legal (`apply_move` raises otherwise); a repeated position key → ignored; the final key must equal the target key; every position on the line must already exist (no creation) — otherwise ignored with a reason string. Returns `(','.join(ucis), '')`. `_api_moves_outcome`: reason → `{'moves_ignored': reason}`; no live task → `{'moves': route}` if subtree, else ignored 'no request is waiting on this position, so there is nothing to label'; if status in (queued, already-queued, already-requested) and the task has no route, conditional UPDATE `route=route WHERE route=''`; reply `{'moves': route}` if the task's route equals it, else `{'moves_ignored': 'this request already shows another line'}`. The label never affects ladder, dedup, order or budget.
- **Inputs / outputs.** Writes `AnalysisTask.route` only when empty.
- **Code.** `atomicdb/views.py:API_MOVES_MAX_PLIES:2819`, `:_api_moves_route:2823`, `:_api_moves_outcome:3520`.
- **Tests.** `test_api_request_moves.py::ApiRequestMovesTests::*` (14); `test_api_request_children.py::RequestChildrenApiTests::test_a_line_sent_on_a_repeated_call_labels_the_requests`, `::test_joining_a_request_already_on_an_engine_labels_it`, `::test_a_line_already_on_a_request_stays`, `::test_a_line_at_the_ply_limit_is_ignored_with_its_reason`. **Gaps:** none.
- **Determinism hazards.** none.
- **History.** 14-Nov (PR #243, MoKy).
- **Ordering.** Before (route passed as `checked_route`) and after (`_api_moves_outcome`) `_queue_request`.

### Q-6.32 Multi-select of moves (`/atomicdb/request-moves/<key>/`)
- **Statement.** CSRF POST; anonymous → 403 `login-required`. Unknown → 404; closed → `already-solved`. `chosen` via Q-5.2 (invalid → 400 `bad-budget`; `chosen == 10B` → 400 `top-rung-single`; a non-chooser's 10B is dropped). `moves` split on commas, stripped, deduplicated in order; legal ones kept; `ignored` = first 64 dropped tokens cut to 8 chars; none legal → 400 `no-moves`; more than `MULTI_REQUEST_CAP = 64` legal → 400 `too-many`. Cap check (Q-5.10) once. Line on the page: validated `route` or the canonical route; moves whose child key is on that line → `blocked` (nothing built). For each wanted move in order: child = existing edge child or `_materialise_child(pos, uci)` (creates node/edge as following it would, POS-2/3); `_queue_request(child, ip, username, chosen, raw_route=route+','+uci if route else '', with_place=False, one_search=True, only_if_missing=(chosen is not None))`; status `queue-full-account` → this and all remaining moves `queue-full`, stop. Reply `{status: 'partial' if any queue-full else 'done', counts (per status, + illegal), moves: [{uci, status}], ignored, left_out}`; `DBEvent('BULK_REQUEST', {ip, key, via:'moves', queued, candidates})`. Each move commits separately.
- **Inputs / outputs.** Tasks via Q-6.26; DBEvent `BULK_REQUEST`; may create positions/edges.
- **Code.** `atomicdb/views.py:MULTI_REQUEST_CAP:192`, `:api_request_moves:3059`, `:_request_moves:3144`.
- **Tests.** `test_multi_request.py::RequestMovesTests::*`, `::DepthTests::*`, `::SpentLadderTests::*`, `::TargetDepthTests::*`, `::BarDepthTests::*`, `::AccountCapTests::*`, `::RouteTests::*`, `::TallyTests::*`, `::BlockedRowTests::*`, `::CsrfTests::*` (38 total). **Gaps:** none major.
- **Determinism hazards.** Child key computation for non-edges via `_child_keys` (page cache, views) — out of scope.
- **History.** 29-Oct (PR #98); one search per move (PR #99).
- **Ordering.** Door → per move Q-6.24..26.

### Q-6.33 Request children API (`/atomicdb/api/request-children`)
- **Statement.** CSRF-exempt POST. Order: identity → FEN → `asks_for_subtree(budget)` → 400 (for everybody) → `chosen_rung_for` (invalid → 400) → priority → optional `select` (`_selected_moves`: tokens from all `select` values split on commas/whitespace, lowercase, dedup; empty → 400 'select names no move'; none legal → 400 with `skipped` illegal list; more than 64 legal → 400) → `only_if_missing`. Seed + `ApiRequestLog` + SEEDED event as Q-6.30. `moves`/`route` label: as Q-6.31 but a line of `>= 64` plies is ignored ("longer than 63 plies"). Closed → `already-solved` with empty lists. Cap → 429 with `reason`. Line for blocking: validated route or canonical; the position itself is also "on line". Candidates: with `select`, every named legal move (blocked ones skipped `blocked`); without, for every legal move in movegen order: blocked → skip; a known transposition not yet an edge and (closed or not unexplored) → `_materialise_child` first (links it); closed child → skip `closed`; existing explored child → skip `already-analyzed`; else candidate. Purchase loop over candidates: when `len(queued) >= CHILDREN_REQUEST_CAP = 64`, the rest are skipped `batch-cap`; materialise if needed; closed → `closed`; `_queue_request(child, ip, username, chosen, checked_route=route+','+uci (if route), with_place=False, one_search=True, only_if_missing=only_if_missing or chosen is not None, only_unexplored=not named, background)`; `already-solved` → `closed`; `queue-full-account` → rest skipped with that reason, stop; task = `_api_request_task(child, username, newest=(status=='queued'))` (newest = highest generation then id); `queued` entries carry `task, budget_nodes, task_state, priority`; other statuses go to `skipped` with `reason` (+`completed_budget`, + task fields for already-queued/requested, + `backed` for already-queued); label per Q-6.31. `skipped` sorted by the order of the request (select list or legal moves). `counts = {'queued': n, <reason>: n...}` (+`illegal` when more than 64 illegal). DBEvent `BULK_REQUEST` (`via` 'api-children-select' or 'api-children'). Status: `partial` if any queue-full-account, `nothing-to-do` if no candidates, else `done`. Background default True.
- **Inputs / outputs.** As Q-6.32 plus seeding.
- **Code.** `atomicdb/views.py:CHILDREN_REQUEST_CAP:3587`, `:api_public_request_children:3591`, `:_selected_moves:3678`, `:_request_children:3720`.
- **Tests.** `test_api_request_children.py::RequestChildrenApiTests::*` (31), `test_api_request_children.py::RequestSelectedChildrenTests::*` (21); `test_request_priority.py::DoorTests::test_request_children_is_background_by_default`, `::test_request_children_takes_priority_normal`. **Gaps:** none major.
- **Determinism hazards.** none beyond callees; movegen order from FSF.
- **History.** 23-Nov (#300 → PR #315); `select` 24-Nov (#303 → PR #315).
- **Ordering.** Door → per move Q-6.24..26.

### Q-6.34 Unexplored-moves button (`/atomicdb/request-unexplored/<key>/`)
- **Statement.** CSRF POST. Unknown → 404; closed → `already-solved`. Cap check (Q-5.10) for the session user or ''. Route validated as elsewhere (failure → ''). In `atomic()`: lock the parent Position (`select_for_update`); `pending = unexplored_children(pos)` (materialised children with `is_unexplored`, edge-id order); none → `{'status':'nothing-to-do','queued':0}`; else `queued = enqueue_unexplored_children(pos, requested_by, route)` (Q-6.35, source USER, cap 64), `RequestLog(ip, position=pos)`, `DBEvent('BULK_REQUEST', {ip, key, queued, candidates})`. Reply `{'status':'queued','queued':n,'candidates':len(pending)}` (status is 'queued' even if n == 0).
- **Inputs / outputs.** Tasks; RequestLog; DBEvent.
- **Code.** `atomicdb/views.py:api_request_unexplored:2104`.
- **Tests.** `test_explorer_proof_ui.py::UnexploredButtonTests::test_it_queues_every_reply_nobody_looked_at`, `::test_the_click_is_capped`, `::test_a_second_click_queues_nothing_new`, `::test_a_fully_explored_position_says_so`; `tests.py::ExplorerRequestCsrfTests::*`; `test_lanes.py::AccountQueueCapTests::test_the_bulk_button_respects_the_same_allowance`. **Gaps:** only materialised children are considered (legal moves without an edge are not; contrast Q-6.33).
- **Determinism hazards.** none beyond callees.
- **History.** Aug; walked/seeded children included 8-Aug and 11-Oct.
- **Ordering.** Door → Q-6.35.

### Q-6.35 enqueue_unexplored_children (request path and AUTO arm path)
- **Statement.** Predicates: `is_unexplored(child)` = UNKNOWN and `nodes_invested` falsy and `backed_nodes` falsy (seeded or walked numbers do not exempt); `is_unjudged(child)` = UNKNOWN and `eval_cp is None` and `backed_eval is None`. `enqueue_unexplored_children(pos, cap=64, source=USER, requested_by='', route='', children=None)`: children = `unexplored_children(pos)` (edge-id order) or the caller's list. For each child until `queued >= cap`: `clamp = _short_mate_clamp(child)`; budget = `max(128M, budget_for(child))` if no clamp else `budget_for(child)`; if source != USER, `budget = server_picked_budget(budget, clamp, pos=child)`. `child_route = route + ',' + move` when route and edge known. Dedup by POSITION: `vivo` = lowest-id live task of the child (any generation). If vivo: `respaldado = add_requester(vivo, requested_by)`; if vivo is PENDING and `vivo.source != source`: set `vivo.source = source` (this DEMOTES a USER request to AUTO when called with AUTO), fill empty route, stamp requested_at only when the new source is USER, save; count +1. Elif respaldado count +1. Continue. Else in a savepoint on the AtomicDB connection create `AnalysisTask(position=child, generation=child.visits, budget, multipv=multipv_for(child.visits, budget, clamp), source, requested_by, route=child_route, requested_at=now if USER else None)` → +1. On IntegrityError: re-read vivo; if live, `add_requester` and +1 if joined; else if a CANCELLED row holds `(child, child.visits)`, `_revive_withdrawn(withdrawn, budget, multipv, requested_by, child_route, source=source)` +1; else skip. Returns the count (joins included). No LIFO, no `background` argument (always False on create). `arm` is ''. Part 6 owns the arm accounting of AUTO callers; the AUTO caller in this part is Q-7.11.
- **Inputs / outputs.** Creates/updates AnalysisTask rows; may demote source.
- **Code.** `atomicdb/ingest.py:UNEXPLORED_CLICK_CAP:9568`, `:is_unexplored:9571`, `:unexplored_children:9611`, `:is_unjudged:9619`, `:unjudged_children:9634`, `:enqueue_unexplored_children:9642`.
- **Tests.** `test_explorer_proof_ui.py::UnexploredButtonTests::*` (incl. `test_a_seeded_value_no_longer_exempts_a_reply`, `test_a_backed_reply_counts_as_known`, `test_a_walked_number_does_not_exempt_a_reply`); `test_mate_clamp.py::UnexploredChildrenTests::test_a_lost_race_for_the_same_generation_does_not_break_the_click`, `::test_children_without_information_keep_the_request_floor`, `::test_a_child_that_already_claims_a_short_mate_is_verified`; `test_queue_control.py::WithdrawCostsNothingTests::test_the_bulk_button_can_buy_a_withdrawn_reply`; `test_idle_pool_budget.py::VisitorRequestTests::test_the_same_child_keeps_its_visitor_price`; `test_backprop_floor.py::FloorAndTheBulkButtonTests::*`; `test_showcase_bound.py::WalkedUnexploredTests::test_seeded_eval_counts_as_unexplored`; `test_queue_control.py::BackersStayInTheVisitorBandTests::test_a_degraded_request_does_not_outrank_position_priority`. **Gaps:** an absorbed row on the slot in the IntegrityError branch is not revived (only CANCELLED is) and the move is silently skipped; USER→AUTO demotion as a deliberate rule is only indirectly pinned.
- **Determinism hazards.** `.first()` on the CANCELLED slot without order (unique slot, so safe); savepoint routing (`router.db_for_write`); IntegrityError race path depends on concurrency.
- **History.** Dedup by position + add_requester 17-Aug (math_god); IntegrityError path 6-Sep (Eclipsia); revive withdrawn reply (Oct); `is_unexplored` drops `backed_eval is None` 11-Oct.
- **Ordering.** Inside the caller's transaction (parent locked by Q-6.34).

### Q-6.36 Verify-PV door (queueing side only)
- **Statement.** `POST /atomicdb/pv-verify/<key>/` (CSRF). Unknown → 404; closed → `already-solved`; cap check (Q-5.10, 429); route validated; `line` = int(POST.line) if present else 0; unparseable or negative → 400 with `status 'unknown-line'`. In `atomic()` lock the Position and call `ingest.enqueue_pv_verification(pos, requested_by, route, line)` (MPV-6). Reply `{status: 'unknown-line'|'queued'|'nothing-to-do', queued, line, from_tree, plies, covered_plies, message}`; 400 when 'unknown-line'. Writes NO `RequestLog` (deliberate: it does not buy a rung on this position).
- **Inputs / outputs.** Tasks via MPV-6.
- **Code.** `atomicdb/views.py:api_pv_verify:2451`, `:_pv_verify_payload:2532`.
- **Tests.** (MPV-6 tests in `test_pv_verify.py`); **Gaps:** cap and line-parse refusals of this door not pinned in the Q inventory.
- **Determinism hazards.** none here.
- **History.** 30-Jul/3-Aug (Wolfram).
- **Ordering.** Door → MPV-6.

### Q-6.37 Move to front (bump), web and script
- **Statement.** Web: `POST /atomicdb/queue/bump/<id>/` (CSRF; anonymous → redirect to `/login/?next=/atomicdb/me/`). Script: `POST /atomicdb/api/queue-bump/<id>[/]` (CSRF-exempt, credentials in form or JSON body via `_resolve_script_account`; bad → 403 `{'error':'bad credentials'}`; GET → 405). Both: in `atomic()`, lock the row filtered by `pk`, `source=USER`, `requested_by=account`, `state=PENDING`; none → `not-yours` (same answer for foreign/served/absent); `seq = front_of_own_queue(task)`; None → `already-first`; else UPDATE `queue_seq=seq` → `moved`, then `invalidate_queue_ahead(id)`. Web with `back` → redirect to `back` if it starts with `/atomicdb/` else the account page; else JSON `{status, moved}`. A backer cannot bump (author only).
- **Inputs / outputs.** Writes `queue_seq`.
- **Code.** `atomicdb/views.py:api_queue_bump:8102`, `:api_queue_bump_script:2358`, `:_resolve_script_account:2332`.
- **Tests.** `test_queue_control.py::OwnQueueBumpTests::*` (10), `::BumpOnTheProfileTests::*`, `::BumpOnThePositionTests::*`, `::ScriptedQueueDoorTests::test_a_scripted_bump_moves_only_the_request_of_its_own_account`, `::test_a_scripted_bump_forgets_the_cached_place`. **Gaps:** none.
- **Determinism hazards.** none beyond Q-6.17.
- **History.** 14-Aug (PR #44); script door 30-Aug.
- **Ordering.** Door → Q-6.17.

### Q-6.38 Withdraw / undo, web and script
- **Statement.** Web `POST /atomicdb/queue/cancel/<id>/` (CSRF; anonymous → login redirect); script `POST /atomicdb/api/queue-cancel/<id>[/]` (credentials). Rows filtered by `pk`, `source=USER`, `requested_by=account`. `undo` = POST `undo == '1'` (script also accepts JSON `undo` in ('1', 1, True)). Undo: lock the CANCELLED row (web uses `select_related('position')`); none → `nothing-to-undo`; else `restore_cancelled` → `restored`, or web `cannot-restore` / script `nothing-to-undo` when it returns False. Withdraw: lock the PENDING row; none → `not-yours`; else status = `withdraw_requester(task)` (`cancelled` | `handed-over`). Web JSON `{status, task, key}` or redirect with `?<status>=<id>` to a safe `back`; script JSON `{status}`.
- **Inputs / outputs.** Q-6.19 / Q-6.21 transitions.
- **Code.** `atomicdb/views.py:api_queue_cancel:8195`, `:_own_queue_page:8173`, `:api_queue_cancel_script:2393`.
- **Tests.** `test_queue_control.py::WithdrawOwnRequestTests::*`, `::WithdrawKeepsOtherBackersTests::*`, `::WithdrawOnThePagesTests::*`, `::WithdrawOnThePositionTests::*`, `::ScriptedQueueDoorTests::test_a_scripted_cancel_withdraws_and_undo_restores_the_same_row`, `::test_a_scripted_cancel_takes_undo_from_a_json_body`, `::test_a_scripted_cancel_cannot_touch_the_request_of_a_stranger`. **Gaps:** the differing failure word (`cannot-restore` vs `nothing-to-undo`) is not pinned.
- **Determinism hazards.** none beyond Q-6.21.
- **History.** 16-Aug (PR #44); script door 30-Aug.
- **Ordering.** Door → Q-6.19/6.21.

### Q-6.39 Clear own queue door
- **Statement.** `POST /atomicdb/queue/clear/` (CSRF; anonymous → login redirect). POST `confirm != '1'` → 400 `{'status':'confirm-required','cancelled':0,'handed_over':0}`. Else in `atomic()` `clear_own_queue(username)` → `{'status':'cleared','cancelled':n,'handed_over':m}` or redirect `?cleared=n&handed=m`.
- **Inputs / outputs.** Q-6.20.
- **Code.** `atomicdb/views.py:api_queue_clear:8280`.
- **Tests.** `test_queue_control.py::ClearOwnQueueTests::test_a_post_without_the_confirmation_empties_nothing`, `::test_without_a_session_it_leads_to_the_login`, `::test_a_get_empties_nothing`. **Gaps:** none.
- **Determinism hazards.** none.
- **History.** PR #44.
- **Ordering.** Door → Q-6.20.

### Q-6.40 My queue (served list)
- **Statement.** `GET` with session or `POST` with credentials (form/JSON) `/atomicdb/api/my-queue[/]`; other methods 405; no identity → 401. Rows: tasks with `requested_by=account` and live, ordered `(LEASED first, background, queue_seq, id)`, first 200, each `{task_id, position, fen, budget_nodes, state, queue_seq, priority ('low'|'normal'), backed:false, moves: route, bump: '/atomicdb/api/queue-bump/<id>/', cancel: '/atomicdb/api/queue-cancel/<id>/'}`; then up to `200 - len` live tasks NOT authored by the account whose `also_requested_by` contains it (ordered by id; Postgres JSON containment, SQLite in-Python), with `backed:true`, `bump/cancel: null`. Note: no `source` filter (a demoted AUTO row with this `requested_by` is listed).
- **Inputs / outputs.** Read-only.
- **Code.** `atomicdb/views.py:api_my_queue:2164`, `:_backed_by:2314`.
- **Tests.** `test_queue_control.py::QueueApiAndLifoTests::test_my_queue_is_private_ordered_and_actionable`, `::test_my_queue_needs_a_session_or_credentials`; `test_queue_control.py::ScriptedQueueDoorTests::test_my_queue_answers_form_credentials_without_a_session`, `::test_my_queue_lists_requests_this_account_backed`, `::test_my_queue_answers_json_credentials`, `::test_the_routes_answer_without_the_trailing_slash`, `::test_wrong_credentials_and_disabled_accounts_are_refused`. **Gaps:** none.
- **Determinism hazards.** Vendor-dependent JSON containment path (`views.py:2326`), same result.
- **History.** P3 28-Aug; script door 30-Aug; backed rows 8-Nov (PR #195).
- **Ordering.** Read-only.

### Q-6.41 Newest-first toggle
- **Statement.** `POST /atomicdb/queue/lifo/` (login required → 401 JSON; GET → 405): `ContributorPref.get_or_create(account=username)`, `lifo_queue = (POST.lifo == '1')`; redirect to `back` if it starts with `/atomicdb/`, else the user page. Only affects future purchases (Q-6.18).
- **Inputs / outputs.** Writes `ContributorPref.lifo_queue`.
- **Code.** `atomicdb/views.py:api_pref_lifo:2433`.
- **Tests.** `test_queue_control.py::QueueApiAndLifoTests::test_lifo_endpoint_is_post_only_and_requires_login`, `::test_switching_lifo_off_leaves_the_next_request_at_the_tail`. **Gaps:** none.
- **Determinism hazards.** none.
- **History.** P5 (28-Aug), PR #48.
- **Ordering.** n/a.

### Q-6.42 Backlog adoption: batch, places and caps
- **Statement.** `adopt(username, backlog, size)`: '' → `sign-in`; unknown backlog or `size not in sizes_of(backlog)` (default `(25, 50, 100)`; deep-wins `(5, 10, 20)`) → `bad-request`. `room = REQUEST_QUEUE_MAX - count(PENDING USER requested_by=username on UNKNOWN positions)`; `room <= 0` → write the LANE_CAP_HIT receipt (Q-5.11) and return `queue-full`. Places: `places_of(budget) = _rung_matching(budget) / 128M` (8M → 1/16, 32M → 1/4, 128M → 1, 512M → 4); `free = ADOPT_WAITING_MAX (500) - adopted_waiting(username)` where adopted_waiting = sum of places over PENDING USER rows of the account with `arm` in the backlog arms on UNKNOWN positions; `free < min_places_of(backlog)` → `adopted-full`. `want = min(size * weight_of(backlog), free)` (weight 4 for deep-wins, else 1). In ONE `atomic()`: loop while `places < want`, `adopted < room`, not capped and `scanned < scan_max` (600; clear-wins 2,000): take `min(ceil((want-places)/smallest) + 25, room - adopted + 25, scan_max - scanned)` OPEN `BacklogItem` rows of the backlog ordered by id with `SELECT FOR UPDATE SKIP LOCKED`; read their positions filtered `status='UNKNOWN'` and `priority > DEAD/2`; for each item (stop when `places >= want` or `adopted >= room`): `reason = _verdict(...)` (Q-6.43); if None: budget per Q-6.43; if `places + places_of(budget) > free` → capped, stop (row stays OPEN); `task = _mint(...)` (Q-6.44); None → reason `queued`; else `places += places_of(task.budget_nodes)`. Item transitions: `HELD` (reason held), `ADOPTED` (adopted_by, adopted_at, task_id), else `DROPPED` (reason). Result `adopted` if any, else `adopted-full` if capped else `empty`. Deletes the counts cache. JSON door returns 400 for bad-request, 429 (+Retry-After) for queue-full, else 200; anonymous JSON → 401 `sign-in`.
- **Inputs / outputs.** `BacklogItem.state/adopted_by/adopted_at/task_id/reason`; tasks via Q-6.44.
- **Code.** `atomicdb/backlog.py:ADOPT_SIZES:157`, `:BACKLOGS:162`, `:places_of:234`, `:ADOPT_WAITING_MAX:283`, `:_places_waiting:593`, `:_queue_room:681`, `:adopt:697`, `:api_adopt:800`.
- **Tests.** `test_backlog.py::Adopting::*`, `::Caps::*`, `::ClearWinsTiers::test_a_batch_counts_searches_of_128m`, `::test_a_batch_stops_at_its_places`, `::test_the_accounts_places_cap_the_batch`, `::test_a_dear_position_that_does_not_fit_stays_open`, `::DeepWins::test_each_waiting_position_takes_four_places`. **Gaps:** the `room` count uses PENDING USER rows of any arm; interleaving of two adopters with SKIP LOCKED unpinned (no-op on SQLite).
- **Determinism hazards.** `SKIP LOCKED` (row selection depends on concurrent adopters, `backlog.py:730`); wall clock `now`.
- **History.** 15-Nov (PR #251); places by rung 18-Nov (PR #281); deep wins 16-Nov (PR #272); best line 17-Nov (#273 → PR #274).
- **Ordering.** Door → Q-6.43 → Q-6.44.

### Q-6.43 Backlog adoption: verdict, budget and width
- **Statement.** `_verdict(backlog, row, repaired)` returns None (adopt) or a drop reason: row missing (closed or `priority <= DEAD/2`) → `closed`. Best line: `best_line_shallow(last_analysis)` None → `done`; not `_still_reachable` → `closed`. Deep wins: not (`UNKNOWN`, `visits == 2`, side to move ahead by ≥ 2500 cp on `backed_eval` else `eval_cp`) or a COMPLETED searched task with arm `deep25` exists → `done`; bid off its rung → `deep`; unreachable → `closed`. Clear wins: not `sweep.qualifies` → `done`; tier None (value under 3300 for the side to move) → `held`; off rung → `deep`; unreachable → `closed`. Lost lines: off rung → `deep`; not `lines_short` (stored lines `< min(5, legal moves)`) or showcase restricted (newest showcase pass has `restricted`) → `done`. Shallow: showcase budget ≥ `ADOPT_BUDGET` → `deep`; already repaired (idle_repair) or not `shallow_now` → `done`. Budgets: `ADOPT_BUDGET = 128M*1005//1000 = 128,640,000`; `DEEP_BUDGET = 512M*1005//1000 = 514,560,000`; clear-wins tiers by side-to-move value: ≥5200 → 8M, ≥3900 → 32M, ≥3300 → ADOPT_BUDGET. `adopt_budget`: base (tier or backlog base); if backlog in (lost-lines, clear-wins, deep-wins) and base ≥ ADOPT_BUDGET: `max(base, showcase_budget * 1002 // 1000)`. Off rung = `_rung_matching(adopt_budget) > _rung_matching(base)`. MultiPV: lost-lines 5, clear-wins 1 (`sweep.SWEEP_MULTIPV`), deep-wins 1, others `multipv_for(visits, ADOPT_BUDGET)`.
- **Inputs / outputs.** Reads Position fields and tasks.
- **Code.** `atomicdb/backlog.py:CLOSED..HELD:302-306`, `:showcase_budget:322`, `:lines_short:329`, `:clear_win_tier:374`, `:deep_win_now:407`, `:best_line_shallow:422`, `:adopt_budget:467`, `:_off_rung:481`, `:_verdict:488`, `:_multipv:540`.
- **Tests.** `test_backlog.py::RecheckAtAdoption::*` (8), `::ClearWinsTiers::test_the_value_of_the_side_to_move_picks_the_search`, `::test_a_small_search_never_bids_over_the_shown_one`, `::DeepWins::test_a_batch_buys_one_line_at_512m`, `::test_the_bid_stays_on_the_512m_rung`, `::test_closed_done_deep_and_queued_positions_leave_the_list`, `::Adopting::test_the_price_beats_the_search_it_redoes`, `::test_shallow_positions_get_the_house_width_of_a_revisit`. **Gaps:** best-line drop reasons only partly pinned.
- **Determinism hazards.** `_still_reachable` reads the graph at adoption time.
- **History.** PRs #251, #272, #274, #281.
- **Ordering.** Inside Q-6.42.

### Q-6.44 Backlog adoption: mint on the slot
- **Statement.** `_mint(key, visits, multipv, arm, username, now, budget)`: `get_or_create(position=key, generation=visits, defaults={budget_nodes, multipv, source USER, requested_by=username, requested_at=now, arm})`. Created → task. If the slot is CANCELLED or absorbed: `_revive_withdrawn(task, budget, multipv, username)` (background False, route '') then set `arm`, save → task. If PENDING, not USER, and `requested_by in ('', username)`: promote — `source=USER`, `requested_by=username`, `budget_nodes=max(.,budget)`, `requested_at=now`, set `arm` only if empty → task. Otherwise (someone's request, or a running search, or a delivered row) → None (`queued` drop reason). No LIFO, no lane, no backer.
- **Inputs / outputs.** Creates/updates AnalysisTask with `arm` ∈ {relines, shallow, sweep16, deep25, bestline}.
- **Code.** `atomicdb/backlog.py:_mint:642`.
- **Tests.** `test_backlog.py::RecheckAtAdoption::test_a_server_search_waiting_on_the_slot_becomes_the_request`, `::test_a_withdrawn_row_on_the_slot_comes_back_as_the_request`, `test_backlog.py::DeepWins::test_a_server_search_waiting_on_the_slot_becomes_the_request`, `test_backlog.py::Adopting::test_a_batch_becomes_the_adopters_requests`. **Gaps:** a PENDING USER row of another person on the slot (left alone) unpinned.
- **Determinism hazards.** Unique-slot race with non-locking producers (no IntegrityError handling).
- **History.** PR #251.
- **Ordering.** Inside Q-6.42.

### Q-6.45 Backlog subscriptions (periodic top-up)
- **Statement.** `subscribe` upserts `BacklogSubscription(username, backlog)` with `batch_size`, `active=True`, `last_status=''` (sizes validated as Q-6.42); `unsubscribe` sets `active=False, last_status='stopped'` on active rows. `top_up()` (run every ~5 min by `manage.py warm_public_snapshot`): at most 40 active subscriptions (`TOPUP_MAX_SUBSCRIPTIONS`) ordered by `checked_at` ascending NULLS FIRST then id; enabled = `User.is_active` (not Profile.enabled); no new subscription started after 30 s (`TOPUP_SECONDS`, monotonic) — the rest counted `deferred`. Per subscription (`_top_up_one`): bad config → ('bad-request', 0, off); account not active → ('account-off', off); backlog has no OPEN item → ('empty', off); `waiting = backlog_waiting` (places / weight) `>= batch_size // 1` → ('enough', on); else up to 2 batches (`TOPUP_BATCHES_PER_PASS`) while `waiting + units < 2 * batch_size`: `adopt(...)`; 'empty' → off if nothing adopted and no OPEN item on a fresh check, else stop; no adoption → status = the refusal (only if nothing adopted earlier), stop; else accumulate. Exceptions → 'error' (stays on). Record with a conditional UPDATE (`active=True` only, so an untick during the pass wins): `checked_at=now`, `last_status`, `active`, `adopted_total += adopted`.
- **Inputs / outputs.** `BacklogSubscription.*`; tasks via Q-6.42.
- **Code.** `atomicdb/backlog.py:SUBSCRIBE_DEFAULT_SIZE:848`, `:TOPUP_*:854-860`, `:subscribe:895`, `:unsubscribe:907`, `:_top_up_one:926`, `:top_up:962`.
- **Tests.** `test_backlog.py::TopUp::*` (15), `test_backlog.py::Subscribing::*` (6), `test_backlog.py::ClearWinsTiers::test_the_top_up_fills_two_batches_of_places`, `test_backlog.py::DeepWinsSubscriptions::*`. **Gaps:** none major.
- **Determinism hazards.** Wall clock and `time.monotonic()` budget (`backlog.py:972`, `:994`): how many subscriptions a pass serves depends on speed. Per-pass memo of `has_open`.
- **History.** 15-Nov (PR #256, Torom).
- **Ordering.** Periodic; independent of request doors.

---

## Q-7 Frontier, subtree and breadth swap

### Q-7.1 Frontier ranking
- **Statement.** `_frontier_rank(child, stm_white)` = `best_known_eval(child)` (BKD-10) from the mover's side (`known` if White to move else `-known`), or `-9_999.5` when nothing is known (an unanalysed child outranks a known losing one only below -9999.5). `_rank_frontier(parent, moves)` with `moves` = `(move_uci, child)` in movegen (edge-id) order: drop children with `status != 'UNKNOWN'`; dedupe children by key (first move wins for `by_move`; first occurrence for `live`). `ranked` = children in the order of `parent.last_analysis` lines (`line['move']` mapped through `by_move`; non-dict lines and moves not among live edges are ignored; each child once). `rest` = remaining live children, STABLE-sorted by `-_frontier_rank` (ties keep movegen order). `informed = bool(ranked) or any(rest child has eval_cp is not None)`. Return `(ranked + rest, informed)`.
- **Inputs / outputs.** Reads `Position.fen, last_analysis`; child `key, status, eval_cp, backed_eval` (+ best_known_eval inputs).
- **Code.** `atomicdb/ingest.py:_frontier_rank:7886`, `:_rank_frontier:7899`.
- **Tests.** `test_frontier.py::OrNodeExpansionTests::test_multipv_order_picks_the_top_k`, `::test_child_evals_order_the_expansion_without_multipv`, `::test_no_ordering_at_all_falls_back_to_the_first_legal_moves`, `::test_multipv_entries_the_movegen_rejects_are_ignored`, `::test_solved_children_never_take_a_slot`; `test_selector_leak.py::RegretUsesBestKnownEvalTests::test_the_frontier_ranks_a_backed_child_as_informed`. **Gaps:** `informed` is True for a backed-only child only through `_frontier_rank`, but the `informed` flag itself reads `eval_cp` only (a child with only `backed_eval` does not make the level "informed") — unpinned subtlety.
- **Determinism hazards.** Relies on edge `id` order for movegen order (`ingest.py:7959-7961`).
- **History.** 27-Jul (PR #31); backed-child informed (selector leak fix).
- **Ordering.** Used by Q-7.2 and Q-7.8.

### Q-7.2 Frontier width and slice
- **Statement.** `_frontier_width(attacker_to_move, informed)`: OR node → 3 if informed else 8; AND node → 64. `_frontier_children(parent, limit)`: edges ordered by id with children; rank (Q-7.1); width = `_frontier_width(parent stm is white, informed)` (White is always the attacker here), `min(width, 64)`, and `min(width, max(0, limit))` when a limit is given; return `ranked[:width]`.
- **Inputs / outputs.** Read-only.
- **Code.** `atomicdb/ingest.py:_frontier_width:7944`, `:_frontier_children:7951`.
- **Tests.** `test_frontier.py::AndNodeExpansionTests::test_every_legal_reply_is_queued`, `::test_cap_bounds_a_wide_and_node`, `::test_no_click_can_exceed_the_hard_task_cap`, `::test_declared_caps_match_the_approved_design`. **Gaps:** none.
- **Determinism hazards.** none beyond edge order.
- **History.** PR #31.
- **Ordering.** Inside Q-7.4/7.5.

### Q-7.3 Spent-ladder probe and expansion on read
- **Statement.** `_ladder_spent_keys(children)` = keys whose MAX COMPLETED `budget_nodes` ≥ 2B (bought, incl. absorbed). `_ensure_expanded(pos)`: if `pos.expanded` or closed, return pos unchanged; else lock the Position (`select_for_update`) and `expand(pos)` (EXP-1), return the locked row.
- **Inputs / outputs.** May create Edge/Position rows (EXP-1).
- **Code.** `atomicdb/ingest.py:_ladder_spent_keys:7971`, `:_ensure_expanded:7992`.
- **Tests.** `test_frontier.py::PartialExhaustionTests::test_child_with_a_spent_ladder_is_counted_not_recursed`, `::test_a_child_already_queued_is_reported_as_covered`. **Gaps:** `_ensure_expanded` on an unexpanded spent node unpinned.
- **Determinism hazards.** none.
- **History.** PR #31.
- **Ordering.** Inside Q-7.5.

### Q-7.4 Frontier expansion one ply below
- **Statement.** `_expand_frontier(parent, limit, requested_by, route, background)`: lock parent, `expand(parent)` (no-op if edges exist); if route, map child key → move_uci over all edges (last edge wins on duplicate child — dict build order by unordered query); for each child of `_frontier_children(parent, limit)`: `child_route = route + ',' + move` if route and known else ''; `outcome = _request_rung(child, requested_by, child_route, background=background)` (no budget_floor, no flags: children climb their own ladder); count `children_exhausted` for `_LADDER_EXHAUSTED`, `children_solved` for `already-solved`, else `children_queued` (queued AND already-queued). `children_considered` counts all.
- **Inputs / outputs.** Tasks via Q-6.4..6.12.
- **Code.** `atomicdb/ingest.py:_expand_frontier:8007`.
- **Tests.** `test_frontier.py::OrNodeExpansionTests::test_children_enter_the_ladder_at_their_own_floor`; `test_frontier.py::PartialExhaustionTests::*`; `test_notifications.py::NotificationCreationTests::test_an_expansion_notifies_through_each_child_it_bought`. **Gaps:** route move for a child reached by two moves (unordered dict) unpinned.
- **Determinism hazards.** Unordered `Edge.objects.filter(parent=parent)` dict for routes (`:8025`): with two moves to one child, which move labels the route depends on row order.
- **History.** PR #31; route per child (Aug).
- **Ordering.** Inside Q-7.5.

### Q-7.5 Frontier descent (breadth swap walk)
- **Statement.** `_descend_frontier(pos, requested_by, route, background)`: totals zeroed; `visited={pos.key}`; loop: `remaining = 64 - children_queued`; ≤0 → `saturated` stop `budget-spent`. `node = _ensure_expanded(node)`; `children = _frontier_children(node, limit=remaining)`; `spent = _ladder_spent_keys(children)`; if `len(spent) < len(children)` (some child still buyable; a level with no children falls to the else branch) → add `_expand_frontier(node, remaining, ...)` counters, return `expanded` stop `queued`. Else (all spent, or no children) add `len(children)` to considered and exhausted; `following` = first child not visited (best-first); none → `saturated` stop `no-candidate`; if `plies >= 32` → `saturated` stop `depth-guard`; extend `walked_route` with the edge move (unordered `.first()`; missing → route dropped to ''); mark visited; descend one ply. Result `RequestOutcome(status, descent_plies, descent_key, descent_stop, children_*)`. Writes nothing on the way down except expansions.
- **Inputs / outputs.** Tasks via Q-7.4; edges via Q-7.3.
- **Code.** `atomicdb/ingest.py:_descent_outcome:8044`, `:_descend_frontier:8050`.
- **Tests.** `test_frontier.py::DescentTests::*` (11), `test_frontier.py::DescentRequestApiTests::*`. **Gaps:** none major.
- **Determinism hazards.** `Edge...values_list('move_uci').first()` without order_by (`:8113`) when two moves reach the same child.
- **History.** 27-Jul (PR #31). Since 29-Oct only the breadth swap walks this way.
- **Ordering.** Called by Q-6.22 when Q-7.6 is true.

### Q-7.6 Breadth-swap eligibility
- **Statement.** `_breadth_swap_eligible(pos)` = setting `ATOMICDB_BREADTH_SWAP` true (env `1|true|yes`, default off) AND `status == 'UNKNOWN'` AND `abs(eval_cp or 0) < 9000` AND `_completed_max_budget(pos) >= 512M` (bought, incl. absorbed). Only consulted when no budget was chosen and none of only_if_missing/one_search/only_unexplored is set.
- **Inputs / outputs.** Read-only.
- **Code.** `atomicdb/ingest.py:_breadth_swap_eligible:8544`; `OpenSite/settings.py:185`.
- **Tests.** `test_frontier.py::BreadthSwapTests::test_the_first_deep_look_is_never_swapped`, `::test_the_seeding_pass_is_never_swapped`, `::test_a_mate_band_node_still_buys_depth`, `::test_a_deep_revisit_request_expands_instead_of_deepening`. **Gaps:** production value of the flag is not in the repo.
- **Determinism hazards.** Environment switch.
- **History.** 29-Jul (PR #31, off); 512M threshold 30-Jul (Wolfram).
- **Ordering.** First check inside Q-6.22.

### Q-7.7 Subtree attacker
- **Statement.** `_subtree_attacker_white(pos)` = True iff `value is None or value >= 0`, with `value = backed_eval if not None else eval_cp` (White POV). Fixed for the whole walk.
- **Inputs / outputs.** Read-only.
- **Code.** `atomicdb/ingest.py:_subtree_attacker_white:8161`.
- **Tests.** `test_subtree_request.py::WalkShapeTests::test_white_ahead_and_to_move_takes_its_best_three`, `::test_black_ahead_makes_white_the_defender`, `::test_the_backed_value_decides_before_the_own_eval`, `::test_a_level_value_keeps_white_as_the_attacker`, `::test_the_attacker_holds_for_the_whole_walk`. **Gaps:** none.
- **Determinism hazards.** none.
- **History.** 29-Oct (PR #99).
- **Ordering.** Start of Q-7.10.

### Q-7.8 Subtree walk: parents, replies, candidates
- **Statement.** A level is a list of `(key, route)`. Chunks of 64 parents: `_subtree_parents` reads `key, fen, last_analysis, expanded, status` for the chunk (one query), keeps walk order, drops missing and non-UNKNOWN keys, and expands (`select_for_update` + `expand`) unexpanded ones. `_subtree_replies` reads all edges of the chunk ordered by edge id: `(parent, move, child, child.status, child.eval_cp, child.backed_eval)`; returns the rows-read count. `_subtree_candidates`: per parent in walk order: rank (Q-7.1); `attacker_to_move = (parent stm is white) == attacker_white`; for each child in `ranked[:_frontier_width(attacker_to_move, informed)]` (no 64 click cap here beyond AND=64) not in `visited`: add to visited, `child_route = parent.route + ',' + first_move(child)` if parent.route else ''; append. A transposed child keeps the first place it got.
- **Inputs / outputs.** May expand positions.
- **Code.** `atomicdb/ingest.py:_SubtreeChild:8131`, `:_SubtreeParent:8148`, `:_subtree_parents:8174`, `:_subtree_replies:8202`, `:_subtree_candidates:8223`.
- **Tests.** `test_subtree_request.py::WalkShapeTests::test_solved_children_never_take_a_slot`, `::test_the_route_follows_the_path`, `::test_without_a_route_there_is_none_to_extend`; `test_subtree_request.py::TranspositionTests::*`; `test_subtree_request.py::WalkBoundTests::test_the_cost_does_not_grow_with_the_width_of_a_level`. **Gaps:** `_SubtreeChild` lacks `backed_nodes`/`nodes_invested`, so `best_known_eval` on it sees only eval/backed (fine) — unpinned.
- **Determinism hazards.** Edge id order; visited set is insertion-ordered by walk (deterministic).
- **History.** PR #99.
- **Ordering.** Inside Q-7.10.

### Q-7.9 Subtree coverage classification
- **Statement.** `_subtree_coverage(keys, requested_by)` → `(searched, live)`. `searched` = keys with a COMPLETED task, `nodes_searched > 0`, `budget_nodes >= 128M`. For the rest, live tasks (PENDING/LEASED) that are USER (any budget) or have `budget_nodes >= 128M` (any source) classify each key: non-USER → `server`; USER with `requested_by == author` or `requested_by` (non-empty) in `also_requested_by` → `mine` (two anonymous clicks: '' == '' → mine); else `others`. Per key the highest of precedence `server < others < mine` wins. Server work below 128M covers nothing.
- **Inputs / outputs.** Read-only.
- **Code.** `atomicdb/ingest.py:_subtree_coverage:8251`.
- **Tests.** `test_subtree_request.py::WhoseSearchTests::*` (6); `test_subtree_request.py::WalkShapeTests::test_a_searched_position_is_walked_through_not_bought_again`, `::test_a_rung_bought_but_never_searched_is_not_a_search`, `::test_a_position_in_flight_is_not_walked_through`. **Gaps:** none.
- **Determinism hazards.** none (max over precedence).
- **History.** PR #99.
- **Ordering.** Inside Q-7.10.

### Q-7.10 Subtree request: purchase loop, counting, stop, event
- **Statement.** `request_subtree(pos, requested_by, route, background)` in one `atomic()`: closed root → `already-solved`. Counters `searches_queued, searches_waiting, positions_searched, positions_others`; `counted = queued + waiting`. Breadth-first: while level non-empty and stop == 'exhausted': `counted >= 78` → stop `cap`; `plies >= 32` → `depth-guard`; `plies += 1`; per chunk of 64 parents: if `scanned >= 4096` → `scan-limit` (checked BEFORE reading a chunk, so the chunk that crosses the line is read); read replies, add to scanned; candidates; coverage; per candidate in order: `counted >= 78` → `cap`, stop; searched → `positions_searched += 1`, walk through (append to next level); `mine` → `searches_waiting += 1`, and if not background UPDATE live USER rows of that position with `background=True` → False (any author); `others` → `positions_others += 1` (not walked); else `_request_rung(Position(key), requested_by, child_route, only_if_missing=True, background)` (target 128M; a short-mate clamp may make it cheaper): `already-analyzed` → counted as searched and walked through; `queued`/`already-queued` → `searches_queued += 1`, `deepest = plies`; `already-solved` → nothing. `server` rows are bought through `_request_rung` (promotion). If `searches_queued > 0`: `DBEvent('SUBTREE_REQUEST', {key, queued, waiting, plies: deepest, stop})`. Return `RequestOutcome('expanded' if counted else 'saturated', subtree=True, subtree_plies=deepest, subtree_stop=stop, positions_scanned=scanned, **counts)`. Reached by `budget_floor == 10B` (any chooser, no ladder check) or by an exhausted ladder without `one_search`.
- **Inputs / outputs.** Tasks (USER, ≤128M); DBEvent `SUBTREE_REQUEST`; may clear `background`.
- **Code.** `atomicdb/ingest.py:SUBTREE_EVENT:8128`, `:request_subtree:8305`.
- **Tests.** `test_subtree_request.py::PlainButtonTests::*`, `::SliderTests::*`, `::ApiTests::*`, `::WalkBoundTests::test_one_click_buys_78_at_most`, `::test_the_best_line_gets_its_searches_when_the_cap_cuts`, `::test_the_ply_guard_stops_a_long_searched_line`, `::test_the_rows_one_click_reads_are_bounded`, `::NumbersTests::*`; `test_multi_request.py::SpentLadderTests::test_its_own_button_still_buys_the_searches_below`. **Gaps:** the background lift touching other authors' rows unpinned; LIFO reversal (Q-6.18) unpinned.
- **Determinism hazards.** One long transaction holding many Position locks (deadlock potential with landings); edge-id order.
- **History.** 29-Oct (PR #99) replacing the 10B rung; background lift 25-Nov (PR #331).
- **Ordering.** Called from Q-6.22.

### Q-7.11 Uncertainty expansion (AUTO arm after a landing)
- **Statement.** `_uncertainty_expand(keys)` (keys = landed position + its parents), only if `ATOMICDB_BREADTH_SWAP`: for each UNKNOWN row among keys (UNORDERED query) with both `eval_cp` and `backed_eval` non-null, both `|.| < 9000`, and `|eval_cp - backed_eval| > 150` (`UNCERTAINTY_GAP`): `queued = enqueue_unexplored_children(row, cap=2, source=AUTO, children=unjudged_children(row))` (Q-6.35: AUTO, `server_picked_budget`, may DEMOTE a pending USER request on such a child to AUTO); if queued > 0: `DBEvent('UNCERTAINTY_EXPAND', {key, own, backed, queued})`; stop after the row that makes the total ≥ 4 (`UNCERTAINTY_EXPAND_CAP`; total can reach 5).
- **Inputs / outputs.** AUTO tasks (arm ''); DBEvent.
- **Code.** `atomicdb/ingest.py:UNCERTAINTY_GAP:8440`, `:UNCERTAINTY_EXPAND_CAP:8441`, `:_uncertainty_expand:8508`; caller `:propagate_landings:1442`.
- **Tests.** `test_frontier.py::BreadthSwapTests::test_uncertainty_gap_buys_virgin_children`, `::test_uncertainty_stays_quiet_without_flag_or_gap`; `test_idle_pool_budget.py::BreadthSwapArmTests::test_the_uncertainty_expansion_is_floored_past_a_clamp`, `::test_the_uncertainty_expansion_still_verifies_a_short_mate`. **Gaps:** cap overshoot to 5 and row order unpinned.
- **Determinism hazards.** Unordered `Position.objects.filter(key__in=...)` with a cumulative cap (`:8520`, `:8539`); environment flag.
- **History.** 29-Jul (PR #31).
- **Ordering.** In `propagate_landings` per landing, after the status cascade and backed climb, before `_witness_refuted_revisit`, `_queue_value_cascade`, idle repair and autosolve (`ingest.py:1442-1458`).

### Q-7.12 Witness-refuted revisit (AUTO arm after a landing)
- **Statement.** `_witness_refuted_revisit(pos, parent_keys)`, only if `ATOMICDB_BREADTH_SWAP`: re-read `pos` (`status, eval_cp, backed_eval`); closed/missing → 0; `child_value = backed_eval if not None else eval_cp`; None or `|.| >= 9000` → 0. For UNKNOWN parents (UNORDERED query) until 2 created (`WITNESS_REFUTED_CAP`): skip if `parent.eval_cp` None or in mate band; `witness = last_analysis[0].pv[0]` if the first line is a dict with a pv, else `parent.best_move`; skip if none or if no Edge `(parent, witness, child=pos)`; refuted iff (White to move and `child_value < parent.eval_cp - 150`) or (Black to move and `child_value > parent.eval_cp + 150`); `budget = server_picked_budget(max(32M, budget_for(parent)), pos=parent)`; `get_or_create(position=parent, generation=parent.visits, defaults={budget_nodes, multipv: 2 (DEPTH_MULTIPV), source: AUTO})` (arm ''); if created: +1 and `DBEvent('WITNESS_REFUTED', {parent, child, move, claimed: parent.eval_cp, child_value})`. An existing slot row (any state) blocks it silently.
- **Inputs / outputs.** AUTO task on the parent; DBEvent.
- **Code.** `atomicdb/ingest.py:WITNESS_REFUTED_CAP:8450`, `:_witness_refuted_revisit:8453`; caller `:propagate_landings:1443`.
- **Tests.** `test_frontier.py::BreadthSwapTests::test_a_refuted_own_line_queues_a_deep_revisit_of_the_parent`, `::test_a_witness_within_the_gap_stays_quiet`; `test_idle_pool_budget.py::BreadthSwapArmTests::test_the_witness_refuted_revisit_is_floored`. **Gaps:** cap/row order; Edge `.first()` existence only.
- **Determinism hazards.** Unordered parent query with a cap of 2 (`:8471`); environment flag; unique-slot race (IntegrityError not caught).
- **History.** 29-Jul (PR #31, three community sightings of "broken backprop").
- **Ordering.** Right after Q-7.11 in `propagate_landings`.

---

#### Part 4 cross-references
- PRI-5 / EXP-4 (part 3): `budget_for`, `server_picked_budget`, `multipv_for`, `_short_mate_clamp`, `_rung_matching`, `REQUEST_BUDGET_LADDER`/`BUDGET_LADDER` definitions.
- Q-1 (part 1): AnalysisTask fields/constraints; Q-2.x (part 1): lease order consuming `lane_ahead`, `backer_rank`, `served_rank`, `named_first`, own-first affinity.
- Q-3 (part 5): `absorb_tasks`/`_absorb_shadowed` (create absorbed rows revived here), notifications on landing, `_verify_after` (consumes `then_verify`).
- Q-8 (part 6): arm accounting; `enqueue_unexplored_children` is also used by part-6 arms (dn repair).
- MPV-6: `enqueue_pv_verification`, `_buy_verification` (applies Q-6.18).
- POS-2/POS-3: `get_or_create_position_linked`, `settle_linked_children`, `_materialise_child`.
- BKD-10: `best_known_eval`. EXP-1: `expand`. EXP-6: `propagate_landings` (call site of Q-7.11/7.12).
- REV-3: `_queue_disputed_reanalysis` (not catalogued here).

---

## Part 6: Background arms, sweep, idle repair, autosolve, campaigns

Scope: the server-side producers that mint `AnalysisTask` / `SolveTask` rows on their own (no person asked), the hourly spend ceiling they share, the +16 sweep, the idle-fleet repair arm, the autosolve arm, and exploration-campaign ownership. Line numbers are against `origin/spell-runner` `f59413e`. Constants are quoted with their values; "M" = 10^6 nodes, "B" = 10^9 nodes.

Common vocabulary used below:
- **Slot of a visit.** `AnalysisTask` has the unique constraint `uniq_task_per_generation (position, generation)` (models.py, `AnalysisTask.Meta`). Every producer in this part mints on `generation = position.visits` (the position's next visit). A row that already holds that slot, in any state, blocks creation (`get_or_create` returns it).
- **Absorbed row.** `ingest.absorb_tasks` (ingest.py:5770) turns PENDING rows into `COMPLETED` with `nodes_searched=0`, `completed=now`, machine left `''`. `ingest.is_absorbed(task)` (ingest.py:5789) = `state==COMPLETED and not nodes_searched and not machine`.
- **Open/alive.** In idle_repair and autosolve, `_OPEN = {'status': 'UNKNOWN', 'priority__gt': ingest.DEAD / 2}` with `DEAD = -1e9` (ingest.py:5835), i.e. open and not a tombstone (priority > -5e8).
- **Server-picked floor.** `ingest.server_picked_budget(budget, clamp=None, pos=None)` (ingest.py:446, owned by PRI-5/EXP-4): if `clamp` given and `budget <= clamp[0]` return `budget`; else if `caps_interior(pos, budget)` (pos has truthy `backed_move`, `budget > AUTO_INTERIOR_MAX_NODES`=128M and `ATOMICDB_AUTO_INTERIOR_DEEP_RUNGS` not set) then `budget = 128M`; return `max(budget, SERVER_PICKED_MIN_NODES)` with `SERVER_PICKED_MIN_NODES = REQUEST_BUDGET_LADDER[0] = 128M`.
- Ladders (ingest.py:35,44): `BUDGET_LADDER = [8M, 32M, 128M, 512M, 2B]`, `REQUEST_BUDGET_LADDER = [128M, 512M, 2B]`. `DEPTH_MULTIPV = 2` (ingest.py:259). `MATE_BAND = 9000` (ingest.py:94), `TB_CLAMP_CEILING = 9500` (ingest.py:134).

---

### Q-8.1 Arm tag versus service band
- **Statement.** Every task a background producer creates carries two independent labels. `source` decides the service band (`USER` > `FILL` > `AUTO`, served by descending alphabetical `-source`; part 1 owns the ordering) and `arm` names the producer, used only for that producer's own stock cap and hourly ceiling. Arm values in this part: AnalysisTask arms `'coverage'` (FILL), `'dn'` (FILL), `'quality'` (FILL), `'repair'` (AUTO), `'sweep16'` (FILL from the drip; USER when adopted from the community backlog, owned by Q-6); SolveTask arms `'debt'`, `'fragile'`, `'autosolve95'`. Arm values owned elsewhere but paying the same ceiling: `'cascade'` and `'descend'` (PRI-4), `'campaign'` (PRI-4, no ceiling). A producer counts its stock as `state='PENDING' AND arm=<its arm>` only; another arm's rows never consume its room. When a producer "promotes" an existing PENDING AUTO row of the same slot to FILL it also rewrites `arm` to its own arm, and the promotion counts against its room exactly like a creation. `AnalysisTask.arm` is `CharField(max_length=16)`, `SolveTask.arm` `max_length=32`; both default `''`.
- **Inputs / outputs.** Reads/writes `AnalysisTask.source`, `AnalysisTask.arm`, `SolveTask.arm`.
- **Code.** `atomicdb/models.py:AnalysisTask` (Source choices and `arm` field); `atomicdb/ingest.py` arm constants: `DEBT_ARM` 8921, `AUTOSOLVE95_ARM` 9003, `COVERAGE_ARM/DN_ARM/QUALITY_ARM` 9041-9043, `IDLE_REPAIR_ARM` 9104, `SWEEP16_ARM` 9109; `atomicdb/migrations/0030_analysistask_arm.py`.
- **Tests.** `test_adversarial_explorer.py::DnRepairTests::test_it_shares_the_fill_cap_with_coverage_completion` (despite its name, asserts a coverage row does NOT consume dn room); `test_quality_tolerance.py::QualityConvergenceTests::test_another_arms_backlog_does_not_spend_this_arms_quota`; `test_idle_repair.py::CapAndRateTests::test_what_other_arms_queue_is_not_counted`; `test_autosolve.py::CapAndRateTests::test_what_other_arms_queue_is_not_counted`; `test_engine_debt.py::EnqueueDebtTests::test_the_cap_is_counted_over_THIS_ARM_and_no_other`; `test_adversarial_explorer.py::DnRepairTests::test_it_never_disguises_itself_as_a_visitor`. **Gaps:** no test pins that a promotion rewrites `arm` (only that it is not duplicated: `test_coverage_completion.py::CoverageGapTests::test_an_existing_auto_task_is_promoted_not_duplicated`).
- **Determinism hazards.** none in the rule itself (see each producer).
- **History.** Per-arm caps replaced a shared FILL cap in e5ab327 ("cada brazo de la cola cuenta SU cupo", PR #31): the second producer used to find the shared cap spent; measured on 20-Aug that the debt cap counted fragile rows and both arms stalled for weeks (`enqueue_engine_debt` docstring, fixed in the PR #48 production sync).
- **Ordering.** Prerequisite of every Q-8/Q-10/SOL-4 producer.

### Q-8.2 Hourly rate setting per arm
- **Statement.** `arm_rate(arm)` returns the number of tasks the arm may CREATE per rolling hour. Lookup: `ARM_RATE_SETTING[arm] = (setting_name, default)`; value = `getattr(settings, name, None)`, and if that is `None`, `os.environ.get(name, '')`; the value is `str(raw).strip()`; if it is all decimal digits (`str.isdigit()`) it is `int(raw)`, otherwise the default (a typo, a negative sign, a float or the empty string all yield the default; `'0'` yields 0 = arm off). Defaults: `sweep16` `ATOMICDB_ARM_RATE_SWEEP16`=1200; `cascade` `ATOMICDB_ARM_RATE_CASCADE`=240; `quality` `ATOMICDB_ARM_RATE_QUALITY`=120; `descend` `ATOMICDB_ARM_RATE_DESCEND`=120; `repair` `ATOMICDB_ARM_RATE_REPAIR`=60; `autosolve95` `ATOMICDB_ARM_RATE_AUTOSOLVE`=60. An arm not in the table raises `KeyError` (coverage, dn, debt, fragile, campaign have NO hourly ceiling; see Q-8.5). `OpenSite/settings.py` mirrors CASCADE/QUALITY/DESCEND as `os.environ.get(name, '')` (so an unset env gives `''` -> default); the others are read from the environment directly.
- **Inputs / outputs.** Reads settings / process environment. Pure function otherwise.
- **Code.** `atomicdb/ingest.py:ARM_RATE_SETTING:9110`, `arm_rate:9120`.
- **Tests.** `test_arm_rate_budget.py::ArmRateKnobTests::test_the_defaults_are_the_ones_the_owner_agreed`, `::test_the_environment_lowers_the_ceiling`, `::test_a_mistyped_value_keeps_the_default`; `test_idle_repair.py::CapAndRateTests::test_the_default_ceiling`, `::test_the_lever_works_from_the_environment`, `::test_the_other_arms_keep_their_settings`; `test_autosolve.py::CapAndRateTests::test_the_default_ceiling`, `::test_the_lever_works_from_the_environment`. **Gaps:** no test for the sweep16 default (1200) or for a negative/float value.
- **Determinism hazards.** Settings/env switch read on every call (process environment of the selector service vs. the ingest/web processes may differ: the knob must be set in every process that mints for that arm).
- **History.** c76fcbe 2026-08-17 "los brazos del walker gastan por hora, no por cola" (PR #44); repair added fc9f14b (PR #100), autosolve 94c0262 (PR #101), sweep16 1495ea4 (PR #224).
- **Ordering.** Read by Q-8.3 and by each producer's early "rate <= 0 -> do nothing" exit.

### Q-8.3 Hourly budget room (rolling window)
- **Statement.** `_arm_budget_room(arm, tasks=None)` = how many MORE rows the arm may create now. `rate = arm_rate(arm)`; if `rate <= 0` return 0 without a query and without a receipt. Otherwise `made = COUNT(rows of the table with arm=<arm> AND created >= now - ARM_RATE_WINDOW)` where `ARM_RATE_WINDOW = 1 hour` and the table is `AnalysisTask` unless `tasks` (a queryset/manager, e.g. `SolveTask.objects`) is given. All states count (a COMPLETED, CANCELLED or absorbed row created inside the hour still counts). If `made >= rate`: emit the receipt (Q-8.4) and return 0. Else return `rate - made`. Callers clip their batch to this value (so the ceiling is exact, not "ceiling plus one batch"). The window is rolling, not aligned to clock hours. Sweep `_revive` resets `created=now` so a revived row counts as created now (Q-10.7). Two processes may read the same count and both buy (accepted overshoot bounded by concurrency).
- **Inputs / outputs.** Reads `AnalysisTask.arm, created` (index `atomic_task_arm_created (arm, created)`, migration 0048) or `SolveTask.arm, created`. May write a DBEvent (Q-8.4).
- **Code.** `atomicdb/ingest.py:ARM_RATE_WINDOW:9083`, `_arm_budget_room:9143`; `atomicdb/migrations/0048_analysistask_arm_rate_index.py`.
- **Tests.** `test_arm_rate_budget.py::CascadeRateTests::test_an_arm_at_its_hourly_count_buys_nothing`, `::test_the_empty_queue_does_not_reopen_the_ceiling`, `::test_the_window_rolls_and_an_old_hour_is_forgotten`, `::test_the_hourly_room_clips_the_batch_below_the_burst_brake`, `::test_below_the_ceiling_the_burst_brake_is_what_binds`, `::test_a_rate_of_zero_turns_the_arm_off`; `test_idle_repair.py::CapAndRateTests::test_the_hour_rolls`; `test_autosolve.py::CapAndRateTests::test_the_hour_rolls`, `::test_the_ceiling_counts_this_arms_passes_only`; `test_sweep.py::SweepDripTests::test_the_hourly_ceiling_clips_the_batch`. **Gaps:** no test that CANCELLED/absorbed rows created inside the hour still count.
- **Determinism hazards.** Wall clock: `timezone.now()` at 9166 defines the window, and `created` is `auto_now_add` (wall clock at insert). READ COMMITTED race: concurrent producers (ingest writers vs. selector service) read the same COUNT (documented, ingest.py:9074-9076).
- **History.** c76fcbe (PR #44, 15/17-Aug incident: 4,551 cascade purchases in one hour against a stock cap of 16). The `tasks=` parameter added for autosolve (PR #101).
- **Ordering.** Callers check it BEFORE minting; for quality it is checked before the stock cap, for idle_repair/autosolve after it (both take the min).

### Q-8.4 Arm-rate receipt event
- **Statement.** When Q-8.3 returns 0 because the ceiling is reached (not because the rate is 0), `_arm_rate_receipt(arm, rate, made)` writes ONE `DBEvent(kind='ARM_RATE_BUDGET', payload={'arm', 'rate', 'made', 'window_minutes': 60})` unless an event of that kind with `payload.arm == arm` exists with `ts >= now - ARM_RATE_RECEIPT_WINDOW` (10 minutes). Different arms get separate receipts. The kind is hidden from the public feed (`views.FEED_HIDDEN_KINDS`).
- **Inputs / outputs.** Reads/writes `DBEvent(kind, ts, payload)`.
- **Code.** `atomicdb/ingest.py:ARM_RATE_RECEIPT_WINDOW:9088`, `ARM_RATE_EVENT:9089`, `_arm_rate_receipt:9173`.
- **Tests.** `test_arm_rate_budget.py::ArmRateReceiptTests::test_a_skip_leaves_a_receipt_with_its_numbers`, `::test_a_second_skip_inside_ten_minutes_leaves_none`, `::test_the_next_stretch_gets_its_own_receipt`, `::test_another_arm_is_another_receipt`; `test_arm_rate_budget.py::ArmRateFeedTests::test_the_receipt_stays_out_of_the_front_page_feed` (NONE). **Gaps:** none material.
- **Determinism hazards.** Wall clock (`timezone.now()` 9174) and `DBEvent.ts`; check-then-insert race can write two receipts.
- **History.** c76fcbe (PR #44).
- **Ordering.** Inside Q-8.3.

### Q-8.5 Which producers pay the hourly ceiling
- **Statement.** Only producers that buy on the strength of their own results (the "walker" arms) are rate-limited: `sweep16` drip, `cascade` (`_queue_value_cascade`), `quality` (`_queue_quality_convergence` and `_queue_cycle_disambiguation`, one shared bag), `descend` (value descent in `_next_tasks_by_proof`), `repair` (idle repair, landing and backlog), `autosolve95` (on `SolveTask`). NOT rate-limited: visitor requests (`request_analysis` and every request path), `bootstrap_root`, `top_up_analysis_pool`, `_queue_disputed_reanalysis` (REV-3), the uncertainty expansion and witness-refuted revisit (part 4), `enqueue_coverage_completion`, `enqueue_dn_repair`, `enqueue_engine_debt`, `enqueue_fragile_mate_solves`, `enqueue_unexplored_children`. Those unrated producers are bounded only by their stock caps and per-call caps.
- **Inputs / outputs.** n/a (policy).
- **Code.** call sites of `_arm_budget_room`: `atomicdb/ingest.py:4185, 4252, 7008, 7087`; `atomicdb/idle_repair.py:_room:249`; `atomicdb/autosolve.py:_room:232`; `atomicdb/sweep.py:drip:616`.
- **Tests.** `test_arm_rate_budget.py::UnbudgetedDoorsTests::test_a_visitor_request_is_never_rate_limited`, `::test_the_root_seeding_is_never_rate_limited`, `::test_the_top_up_is_never_rate_limited`, `::test_the_disputed_witness_rebuy_is_never_rate_limited`; `test_arm_rate_budget.py::QualityRateTests::test_the_cycle_disambiguation_shares_the_same_bag`. **Gaps:** no test asserts coverage/dn/debt/fragile are unrated.
- **Determinism hazards.** none beyond Q-8.3.
- **History.** c76fcbe (PR #44); repair/autosolve/sweep16 joined later (PR #100, #101, #224).
- **Ordering.** n/a.

### Q-8.6 Engine debt: candidates
- **Statement.** A position is engine debt iff `closure='MATE_PV'`, `status IN ('WHITE_WIN','BLACK_WIN')`, `won_line IS NOT NULL AND won_line <> ''`, and (`proof='ENGINE'` OR `proof IS NULL`). Candidates exclude every position that has a `SolveTask` with `arm='debt'` in state PENDING, LEASED or COMPLETED (a FAILED debt task does NOT exclude: the position is bought again; see open question). Candidates are ordered by `key` ascending; at most `room * 2` rows are read (`candidates[:room*2]`), and the loop stops when `room` tasks are made. The goal is the position's `status` (never the ProofCampaign goal).
- **Inputs / outputs.** Reads `Position.closure, status, won_line, proof, key`; `SolveTask.position_id, arm, state`.
- **Code.** `atomicdb/ingest.py:enqueue_engine_debt:8924` (lines 8963-8974).
- **Tests.** `test_engine_debt.py::EnqueueDebtTests::test_an_uncertified_mate_becomes_one_f0_task`, `::test_the_goal_follows_the_status_not_the_campaign`, `::test_an_unclassified_witness_counts_as_debt_too`, `::test_a_certified_mate_is_not_debt`, `::test_a_witnessless_row_is_left_to_the_safety_net`, `::test_it_never_enqueues_the_same_position_twice`. **Gaps:** FAILED re-buy unpinned; the `room*2` read window unpinned.
- **Determinism hazards.** none (ordered by key). The `taken` set is materialised in Python (one query) and passed as `NOT IN`.
- **History.** 204d17e 2026-07-28 "Put the uncertified debt and the missing replies on the fleet" (PR #31).
- **Ordering.** After Q-8.7's room check.

### Q-8.7 Engine debt: cap, task shape, receipt
- **Statement.** `enqueue_engine_debt(cap=DEBT_QUEUE_CAP=500, limit=None)`: `pending = COUNT(SolveTask state='PENDING' AND arm='debt')`; `room = max(0, cap - pending)`, then `min(room, limit)` if `limit` given; if `room <= 0` return 0. Each new task: `SolveTask(position, goal=position.status, budget_stage='F0', budget_nodes=DEBT_STAGE_NODES=2,000,000, arm='debt')`, other fields default (`state='PENDING'`, `attempts=0`). Inserted with one `bulk_create(..., ignore_conflicts=True)` (SolveTask has no unique constraint, so `ignore_conflicts` never drops a row). If anything was made, one `DBEvent('DEBT_ENQUEUED', {'created', 'pending_before', 'cap'})`. Returns the count made. Called once per selector pass with `cap=--debt-cap` (default 500) unless `--no-debt`. Debt is served LAST by `api_solve_acquire` (`order_by('is_debt', '-budget_nodes', 'id')`, part 2).
- **Inputs / outputs.** Writes `SolveTask` rows; `DBEvent DEBT_ENQUEUED`.
- **Code.** `atomicdb/ingest.py:DEBT_STAGE_NODES:8917`, `DEBT_QUEUE_CAP:8920`, `enqueue_engine_debt:8924`; `atomicdb/management/commands/refresh_selector.py:308-310`.
- **Tests.** `test_engine_debt.py::EnqueueDebtTests::test_the_cap_is_counted_over_THIS_ARM_and_no_other`, `::test_the_arms_own_pending_rows_still_hold_it_back`, `::test_the_cap_tops_up_only_the_room_that_is_left`; `test_engine_debt.py::DebtIsServedLastTests::test_a_request_outranks_the_debt_even_though_it_is_bigger`, `::test_the_debt_is_served_once_nothing_else_is_waiting`; `test_engine_debt.py::RecertifyDefersToTheFleetTests::test_a_witness_the_fleet_holds_is_left_alone` (MPV-2 interaction). **Gaps:** the `limit` argument.
- **Determinism hazards.** COUNT-then-insert race (only the selector service calls it, single process, so practically none).
- **History.** 204d17e (PR #31); per-arm cap fix in PR #48 sync (e39a8dc "Reconcile audited AtomicDB production fixes": 550 PENDING vs cap 500, both solver arms stalled since July).
- **Ordering.** Selector cycle step `debt`, after `solve-leases` (SOL-5) and before `coverage` (Q-8.22).

### Q-8.8 Coverage gap predicate
- **Statement.** `_coverage_gap(fen, children, missing_max)` returns the list of `(move, child_id)` still unjudged, or `None`. `stm_white = fen.split()[1]=='w'`; mover_win/mover_loss are the status strings for the side to move winning/losing. Iterating the children `(move, child_id, status, known)` where `known = child.backed_eval if not None else child.eval_cp` (White's point of view): status == mover_win -> return None immediately; status == mover_loss -> `informed += 1`; status == 'DRAW' -> return None; `known is None` (and status UNKNOWN) -> append to missing; otherwise `mover_value = known if stm_white else -known`; if `mover_value > -COVERAGE_DECISIVE_CP` (-800) return None; else `informed += 1`. After the loop: return None if `missing` empty, `len(missing) > missing_max` (3), or `informed == 0`; else return `missing` in the iteration order. Note: a child with a non-UNKNOWN status other than the three named (none exist) would fall to the value branch.
- **Inputs / outputs.** Reads child `status, eval_cp, backed_eval` via `_coverage_children` (Edge rows `parent_id, move_uci, child_id, child__status, child__eval_cp, child__backed_eval`, grouped by parent).
- **Code.** `atomicdb/ingest.py:COVERAGE_MISSING_MAX:9021`, `COVERAGE_DECISIVE_CP:9022`, `_coverage_children:9183`, `_coverage_gap:9195`.
- **Tests.** `test_coverage_completion.py::CoverageGapTests::test_the_wolfram_shape_asks_for_exactly_the_missing_move`, `::test_a_balanced_node_is_left_alone`, `::test_one_survivable_reply_disqualifies_the_node`, `::test_a_winning_reply_for_the_mover_disqualifies_it`, `::test_too_many_blanks_is_ordinary_exploration_not_this`. **Gaps:** the DRAW branch; `mover_value == -800` boundary (exactly -800 disqualifies because `-800 > -800` is false -> counted as informed; i.e. -800 qualifies).
- **Determinism hazards.** Row order: `_coverage_children` (9184) has no `order_by`, so the order of `missing` (which decides which missing children are bought when room runs out mid-node) follows DB row order. The early returns make the verdict itself order-independent.
- **History.** 204d17e (PR #31); 8fa3a09 same day.
- **Ordering.** Inside Q-8.9.

### Q-8.9 Coverage completion: scan and minting
- **Statement.** `enqueue_coverage_completion(cap=COVERAGE_QUEUE_CAP=100, missing_max=3, scan=COVERAGE_SCAN_ROWS=2000)`: `pending = COUNT(AnalysisTask PENDING, arm='coverage')`; `room = max(0, cap - pending)`; 0 -> return 0. Candidates: up to `scan` rows of `Position(status='UNKNOWN', expanded=True)` ordered by `-updated`, reading `key, fen`. For each candidate (until `made >= room`): compute the gap (Q-8.8); for each `(move, child_id)` in it: re-read the child (`Position.filter(key=child_id).first()`); skip if missing or `status != 'UNKNOWN'`; `get_or_create(position_id=child_id, generation=child.visits, defaults={budget_nodes: server_picked_budget(max(COVERAGE_SEED_NODES=8M, budget_for(child)), pos=child), multipv: DEPTH_MULTIPV=2, source: FILL, arm: 'coverage'})`. Created -> `made += 1`. Existing row with `state='PENDING' and source='AUTO'` -> conditional update `filter(id, state='PENDING', source='AUTO').update(source='FILL', arm='coverage')`; if 1 row updated, `made += 1`. Any other existing row (LEASED, COMPLETED, CANCELLED, FILL, USER) -> nothing, no room spent. The effective budget is at least 128M (server floor) and at most 128M on an interior child; `budget_for(child)` = visit ladder `BUDGET_LADDER[min(visits,4)]` raised to the short-mate clamp or to 128M if `|eval_cp| >= 9000` (PRI-5). Note the budget of a promoted row is NOT changed. If `made`: `DBEvent('COVERAGE_ENQUEUED', {'created': made, 'pending_before', 'cap', 'scanned': len(candidates)})`. Called each selector pass with `cap=--coverage-cap` unless `--no-coverage`.
- **Inputs / outputs.** Reads `Position.status, expanded, updated, key, fen, visits, eval_cp, backed_move`; writes `AnalysisTask` (create or `source/arm` update); `DBEvent COVERAGE_ENQUEUED`.
- **Code.** `atomicdb/ingest.py:COVERAGE_QUEUE_CAP:9044`, `COVERAGE_SCAN_ROWS:9047`, `COVERAGE_SEED_NODES:9049`, `enqueue_coverage_completion:9232`.
- **Tests.** `test_coverage_completion.py::CoverageGapTests::test_the_global_cap_is_respected`, `::test_it_records_what_it_did`, `::test_an_existing_auto_task_is_promoted_not_duplicated`; `test_coverage_completion.py::CoverageClosesTheNodeTests::test_completing_the_coverage_lifts_the_guard_and_closes`; `test_idle_pool_budget.py::CoverageAndDnTests::test_coverage_completion_is_floored`; `test_selector_service.py::SelectorCycleResilienceTests::test_a_broken_arm_does_not_stop_the_steps_behind_it`. **Gaps:** the "promoted row keeps its old budget" behaviour; interior child capping.
- **Determinism hazards.** Wall clock / row order: candidates are ordered by `-updated` (`auto_now` wall-clock timestamp; ties have no tie-break, ingest.py:9250). Row order of `_coverage_children` (Q-8.8). The conditional update guards a READ COMMITTED race with the lease (a row leased between read and update is not promoted and not counted).
- **History.** 204d17e (PR #31); per-arm counting e5ab327 (PR #31); server floor added in the October floor work (see PRI-5).
- **Ordering.** Selector step `coverage`, after `debt`, before the adversarial arms (Q-8.22).

### Q-8.10 Adversarial-arms switch
- **Statement.** `adversarial_arms_enabled()` = `bool(settings.ATOMICDB_ADVERSARIAL)` (default False; settings parse the env var as true iff lower-cased value in `('1','true','yes')`). The selector runs `enqueue_dn_repair` and `enqueue_fragile_mate_solves` only when it is true, unless the command line forces it: `refresh_selector --adversarial` / `--no-adversarial` (option `adversarial`, default `None` = read the setting).
- **Inputs / outputs.** Reads settings / CLI.
- **Code.** `atomicdb/ingest.py:adversarial_arms_enabled:9552`; `atomicdb/management/commands/refresh_selector.py:319-328`.
- **Tests.** `test_adversarial_explorer.py::AdversarialSwitchTests::test_the_arms_are_off_by_default`, `::test_the_setting_turns_them_on`, `::test_the_selector_service_leaves_them_alone_while_it_is_off`, `::test_the_selector_service_runs_both_arms_when_it_is_on`, `::test_a_single_pass_can_veto_them`, `::test_a_single_pass_can_force_them`.
- **Determinism hazards.** Settings switch (production value is in the selector unit's environment; `Documentation/atomicdb-selector.service` ships it commented out).
- **History.** 7a592e9 2026-07-28 "the explorer refutes itself, and we measure whether it helps" (PR #31).
- **Ordering.** Gates Q-8.11..Q-8.14.

### Q-8.11 dn repair: which nodes
- **Statement.** `enqueue_dn_repair(cap=DN_REPAIR_QUEUE_CAP=50, floor=DN_REPAIR_FLOOR=2, replies=DN_REPAIR_REPLIES=3, per_cycle=DN_REPAIR_MAX_PER_CYCLE=24, max_nodes=DN_REPAIR_MAX_NODES=200, campaigns=None)`: `pending = COUNT(PENDING, arm='dn')`; `room = min(max(0, cap - pending), max(0, per_cycle))`; 0 -> return 0. `campaigns = proof.active_campaigns()` (ProofCampaign `active=True`, ordered by `id`) unless given; none -> return 0. For each campaign in order, for each row `(key, fen, pn, dn)` of `proof.frontier_and_rows(campaign)` (the proof frontier restricted to AND nodes, i.e. defender to move, ordered by `(pn, position_id)`, at most `PROOF_FRONTIER_SCAN=20,000` rows; PN-10 owns it): stop when `made >= room` or `scanned >= max_nodes`; skip without counting when `dn > floor`; otherwise `scanned += 1` and load `Position(key=key, status='UNKNOWN', expanded=True)`; skip if not found (unexpanded or closed). Then buy its replies (Q-8.12). `nodes_repaired` counts nodes where at least one reply was queued.
- **Inputs / outputs.** Reads `ProofCampaign.active`, `ProofNode.pn/dn` via frontier, `Position.status, expanded`.
- **Code.** `atomicdb/ingest.py:DN_REPAIR_*:9325-9343`, `enqueue_dn_repair:9364`; `atomicdb/proof.py:active_campaigns:414`, `frontier_and_rows:1867`, `frontier_rows`.
- **Tests.** `test_adversarial_explorer.py::DnRepairTests::test_a_thin_obligation_gets_its_unlooked_replies_bought`, `::test_the_floor_is_what_decides`, `::test_an_attacker_node_is_never_repaired`, `::test_an_unexpanded_node_is_not_a_coverage_claim`, `::test_the_batch_is_bounded_per_cycle`, `::test_the_number_of_nodes_LOOKED_at_is_bounded_too`.
- **Determinism hazards.** Frontier order is `(pn, position_id)` except the `pn <= 0` slice, which `frontier_rows` fetches "sin orden ni corte" (proof.py docstring) and concatenates first: row order dependence for that (normally empty) slice. Values of pn/dn depend on when the proof refresh last ran (PN-4).
- **History.** 7a592e9 (PR #31).
- **Ordering.** Selector step `dn-repair`, after `coverage` (rooms are per arm, so the order only matters for which arm's purchase a shared slot goes to: a child coverage just created is an existing FILL row for dn repair and is not counted).

### Q-8.12 dn repair: what is bought
- **Statement.** For an eligible AND node, the replies are `unjudged_children(parent)[:replies]` (Q-8.17: materialised children with `status='UNKNOWN'`, `eval_cp IS NULL`, `backed_eval IS NULL`, in Edge id order). For each, while `made < room`: `budget = server_picked_budget(max(DN_REPAIR_SEED_NODES=8M, budget_for(child)), pos=child)`; `get_or_create(position=child, generation=child.visits, defaults={budget_nodes: budget, multipv: multipv_for(child.visits, budget), source: FILL, arm: 'dn'})`. Created -> `made += 1`. Existing PENDING AUTO -> conditional `update(source='FILL', arm='dn')`, counted if it updated. Others ignored. If `made`: `DBEvent('DN_REPAIR', {'queued', 'nodes', 'examined': scanned, 'floor', 'pending_before', 'cap', 'per_cycle'})`. `multipv_for` (EXP-4) gives the first-look width (5 lines for an unvisited child at these budgets).
- **Inputs / outputs.** Writes AnalysisTask rows / updates; `DBEvent DN_REPAIR`.
- **Code.** `atomicdb/ingest.py:_dn_repair_replies:9346`, `enqueue_dn_repair:9406-9443`.
- **Tests.** `test_adversarial_explorer.py::DnRepairTests::test_a_reply_the_tree_already_judged_is_not_bought_again`, `::test_the_batch_leaves_a_receipt`, `::test_a_quiet_pass_leaves_no_receipt`, `::test_it_never_disguises_itself_as_a_visitor`; `test_idle_pool_budget.py::CoverageAndDnTests::test_dn_repair_is_floored`.
- **Determinism hazards.** none beyond Q-8.11 (children ordered by Edge id).
- **History.** 7a592e9 (PR #31); switch from `unexplored_children` to `unjudged_children` 1802711 2026-08-10 (PR #44): with the human button re-buying seeded replies, the automatic arm would have looped.
- **Ordering.** Inside Q-8.11.

### Q-8.13 Fragile mate claim predicate
- **Statement.** `fragile_mate_claims(scan=FRAGILE_SCAN_ROWS=2000)`: up to `scan` positions with `status='UNKNOWN'` and `backed_eval >= 9000 OR backed_eval <= -9000`, ordered by `-updated`. Their children are read with `_backed_children_by_parent` (BKD-7) and a row qualifies iff `coverage_is_partial(row, children)` (BKD-4) is true. Goal = `'WHITE_WIN'` if `backed_eval > 0` else `'BLACK_WIN'` (by sign of the backed value, never the side to move). Returns `[(Position, goal)]` in scan order.
- **Inputs / outputs.** Reads `Position.status, backed_eval, updated` and children rows.
- **Code.** `atomicdb/ingest.py:FRAGILE_SCAN_ROWS:9470`, `fragile_mate_claims:9477`.
- **Tests.** `test_adversarial_explorer.py::FragileMateClaimTests::test_the_goal_follows_the_sign_of_the_claim`, `::test_a_positive_claim_asks_for_white`, `::test_a_fully_covered_claim_is_not_fragile`, `::test_a_claim_below_the_mate_band_is_ordinary_exploration`, `::test_a_closed_position_is_debt_not_a_fragile_claim`.
- **Determinism hazards.** `-updated` order with no tie-break (wall-clock `auto_now`), 9487.
- **History.** 7a592e9 (PR #31).
- **Ordering.** Inside Q-8.14.

### Q-8.14 Fragile mate claims: minting
- **Statement.** `enqueue_fragile_mate_solves(cap=FRAGILE_QUEUE_CAP=50, scan=2000)`: `pending = COUNT(SolveTask PENDING, arm='fragile')`; `room = max(0, cap - pending)`; 0 -> return 0. Candidates from Q-8.13; none -> 0. Excluded (`taken`): positions with a `fragile` SolveTask in PENDING/LEASED/COMPLETED, plus positions with ANY SolveTask in PENDING/LEASED. (A FAILED fragile task does not exclude.) For each candidate in order, until `room` made: `SolveTask(position, goal, budget_stage='F0', budget_nodes=FRAGILE_STAGE_NODES=2,000,000, arm='fragile')`. One `bulk_create(ignore_conflicts=True)`; `DBEvent('FRAGILE_ENQUEUED', {'created', 'pending_before', 'cap', 'candidates', 'white', 'black'})`. A DISPROVED result on an UNKNOWN position closes nothing (`_dispute_from_solver`, REV-4/SOL-2). Served after non-debt tasks of bigger budget (autosolve 8M first) and before debt (part 2 ordering).
- **Inputs / outputs.** Writes SolveTask rows, `DBEvent FRAGILE_ENQUEUED`.
- **Code.** `atomicdb/ingest.py:FRAGILE_ARM:9464`, `FRAGILE_QUEUE_CAP:9469`, `FRAGILE_STAGE_NODES:9474`, `enqueue_fragile_mate_solves:9500`.
- **Tests.** `test_adversarial_explorer.py::FragileMateClaimTests::test_it_asks_at_the_cheap_rung`, `::test_the_same_question_is_not_asked_twice`, `::test_a_position_with_a_live_solve_is_left_alone`, `::test_its_cap_is_its_own_and_the_debt_queue_cannot_starve_it`, `::test_the_cap_bounds_the_batch`, `::test_the_batch_leaves_a_receipt_with_both_signs`, `::test_a_disproved_answer_still_closes_nothing`; `test_adversarial_explorer.py::AdversarialSwitchTests::test_the_selector_service_runs_both_arms_when_it_is_on`. **Gaps:** FAILED re-buy.
- **Determinism hazards.** inherits Q-8.13 order.
- **History.** 7a592e9 (PR #31).
- **Ordering.** Selector step `fragile`, right after `dn-repair`, only with the switch on.

### Q-8.15 Quality arm purchases (task shape only)
- **Statement.** Two producers, both run inside the backed ascent (BKD-11 owns WHEN they fire), share arm `'quality'`, `QUALITY_QUEUE_CAP=50` and one hourly bag (`ATOMICDB_ARM_RATE_QUALITY`=120). Both: `hourly = _arm_budget_room('quality')`; 0 -> return 0; `pending = COUNT(PENDING, arm='quality')`; `room = min(hourly, max(0, 50 - pending))`. (a) `_queue_cycle_disambiguation(picks)` with picks `(move_uci, parent_key, floor_quality)`: child via `Edge(parent, move).first()`; skip if missing or child not UNKNOWN; `budget = server_picked_budget(_rung_at_least(max(floor_quality, child.nodes_invested + 1)))` (no `pos`: no interior cap, floor 128M only); `get_or_create(child, generation=child.visits, defaults={budget, multipv_for(child.visits, budget), FILL, 'quality'})`; an existing PENDING row with smaller budget is raised by conditional update `filter(id, state='PENDING', budget_nodes=<old>).update(budget_nodes=budget, source='FILL', arm='quality')` and counted. Event `CYCLE_DISAMBIGUATION {'queued', 'chosen_cycles'}`. (b) `_queue_quality_convergence(discrepancies)` with `(move_uci, parent_key, own_quality)`: `budget = _rung_matching(own_quality)` (smallest BUDGET_LADDER rung `>= nodes/(1+0.01)`); if `caps_interior(child, budget)` -> 128M; skip if `child.nodes_invested >= budget`; then `server_picked_budget(budget, pos=child)`; same create/raise logic; event `QUALITY_CONVERGENCE {'queued', 'discrepancies'}`. In both, the raise update has no `source` filter: it sets `source='FILL', arm='quality'` on ANY PENDING row of the slot with a smaller budget, so a pending USER request on that child is demoted to the FILL band (see open questions).
- **Inputs / outputs.** Writes AnalysisTask; DBEvents above.
- **Code.** `atomicdb/ingest.py:_rung_matching:4143`, `_queue_cycle_disambiguation:4155`, `_queue_quality_convergence:4234`.
- **Tests.** `test_arm_rate_budget.py::QualityRateTests::test_the_convergence_stops_at_its_hourly_count`, `::test_the_cycle_disambiguation_shares_the_same_bag`, `::test_with_room_left_the_purchase_is_the_one_of_always`, `::test_a_rate_of_zero_turns_the_arm_off`; `test_quality_tolerance.py::QualityConvergenceTests::test_the_fill_cap_still_bounds_it`, `::test_a_parent_searched_at_512m_buys_its_child_512m_not_2b`, `::test_another_arms_backlog_does_not_spend_this_arms_quota`; `test_idle_pool_budget.py::QualityArmTests::*`; `test_sweep.py::InteriorRungPolicyTests::*`. **Gaps:** the raise of an existing PENDING USER row to FILL.
- **Determinism hazards.** `Edge.filter(parent, move).first()` without order_by (4197, 4266): (parent, move) is not unique in production (two moves to one child is the reverse case; one move to one child is unique by the edge index `(parent, move_uci)` per autosolve's comment, so practically deterministic).
- **History.** See BKD-11; hourly bag c76fcbe (PR #44); interior cap 1495ea4 (PR #224).
- **Ordering.** Inside the backed ascent (BKD-7), i.e. inside a landing's propagation.

### Q-8.16 "Unexplored" predicate (human button)
- **Statement.** `is_unexplored(child)` = `child.status == 'UNKNOWN' and not (child.nodes_invested or 0) and not (child.backed_nodes or 0)`. A seeded `eval_cp`, a walked/seeded `backed_eval` with zero `backed_nodes`, all count as unexplored. `unexplored_children(pos)` = children of materialised edges `Edge(parent=pos)` ordered by `Edge.id`, filtered by the predicate. The explorer row labels and the button count use the same predicate.
- **Inputs / outputs.** Reads `Position.status, nodes_invested, backed_nodes`.
- **Code.** `atomicdb/ingest.py:is_unexplored:9571`, `unexplored_children:9611`.
- **Tests.** `test_explorer_proof_ui.py::UnexploredButtonTests::test_a_seeded_value_no_longer_exempts_a_reply`, `::test_a_backed_reply_counts_as_known`, `::test_a_walked_number_does_not_exempt_a_reply`, `::test_a_seeded_number_backed_by_itself_does_not_exempt_a_reply`, `::test_the_row_and_the_button_agree_on_a_walked_reply`; `test_showcase_bound.py::WalkedUnexploredTests::test_seeded_eval_counts_as_unexplored`, `::test_own_search_or_backing_is_explored`.
- **Determinism hazards.** none (Edge id order).
- **History.** 8fa3a09 (PR #31); seeds re-bought 1802711 (PR #44); `backed_nodes` discriminant b42b006 2026-10-11 (PR #62, Eclipsia report on walked replies).
- **Ordering.** Default child list of Q-8.18.

### Q-8.17 "Unjudged" predicate (automatic arms)
- **Statement.** `is_unjudged(child)` = `status == 'UNKNOWN' and eval_cp is None and backed_eval is None` (stricter than Q-8.16: a seeded eval is a judgement). `unjudged_children(pos)` = materialised children in `Edge.id` order filtered by it. Used by dn repair (Q-8.12), the uncertainty expansion (part 4, `_uncertainty_expand`), and the value cascade (PRI-4, `_queue_value_cascade`).
- **Inputs / outputs.** Reads `Position.status, eval_cp, backed_eval`.
- **Code.** `atomicdb/ingest.py:is_unjudged:9619`, `unjudged_children:9634`.
- **Tests.** `test_adversarial_explorer.py::DnRepairTests::test_a_reply_the_tree_already_judged_is_not_bought_again`. **Gaps:** no direct unit test of the predicate.
- **Determinism hazards.** none.
- **History.** 1802711 (PR #44).
- **Ordering.** n/a.

### Q-8.18 Bulk child purchase: budget and width
- **Statement.** `enqueue_unexplored_children(pos, cap=UNEXPLORED_CLICK_CAP=64, source=USER, requested_by='', route='', children=None)` iterates `children` (default `unexplored_children(pos)`) and stops when `queued >= cap`. Per child: `clamp = _short_mate_clamp(child)` (EXP-4). If `clamp is None`: `budget = max(REQUEST_BUDGET_LADDER[0]=128M, budget_for(child))`; else `budget = budget_for(child)` (the visit ladder raised to the clamp). If `source != USER` (system caller): `budget = server_picked_budget(budget, clamp, pos=child)`. Width: `multipv_for(child.visits, budget, clamp=clamp)`. `child_route = route + ',' + edge.move_uci` when `route` is non-empty and the child has an edge from `pos` (map built from `Edge(parent=pos)`; with two edges to the same child the last row read wins), else `''`.
- **Inputs / outputs.** Reads child `visits, eval_cp, mate fields` (clamp), Edge `move_uci`.
- **Code.** `atomicdb/ingest.py:UNEXPLORED_CLICK_CAP:9568`, `enqueue_unexplored_children:9642-9691`.
- **Tests.** `test_mate_clamp.py::UnexploredChildrenTests::test_children_without_information_keep_the_request_floor`, `::test_a_child_that_already_claims_a_short_mate_is_verified`; `test_idle_pool_budget.py::VisitorRequestTests::test_the_same_child_keeps_its_visitor_price`; `test_idle_pool_budget.py::BreadthSwapArmTests::test_the_uncertainty_expansion_is_floored_past_a_clamp`, `::test_the_uncertainty_expansion_still_verifies_a_short_mate`; `test_explorer_proof_ui.py::UnexploredButtonTests::test_the_click_is_capped`; `test_queue_fairness.py::BulkRouteInheritanceTests::test_bulk_children_inherit_route_plus_their_move`.
- **Determinism hazards.** `edges_by_child` dict built from an unordered Edge query (9663): with two moves to the same child (atomic captures), which move is appended to the route depends on row order.
- **History.** 8fa3a09, 953b360 (uncertainty trigger + source), 1c64e73 (requester notice), all PR #31; route inheritance later (queue fairness work).
- **Ordering.** Precedes Q-8.19/Q-8.20 for each child.

### Q-8.19 Bulk child purchase: joining a live task
- **Statement.** Before creating, the live task of the child is looked up by POSITION (not generation): `AnalysisTask(position=child, state IN (PENDING, LEASED)).order_by('id').first()`. If found: `respaldado = add_requester(vivo, requested_by)` (Q-6 owns `add_requester`). Then, if `vivo.state == PENDING and vivo.source != source`: set `vivo.source = source`; if `child_route` and `vivo.route` empty, set `vivo.route = child_route`; save `source, route` and, when `source == USER`, also `requested_at = now` (`_stamp_request`); count it (`queued += 1`). Elif `respaldado`: count it. Continue to the next child. Consequence: a system caller (`source=AUTO`, the uncertainty expansion) that meets a PENDING USER task OVERWRITES its source to AUTO (a demotion; `live_request.py:852` documents it). A USER click on a PENDING FILL/AUTO row promotes it to USER.
- **Inputs / outputs.** Reads/writes `AnalysisTask.source, route, requested_at`, plus whatever `add_requester` writes (`also_requested_by`, `backers`, queue placement).
- **Code.** `atomicdb/ingest.py:enqueue_unexplored_children:9692-9720`.
- **Tests.** `test_explorer_proof_ui.py::UnexploredButtonTests::test_a_second_click_queues_nothing_new`. **Gaps:** the USER->AUTO demotion by a system caller is unpinned; promotion AUTO->USER by the button is unpinned here.
- **Determinism hazards.** READ COMMITTED: the live lookup and the save are not conditional (`vivo.save(update_fields=...)` at 9716 overwrites whatever a concurrent lease wrote to `source/route`; state is not re-checked at write time).
- **History.** 1c64e73 (PR #31); join via `add_requester` after math_god's 17-Aug report (it "just ignores my request").
- **Ordering.** Before Q-8.20.

### Q-8.20 Bulk child purchase: create, lost race, withdrawn slot
- **Statement.** With no live task: inside a savepoint on the AtomicDB connection (`transaction.atomic(using=router.db_for_write(AnalysisTask))`) create `AnalysisTask(position=child, generation=child.visits, budget_nodes=budget, multipv=multipv_for(child.visits, budget, clamp), source=source, requested_by=requested_by, route=child_route, requested_at=now if source==USER else None)`; success -> `queued += 1`. On `IntegrityError` (slot `(position, generation)` taken): re-look for a live task (same query as Q-8.19); if found, `add_requester` and count if it returned true; else look for a CANCELLED row on `(child, child.visits)` (`.first()`, unique anyway) and, if found, `_revive_withdrawn(withdrawn, budget, multipv, requested_by, child_route, source=source)` (Q-6: state PENDING, completed NULL, all request fields reset, `requested_at=now`) and count it. A COMPLETED (absorbed or delivered) row on the slot is NOT revived here: the child is skipped silently.
- **Inputs / outputs.** Creates/revives AnalysisTask.
- **Code.** `atomicdb/ingest.py:enqueue_unexplored_children:9721-9770`; `_revive_withdrawn:7614`.
- **Tests.** `test_mate_clamp.py::UnexploredChildrenTests::test_a_lost_race_for_the_same_generation_does_not_break_the_click`; `test_queue_control.py::WithdrawCostsNothingTests::test_the_bulk_button_can_buy_a_withdrawn_reply`; `test_explorer_proof_ui.py::UnexploredButtonTests::test_it_queues_every_reply_nobody_looked_at`. **Gaps:** absorbed-slot skip (the single request path does revive absorbed rows via `_request_rung`, Q-6; this path does not).
- **Determinism hazards.** Unique-constraint race resolution depends on commit timing (READ COMMITTED). `timezone.now()` for `requested_at`.
- **History.** IntegrityError branch (Eclipsia 6-Sep, a 500 on the button) in PR #48 sync (53b5030); withdrawn revival 4290c46 (PR #55).
- **Ordering.** After Q-8.19.

### Q-8.21 Bulk unexplored endpoint (call site)
- **Statement.** `POST /atomicdb/api/request-unexplored/<key>` (`views.api_request_unexplored`, CSRF-protected, not worker protocol): 405 on non-POST; 404 `{'status':'unknown-position'}`; closed position -> `{'status':'already-solved'}`; account queue full (`_account_queue_full`, Q-6) -> 429 response; route validated with `_validated_play_route` (invalid -> `''`). Inside `atomic()`: `select_for_update` the position, `pending = unexplored_children(pos)`; empty -> `{'status':'nothing-to-do','queued':0}`; else `queued = enqueue_unexplored_children(pos, requested_by=<username or ''>, route=route)` (source USER), `RequestLog(ip, position)`, `DBEvent('BULK_REQUEST', {'ip','key','queued','candidates'})`; reply `{'status':'queued','queued','candidates'}`.
- **Inputs / outputs.** As above.
- **Code.** `atomicdb/views.py:api_request_unexplored:2104`.
- **Tests.** `test_explorer_proof_ui.py::UnexploredButtonTests::*` (incl. `test_a_solved_position_has_nothing_to_offer`, `test_a_fully_explored_position_says_so`, `test_get_is_refused`, `test_an_unknown_position_is_a_404`); `test_lanes.py::AccountQueueCapTests::test_the_bulk_button_respects_the_same_allowance`; `tests.py::ExplorerRequestCsrfTests::*`.
- **Determinism hazards.** Row lock on the parent (`select_for_update`) serialises two clicks on the same parent.
- **History.** 8fa3a09 (PR #31); queue cap gate added with the account allowance (Q-6).
- **Ordering.** Request path; part 4 (Q-6) owns the account cap and `add_requester`.

### Q-8.22 Background steps of the selector cycle
- **Statement.** `manage.py refresh_selector --loop --interval 60` (systemd `atomicdb-selector.service`) runs, per pass, in this fixed order, each step wrapped so an exception is logged, recorded in the pass JSON (`failures[name]`) and yields its default (0 or None) without stopping later steps: `public-counters-pre` (only with priorities on), `priorities` (`refresh_priorities(force=True)`, PRI-3; skipped with `--no-priorities`, which production uses since 29-Aug), `solve-leases` (`recycle_stale_solve_leases`, SOL-5), `debt` (Q-8.7, `--debt-cap` 500, `--no-debt`), `coverage` (Q-8.9, `--coverage-cap` 100, `--no-coverage`), `adversarial-flag` then `dn-repair` (`--dn-repair-cap` 50) and `fragile` (`--fragile-cap` 50) when on (Q-8.10), `autosolve95` (`autosolve.drip_backlog(target=--autosolve-cap)`, default 16, skipped when the cap <= 0), `repair-backlog` (`idle_repair.drip_backlog()`, `--no-repair-backlog`), `sweep16` (`sweep.drip(cap=--sweep16-cap 256, user_backlog_max=--sweep16-user-backlog 500)`, skipped when cap <= 0), `pool` (`top_up_analysis_pool(--pool-target 512)`, PRI-4), `public-counters`. Then it sleeps until `interval - elapsed` (min interval 1 s) in 0.05-0.5 s slices. The capped arms run before the pool so the pool cannot eat their room (the pool counts AUTO tasks only, PRI-4).
- **Inputs / outputs.** Orchestration; JSON line per pass to stdout.
- **Code.** `atomicdb/management/commands/refresh_selector.py:handle:229-410`, options 122-210.
- **Tests.** `test_selector_service.py::RefreshSelectorCommandTests::test_stale_solve_leases_are_recycled_before_queue_caps`, `::test_autosolve_cap_is_forwarded_and_reported`, `::test_an_autosolve_cap_of_zero_skips_the_walk`; `test_selector_service.py::SelectorCycleResilienceTests::test_a_broken_arm_does_not_stop_the_steps_behind_it`; `test_idle_repair.py::SelectorStepTests::*`; `test_sweep.py::SweepDripTests::test_the_selector_service_runs_the_step`; `test_autosolve.py::SelectorStepTests::*`; `test_adversarial_explorer.py::AdversarialSwitchTests::*`.
- **Determinism hazards.** Wall clock: pass cadence (`time.monotonic`, `--interval`), so how many arm steps run between two landings depends on timing; CLI flags change behaviour; a step's exception silently skips its work for that pass.
- **History.** Step isolation added after a crash loop starved the pool; `solve-leases` moved ahead of the caps (20-Aug); autosolve/repair/sweep steps PR #100/#101/#224.
- **Ordering.** Runs concurrently with ingest writers and web leases (different processes).

### Q-8.23 Background producers riding on a landing
- **Statement.** After `backup_cascade` and `backup_backed_evals` of a batch of landings, `ingest.propagate_landings` runs, for EACH landing in batch order: `_uncertainty_expand([pos.key, *parents])` (part 4), `_witness_refuted_revisit(pos, parents)` (part 4), `_queue_value_cascade(pos)` (PRI-4), `idle_repair.queue_from_landing(pos, parents)` (Q-10.26), `autosolve.queue_from_landing(pos)` (SOL-4.7), then the post-commit rechecks (CAS-6). `propagate_landings` is called synchronously at the end of `ingest_analysis` (ingest.py:1306) when propagation is not deferred, or by `propagation_queue.drain_batch` (propagation_queue.py:450) with the merged landings of a batch when deferred (Q-4). idle_repair and autosolve wrap their purchase in a savepoint and swallow every exception (log only): a broken arm never fails the landing.
- **Inputs / outputs.** `landing.pos` (the position object as the landing left it: `last_analysis` already the decided showcase), `landing.parents`.
- **Code.** `atomicdb/ingest.py:propagate_landings:1397` (lines 1440-1458).
- **Tests.** `test_idle_repair.py::FailureContainmentTests::test_a_broken_arm_does_not_cost_the_landing`; `test_autosolve.py::FailureContainmentTests::test_a_broken_arm_does_not_cost_the_landing`; `test_idle_repair.py::ShallowLandingTests::*`; `test_autosolve.py::LandingTests::*`.
- **Determinism hazards.** In deferred mode the hooks run at drain time, possibly after later landings changed the same rows; batch composition (BATCH_SIZE, timing) decides which landings share a pass (Q-4).
- **History.** Hooks added by PR #100 and #101 (29-Oct).
- **Ordering.** After CAS-1 and BKD-7 of the batch; before CAS-6 rechecks.

---

### Q-10.1 Sweep16: the qualifying rule
- **Statement.** `qualifies(pos)` is true iff `pos.status == 'UNKNOWN'`, `pos.visits == 1`, `100,000,000 <= (pos.nodes_invested or 0) <= 141,000,000` (`SEARCHED_MIN_NODES`, `SEARCHED_MAX_NODES`), and the shown value `v = backed_eval if not None else eval_cp` (White's view) favours the side to move by +16..+90 pawns: White to move (`fen.split()[1]=='w'`) and `1600 <= v < 9000`, or Black to move and `-9000 < v <= -1600` (`SWEEP_LOW_CP=1600`, `SWEEP_HIGH_CP=MATE_BAND=9000`, the upper bound excluded). `v is None` -> false. `candidates_q()` is the same rule in SQL using the annotation `sweep_value = Coalesce(backed_eval, eval_cp)` and `fen LIKE '% w %'` / `'% b %'`.
- **Inputs / outputs.** Reads `Position.status, visits, nodes_invested, backed_eval, eval_cp, fen`.
- **Code.** `atomicdb/sweep.py:favoured_value:133`, `searched_once:142`, `qualifies:148`, `candidates_q:158`; constants 100-111.
- **Tests.** `test_sweep.py::SweepRuleTests::test_white_to_move_and_winning_by_16_to_90`, `::test_black_to_move_and_winning_too`, `::test_the_value_shown_is_the_backed_one`, `::test_only_one_search_of_about_128m`, `::test_only_open_positions`, `::test_no_value_no_sweep`.
- **Determinism hazards.** none.
- **History.** 1495ea4 2026-11-11 "Add the +16 sweep and drop deep rungs on interior positions" (PR #224).
- **Ordering.** Used by Q-10.2/Q-10.3 at plan time and re-checked by Q-10.6 at drip time.

### Q-10.2 Sweep16: walk of White's strategy tree
- **Statement.** `walk_strategy_tree(max_nodes=TREE_MAX_NODES=2,500,000, max_plies=TREE_MAX_PLIES=200, deadline=None, root=None, match=None, on_level=None)`: root = `root_key()` (start position). If the root row is missing or not UNKNOWN -> `([], stats)`. Breadth-first by ply: `level` starts `[(root, root.backed_move)]`, `visited={root}`. For `ply` in `0..max_plies`: stop if level empty; stop with `stopped='max_nodes'` if `stats.nodes >= max_nodes`; stop with `'time'` if `time.monotonic() > deadline`; `nodes += len(level)`; `white = (ply % 2 == 0)` (assumes White to move at the root). Level is processed in chunks of `TREE_CHUNK=300` parents. White node: follow only the edge `(parent, backed_move)` when `backed_move` is set (if that edge's child is not among the rows, fall back), else all edges; the pick is the child whose move equals `backed_move`, else the child with the highest `known_eval_of(status, backed_eval, eval_cp)` (strict `>`: the first row met wins ties; children with unknown value are ignored); skip if no pick, pick not UNKNOWN, or already visited; else mark visited and append `(child, child.backed_move)`. Black node: for every child with `status=='UNKNOWN'`: `value = backed_eval if not None else eval_cp`; if not yet found and `match(visits, nodes_invested, value)` (default `_sweep_match` = `searched_once` and `favoured_value(True, value)`, i.e. the child is White to move) append to `found`; if not visited, mark and enqueue. A `for ... else` sets `stopped='max_plies'` when the loop runs out of plies without break. Returns `(found, stats)` with `stats={'nodes','plies','stopped'}`. Only open positions are walked. Candidates are only ever White-to-move children of Black nodes.
- **Inputs / outputs.** Reads Edge rows with child `status, eval_cp, backed_eval, backed_move, visits, nodes_invested`.
- **Code.** `atomicdb/sweep.py:walk_strategy_tree:180`.
- **Tests.** `test_sweep.py::SweepPlanTests::test_the_walk_finds_only_what_the_tree_holds`, `::test_the_walk_follows_the_best_child_without_a_backed_move`, `::test_the_walk_stops_at_its_node_cap`; `test_backlog.py::BestLineLoader::test_the_sweep_walk_is_unchanged`. **Gaps:** tie order of the best child; deadline stop; Black candidates appearing at White plies (never, by construction).
- **Determinism hazards.** Row order: the Edge queries (224-240) have no `order_by`, so (a) the tie-break of the White best child and (b) the order of `found` and of the next level within a ply follow DB row order. Wall clock: `deadline` (`time.monotonic`) truncates the walk at a timing-dependent ply.
- **History.** 1495ea4 (PR #224). `match`/`on_level` parameters added for the community backlog loaders (Q-6, `backlog.deep_tree_match`).
- **Ordering.** First half of Q-10.4.

### Q-10.3 Sweep16: scan of the rest and its order
- **Statement.** `scan_candidates(exclude, prefixes=SCAN_PREFIXES, deadline)`: for each of the 256 two-hex-digit key prefixes `'00'..'ff'` in order, one statement `Position WHERE candidates_q() AND key >= prefix AND key < next_prefix` (no upper bound for `'ff'`), collecting `(abs(sweep_value), key)` for keys not in `exclude`; stops with `'time'` when the deadline passes. Sorted by `(-abs(value), key)`: highest absolute value first, ties by key ascending.
- **Inputs / outputs.** Reads Position.
- **Code.** `atomicdb/sweep.py:SCAN_PREFIXES:123`, `scan_candidates:283`.
- **Tests.** `test_sweep.py::SweepPlanTests::test_the_plan_puts_the_tree_first_then_the_highest_value`.
- **Determinism hazards.** Deadline (wall clock) may cut the scan after an arbitrary prefix; otherwise deterministic.
- **History.** 1495ea4 (PR #224).
- **Ordering.** Second half of Q-10.4, with `exclude = set(tree keys)`.

### Q-10.4 Sweep16: the plan and its files
- **Statement.** `build_plan(max_nodes, max_plies, time_limit=7200.0 s, save=True)`: one deadline `monotonic + time_limit` (none when 0) shared by the walk and the scan; plan = `{'build_id': uuid4 hex, 'built_at': now ISO, 'tree_count', 'rest_count', 'tree_stats', 'scan_stats', 'seconds', 'keys': tree + rest}`. `save_plan` stores it in memory and, when `ATOMICDB_STATE_DIR` is set, writes `<dir>/sweep16.json` atomically (`.tmp` + `os.replace`) and resets the cursor file `<dir>/sweep16_cursor.json` to `{'build_id', 'cursor': 0}`. `load_plan()` re-reads the file only when its mtime changed; missing file -> `None` (sweep off); unreadable or without a `keys` list -> `None` with a warning (memory copy not cleared). Without a state dir the plan lives only in process memory. `_load_cursor(build_id)` returns the memory cursor if it is for this build, else the file's cursor if its `build_id` matches and it is an int >= 0, else 0. Rebuilt by `manage.py sweep_plus16 --rebuild` from `atomicdb-sweep16.timer` daily at 03:30 (RandomizedDelaySec 10 min, not Persistent), single copy enforced by an `O_EXCL` lock file `<dir>/sweep16.lock` (stale after 6 h). `--dry-run` builds without saving. Building the list is what switches the sweep on.
- **Inputs / outputs.** Files `sweep16.json`, `sweep16_cursor.json`, `sweep16.lock` in `ATOMICDB_STATE_DIR`; module cache `_memory`.
- **Code.** `atomicdb/sweep.py:build_plan:311`, `_state_path:335`, `_write_json:340`, `save_plan:347`, `load_plan:361`, `_load_cursor:384`, `_save_cursor:405`; `atomicdb/management/commands/sweep_plus16.py`; `Documentation/atomicdb-sweep16.timer`.
- **Tests.** `test_sweep.py::SweepPlanTests::test_the_plan_puts_the_tree_first_then_the_highest_value`; `test_sweep.py::SweepDripTests::test_a_new_list_starts_at_its_head`, `::test_the_walk_resumes_from_disk`, `::test_the_command_rebuilds_and_reports`, `::test_a_dry_run_stores_nothing`. **Gaps:** the lock file; unreadable-plan behaviour.
- **Determinism hazards.** Files on disk and module-level cache (`_memory`, 128); `uuid4` build id; deadline; mtime-based reload (a running selector picks a new plan on its next drip; a rewind written by another process is NOT picked up while the memory cursor is for the same build: see Q-10.11).
- **History.** 1495ea4 (PR #224).
- **Ordering.** Must exist before Q-10.5 queues anything.

### Q-10.5 Sweep16 drip: gates and room
- **Statement.** `drip(cap=SWEEP_PENDING_CAP=256, user_backlog_max=SWEEP_USER_BACKLOG_MAX=500, scan=SWEEP_SCAN=2048)`: return 0 if `cap <= 0` or `arm_rate('sweep16') <= 0`; 0 if no plan or empty `keys`; `pending = pending_count()` (= COUNT PENDING, arm sweep16, source FILL; adopted USER rows excluded); `room = cap - pending`, 0 -> return; if `user_backlog_max >= 0 and share() <= 0 and user_backlog() > user_backlog_max` -> return 0 (`user_backlog()` = COUNT PENDING source USER, all accounts); `room = min(room, _arm_budget_room('sweep16'))` (the hourly count includes adopted USER rows of the arm), 0 -> return.
- **Inputs / outputs.** Reads AnalysisTask counts; settings.
- **Code.** `atomicdb/sweep.py:user_backlog:545`, `pending_count:550`, `drip:591-618`.
- **Tests.** `test_sweep.py::SweepDripTests::test_without_a_list_nothing_is_queued`, `::test_the_arm_at_zero_stops_it`, `::test_the_hourly_ceiling_clips_the_batch`, `::test_it_waits_while_the_requests_pile_up`, `::test_with_a_share_it_queues_whatever_the_requests`, `::test_it_only_tops_up_to_its_cap_in_list_order`.
- **Determinism hazards.** Settings switches (`ATOMICDB_SWEEP16_SHARE`, `ATOMICDB_ARM_RATE_SWEEP16`); counts race with leases.
- **History.** 1495ea4; share bypass of the user-backlog gate 11657b8 (PR #224).
- **Ordering.** Before Q-10.6.

### Q-10.6 Sweep16 drip: per-key recheck and minting
- **Statement.** Starting at `cursor = _load_cursor(build_id)`, while `made < room`, `cursor < len(keys)` and `cursor - start < scan`: take `batch = keys[cursor : cursor + min(SWEEP_BATCH=256, scan - (cursor - start))]`; load those positions (`key, fen, status, visits, nodes_invested, eval_cp, backed_eval`) and the set `live` of positions with a PENDING or LEASED task (any arm, any source). Inside `atomic()`, for each key in batch order while `made < room`: `used += 1`; skip if the position is missing, in `live`, `not qualifies(pos)`, or `not _still_reachable(pos)` (`_still_reachable`: no parent edges -> true; else at least one parent with status UNKNOWN); else `get_or_create(position=pos, generation=pos.visits, defaults={budget_nodes: SWEEP_NODES = 128,000,000*1005//1000 = 128,640,000, multipv: SWEEP_MULTIPV=1, source: FILL, arm: 'sweep16'})`; count if created or if `_revive(task)` (Q-10.7) returned true. `cursor += used` (keys not reached because room ran out are not consumed). A skipped key is passed over for good (the cursor moves past it). Note `qualifies` requires `visits == 1`, so the slot is generation 1.
- **Inputs / outputs.** Writes AnalysisTask; reads Edge for reachability.
- **Code.** `atomicdb/sweep.py:SWEEP_NODES:100`, `drip:619-651`; `atomicdb/ingest.py:_still_reachable:6770`.
- **Tests.** `test_sweep.py::SweepDripTests::test_it_queues_one_line_of_128m_in_the_fill_band`, `::test_every_key_is_checked_again`, `::test_a_position_whose_parents_are_all_proven_is_passed_over`, `::test_the_scan_is_bounded`, `::test_a_task_somebody_searched_is_not_queued_again`; `test_backlog.py::ClearWins::test_the_drip_passes_over_an_adopted_position`.
- **Determinism hazards.** `live` is read outside the `atomic()` block and before minting (race with leases/requests); otherwise ordered by plan.
- **History.** 1495ea4 (PR #224); 128.64M overbid 8ba2035 2026-11-15 (PR #255, MoKy's report: ~1,800 of 3,000 sweep searches neither closed nor replaced the shown search).
- **Ordering.** After Q-10.5; then Q-10.8.

### Q-10.7 Sweep16: reviving its own absorbed task
- **Statement.** `_revive(task)` returns false unless `task.arm == 'sweep16'`, `task.source == FILL` and `is_absorbed(task)`. Then a conditional update `filter(pk, arm='sweep16', source='FILL', state='COMPLETED', nodes_searched=0, machine='').update(state='PENDING', completed=None, created=now, budget_nodes=GREATEST(budget_nodes, SWEEP_NODES))`; returns whether a row was updated. The new `created` makes it count against this hour's ceiling. Another arm's absorbed row on the slot, or a row somebody searched, is not revived (the key is skipped).
- **Inputs / outputs.** Writes `AnalysisTask.state, completed, created, budget_nodes`.
- **Code.** `atomicdb/sweep.py:_revive:566`.
- **Tests.** `test_sweep.py::SweepDripTests::test_an_absorbed_task_is_queued_again_from_the_head`, `::test_a_revived_task_is_bought_at_todays_budget`, `::test_a_revived_task_counts_against_the_cap`, `::test_another_arms_absorbed_task_is_left_alone`, `::test_the_hourly_absorb_pass_leaves_its_tasks_alone`, `::test_a_search_that_lands_while_it_waits_still_absorbs_it`.
- **Determinism hazards.** Wall clock `created=timezone.now()`.
- **History.** 405a42b 2026-11-13 "Stop the hourly absorb pass from emptying the +16 sweep's queue" (PR #238); budget raise c706725 (PR #257).
- **Ordering.** Inside Q-10.6.

### Q-10.8 Sweep16 drip: cursor save and receipt
- **Statement.** After the loop, `_save_cursor(build_id, cursor)` (memory and file) always runs, even when nothing was made. If `made > 0`: `note_waiting(True)` (Q-10.10) and `DBEvent('SWEEP16_ENQUEUED', {'queued', 'pending_before', 'cap', 'cursor', 'total': len(keys)})` (hidden from the feed). Returns `made`.
- **Inputs / outputs.** File `sweep16_cursor.json`, cache key, DBEvent.
- **Code.** `atomicdb/sweep.py:drip:652-658`.
- **Tests.** `test_sweep.py::SweepDripTests::test_it_queues_one_line_of_128m_in_the_fill_band`, `::test_the_walk_resumes_from_disk`; `test_sweep.py::SweepServiceOrderTests::test_its_receipt_stays_out_of_the_feed` (NONE).
- **Determinism hazards.** Files on disk; cache.
- **History.** 1495ea4 (PR #224).
- **Ordering.** End of the drip.

### Q-10.9 Sweep16 fleet share at lease time
- **Statement.** `share()` = `settings.ATOMICDB_SWEEP16_SHARE` or env of that name, parsed as float; parse error or NaN -> `SHARE_DEFAULT=0.14`; clamped to [0, 1]; empty string is a parse error -> 0.14. `take_share(fraction=None)`: false if `fraction <= 0` or `not has_waiting()`; true if `fraction >= 1`; else `n = _next_lease_number()` (`cache.incr('atomicdb:sweep16:leases')`, created with `add(key, 0, None)` on first use; any cache exception -> None); if `n is None` -> `random.random() < fraction`; else true iff `int(n*fraction) != int((n-1)*fraction)` (exactly `fraction` of the counted leases, evenly spread). Call site (part 1, `views.api_lease:1397`): a non-personal worker with `take_share()` true first tries `choose_pending(sweep_only=True)` (ordered = `waiting_tasks()`, same per-row checks as normal); if that yields nothing it calls `note_waiting(False)`; then the normal choice runs.
- **Inputs / outputs.** Shared cache (Redis in production) counter; settings.
- **Code.** `atomicdb/sweep.py:SHARE_SETTING:464`, `share:471`, `_next_lease_number:519`, `take_share:531`; `atomicdb/views.py:api_lease:1288,1397-1402`.
- **Tests.** `test_sweep.py::SweepShareRuleTests::test_the_share_reads_its_setting`, `::test_zero_never_takes_it`, `::test_one_always_takes_it_when_there_is_something`, `::test_nothing_waiting_nothing_taken`, `::test_the_fraction_is_exact_and_spread`, `::test_without_a_cache_it_falls_back_to_a_coin`; `test_sweep.py::SweepShareLeaseTests::*`; `test_max_budget.py::SweepShareTests::test_the_sweep_share_respects_the_cap_and_falls_through`.
- **Determinism hazards.** Shared mutable counter in an external cache (order of leases across web processes), randomness fallback (`random.random()`, 541), settings switch.
- **History.** 11657b8 2026-11-11 "Give the +16 sweep a fixed share of the leases" (PR #224).
- **Ordering.** Inside the lease, before the normal candidate choice.

### Q-10.10 Sweep16 waiting cache
- **Statement.** `waiting_tasks()` = `AnalysisTask(state='PENDING', arm='sweep16', source='FILL', position__status='UNKNOWN').order_by('id')`. `has_waiting()` returns the cached boolean under `'atomicdb:sweep16:waiting'` if present, else runs `waiting_tasks().exists()` and caches it for `WAITING_CACHE_SECONDS=60`; cache errors are ignored. `note_waiting(v)` sets the key for 60 s. Set true by a drip that queued; set false by a lease whose sweep-only choice found nothing.
- **Inputs / outputs.** Cache key; AnalysisTask read.
- **Code.** `atomicdb/sweep.py:WAITING_CACHE_*:466-468`, `note_waiting:490`, `has_waiting:497`, `waiting_tasks:510`.
- **Tests.** `test_sweep.py::SweepShareRuleTests::test_waiting_is_cached`; `test_sweep.py::SweepShareLeaseTests::test_a_stale_yes_is_corrected_by_the_lease`.
- **Determinism hazards.** Cache TTL (wall clock), stale values for up to 60 s.
- **History.** 11657b8 (PR #224).
- **Ordering.** Read by Q-10.9.

### Q-10.11 Sweep16: stop, withdraw, rewind, status
- **Statement.** `forget_plan()` clears memory and deletes both files (sweep stops). `withdraw_pending()` DELETES (hard delete) every `AnalysisTask(state='PENDING', arm='sweep16', source='FILL')`; adopted USER rows stay; returns the count. `rewind()` writes cursor 0 for the current plan's build id (memory and file); a selector process already running keeps its in-memory cursor for that build (`_load_cursor` prefers memory) until restarted. `status()` reports rate, pending, adopted_pending, leased, completed counts, user backlog and plan metadata with cursor and `left`. CLI: `sweep_plus16 --stop` (forget), `--withdraw` (forget + withdraw), `--rewind`, `--drip [--cap]`, `--status`.
- **Inputs / outputs.** Files; deletes AnalysisTask rows.
- **Code.** `atomicdb/sweep.py:rewind:415`, `forget_plan:430`, `status:661`, `withdraw_pending:683`; `atomicdb/management/commands/sweep_plus16.py:handle`.
- **Tests.** `test_sweep.py::SweepDripTests::test_rewinding_without_a_list_does_nothing`, `::test_the_command_rewinds`, `::test_the_command_stops_and_withdraws`; `test_backlog.py::ClearWins::test_a_sweep_task_waiting_on_the_slot_becomes_the_request`.
- **Determinism hazards.** Module cache vs. file (cross-process rewind not seen by a running selector); hard delete frees the slot for any producer.
- **History.** 1495ea4 (PR #224); USER exemption with the community backlog (16-Nov).
- **Ordering.** Operator actions.

### Q-10.20 Idle repair: shallow showcase predicate
- **Statement.** `shallow_showcase(last_analysis)` returns the node count of the showcase or `None`. The showcase is the first dict line of `last_analysis` without a truthy `prior_pass`. Not shallow (`None`) when: no such line; `line['_budget']` is not an int or `< REPAIR_MIN_NODES = BUDGET_LADDER[2] = 128M`; the depth parsed from `line['raw']` by `ingest.info_fields` is missing or `>= REPAIR_SHALLOW_DEPTH = 18`; `line['mate']` is not None, or `eval_cp` is not an int, or `|eval_cp| >= 9000`. Otherwise returns `_budget`.
- **Inputs / outputs.** Reads `Position.last_analysis`.
- **Code.** `atomicdb/idle_repair.py:REPAIR_MIN_NODES:67`, `REPAIR_SHALLOW_DEPTH:68`, `shallow_showcase:129`.
- **Tests.** `test_idle_repair.py::ShallowRuleTests::test_a_shallow_showcase_of_128m_is_found`, `::test_depth_18_is_not_shallow`, `::test_under_128m_is_not_this_arms_business`, `::test_an_unknown_depth_is_not_a_shallow_one`, `::test_a_mate_is_not_repaired`, `::test_only_the_current_pass_is_the_showcase`.
- **Determinism hazards.** none.
- **History.** fc9f14b 2026-10-29 "Let the idle fleet repair shallow and unanalysed positions" (PR #100).
- **Ordering.** Inside Q-10.26 / Q-10.27.

### Q-10.21 Idle repair: repair budget
- **Statement.** `repair_budget(searched, interior=False)`: `rung = _rung_matching(searched)` (smallest BUDGET_LADDER rung with `searched <= rung*1.01`, else 2B); `None` if `rung > REPAIR_MAX_RUNG = 512M`; `None` if `interior` and `rung > AUTO_INTERIOR_MAX_NODES (128M)` and `ATOMICDB_AUTO_INTERIOR_DEEP_RUNGS` not set; `budget = rung * (1000 + REPAIR_OVERBID_PER_MILLE=5) // 1000` (128M -> 128,640,000; 512M -> 514,560,000); return `budget` if `budget > searched` else `None`. "Interior" is `ingest.is_interior(pos)` (truthy `backed_move`) at landing, and truthy `backed_move` read from the row in the backlog walk (same test). Width `REPAIR_MULTIPV = DEPTH_MULTIPV = 2`. The budget does NOT go through `server_picked_budget` (it is already >= 128M).
- **Inputs / outputs.** Pure, plus the settings switch.
- **Code.** `atomicdb/idle_repair.py:REPAIR_MAX_RUNG:71`, `REPAIR_OVERBID_PER_MILLE:84`, `REPAIR_MULTIPV:89`, `repair_budget:155`.
- **Tests.** `test_idle_repair.py::ShallowRuleTests::test_the_repair_outbids_the_rung_it_repairs_by_a_hair`, `::test_the_overbid_keeps_it_on_the_same_rung`, `::test_a_2b_search_is_not_bought_again`, `::test_a_search_reported_past_the_overbid_is_left_alone`; `test_sweep.py::InteriorRungRuleTests::test_an_interior_repair_above_the_first_rung_is_not_bought`, `::test_the_setting_restores_the_deep_repair`; `test_idle_repair.py::ShallowLandingTests::test_a_shallow_512m_buys_the_512m_rung`, `::test_a_shallow_2b_is_not_bought_again`.
- **Determinism hazards.** Settings switch.
- **History.** fc9f14b (PR #100); interior rule 1495ea4 (PR #224).
- **Ordering.** After Q-10.20.

### Q-10.22 Idle repair: connector predicate
- **Statement.** A connector is an open, live position with `visits == 0` (no analysis ever applied) that has at least one parent with `status='UNKNOWN' and visits > 0`, and sits above a just-searched position (it is a parent of the landed position, or, in the backlog walk, a parent of a completed search's position). At landing: `connector_parents(parent_keys)` takes the sorted distinct non-empty parent keys, cut to `REPAIR_PARENTS_MAX=16`, keeps those with `visits=0` and `_OPEN`, then keeps those with an open searched parent (`_linked_to_searched`), returned sorted. Backlog: `_connectors_of(child_keys)` reads `Edge(child in keys, parent status UNKNOWN, parent visits 0, parent priority > DEAD/2)` and filters through `_linked_to_searched`, giving `{child: [parents]}`. Connector price: `server_picked_budget(128M)` = 128M and width `multipv_for(0, 128M)` (first-look width).
- **Inputs / outputs.** Reads Position `visits, status, priority`, Edge.
- **Code.** `atomicdb/idle_repair.py:REPAIR_PARENTS_MAX:99`, `_connector_price:176`, `_linked_to_searched:183`, `connector_parents:192`, `_connectors_of:206`.
- **Tests.** `test_idle_repair.py::ConnectorTests::test_an_unsearched_link_between_searched_positions_is_bought`, `::test_without_a_searched_parent_it_is_not_a_connector`, `::test_a_link_with_a_search_of_its_own_is_not_a_connector`, `::test_a_solved_link_is_not_a_connector`, `::test_a_closed_grandparent_does_not_make_one`, `::test_a_dead_branch_is_not_bought`, `::test_a_connector_is_bought_once`, `::test_a_shallow_landing_below_a_connector_buys_both`, `::test_another_arms_search_of_the_link_is_enough`; `test_idle_repair.py::BacklogTests::test_connectors_of_the_past_are_found_too`.
- **Determinism hazards.** `_connectors_of` (208) has no order_by: the order of each child's parent list follows row order (affects which connector is minted first when room runs out). The cut to 16 parents is by sorted key (deterministic).
- **History.** fc9f14b (PR #100).
- **Ordering.** Inside Q-10.26 / Q-10.27.

### Q-10.23 Idle repair: once per position
- **Statement.** `_already_repaired(keys)` = positions among `keys` with an `arm='repair'` task that is PENDING or LEASED, or COMPLETED with `nodes_searched > 0`. An absorbed repair (COMPLETED, 0 nodes) does not count, so the position can be repaired again. A repair that ran (even if it came out shallow) is never bought again.
- **Inputs / outputs.** Reads AnalysisTask.
- **Code.** `atomicdb/idle_repair.py:_already_repaired:220`.
- **Tests.** `test_idle_repair.py::ShallowLandingTests::test_a_second_shallow_landing_does_not_queue_again`, `::test_a_repair_that_came_out_shallow_is_not_repaired`, `::test_a_position_repaired_once_is_never_bought_another`, `::test_a_repair_absorbed_unsearched_does_not_count`; `test_idle_repair.py::BacklogTests::test_what_is_already_repaired_is_skipped_without_room`.
- **Determinism hazards.** none.
- **History.** fc9f14b (PR #100).
- **Ordering.** Before Q-10.24.

### Q-10.24 Idle repair: room
- **Statement.** `_room(cap, reserve=0)`: `pending = COUNT(PENDING, arm='repair', position.status='UNKNOWN')` (repairs left on closed positions hold no place); `room = cap - pending`; `<= 0` -> `(0, pending)`; `hourly = _arm_budget_room('repair') - reserve`; return `(max(0, min(room, hourly)), pending)`. Landing uses `cap = REPAIR_QUEUE_CAP = 16`, `reserve = 0`. Backlog uses `cap = target` (default `REPAIR_BACKLOG_TARGET = 8`) and `reserve = rate - rate // 2` (with rate 60, reserve 30: the walk may use only half the hour).
- **Inputs / outputs.** Counts.
- **Code.** `atomicdb/idle_repair.py:REPAIR_QUEUE_CAP:93`, `REPAIR_BACKLOG_TARGET:96`, `_room:236`.
- **Tests.** `test_idle_repair.py::CapAndRateTests::test_a_full_queue_buys_nothing`, `::test_repairs_left_on_closed_positions_hold_no_place`, `::test_what_other_arms_queue_is_not_counted`, `::test_an_arm_at_its_hourly_count_buys_nothing`, `::test_zero_turns_the_arm_off`; `test_idle_repair.py::BacklogTests::test_it_stops_at_half_the_cap`, `::test_half_the_hourly_ceiling_is_left_to_landings`.
- **Determinism hazards.** Q-8.3 (wall clock window, races).
- **History.** fc9f14b (PR #100).
- **Ordering.** Before Q-10.25.

### Q-10.25 Idle repair: minting
- **Statement.** `_mint(targets, room)` with targets `(key, kind, budget, multipv)` in order: stop at `room`; re-read `visits` of the position with `_OPEN`; skip if gone/closed/dead, or if `kind == 'connector'` and `visits > 0` now; `get_or_create(position_id=key, generation=visits, defaults={budget_nodes, multipv, source: AUTO, arm: 'repair'})`; only a created row counts (an existing row on the next-visit slot, from any producer, is "the next search" and the repair is dropped). `fits_slot` routes these tasks only to narrow slots (Q-10.28).
- **Inputs / outputs.** Creates AnalysisTask (AUTO, repair).
- **Code.** `atomicdb/idle_repair.py:_mint:253`.
- **Tests.** `test_idle_repair.py::ShallowLandingTests::test_a_shallow_search_of_128m_queues_one_repair`, `::test_a_task_already_on_the_next_visit_is_the_next_search`, `::test_a_closed_position_is_not_repaired`; `test_idle_repair.py::PoolTests::test_waiting_repairs_do_not_shrink_the_pool`.
- **Determinism hazards.** Unique-slot race (IntegrityError inside `get_or_create` is retried by Django as a get).
- **History.** fc9f14b (PR #100).
- **Ordering.** After Q-10.24.

### Q-10.26 Idle repair at a landing
- **Statement.** `queue_from_landing(pos, parent_keys)`: return 0 if `arm_rate('repair') <= 0`. Targets: the landed position as SHALLOW if `shallow_showcase(pos.last_analysis)` gives nodes and `repair_budget(nodes, interior=is_interior(pos))` is not None (multipv 2); then each connector parent (Q-10.22) at the connector price. No targets -> 0 (no write). Then inside a savepoint (`database.atomic()`): drop targets already repaired (Q-10.23); compute room with cap 16 (Q-10.24); mint (Q-10.25); if any made, receipt `DBEvent('IDLE_REPAIR', {'queued', 'shallow', 'connector', 'keys', 'pending_before', 'cap': 16, 'via': 'landing', 'key': pos.key})`. Any exception -> logged, returns 0.
- **Inputs / outputs.** As above.
- **Code.** `atomicdb/idle_repair.py:queue_from_landing:290`, `_receipt:280`.
- **Tests.** `test_idle_repair.py::ShallowLandingTests::*` (e.g. `test_a_deep_search_queues_nothing`, `test_a_search_under_128m_queues_nothing`, `test_a_mate_score_queues_nothing`, `test_a_search_without_its_info_line_queues_nothing`, `test_a_shallow_pass_under_a_deeper_showcase_queues_nothing`, `test_each_batch_leaves_one_hidden_event`, `test_a_landing_with_nothing_to_repair_writes_nothing`, `test_the_common_landing_costs_one_read_of_its_parents`); `test_idle_repair.py::ArbitrationTests::test_the_repair_takes_the_showcase_of_the_search_it_repairs`; `test_idle_repair.py::FailureContainmentTests::test_a_broken_arm_does_not_cost_the_landing`.
- **Determinism hazards.** Exceptions swallowed (a transient DB error silently skips a purchase). In deferred propagation the hook sees the position as of drain time.
- **History.** fc9f14b (PR #100).
- **Ordering.** Q-8.23 (after the value cascade hook, before autosolve).

### Q-10.27 Idle repair and autosolve: the backlog walk
- **Statement.** One step per selector pass walks the completed searches newest first. State: module dict `_backlog = {'cursor', 'loaded'}`; the cursor is loaded once per process from `<ATOMICDB_STATE_DIR>/idle_repair.json` (autosolve: `autosolve.json`) — any read error or a non-positive/non-int value means the top. Step: return 0 if the arm rate is 0 (autosolve also if `target <= 0`, with `target = min(target, 16)`); room via `_room(target, reserve=rate - rate//2)`; 0 -> return 0 (no walk, cursor unchanged). `cursor = saved cursor or 1 + max(AnalysisTask.id)`; `floor = max(0, cursor - window)` with `window = 2,000`; read up to `scan = 200` rows `AnalysisTask(id < cursor, id >= floor, state=COMPLETED)` (idle repair adds `nodes_searched >= 128M`) ordered by `-id`, as `(id, position_id)`; `resume = last row id if len(rows) >= scan else floor`; `keys` = distinct positions in that order. Inside `atomic()` compute targets per row (idle repair: SHALLOW from the row's `last_analysis` if open and not already repaired, plus its connectors not already repaired; a `seen` set prevents double-buying across rows; autosolve: SOL-4.8). When the made count reaches room mid-row, `resume = that task id + 1` (the row is read again next time; what it already bought is skipped by dedupe). Receipt with `via='backlog'`, `cursor=resume`, `scanned=len(rows)`, `cap=target`. Finally `_save_backlog(resume if resume > 0 else None)` (atomic file write; `None` = wrap to the newest search next time). Without a state dir the cursor lives in process memory only.
- **Inputs / outputs.** Files `idle_repair.json`, `autosolve.json`; module caches; AnalysisTask/SolveTask writes.
- **Code.** `atomicdb/idle_repair.py:_backlog:361`, `_backlog_state:369`, `_save_backlog:390`, `reset_backlog_state:404`, `drip_backlog:415`; `atomicdb/autosolve.py:_backlog:326`..`drip_backlog:380`.
- **Tests.** `test_idle_repair.py::BacklogTests::*`, `test_idle_repair.py::BacklogStateTests::*`; `test_autosolve.py::BacklogTests::*`, `test_autosolve.py::BacklogStateTests::*` (incl. `test_it_does_not_share_the_repair_walks_file`, `test_past_the_oldest_search_it_starts_again_from_the_newest`, `test_a_step_that_fills_its_room_mid_search_reads_it_again`).
- **Determinism hazards.** Files on disk + module-level cache; the walk position depends on how many passes ran (wall clock cadence) and on the global `AnalysisTask.id` sequence; ids are dense only per sequence (gaps from rollbacks).
- **History.** fc9f14b (PR #100), 94c0262 (PR #101).
- **Ordering.** Selector steps `autosolve95` then `repair-backlog` (Q-8.22).

### Q-10.28 Idle repair: slot routing at lease
- **Statement.** `fits_slot(task, threads)`: true for any task that is not (`arm == 'repair'` and `source == AUTO`); for those, true iff `0 < threads <= REPAIR_MAX_THREADS = 8` (`threads` = the slot's reported threads per engine; 0/unknown is not trusted). A visitor who asks for the same position promotes the waiting repair to USER (Q-6 `_request_rung`), after which it is routed to anyone. Part 1 owns the lease loop that calls it (`views.api_lease:1333`: a non-fitting candidate is skipped, the slot gets something else).
- **Inputs / outputs.** Reads `task.arm, task.source`; the slot's `threads` from the lease request.
- **Code.** `atomicdb/idle_repair.py:REPAIR_MAX_THREADS:102`, `fits_slot:115`; `atomicdb/views.py:1333`.
- **Tests.** `test_idle_repair.py::ShallowRuleTests::test_only_a_server_picked_repair_is_routed`; `test_idle_repair.py::LeaseRoutingTests::*`; `test_backlog.py::Adopting::test_shallow_positions_get_the_house_width_of_a_revisit`.
- **Determinism hazards.** Depends on worker-reported threads.
- **History.** fc9f14b (PR #100).
- **Ordering.** Lease time (part 1, Q-2).

---

### SOL-4.1 Autosolve: what "scores a mate" means
- **Statement.** `mate_goal(line)`: `None` unless `line` is a dict whose `eval_cp` is an int (not bool) with `TB_CLAMP_CEILING (9500) < |eval_cp| < 10000`. If `line['mate']` is a non-zero int whose sign differs from the sign of `eval_cp`, `None` (self-contradicting). Otherwise `'WHITE_WIN'` if `eval_cp > 0` else `'BLACK_WIN'` (White's point of view, independent of side to move). `±9500` exactly (tablebase/huge eval band) is not a mate. `AUTOSOLVE95_THRESHOLD = 9500` is defined but unused by code (kept for tests/docs).
- **Inputs / outputs.** Pure.
- **Code.** `atomicdb/autosolve.py:mate_goal:113`; `atomicdb/ingest.py:AUTOSOLVE95_*:9003-9005`.
- **Tests.** `test_autosolve.py::MateRuleTests::test_a_mate_score_names_the_side_that_mates`, `::test_the_clamp_is_not_a_mate`, `::test_a_line_that_contradicts_itself_claims_nothing`, `::test_nothing_else_is_a_claim`.
- **Determinism hazards.** none.
- **History.** 94c0262 2026-10-29 "Buy one solver pass for the mates the engine reported, without a scan" (PR #101), replacing a per-tick scan by `eval_cp OR backed_eval`.
- **Ordering.** Inside SOL-4.2.

### SOL-4.2 Autosolve: claims of a showcase
- **Statement.** `claims(last_analysis)`: lines = dict lines without truthy `prior_pass`; none -> `(None, [])`. For each line in order, if `mate_goal(line)` is not None and `line['move']` is a str not already seen, record `move -> (|eval_cp|, goal)` (first line per move wins). Moves are sorted by `-|eval_cp|` (shortest mate first; Python's stable sort keeps line order on ties). Returns `(mate_goal(lines[0]), [(move, goal), ...])`: the own goal comes only from the FIRST current line.
- **Inputs / outputs.** Reads `Position.last_analysis`.
- **Code.** `atomicdb/autosolve.py:claims:131`.
- **Tests.** `test_autosolve.py::MateRuleTests::test_only_the_current_pass_claims_and_shortest_mates_come_first`, `::test_a_showcase_whose_first_line_is_no_mate_claims_its_children`, `::test_an_empty_showcase_claims_nothing`.
- **Determinism hazards.** none (stable sort over stored line order).
- **History.** 94c0262 (PR #101).
- **Ordering.** Inside SOL-4.7 / SOL-4.8.

### SOL-4.3 Autosolve: targets
- **Statement.** `_targets(parent, own, moves, children, alive)`: `[]` if the parent is not alive (open with priority > DEAD/2). Else `[(parent, own, 'own')]` if `own` is not None, followed, for each `(move, goal)` in claims order, by `(child, goal, 'child')` where `child = children[(parent, move)]` (Edge lookup, `_children`: one read of `Edge(parent in parents, move_uci in moves)` cut back to the exact pairs), if the child is alive and not already in the list (two moves to one child buy it once; the parent itself is never a child target). One ply only: no rows are created to queue proofs; an unknown move (no edge) buys nothing.
- **Inputs / outputs.** Reads Edge, Position (`_open`).
- **Code.** `atomicdb/autosolve.py:_children:152`, `_open:169`, `_targets:177`.
- **Tests.** `test_autosolve.py::LandingTests::test_a_reported_mate_buys_the_position_and_the_child`, `::test_the_goal_is_the_side_that_mates_not_the_side_to_move`, `::test_one_ply_and_no_deeper`, `::test_a_mate_on_a_lower_line_buys_its_child_only`, `::test_the_position_first_then_the_shortest_mates`; `test_autosolve.py::DirectLandingTests::*`.
- **Determinism hazards.** none (dict keyed by exact pair; with two edges `(parent, move)` to different children impossible by the unique `(parent, move_uci)` edge index cited in `_children`).
- **History.** 94c0262 (PR #101).
- **Ordering.** Before SOL-4.4.

### SOL-4.4 Autosolve: never ask twice
- **Statement.** `_already_asked(goals)` with `goals = {key: goal}`: a key is excluded if it has ANY `autosolve95` SolveTask (any state, ever), OR any SolveTask in PENDING/LEASED (any arm), OR a COMPLETED SolveTask of any arm with `budget_nodes >= AUTOSOLVE_NODES (8M)` and the same goal. A smaller completed pass or the other goal does not exclude.
- **Inputs / outputs.** Reads SolveTask `position_id, arm, state, goal, budget_nodes`.
- **Code.** `atomicdb/autosolve.py:_already_asked:196`.
- **Tests.** `test_autosolve.py::NeverAgainTests::*` (`test_a_second_landing_does_not_buy_again`, `test_a_failed_pass_is_never_bought_again`, `test_a_pass_that_proved_nothing_is_never_bought_again`, `test_a_pass_still_running_is_not_doubled`, `test_another_arms_pass_on_its_way_is_the_same_question`, `test_a_bigger_completed_pass_answered_the_same_goal`, `test_but_not_the_other_goal_nor_a_smaller_pass`); `test_autosolve.py::SelectorStepTests::test_a_failed_pass_is_not_bought_again_tick_after_tick`.
- **Determinism hazards.** none.
- **History.** 94c0262 (PR #101) (the old scan re-bought a failed pass every two minutes).
- **Ordering.** Before SOL-4.5.

### SOL-4.5 Autosolve: room
- **Statement.** `_room(cap, reserve=0)`: `pending = COUNT(SolveTask arm='autosolve95', state=PENDING)` (closed positions included: `api_solve_acquire` serves a PENDING pass whatever its position became); `room = cap - pending`, `<= 0` -> `(0, pending)`; `hourly = _arm_budget_room('autosolve95', SolveTask.objects) - reserve` (the hourly count is over the SolveTask table); `(max(0, min(room, hourly)), pending)`. Landing: `cap = AUTOSOLVE_QUEUE_CAP = 32`, reserve 0. Backlog: `cap = target <= AUTOSOLVE_BACKLOG_TARGET = 16`, `reserve = rate - rate//2`.
- **Inputs / outputs.** Counts.
- **Code.** `atomicdb/autosolve.py:AUTOSOLVE_QUEUE_CAP:96`, `AUTOSOLVE_BACKLOG_TARGET:99`, `_room:222`.
- **Tests.** `test_autosolve.py::CapAndRateTests::test_a_full_queue_buys_nothing`, `::test_passes_left_on_closed_positions_still_hold_their_place`, `::test_what_other_arms_queue_is_not_counted`, `::test_the_room_left_is_all_a_landing_takes`, `::test_an_arm_at_its_hourly_count_buys_nothing`, `::test_the_ceiling_counts_this_arms_passes_only`, `::test_zero_turns_the_arm_off`; `test_autosolve.py::BacklogTests::test_half_the_hourly_ceiling_is_left_to_landings`, `::test_it_stops_at_half_the_cap`, `::test_the_target_never_passes_half_the_cap`.
- **Determinism hazards.** Q-8.3.
- **History.** 94c0262 (PR #101).
- **Ordering.** Before SOL-4.6.

### SOL-4.6 Autosolve: minting and receipt
- **Statement.** `_mint(targets)`: one `SolveTask.objects.bulk_create` of `SolveTask(position_id=key, goal=goal, budget_stage='F0', budget_nodes=AUTOSOLVE95_STAGE_NODES=8,000,000, arm='autosolve95')` per target (no `ignore_conflicts`; there is no unique constraint, so two processes can buy the same position at the same moment: accepted, costs one 8M pass). Receipt `DBEvent('AUTOSOLVE95_ENQUEUED', {'queued', 'own', 'child', 'keys', 'pending_before', 'cap', 'via', ...})` (hidden from feed). Served by `api_solve_acquire` before fragile (2M) and debt (part 2: non-debt first, then larger budget, then id). Only workers started with `--solve` take SolveTasks.
- **Inputs / outputs.** Creates SolveTask; DBEvent.
- **Code.** `atomicdb/autosolve.py:_mint:237`, `_receipt:253`; `atomicdb/views.py:api_solve_acquire:1969-1980`.
- **Tests.** `test_autosolve.py::LandingTests::test_each_batch_leaves_one_hidden_event`; `test_autosolve.py::ServingTests::test_the_solver_leases_it_with_its_goal_and_stage`, `::test_it_goes_before_the_debt`, `::test_a_verified_certificate_closes_the_position`.
- **Determinism hazards.** Duplicate SolveTasks under concurrency (no unique row).
- **History.** 94c0262 (PR #101).
- **Ordering.** After SOL-4.5.

### SOL-4.7 Autosolve at a landing
- **Statement.** `queue_from_landing(pos)`: 0 if `arm_rate('autosolve95') <= 0`; `(own, moves) = claims(pos.last_analysis)`; nothing claimed -> 0 with no query. Inside a savepoint: children of the claimed moves, `alive = _open([pos.key, *children])`, targets (SOL-4.3), drop already asked (SOL-4.4, using each target's goal), room with cap 32 (SOL-4.5), `_mint(targets[:room])`, receipt `via='landing', key=pos.key`. Exceptions logged, return 0.
- **Inputs / outputs.** As above.
- **Code.** `atomicdb/autosolve.py:queue_from_landing:263`.
- **Tests.** `test_autosolve.py::LandingTests::*`, `test_autosolve.py::DirectLandingTests::*`, `test_autosolve.py::FailureContainmentTests::test_a_broken_arm_does_not_cost_the_landing`, `test_autosolve.py::LandingTests::test_a_landing_with_no_mate_costs_no_query`.
- **Determinism hazards.** Swallowed exceptions; deferred-mode timing (Q-8.23).
- **History.** 94c0262 (PR #101).
- **Ordering.** Last background hook of a landing (Q-8.23).

### SOL-4.8 Autosolve: backlog walk specifics
- **Statement.** Same walk as Q-10.27 with these differences: rows are any COMPLETED AnalysisTask (no node filter); for the distinct keys, `claims` is computed from each OPEN position's `last_analysis`; `alive = set(claimed) | _open(children)`; each position is processed once at its newest row (`claimed.pop`); goals are merged with `setdefault` (a position claimed by two parents keeps the goal of the first, newest-first, claim) and `_already_asked(goals)` is evaluated once; targets are appended in row order skipping asked ones (and marking them asked); when `len(batch) >= room` the batch is truncated to `room` and `resume = that task id + 1`. One `_mint(batch)`.
- **Inputs / outputs.** As Q-10.27.
- **Code.** `atomicdb/autosolve.py:drip_backlog:380`.
- **Tests.** `test_autosolve.py::BacklogTests::test_the_walk_buys_the_reported_mates_of_the_past`, `::test_the_children_of_a_closed_parent_wait`, `::test_newest_first`, `::test_a_position_searched_twice_is_read_once`, `::test_nothing_scans_the_tree`, `::test_a_step_costs_the_same_over_ten_searches_or_a_hundred`, `::test_a_step_never_reads_more_than_its_scan`; `test_selector_service.py::RefreshSelectorCommandTests::test_autosolve_cap_is_forwarded_and_reported`.
- **Determinism hazards.** Q-10.27 (files, cache, cadence). The goal used for the "completed same goal" exclusion is the first claim's, so a child claimed with opposite goals by two parents is checked against one goal only.
- **History.** 94c0262 (PR #101).
- **Ordering.** Selector step `autosolve95` (Q-8.22).

---

### Q-11.1 Campaign ownership lives on the position
- **Statement.** Exploration-campaign ownership is `Position.campaign` (FK to `Campaign`, nullable, `SET_NULL`). `AnalysisTask` has no campaign column; a task "belongs" to a campaign only through its position (`position__campaign_id`, read at lease time by part 1 for workers following a campaign, and by the selector's campaign bonus, PRI-1). Inheritance happens only when a row is BORN: `get_or_create_position(fen, campaign_id)` / `positions_for` / `create_positions` put `campaign_id` only in the defaults, so an existing position keeps its owner (or lack of one). Callers that pass the parent's owner: `expand` (every new child gets `pos.campaign_id`, ingest.py:737), `_won_line_chain` (each new node inherits the campaign of the node before it in the line; when the walk passes an existing row, the carried campaign becomes that row's campaign, ingest.py:5276-5283), `_materialise_stepwise` (5312, `node.campaign_id`), `_walk_pv_frontier` (Verify PV, 10074) and `views._materialise_child` (explorer click, 4382). Every other creation path (root bootstrap, FEN box, API request by FEN, proof manager) passes `None`. `ProofCampaign` (proof numbers, dn repair) is a different concept and is not inherited this way.
- **Inputs / outputs.** Writes `Position.campaign_id` at insert.
- **Code.** `atomicdb/ingest.py:get_or_create_position:473`, `_get_or_create_position:492`, `positions_for:542`, `create_positions:567`, `expand:~700-740`, `_won_line_chain:~5250-5285`; `atomicdb/views.py:_materialise_child:4368`.
- **Tests.** **Gaps:** no test in the inventory pins the inheritance (only `test_campaigns.py::ActivationTaggingTests::test_the_walk_stops_at_its_cap` builds a tree with `expand`).
- **Determinism hazards.** First-creator-wins: which campaign owns a transposition depends on which path created the row first (global creation order).
- **History.** Campaigns since b409c30 tier 1 (PR #10); inheritance and tagging 649a019 (PR #31).
- **Ordering.** At position creation (POS-2).

### Q-11.2 Subtree tagging at activation
- **Statement.** `views._tag_campaign_subtree(campaign, cap=CAMPAIGN_TAG_MAX_NODES=50,000, batch=CAMPAIGN_TAG_BATCH=500)`: BFS from `campaign.root_id` over ALL edges (open and closed children), `seen` starting with the root; per level, children of the frontier are read in batches of 500 parent keys; new children are appended in read order until `len(seen) >= cap` (the root counts). Then in batches of 500 keys (BFS order): `Position(key in batch, campaign IS NULL).update(campaign=campaign, updated=now)`. Returns `(tagged, len(seen))`. Positions owned by another campaign are never re-tagged. `updated` is set by hand so the selector's delta mode notices the priority change (PRI-3). Pausing or finishing never untags.
- **Inputs / outputs.** Writes `Position.campaign_id, updated`.
- **Code.** `atomicdb/views.py:CAMPAIGN_TAG_MAX_NODES/BATCH`, `_tag_campaign_subtree:8572`.
- **Tests.** `test_campaigns.py::ActivationTaggingTests::test_the_walk_stops_at_its_cap`; `test_selector_v2.py::MarkingWhatMovedTests::test_adopting_a_subtree_into_a_campaign_marks_the_rows`.
- **Determinism hazards.** Row order: the Edge reads (8591-8594) are unordered, so when the cap is hit, WHICH nodes are tagged depends on DB row order. Wall clock `updated=timezone.now()`.
- **History.** 649a019 2026-07-29 (PR #31); `updated` stamp with selector v2 delta.
- **Ordering.** Runs inside the same transaction as the activation (Q-11.3/Q-11.4).

### Q-11.3 Community rotation of the active campaign
- **Statement.** `community_campaign.rotate()` (run by `manage.py rotate_community_campaign` from `atomicdb-community-campaign.timer`, every 10 minutes), inside one `atomic()`: candidates = campaigns in state PROPOSED/ACTIVE/PAUSED whose `root.status == 'UNKNOWN'`, ordered by `(-votes, id)`; `actives` = candidates in ACTIVE (in that order); `stale` = ACTIVE campaigns not among the candidates (solved root). `leader = candidates[0]`, `incumbent = actives[0]`; `winner = leader`, except `winner = incumbent` when both exist and `leader.votes <= incumbent.votes` (ties keep the incumbent; a challenger needs strictly more votes). Every campaign in `actives[1:] + stale + [incumbent if incumbent != winner]` that is ACTIVE is set PAUSED (`apply_state`). If the winner is not ACTIVE: `apply_state(ACTIVE)` (sets `active=True`, `activated_at=now`) and `_tag_campaign_subtree(winner)`. Summary `{'active', 'activated', 'paused': [names], 'tagged'}` printed as sorted JSON. `Campaign.votes` is a cache recomputed by `_recount_campaign_votes` on each vote (not here). Since 5-Nov the vote, not the owner, decides activation; at most one campaign stays ACTIVE after a rotation.
- **Inputs / outputs.** Reads `Campaign.state, votes, id, root.status`; writes `Campaign.state, active, activated_at`; tags positions.
- **Code.** `atomicdb/community_campaign.py:_candidates:28`, `rotate:36`; `atomicdb/management/commands/rotate_community_campaign.py`; `atomicdb/models.py:Campaign.apply_state:238`.
- **Tests.** `test_community_campaign.py::RotateTests::test_the_most_voted_proposal_becomes_active`, `::test_a_tie_keeps_the_incumbent`, `::test_more_votes_take_over_and_the_old_one_is_paused`, `::test_a_solved_root_never_wins`, `::test_only_one_stays_active`, `::test_the_command_prints_its_summary`.
- **Determinism hazards.** Timer cadence (wall clock): the active campaign changes up to 10 minutes after the votes do. `activated_at=timezone.now()`.
- **History.** f155420 2026-11-05 "the community's most-voted line is the one the auto queue pushes" (PR #174).
- **Ordering.** Independent process; its effect reaches the queue through the priority pass (PRI-1 bonus reads ACTIVE campaigns) and through workers following a campaign id (part 1).

### Q-11.4 Owner campaign actions
- **Statement.** `POST campaign_state` (staff only, else 403): `action=activate` -> in one `atomic()`, `apply_state(ACTIVE)` + `_tag_campaign_subtree`; reply `{'status':'activated','id','tagged','visited'}`; `pause` -> PAUSED; `done` -> DONE; anything else 400 `bad-action`; unknown id 404. The owner can activate several at once; the next rotation (Q-11.3) pauses all but the winner.
- **Inputs / outputs.** Campaign state; position tags.
- **Code.** `atomicdb/views.py:campaign_state:8796`.
- **Tests.** **Gaps:** none in the inventory for the endpoint's transitions (UI tests may exist under NONE).
- **Determinism hazards.** none beyond Q-11.2.
- **History.** 649a019 (PR #31).
- **Ordering.** n/a.

### Q-11.5 A campaign closes when its root closes
- **Statement.** In `_emit_closure_events(pos)` (CAS-7; the single door of the five closure kinds), after `NODE_CLOSED`, provenance and pending-claim resolution and `absorb_tasks(all PENDING tasks of pos)`: every `Campaign(root=pos, state='ACTIVE')` gets `DBEvent('CAMPAIGN_CLOSED', {'campaign': name, 'status': pos.status})` and `apply_state(DONE)`. PROPOSED/PAUSED campaigns on that root are not closed here; rotation simply never picks them again (their root is no longer UNKNOWN). A revoked closure (REV-1) does not reopen a DONE campaign.
- **Inputs / outputs.** Campaign state; DBEvent.
- **Code.** `atomicdb/ingest.py:_emit_closure_events:5800` (lines 5826-5832).
- **Tests.** **Gaps:** no test in the part-6 inventory (may be pinned under CAS-7).
- **Determinism hazards.** none.
- **History.** b409c30 tier 1 (PR #10).
- **Ordering.** Part of the closure event sequence (CAS-7).

### Q-11.6 Where campaigns touch the queue (pointers)
- **Statement.** Three effects, owned elsewhere: (1) the selector's priority adds `CAMPAIGN_BONUS * log1p(votes)` (40 x ...) to positions whose campaign is ACTIVE (`_campaign_bonus`, PRI-1); (2) a worker whose `WorkerServing.campaign` / `ContributorPref.serve_campaign` names a campaign id gets a band key that puts AUTO/FILL tasks on positions of that campaign ahead of other selector work but behind every request (`views.api_lease` 1257-1262, Q-2 part 1; a campaign id that no longer exists matches nothing); (3) `_next_tasks_by_proof` tags proof-campaign descents with `arm='campaign'` (PRI-4; that is a ProofCampaign, not an exploration Campaign).
- **Inputs / outputs.** see owners.
- **Code.** `atomicdb/ingest.py:_campaign_bonus:5876`, `CAMPAIGN_ARM:6907`; `atomicdb/views.py:1257`.
- **Tests.** `test_solver_allocation.py::CampaignMintingTests::test_a_campaign_descent_marks_its_tasks`, `::test_a_normal_descent_is_not_marked`.
- **Determinism hazards.** see owners.
- **History.** worker campaign serving 8-Nov (cz_eclipsia).
- **Ordering.** n/a.

---

## Cross-cutting determinism hazards (summary)

The details are in each rule's "Determinism hazards" field. These are the ones that recur, and what v2 has to decide for each.

1. **Wall clock in the queue rules.** These windows, clocks and throttles all read the time:
   - lease windows: 4 min token recycle, 60 min / 24 h legacy recycle, 3 min restart relief, 20 min solve recycle (`views:1029-1047`, `:1373`, `ingest.py:8903`);
   - service order: 24 h starvation from `created`, 1 h served window, 7-day contributor window (`live_request.py:77,92,131,283`; `lanes.py:56`);
   - ingest backoff 5/30/120/600 s and the 900 s stale claim; propagation backoff (`ingest_queue.py:49-53,78-79,181-183`; `propagation_queue.py:107,174-191`);
   - rolling 1 h arm ceilings counted on `created >= now-1h` (`ingest.py:9166`) and the 10 min receipt dedupe;
   - delta-mode selector: 12 h gap, `updated > since` (`6582-6612`); 30 s per-process throttle;
   - the sweep plan's 2 h `time.monotonic` deadline; the 30 s TB sweep in each writer; the 1 h IP dedup, 1 min receipt window and 20 s place cache on requests.

   v2: record "now" as part of every input fact (lease, heartbeat, submit, request, selector tick), so the replay reads the same time. See OQ-10.
2. **Concurrency and row locks.** `SELECT ... FOR UPDATE SKIP LOCKED` plus READ COMMITTED, with an unlocked read before the lock or the compare-and-set, in:
   - the lease (`views:948-951, 1229-1302`);
   - the ingest claim (`ingest_queue.py:93`), with five writers applying jobs out of id order;
   - the propagation batch (`propagation_queue.py:418`);
   - solve acquire (`views:1975`) and backlog adoption (`backlog.py:730`);
   - submit (`views:1619` then `:1670`) and solve submit (`views:2045` then `ingest.py:8766`).

   Count-then-insert caps can be overshot by concurrent producers. Unique-slot `IntegrityError` races are resolved by whoever commits first. In a single writer, the input order decides all of this.
3. **Batch composition changes values.** It depends on timing (`propagation_queue.py:389-422`) and acts through the noise cut (module docstring `:54-73`). The proof refresh runs on a second thread during the next climb (`:316-354`). This is plan decision D2 (OQ-7).
4. **Row order and missing tie-breaks** where a cap or a tie picks the winner:
   - `next_tasks` orders by `-priority` with no second key (`ingest.py:6846`); `bootstrap_root` uses `.first()`;
   - `_uncertainty_expand:8520` and `_witness_refuted_revisit:8471`;
   - sweep16's unordered Edge reads (`sweep.py:224-240`); campaign tagging at its 50,000-node cap (`views:8572`);
   - the order of the moves in a coverage gap; scans ordered by `-updated` with no tie-break (`:9250`, `:9487`);
   - the edge order of `searchmoves` (`views:605-627`); route maps where the last edge read wins (`_expand_frontier:8025`, `_descend_frontier:8113`, `enqueue_unexplored_children`).

   v2: break ties by id everywhere (D3, OQ-24).
5. **Counters derived from table state.** The pn selector's counter is the highest AnalysisTask id plus the attempt number (`_task_counter:7042`), so any concurrent task creation shifts campaign starts and repertoire buckets (OQ-25).
6. **Caches, files and randomness.**
   - Caches: the sweep16 lease counter lives in the cache, with a `random.random()` fallback; the waiting flag is cached for 60 s (`sweep.py:466-542`).
   - Engine gate: the policy file and the digests of the engines directory, cached per process (`engine_policy.py:42-133`).
   - State-dir files: the sweep plan and cursor, the idle-repair and autosolve backlog cursors, and the selector delta marker, each with a per-process memory copy.
   - Tablebases: files on disk and a module-level handle (`tb.py:76-89`).
   - Random values: lease tokens from `secrets.token_urlsafe(32)`.

   v2: make each of these a logged fact or writer state (OQ-12, OQ-37).
7. **Settings and environment switches** that select branches:
   - selector: `ATOMICDB_SELECTOR`, `ATOMICDB_SELECTOR_V2`, `ATOMICDB_SELECTOR_DELTA`, `ATOMICDB_DESCENT`, `ATOMICDB_INLINE_SELECTOR`;
   - ingest and propagation: `ATOMICDB_SYNCHRONOUS_INGEST`, `ATOMICDB_DEFER_PROPAGATION`, `ATOMICDB_PROOF_AFTER_COMMIT`, `ATOMICDB_PROOF_PIPELINE`;
   - arms: `ATOMICDB_ADVERSARIAL`, `ATOMICDB_ARM_RATE_*`, `ATOMICDB_SWEEP16_SHARE`, `ATOMICDB_BREADTH_SWAP`, `ATOMICDB_AUTO_INTERIOR_DEEP_RUNGS`;
   - tablebases and trust: `ATOMICDB_TB_SERVER_MAX_MEN`, `ATOMICDB_TB_TRUSTED`, `ATOMICDB_CLOSURE_TRUST_GATE`;
   - engine gate: `ATOMICDB_ENGINE_POLICY_FILE`, `ATOMICDB_ENGINES_DIR`;
   - `ATOMICDB_STATE_DIR`, plus the selector's command-line caps.

   Their production values must be captured before golden recording (OQ-28).
8. **Work created inside the HTTP lease.** When the queue is empty, `api_lease` mints AUTO tasks in its own transaction (`views:1407-1412`). These rows are deduplicated only by the unique constraint.

---

## Open questions for the owner

The port cannot settle these questions on its own. Each one has options and a recommendation. Plan decisions (D1..D11, `v2-plan.md` section 11) are named where they apply.

### Defects and safety (decide first)

**OQ-1 Double apply of an ingest job after its commit** (Q-3.7, Q-3.16; `ingest_queue.py:342, 359-380, 421-425`).
- Options:
  - (a) wrap the post-commit steps so their failures never reach `_mark_failure`;
  - (b) make `_mark_failure` conditional on `state != DONE`;
  - (c) both.
- **Recommendation:** (c), fixed in v1 now. v2 applies every search exactly once. The shadow should list the jobs v1 applied twice as known differences. They can be found from repeated `summary`/`propagated` rows, or from jobs with `attempts > 0` that are DONE.

**OQ-2 Poison jobs** (Q-3.4). A stale reclaim (900 s) does not count as an attempt, so a job that kills its processor is retried every 15 minutes for ever.
- Options: count reclaims as attempts and park after 5, or keep.
- **Recommendation:** count them. v2 quarantines a payload that fails to apply deterministically, and reports it.

**OQ-17 Solve submit has no ownership check under the lock** (Q-2.59, Q-2.61; `views:2044-2056` vs `ingest.py:8766`).
- Options: keep, or fence on `state = LEASED` plus machine plus token at apply time, as analysis does.
- **Recommendation:** fence. Certificates are verified on the server either way, so what changes is attribution and the "dup" answer a later holder gets.

**OQ-16 Lock artefacts in the lease** (Q-2.20, Q-2.21). Rows locked and then skipped stay invisible to concurrent leases until the commit. A slot that calls twice at once can hold two leases.
- **Recommendation:** v2 defines the selection as "the first eligible row in the total order". It does not reproduce either effect, and the golden tests exclude them.

### Wire and protocol compatibility

**OQ-9 Workers built before 2026072203, without lease tokens** (Q-2.6, Q-2.23, Q-2.33). Heartbeat accepts any token for them, and submit skips the token check. This is plan decision D8.
- **Recommendation:** drop them at cut-over, once no tokenless row is LEASED. This removes the legacy windows (60 min / 24 h) and the legacy-build filter.

**OQ-19 Synchronous-ingest reply shape** (Q-2.46).
- Options: keep both reply shapes, or keep only the queued shape `{"queued": true, "ingest_job": ..., "submit_timing_seconds": ...}`.
- **Recommendation:** keep only the queued shape, which is what production uses. Exclude the timing fields from the golden comparisons.

**OQ-20 Solve submit answers "dup" before checking machine or token** (`views:2048`), so any authenticated account gets "dup" for any completed id.
- **Recommendation:** keep it, for wire compatibility. It is harmless.

**OQ-14 Order of `searchmoves`, and the empty case** (Q-2.29.1). The edge order is not defined. When every child is closed, no restriction is sent at all.
- **Recommendation:** sort by edge id (D3). Owner's call on the empty case: options are (a) send no searchmoves, as v1 does, or (b) not lease the task. Recommend (a), for parity.

### Replay and determinism

**OQ-10 Inputs from the outside world.** These are wall-clock windows (OQ list above), the engine-gate verdict (it depends on the policy file and the engines directory at submit time), TB probes, and random tokens.
- Options: re-evaluate them during replay, or record them in the input fact.
- **Recommendation:** record them. The fact carries `now`, the gate verdict, the TB result used, and the token. This is the precondition of plan decision D1.

**OQ-7 Values depend on batch size** (Q-4.21). The noise cut, the order of single seeds against batch seeds, and a later stage (a) running before an earlier climb all change values.
- **Recommendation:** propagate exactly (D2) once the exact port passes. Until then, the shadow compares values after the tree has come to rest, not raw v1 rows.

**OQ-24 Tie-breaks.** Every unordered `.first()`, every capped scan without `order_by`, `-priority` with no second key, and every edge map where the last edge read wins.
- **Recommendation:** break ties by id or key everywhere (D3). List every site in the golden-test exclusions until v1 is changed the same way.

**OQ-25 The pn selector's counter** (PRI-4.4, `_task_counter:7042`). It is the highest task id plus the attempt number.
- **Recommendation:** use the log sequence number in v2. The AUTO queues will then differ from v1's; accept that, and list it.

**OQ-12 Sweep16 share state** (Q-10.9, Q-10.10, Q-2.28.1). The lease counter lives in the cache, with a random fallback. And when one capped or non-tablebase worker finds no sweep task it can take, `note_waiting(False)` turns the share off for the whole fleet for 60 s.
- **Recommendation:** keep the counter in writer state. Write "not waiting" only when `waiting_tasks()` is really empty.

**OQ-37 State files with a per-process memory copy** (Q-10.4, Q-10.8, Q-10.27, PRI-3.5). These are the sweep plan and cursor, the backlog cursors and the delta marker. A rewind made from another process is not seen by a running selector.
- **Recommendation:** in v2 they become logged facts or writer state.

**OQ-23 v2 bounded-regret approximation at batch boundaries** (PRI-2.4).
- **Recommendation:** moot if OQ-22 drops the regret selector. If it is kept, port v1's global Dijkstra semantics exactly; v2-bounded is off by default and never shipped.

### Semantics the owner should rule on (port exactly first, then change only as listed decisions)

**OQ-22 The regret selector and the `priority` column** (PRI-1..PRI-3). Production runs `pn` with `--no-priorities`.
- Options:
  - (a) port v1 and v2 exactly;
  - (b) port pn only, and keep `priority` as a dead/alive flag;
  - (c) compute it in memory (D5).
- **Recommendation:** (b), with a boolean tombstone. Port regret only if the owner wants a rollback path.

**OQ-26 Tombstone revival** (PRI-1.7). Some revivals set `updated` and some do not, and all of them use the sentinel 0.0.
- **Recommendation:** a boolean dead flag with explicit revive events.

**OQ-27 `bootstrap_root` budget** (PRI-4.10). The docstring says 512M; the code buys 2B. Nothing in production calls it.
- **Recommendation:** drop it, or confirm 2B.

**OQ-29 Value cascade on a position its own landing just closed** (PRI-4.9). The cascade reads `pos.status` from before the closure, so the closed position still buys its replies.
- **Recommendation:** read the status after the cascade. This is a listed, owner-approved fix.

**OQ-30 System callers lower the band of a person's request.**
- `_uncertainty_expand` calls `enqueue_unexplored_children` with `source=AUTO`, which rewrites a PENDING USER row to AUTO (`ingest.py:9710`).
- The quality raise sets any PENDING row of the slot to FILL, USER rows included (`:4221`, `:4295`).
- **Recommendation:** never lower a band; only raise it (USER > FILL > AUTO). Fix it in v1 first; the code already calls it a "degrade".

**OQ-21 FAILED SolveTasks** (Q-2.61, Q-8.7, Q-8.14). The endpoints never retry them, and solve leases have no attempts cap. But the debt and fragile arms buy the same position again on every pass, up to their caps. Autosolve never buys it again.
- Options:
  - (a) FAILED is terminal for every arm;
  - (b) keep;
  - (c) allow a fixed number of retries.
- **Recommendation:** (a), as autosolve already does.

**OQ-39 SolveTask has no uniqueness constraint** (Q-1.17, SOL-4.4). Two processes can buy the same autosolve pass.
- **Recommendation:** in v2, dedupe open rows by (position, arm, goal, stage).

**OQ-4 Live absorption and backfill absorption differ** (Q-3.13, Q-3.25). Live absorption takes any PENDING task with budget <= the arbiter budget. The hourly backfill spares tasks created after the covering search finished.
- **Recommendation:** port both exactly, then align on the creation cut. The cut matters for the +16 sweep's re-buys.

**OQ-5 Mate stops from untrusted accounts** (Q-3.12). An untrusted mate is stored only as a pending claim, yet it arbitrates and absorbs requests at the full task budget.
- **Recommendation:** for an untrusted, unproven mate, use the searched nodes.

**OQ-6 Rejected tablebase results** (Q-3.11, Q-3.17). They still notify the requester and consume the task.
- **Recommendation:** no notice; put the request back in the queue.

**OQ-8 Closures made by a deferred climb carry no provenance** (Q-4.7). Task, account and engine are not stored on the seed.
- **Recommendation:** in v2, the provenance travels with the input fact that caused the closure.

**OQ-18 Release absorbs globally** (Q-2.50, Q-2.8). `_absorb_requeued` absorbs every requeued row on a closed position, not only the rows being released.
- **Recommendation:** absorb per row in the transitions that requeue (gate refusal, release, recycler), and keep as an invariant that no PENDING task sits on a closed position.

**OQ-11 Starvation ages from `created`; the display ages from `requested_at`** (Q-2.17, Q-5.7). A revived anonymous row older than 24 h jumps straight into the named stratum.
- **Recommendation:** port as is, then switch to `requested_at` as a listed decision.

**OQ-13 A personal worker following a campaign can take a stranger's USER request on a campaign position** (Q-2.14, Q-2.15).
- **Recommendation:** port as is, then ask: should the campaign filter be limited to AUTO/FILL?

**OQ-15 The deep-lease cap is keyed by `requested_by`, while the fair share is keyed by the lane account** (Q-2.24).
- **Recommendation:** port as is. Unifying the two on the lane account is a candidate for later.

**OQ-31 Stale fields on revived rows** (Q-6.8). `_revive_withdrawn` keeps the old `lane_account`, `then_verify` and `arm`.
- **Recommendation:** reset all three on revive.

**OQ-32 Undo bypasses the per-account cap** (Q-6.21), and it does not recheck the generation.
- **Recommendation:** check the cap on undo. Leave the stale generation alone.

**OQ-33 "Already searched" has two meanings** (Q-6.2, Q-6.5, Q-7.6, Q-7.9). The chosen rung and the breadth swap count bought rungs, absorbed rows included. `only_if_missing` and the subtree walk count only searches that ran.
- **Recommendation:** port exactly, then unify on "searches that ran".

**OQ-34 `multipv` is not recomputed when a pending request's budget is raised** (Q-6.9).
- **Recommendation:** port exactly. Decide later whether to recompute it with `multipv_for`.

**OQ-35 A backer is lost on the follow-up path** (Q-6.10). When a deeper request lands on an existing follow-up generation owned by someone else, the second person is not recorded.
- **Recommendation:** call `add_requester` there.

**OQ-36 Requests change other people's rows** (Q-6.13, Q-6.26). `then_verify` and the background lift touch every live USER row of the position.
- **Recommendation:** restrict them to rows the caller authors or backs.

**OQ-38 A bulk click skips an absorbed slot without saying so** (Q-8.20). The single-request path revives it.
- **Recommendation:** revive it, so both paths behave the same.

**OQ-3 Seeds never park** (Q-4.13).
- **Recommendation:** moot in v2, which has no seed queue. In the shadow, treat v1 seeds that keep failing as known lag.

### Inputs only the owner can provide

**OQ-28 Production values that are not in the repo:** `ATOMICDB_DESCENT`, `ATOMICDB_ARM_RATE_*`, `ATOMICDB_BREADTH_SWAP`, `ATOMICDB_SWEEP16_SHARE`, `ATOMICDB_TB_SERVER_MAX_MEN`, `ATOMICDB_CLOSURE_TRUST_GATE`, `ATOMICDB_SELECTOR_*`, the engine policy file, the crontab (`absorb_shadowed_tasks` hourly, backlog subscriptions), and the number of ingest writers. Pool target: 64 in production, 512 in the code.
- **Recommendation:** snapshot the production environment, the unit drop-ins and the crontab before recording golden cases. Pin them in the v2 config. Make 64 the v2 default only if the owner confirms.
