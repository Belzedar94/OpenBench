# AtomicDB v1 rules: the specification for the v2 port

Status: **reference for phase 2 (rules) and phase 3 (stage a) of
[`docs/atomicdb/v2-plan.md`](../../docs/atomicdb/v2-plan.md)**. Written
28-Nov-2026, read-only on v1. Nothing here changes v1.

The plan says the port reproduces about forty value rules exactly before
anybody simplifies one (section 7), and that the owner decides every
simplification (D1 to D11, section 11). This document is the list of those
rules, with enough detail to write each one again without reading the Python,
and with what pins it, what makes it non-deterministic, what broke it before,
and what it must run after.

## Where the rules are

| File | Families | Rules |
|---|---|---|
| this file, `rules-spec.md` | the landing pipeline, positions, expansion and showcase (POS, EXP), claims (CLM), status cascade and closures (CAS), mate distance (DTM), backed values (BKD), loops and repetition (LOOP), revocation and trust (REV), values served but not stored (VIEW); plus the determinism register, production drift, defects found, port order and questions for the owner | 104 |
| [`rules-spec-proofs.md`](rules-spec-proofs.md) | proof numbers (PN), mate-PV checks and the online AND/OR proof (MPV), the exact layer of `logic.py` (terminal status, three-valued minimax, clock slack), tablebases (TB), solver and survival certificates (SOL), commands that rewrite proofs (CMD), score bands (SB) | 144 |
| [`rules-spec-queue.md`](rules-spec-queue.md) | the task model and lease protocol, ingest and propagation queues, priority and selector, requests, lanes and fairness, background arms (Q, PRI, SOL-4, SOL-5) | 312 |
| [`test-map.csv`](test-map.csv) | every one of the 4,412 test methods in `atomicdb/test_*.py` and `atomicdb/tests.py`, with the rule IDs it exercises and whether it can run against v2 through the shim | |

The three files share one ID scheme and one section format. Where two
families meet (for example the trust gate, which is MPV-2.8 at the line and
REV-2 at the account), each file names the other.

**Source.** `atomicdb/` at `origin/spell-runner` `f59413e` (the merge of
PR #333). `origin/atomicdb-v2` (`1c4dc43`) is that commit plus the plan, and
`git diff origin/spell-runner origin/atomicdb-v2 -- atomicdb` is empty, so the
line numbers below hold on both branches. Production differs from it in one
semantic constant (section 14).

## How to read a rule

Every rule has a stable ID: the family, the provisional number the test map
uses, and a sub-number (`BKD-5.3` is the third rule of the `BKD-5` group;
`test-map.csv` tags tests with the group, `BKD-5`). Each section has:

- **Statement.** The rule in plain words, precise enough to write again:
  constants with their values, tie-breaks, edge cases.
- **Inputs / outputs.** The fields read and written (`Position.backed_eval`),
  events (`DBEvent kind`), tasks created.
- **Code.** `atomicdb/<file>.py:<function>:<line>`.
- **Tests.** `file::Class::test` that pin it, chosen from `test-map.csv`
  (portable first; the count is the size of the group there). **Gaps:**
  behaviour no test pins.
- **Determinism.** Dependence on the wall clock, row order, set or dict
  iteration order, randomness, database isolation, process state or settings.
  These are what break replay; the plan's D1 (step budgets instead of
  seconds) and D3 (ties by id) are about them. "None" when there is none.
- **History.** The PRs and commits that made the rule what it is, and the
  production bugs behind them.
- **Ordering.** What must run before and after it.

Test references are `file::Class::test_name` under `atomicdb/`.

---

# 1. Conventions every rule shares

## 1.1 Values and signs

- Every stored number is **from White's point of view**: `eval_cp`,
  `backed_eval`, the `eval_cp` of each line of `last_analysis`, the `mate` of a
  line (positive: White mates). Inside `ingest.py` there is no sign change; the
  one flip to the side to move happens when a page or the API renders a row
  (VIEW-2).
- The side to move is the second field of the FEN (`fen.split()[1] == 'w'`).
  "Mover-win" is `WHITE_WIN` when White is to move, `BLACK_WIN` otherwise.
- `_better_for_mover(value, ref, stm_white)` is `value > ref` for White and
  `value < ref` for Black (strict).
- A decided status has a truth value, `_status_eval`: `WHITE_WIN` = +10,000,
  `BLACK_WIN` = -10,000, `DRAW` = 0, `UNKNOWN` = none (`ingest.py:1984`).
- Score bands (SB in the proofs file): ordinary centipawns below 9,000; huge
  engine evals compressed into 9,000..9,249; a tablebase win stored as
  9,500 minus plies to the tables; an engine mate in N moves as
  `10,000 - N`. `MATE_BAND = 9000` (`ingest.py:94`), `TB_CLAMP_CEILING = 9500`.
- `mate_in` counts **plies** (half-moves): the length of the stored witness
  `won_line`, 0 for a terminal position. Engine mates speak in moves; the one
  conversion is `ingest._mate_moves_to_plies` (proofs file, EXP-4.1).

## 1.2 Statuses, closures, proof grades

| Field | Values | Meaning |
|---|---|---|
| `status` | `UNKNOWN`, `WHITE_WIN`, `BLACK_WIN`, `DRAW` | exact verdict, or open |
| `closure` | `TERMINAL`, `MATE_PV`, `MINIMAX`, `TB`, `SOLVE`, NULL | how the verdict was reached |
| `proof` | `ANDOR`, `ENGINE`, `DISPUTED`, `TB_LINE`, NULL | how strong the evidence is |

- A closure is **verified** (`_child_is_verified`, `ingest.py:5386`) when its
  closure is `TERMINAL` or `TB`, or its proof is `ANDOR`.
- A closure is **independent of its children** (`_closure_is_independent`,
  `ingest.py:5420`) when its closure is `TERMINAL` or `TB`, or its closure is
  not `MINIMAX` and its proof is `ANDOR`. Independent closures are never
  revoked by a child and cut the revocation cascade (REV-1).
- Evals never close a position. Only five doors close: a terminal position
  (POS-4), a verified or trusted mate line (MPV-2.6), a minimax over closed
  children (CAS-1), a tablebase (TB-2), a replayed solver certificate (SOL-2).

## 1.3 The rows the rules read and write

`Position` (`models.py:39`), primary key `key` = sha256 hex of the canonical
FEN (POS-1):

| Field | Written by | Read by |
|---|---|---|
| `fen` | creation (POS-2) | everything |
| `status`, `closure`, `proof`, `won_line`, `mate_in`, `clock_slack` | closures (CAS, MPV, TB, SOL), revocation (REV-1) | everything |
| `best_move` | landing (EXP-2.6), cascade witness and distance (CAS-1.3, DTM-1) | backed tie-break (BKD-6), views, mate distance support (DTM-3) |
| `eval_cp` | landing (EXP-2.6), claims (CLM-1) | backed (BKD-1), priority, views |
| `nodes_invested`, `visits`, `time_invested` | landing (EXP-2.2), `apply_job` | backed quality (BKD-1, BKD-5), claims guard (CLM-1), views |
| `last_analysis`, `analysis_passes` | landing (EXP-2.3, EXP-2.4) | showcase bound (BKD-4), claims (CLM-2), views |
| `backed_eval`, `backed_move`, `backed_plies`, `backed_nodes` | the backed ascent only (BKD-7) | backed (BKD-1), loops (LOOP), views, value descent |
| `expanded` | `expand` (EXP-1) | cascade (CAS-1), coverage (BKD-4) |
| `reachable`, `priority`, `campaign_id` | creation, `expand`, revocation, selector | selector (queue file) |
| `updated` | `auto_now` on `save()`, and by hand on some `update()`s | selector delta pass (queue file), sweeps |

`Edge` (`models.py:168`): `id` (autoincrement), `parent`, `move_uci`, `child`;
unique `(parent, move_uci)`; index on `child`. Two moves of one parent may
lead to the same child (two pieces capturing on one square both explode).
Edges are never deleted, and no rule in this file writes an edge except
`expand`, `link_existing_children`, the won-line chain and the explorer's
click (POS-3, EXP-1, MPV-5).

Other tables written by these rules: `DBEvent(kind, payload)`,
`PendingClaim`, `ClosureProvenance`, `AnalysisTask` (absorption and
purchases), `Campaign` (state on root closure), `PropagationSeed`.

## 1.4 Constants of this file

| Constant | Value | Rule |
|---|---|---|
| `STORED_PV_MAX_PLIES` | 24 | EXP-2.3 |
| `PASS_HISTORY_MAX` | 6 | EXP-2.4 |
| `CASCADE_GUARD_LIMIT` | 100,000 pops | CAS-5.1 |
| `CASCADE_REWRITE_LIMIT` | 32 writes per position per call | CAS-5.2 |
| `CASCADE_WAVE_KEYS`, `CASCADE_CACHE_ROWS` | 512, 200,000 | CAS-1.5 (no semantic effect) |
| `MATE_SUPPORT_HOPS` | 64 | DTM-3 |
| `BACKED_MAX_PLIES` | 64 levels | BKD-7.1 |
| `BACKED_MAX_REVISITS` | 64 (= levels) | BKD-7.4 |
| `BACKED_MAX_NODES` | 50,000 rows processed | BKD-7.5 |
| `BACKED_EPSILON_CP` | 10 | BKD-8.1 |
| `BACKED_QUALITY_TOLERANCE` | 0.5 | BKD-5.3 |
| `PROVEN_QUALITY` | 2^60 | BKD-1.1 |
| `BACKED_ECHO_PLIES` | 256 (= 4 x 64) | BKD-1.3 |
| `BACKED_CYCLE_MAX_PLIES` | 32 | LOOP-1, LOOP-2, LOOP-3 |
| `BACKED_CYCLE_MAX_NODES` | 64 re-reads per returning child | LOOP-5.1 |
| `BACKED_CYCLE_MAX_SECONDS` | 1.0 s per top-level valuation | LOOP-5.1 |
| `BACKED_LOOP_MAX_SECONDS` | 1.0 s per ascent | LOOP-5.1 |
| `BACKED_GUARD_KEYS` | 2,000 keys in an event | BKD-9.1 |
| `CARRY_MAX_KEYS` | 64 | BKD-9.2 |
| `BACKED_RETRY_SLEEPS` | 1, 3, 9 s | BKD-13 |
| `RECHECK_MAX_KEYS` | 16 | CAS-6.1 |
| `RECHECK_LOCK_TIMEOUT_MS`, `RECHECK_RETRY_SLEEPS` | 200 ms; 0.5, 2 s | CAS-6 |
| `CLIMB_RECHECK_MAX_KEYS` | 256 | CAS-6.4 |
| `REVOKE_GUARD_LIMIT` | 100,000 | REV-1.1 |
| `RUNG_OVERSHOOT_TOLERANCE` | 0.01 | BKD-11.2 |
| `WON_LINE_MAX_PLIES` | 64 | MPV-5 (proofs file) |
| `ONLINE_MATE_PROOF_SECONDS` | 2.5 s in the repo, **1.0 s in production** | MPV-2.5 (proofs file), section 14 |

Settings that switch rules (all default on): `ATOMICDB_CASCADE_BULK_READS`
(CAS-1.5), `ATOMICDB_FRESH_CONTEXT` (CAS-1.2), `ATOMICDB_CLOSURE_TRUST_GATE`
(REV-2), `ATOMICDB_LINK_CHILDREN_AT_CREATION` (POS-3),
`ATOMICDB_DEFER_PROPAGATION` (EXP-6, queue file Q-4). The queue file lists
the rest.

---

# 2. The landing pipeline: who runs what, in which order

Most rules are cheap functions of rows; what makes v1 hard to reproduce is the
order in which they run and which rows each one has already seen. This is that
order. Every arrow is a call in the same process; "commit" is a database
commit.

## 2.1 A search lands (`ingest_queue.apply_job`, `ingest_queue.py:240`)

Outside any transaction (the "preparers" of the plan):

1. If the payload carries a worker tablebase result (`tb_wdl`):
   `prepare_tb_closure` (TB-2.2). A rejection ends the job with
   `tb_rejected` and no change to the tree.
2. Otherwise `prepare_server_tb_closure` (TB-2.3): a position the server's own
   tables cover closes on them, whatever the worker sent.
3. Otherwise normalise every line to the stored scale (SB-6) and run
   `prepare_mate_proofs` (MPV-2.4): for each line that claims a mate (or a
   line into a won table, TB-3.1), check it move by move and try the
   exhaustive proof, on the wall-clock allowance `ONLINE_MATE_PROOF_SECONDS`.
4. Read the task: `source` (closure attribution, CAS-4), the delivering
   account (`delivered_by`, else the payload's `username`), `engine_sha`,
   `budget_nodes`, `then_verify`. `trusted = account_trusted(account)` (REV-2).

In one transaction, under `closure_attribution(source, provenance)` and
`carried_cuts()`:

5. Lock the `IngestJob`; if it is already `DONE`, return its summary.
6. TB result: `_apply_prepared_tb` (TB-2.4). Otherwise
   `ingest_analysis(..., arbiter_budget=arbiter_budget(lines, searched,
   task.budget_nodes))` (EXP-2, EXP-5.1), then `_absorb_shadowed(job,
   arbiter)` (EXP-5.2).
7. `time_invested += elapsed` unless the result was rejected.
8. The job becomes `DONE`; the requester is notified.

After the commit:

9. If the climb was not deferred: `proof.refresh_proof_numbers([key])`
   (PN-8.4). **Known defect:** an exception here reaches `process_job`, whose
   `_mark_failure` rewrites the committed `DONE` to `PENDING`, and the job is
   applied a second time (section 13.1).
10. `carry_cut_ascents(cut)` when an ascent was cut by the ply guard (BKD-9.2).
11. `then_verify` (and not deferred): `enqueue_pv_verification` in its own
    transaction (MPV-6.6).

## 2.2 Inside `ingest_analysis` (`ingest.py:984`): stage (a)

1. If `mate_proofs` was not prepared by the caller: re-read the row; closed
   means `{'skipped': 'already-closed'}`; otherwise normalise and prepare
   here.
2. `arbiter = max(nodes_budget, arbiter_budget)` (or `nodes_budget`).
3. Lock the position (`select_for_update`). Closed: `already-closed`, nothing
   written.
4. `expand(pos)` (EXP-1). Normalise the lines again (idempotent).
5. `seed_arbitrates` from the stored showcase (CLM-2).
6. For each line, in line order (EXP-2.1): seed the child's eval (CLM-1);
   revoke a refuted closed witness (MPV-3.1 -> REV-1); adopt a certificate on
   a closed child (MPV-4.1); dispute an unproven line on an open child
   (MPV-2.7); keep an untrusted unproven line as a pending claim (CLM-4,
   REV-2); otherwise close the child by its line (MPV-2.6) and materialise
   the won line (MPV-5); track the best eval of the pass (EXP-2.1).
7. Visits, nodes, pass history, showcase arbitration, restricted and
   shallower guards, `best_move` and `eval_cp` (EXP-2.2 to EXP-2.7), saved
   with explicit fields.
8. Build the `Landing` (EXP-6.1): cascade seeds, backed seeds, parents,
   audience, closure source.
9. Stage (b) now (`propagate_landings([landing])`) or later (a
   `PropagationSeed` row in the same transaction, drained by
   `propagation_queue.drain_batch`, queue file Q-4).

## 2.3 Stage (b): `propagate_landings` (`ingest.py:1397`)

For one landing, or for a drained batch of landings with one closure source:

1. Seeds: one landing keeps its lists in order; a batch merges each list
   without duplicates **in key order** (EXP-6.2).
2. `backup_cascade(cascade_seeds)` (CAS-1), which ends with the proof-number
   refresh of `seeds + changed` (PN-8.1), or hands that call to the batch's
   after-commit sink (PN-8.2).
3. `backup_backed_evals(backed_seeds)` (BKD-7), which buys quality
   convergence and cycle disambiguation as it goes (BKD-11).
4. For each landing, in landing order: `_uncertainty_expand`,
   `_witness_refuted_revisit`, `_queue_value_cascade`,
   `idle_repair.queue_from_landing`, `autosolve.queue_from_landing` (queue
   file and SOL-4), then register `recheck_after_commit` (CAS-6.1).
5. Register `recheck_meeting_points` for the climb (CAS-6.4).

After the commit of stage (b): the registered rechecks run, in registration
order; in a batch the proof refreshes collected by the sink run first
(`propagation_queue._after_commit`, queue file Q-4).

## 2.4 Other doors into the same rules

| Door | Rules it runs | Code |
|---|---|---|
| Tablebase landing | TB-2.4, then CAS-1 and BKD-7 from the position, recheck | `ingest.py:_apply_prepared_tb:10387` |
| Solver certificate | SOL-2.x, then CAS-1, BKD-7 after its commit | `ingest.py:apply_solve_result:8723` |
| Revocation | REV-1, then CAS-1 from the revoked keys | `ingest.py:revoke_closure:5508` |
| A click on a move (`goto`, request of a move) | POS-2/POS-3, then CAS-1 + BKD-7 on the parent if the child is closed | `views.py:_materialise_child:4368` |
| Opening a node in the explorer (a GET) | POS-3 relink, CAS-1 + BKD-7 if a linked child is closed | `views.py:_relink_on_view:5860` |
| The proven-line jump (a GET) | MPV-5, then CAS-1 + BKD-7 from what it closed | `views.py:_walk_proven_line:4624` |
| A FEN or opening jump, the public request API | POS-2/POS-3 and the same climb | `views.py:fen_jump:7465`, `_opening_jump:7422`, `api_public_request:3333`, `_request_children:3720` |
| `sweep_stale_backed` (every 15 min) | CAS-2, CAS-6.3, BKD-12 | `management/commands/sweep_stale_backed.py` |
| `recascade_backed`, `recascade_status`, `backfill_mate_distance`, `verify_mates`, `recertify_mates`, `recompute_tb_distance` | the same functions over chosen keys | management commands; CMD in the proofs file |

A page view can therefore close positions and rewrite backed values: v2 must
treat those doors as inputs to the log, not as reads.

## 2.5 What depends on this order

- The cascade runs before the backed ascent: the ascent reads the statuses the
  cascade wrote (proven children enter with their truth value, BKD-1.1), and
  `open_only` ascents skip what the cascade closed.
- Proof numbers run after the cascade (they read `status`) and never feed
  back into values or closures (PN-8.1).
- The arms of step 2.3.4 read the row as stage (a) left it (the drainer passes
  rows read before the climb), not as the climb left it.
- The rechecks run after the commit and read what other transactions
  committed meanwhile. In v2's single writer there is nothing to recheck; the
  rechecks are kept in this document because they decide which value v1
  converges to when two landings race (CAS-6), and the golden cases will
  contain their effects.

---

# 3. Positions and their identity (POS)

### POS-1.1 Canonical FEN and key
- **Statement.** A position is identified by its canonical FEN: board, side to move, castling rights, and an en-passant square **only when an en-passant capture is actually executable** (a right that cannot be used, including one whose capture would explode the capturer's own king or is pinned, is dropped), with the counters replaced by `0 1`. Its key is the lowercase hex sha256 of that string. The counters are erased on purpose: the graph cannot tell two occurrences of a position apart (value-semantics invariant 6), which is why repetition lives in walks (LOOP) and the 50-move rule in `clock_slack` (DTM-5).
- **Inputs / outputs.** In: any FEN. Out: `Position.fen`, `Position.key`.
- **Code.** `atomicdb/logic.py:canonical_fen:167`, `logic.py:key_of:188`, `logic.py:en_passant_changes_the_game:113`, `logic.py:_en_passant_right_exists:147`; `ingest.py:root_key:462` (cached key of the start position).
- **Tests.** `test_en_passant_identity.py::PhantomIsNotIdentityTests::test_a_capture_that_explodes_its_own_king_leaves_no_right`, `test_en_passant_identity.py::IdentityOfEverythingElseTests::test_a_legitimate_right_keeps_its_pre_v2_key`, `test_en_passant_identity.py::IngestUsesTheNewIdentityTests::test_expanding_a_parent_stores_the_child_without_the_phantom`, `test_clock_slack.py::EnPassantAuditTests::test_an_atomic_self_explosion_still_makes_it_emit_one` (52 tests in POS-1). **Gaps:** none found for the identity itself.
- **Determinism.** Depends on the Fairy-Stockfish build (`pyffish`) for legality of the en-passant capture. v2 links the same library (plan section 6).
- **History.** En-passant identity v2 (keys of positions with a phantom right changed; `rekey_en_passant` command). The plan keeps sha256 of the canonical FEN as the public identity.
- **Ordering.** Before everything: every other rule is keyed by it.

### POS-2.1 Creating a position
- **Statement.** A position is created only through `_get_or_create_position` (one row) or `positions_for` / `create_positions` (many). The row gets the canonical FEN, the `campaign_id` it is given (only when it is born; an existing row keeps its owner), and `reachable = True` when it is the start position or when the caller says `born_reachable`. If `logic.terminal_status(fen)` decides it (POS-4.1), it is born closed: `status` = the result, `closure = 'TERMINAL'`, `mate_in = 0`, and `clock_slack = CLOCK_SLACK_MAX` (100) unless the result is `DRAW` (slack NULL). A terminal position born this way emits **no** `NODE_CLOSED` event and absorbs nothing (contrast CAS-7.1). The start position row of an older base that lacks `reachable` is corrected to `True` whenever it is read through these functions.
- **Inputs / outputs.** Writes a `Position` row (`key`, `fen`, `campaign_id`, `reachable`, and for terminals `status`, `closure`, `mate_in`, `clock_slack`). Bulk inserts use `ON CONFLICT DO NOTHING` in key order; a row a concurrent writer inserted is returned as that writer left it.
- **Code.** `atomicdb/ingest.py:_get_or_create_position:492`, `ingest.py:get_or_create_position:473`, `ingest.py:positions_for:542`, `ingest.py:existing_positions:556`, `ingest.py:create_positions:567`.
- **Tests.** `test_ingest_batched_writes.py::ExpandTests::test_terminal_children_are_born_closed_as_before`, `test_clock_slack.py::ClosureSlackTests::test_a_terminal_closure_is_born_with_the_maximum`, `test_clock_slack.py::ClosureSlackTests::test_a_terminal_draw_carries_no_slack`, `test_selector_v2.py::ReachablePropagationTests::test_a_new_position_is_not_reachable_just_for_existing`, `test_selector_v2.py::ReachablePropagationTests::test_a_pre_existing_root_gets_the_mark_on_the_next_pass`, `test_campaigns.py::InheritanceTests::test_a_child_that_already_existed_keeps_having_no_owner` (24 in POS-2). **Gaps:** nothing pins that a terminal born by creation emits no `NODE_CLOSED` (a v2 port that emits one would change the "closures in 24h" counters).
- **Determinism.** Concurrent creation: the second writer gets the first one's row. None in v2's single writer.
- **History.** Batched in #330 (27-Nov), results identical to the per-row path; terminal-at-birth since the first commit `b409c30`.
- **Ordering.** Before EXP-1 and every click path (POS-3).

### POS-3.1 Linking a node born outside `expand` to the children that already exist
- **Statement.** A node created by a click, the PV walk, a FEN jump, an opening jump or the public request API (any path except `expand`) is linked at birth to the children that already exist: one movegen, one lookup of all child keys, one bulk insert of `(parent, move_uci, child)` for each legal move whose child exists (two moves to the same child give two edges), `ignore_conflicts`. Nothing is created that did not exist; the node stays `expanded = False`. A linked child whose `priority <= DEAD/2` (a tombstone, `DEAD = -1e9`) is revived to `priority = 0.0` (without touching `updated`). Returns the keys of the linked children whose status is not `UNKNOWN`; when that list is non-empty, the caller runs `settle_linked_children(pos, parent_keys)`: `backup_cascade([pos])`, then `backup_backed_evals([pos, *parent_keys])`, then re-reads `pos`. A node that is closed or already expanded is left alone. Off when `ATOMICDB_LINK_CHILDREN_AT_CREATION = False`.
- **Inputs / outputs.** Reads child `key`, `status`, `priority`; writes `Edge` rows and child `priority`. Climb as in CAS-1 and BKD-7.
- **Code.** `atomicdb/ingest.py:link_existing_children:630`, `ingest.py:get_or_create_position_linked:676`, `ingest.py:settle_linked_children:688`; callers in `views.py:_materialise_child:4368`, `views.py:_relink_on_view:5860`, `views.py:fen_jump:7465`, `views.py:_opening_jump:7422`, `views.py:api_public_request:3333`, `views.py:_request_children:3720`.
- **Tests.** `test_link_children_at_creation.py::LinkedAtBirthTests::test_a_new_route_revives_a_child_from_the_tombstone`, `test_link_children_at_creation.py::LinkedAtBirthTests::test_linking_twice_or_over_existing_edges_writes_no_duplicate`, `test_link_children_at_creation.py::SolvedChildClimbsTests::test_the_pv_walk_closes_the_node_it_makes_on_its_solved_child`, `test_api_request_children.py::RequestChildrenApiTests::test_known_transpositions_are_linked_before_they_are_skipped` (35 in POS-3, mostly mixed: they go through views). **Gaps:** `link_existing_children` does not stamp `updated` on revived children while `_revive_tombstones` does (REV-1.4); no test says which is intended.
- **Determinism.** The settling setting is read per call.
- **History.** #287 (21-Nov) at birth; #307 (24-Nov) the optional move list used by the relink on view.
- **Ordering.** Runs before the edge from the clicked parent is written in `_materialise_child`; the climb runs after both edges exist.

### POS-3.2 Relink on view
- **Statement.** Rendering an unexpanded open node in the explorer links its missing legal moves to children that already exist (POS-3.1, with the move list from the page's cached child keys), at most once per `RELINK_ON_VIEW_SECONDS` per node (a cache entry), and climbs (`settle_linked_children` with all the node's parents) when a linked child is closed. A database error is swallowed and the page is drawn with the edges there are.
- **Inputs / outputs.** As POS-3.1. A GET request writes edges and may close nodes.
- **Code.** `atomicdb/views.py:_relink_on_view:5860`.
- **Tests.** `test_relink_on_view.py::RelinkOnViewTests` (16 tests, mixed). **Gaps:** none for the rule; the cache throttle is v1 plumbing.
- **Determinism.** Depends on who views what and when (a cache TTL). In v2 a view that would write must become an input to the log (section 2.4) or the relink must move to creation time only.
- **History.** #307 (24-Nov, MoKy).
- **Ordering.** Inside the page view, before the move table is built.

### POS-4.1 Terminal positions
- **Statement.** See the proofs file, POS-4.1 (`logic.terminal_status`: mate, stalemate, explosion of a king, adjudication). In this file it matters at two points: creation (POS-2.1) and the fact that a terminal row is never expanded (`expand` skips non-`UNKNOWN` rows), so a terminal has no edges and the cascade never revisits it (CAS-1.1).
- **Code.** `atomicdb/logic.py:terminal_status:290`.

### POS-5.1 Reachability
- **Statement.** `Position.reachable` means "hangs from the start position". The start position is born reachable. `expand` marks the children of a reachable parent reachable, one ply only (EXP-1.1). Nodes born on click paths are born unmarked. `backfill_reachable` recomputes the whole column (it marks a closed node but does not walk past it). Monotone: edges are never deleted, so a reachable node stays reachable. Only the selector reads it (queue file PRI-2), to price nodes outside its bounded ball (`FAR_REGRET` 30 if reachable, `DISCONNECTED_REGRET` 5 if not).
- **Inputs / outputs.** `Position.reachable`.
- **Code.** `atomicdb/ingest.py:expand:703` (lines 755-761), `ingest.py:_get_or_create_position:492`, `management/commands/backfill_reachable.py`.
- **Tests.** `test_selector_v2.py::ReachablePropagationTests::test_expand_hands_the_mark_to_the_children`, `test_selector_v2.py::ReachablePropagationTests::test_an_unreachable_parent_hands_out_nothing`, `test_ingest_batched_writes.py::ExpandTests::test_a_reachable_parent_marks_its_old_children` (26 in POS-5). **Gaps:** none.
- **Determinism.** None in the rule; the backfill's walk order does not matter (monotone).
- **History.** `53567d5` (3-Aug).
- **Ordering.** Read only by the selector. With D5 (priority computed in memory) the column becomes derivable from the edges.

---

# 4. Landing a search (EXP)

### EXP-1.1 Expansion
- **Statement.** `expand(pos)` runs only on an open (`UNKNOWN`), unexpanded position. It generates every legal move with the server's movegen, in movegen order; computes each child's canonical FEN and key; creates the missing children with the parent's `campaign_id` and `born_reachable = pos.reachable` (POS-2.1, so terminal children are born closed); inserts one edge per legal move in movegen order (`ignore_conflicts`), so edge ids follow movegen order for a fresh expansion; revives every child with `priority <= DEAD/2` to `0.0` (in key order, without touching `updated`); when the parent is reachable, marks every unmarked child reachable; and sets `pos.expanded = True`. Returns the children in movegen order. An existing child keeps its campaign owner, even when it has none (a campaign does not adopt what it finds by transposition).
- **Inputs / outputs.** Reads `pos.expanded`, `pos.status`, `pos.fen`, `pos.campaign_id`, `pos.reachable`; writes `Edge` rows, child rows, child `priority`, child `reachable`, `pos.expanded`.
- **Code.** `atomicdb/ingest.py:expand:703`.
- **Tests.** `tests.py::BackupTests::test_movegen_completeness_on_expand`, `test_ingest_batched_writes.py::ExpandTests::test_terminal_children_are_born_closed_as_before`, `test_ingest_batched_writes.py::ExpandTests::test_a_half_known_expansion_writes_what_the_loop_wrote`, `test_won_line_chain.py::MaterialiseWonLineTests::test_the_chain_is_not_expanded`, `test_campaigns.py::InheritanceTests::test_a_child_born_under_a_tagged_parent_inherits_the_tag` (18 in EXP-1). **Gaps:** nothing pins that edge **ids** follow movegen order, although readers that `order_by('id')` rely on it (the comment at `ingest.py:740`), and edge id is the universal tie-break of the proof numbers (PN in the proofs file). v2 must keep v1's edge ids, or move every such tie to a declared order (D3).
- **Determinism.** Movegen order of pyffish. Edge ids are assigned by the database in insert order.
- **History.** `b409c30` (complete move list, the basis of exact AND closure); `bdb4082` revive tombstones; `4001003` campaign inheritance; `53567d5` reachable; #330 batched writes.
- **Ordering.** First thing inside the landing transaction (EXP-2), before any line is read: the lines name moves whose edges must exist.

### EXP-2.1 Reading the lines of a pass
- **Statement.** `lines` is the engine's MultiPV, each `{'move', 'eval_cp', 'mate', 'pv', 'raw', ...}` in White's view, normalised to the stored scale (SB-6). The lines are walked **in order**. A line whose move has no edge (`Edge(parent, move_uci)` missing) is skipped entirely. For the others, in this order: claim seeding (CLM-1), the mate-proof arms (MPV-3.1, MPV-4.1, MPV-2.7, trust gate CLM-4 / REV-2, closure MPV-2.6). The **best eval of the pass** is the first line, in line order, whose `eval_cp` is not null and strictly better for the mover than the best so far (`>` for White, `<` for Black): a tie keeps the earlier line. **Quirk to reproduce exactly:** a line is left out of the best-eval tracking when its move has no edge, when its child is open and its prepared proof is `NO_MATE` (the dispute arm `continue`s), and when its closure CAS loses to a concurrent closer (`continue`); in every other case (including a child already closed) it competes.
- **Inputs / outputs.** In: `lines`, `mate_proofs` (index -> `(winner_white, pv_rest, verdict, worst_run)`). Out: `best_eval`, `best_move` of the pass.
- **Code.** `atomicdb/ingest.py:ingest_analysis:984` (loop at 1071-1196).
- **Tests.** `tests.py::IngestTests` and `test_backed_eval.py::BackedIngestTests` cover the ordinary path. **Gaps:** no test pins the skip of a `NO_MATE` line from the best eval, nor the tie on equal evals between lines.
- **Determinism.** Line order is the engine's MultiPV order. A concurrent closer of the same child (another transaction) changes whether the line competes; impossible in v2.
- **History.** `d5f5646` mate winner perspective; `3960f81` online AND/OR verdicts; #223 trust gate.
- **Ordering.** After EXP-1; before EXP-2.2.

### EXP-2.2 Visits and nodes
- **Statement.** Every landing that reaches this point (not skipped as already closed) adds `visits += 1` and `nodes_invested += nodes_budget`, where `nodes_budget` is the nodes the worker reports it searched (not the arbiter budget). `time_invested` is added by `apply_job` after the landing (`+= elapsed`, only when the result was not rejected).
- **Inputs / outputs.** `Position.visits`, `Position.nodes_invested`, `Position.time_invested`.
- **Code.** `atomicdb/ingest.py:ingest_analysis:984` (1198-1199); `ingest_queue.py:apply_job:240` (339-341).
- **Tests.** `test_mate_stop.py::ArbiterBudgetTests::test_a_mate_stop_arbitrates_with_its_budget` (credits the nodes run, arbitrates with the budget). **Gaps:** none.
- **Determinism.** None.
- **History.** #225 (11-Nov) separated the arbiter budget from the credited nodes.
- **Ordering.** Before the showcase arbitration (EXP-2.3), which does not read them.

### EXP-2.3 The showcase: which pass the position stands behind
- **Statement.** `last_analysis` holds the current showcase (lines without `prior_pass`) followed by kept lines of an earlier, wider pass (`prior_pass: true`), at most 10 entries. The new snapshot is `capped_analysis(lines[:8])`: the first 8 lines, each non-mate PV cut to `STORED_PV_MAX_PLIES = 24` plies (mate PVs kept whole); its first line carries `_budget = arbiter`. With `stored_budget` = `_budget` of the first current line (0 if none): the pass is **shallower** when there is a current showcase and `arbiter < stored_budget`; it **takes the showcase** when it is not shallower and (there is no current showcase, or `arbiter > stored_budget`, or `len(snapshot) >= len(current)`). Equal budgets therefore go to width: a narrower pass at the same budget does not replace. When it takes the showcase: if the current showcase is longer than the snapshot, the current lines are all kept as `prior_pass`; else if the snapshot is at least as long as the existing prior lines, the prior lines are dropped; `last_analysis = (snapshot + prior)[:10]`. A pass that does not take the showcase leaves `last_analysis` untouched.
- **Inputs / outputs.** Reads/writes `Position.last_analysis`.
- **Code.** `atomicdb/ingest.py:ingest_analysis:984` (1233-1270), `ingest.py:capped_analysis:238`.
- **Tests.** `test_backed_eval.py::BackedIngestTests::test_a_narrow_shallow_pass_does_not_clobber_the_wide_snapshot`, `test_backed_eval.py::BackedIngestTests::test_a_shallow_repeat_never_undoes_a_deeper_search`, `test_scale_prep.py::IngestStoresCappedAnalysisTests::test_the_child_still_gets_its_eval_from_the_full_line`, `test_mate_stop.py::ArbiterBudgetTests::test_a_mate_stop_arbitrates_with_its_budget` (73 in EXP-2). **Gaps:** the 8-line cap and the 10-entry cap are not pinned by name.
- **Determinism.** None.
- **History.** `0bfaeaf` (29-Jul) width; `68f83c7` prior_pass; `d5267bb` (3-Aug) depth first (a 128M pass had overwritten 640M); #92 (27-Oct) prior lines go only to a pass as wide as they are; #225 `_budget = arbiter`.
- **Ordering.** After EXP-2.1 (which read the old showcase for `seed_arbitrates`, CLM-2); feeds the showcase bound (BKD-4) of every later backup of this position.

### EXP-2.4 Pass history
- **Statement.** Every pass is recorded, whether or not it took the showcase: `analysis_passes = [pass_summary(...)] + old entries (dicts only)`, newest first, truncated to `PASS_HISTORY_MAX = 6`. The summary holds the pass's own numbers (`best_eval`/`best_move` before the restricted and shallower guards), the nodes run, width, restricted flag, whether it took the showcase, and info fields parsed from the raw UCI line.
- **Inputs / outputs.** `Position.analysis_passes`.
- **Code.** `atomicdb/ingest.py:pass_summary:210`, `ingest.py:info_fields:194`, `ingest_analysis` (1251-1256).
- **Tests.** `test_public_api.py::QueryApiAnalysisTests::test_a_pass_that_lost_the_arbitration_is_recorded_all_the_same`. **Gaps:** none relevant.
- **Determinism.** The summary carries a timestamp (`timezone.now()` inside `pass_summary`); display only, no rule reads it.
- **History.** `95ab6d4` (16-Aug).
- **Ordering.** Display only; no rule reads it.

### EXP-2.5 A restricted pass can only improve
- **Statement.** A pass searched with `searchmoves` limited to the unsolved moves (`restricted = True`) may move `eval_cp` only in the mover's favour: when it has a best eval and the position has an `eval_cp`, and the best eval is not strictly better for the mover than the stored one, both `best_eval` and `best_move` of the pass are discarded (neither is written).
- **Inputs / outputs.** Reads `restricted`, `pos.eval_cp`; gates EXP-2.6.
- **Code.** `atomicdb/ingest.py:ingest_analysis:984` (1275-1279).
- **Tests.** `test_multipv_policy.py` (restricted passes), `test_backlog.py::RecheckAtAdoption::test_a_restricted_search_is_not_short`. **Gaps:** no test pins that an equal eval is discarded (the comparison is strict).
- **Determinism.** None.
- **History.** `c024cea` (29-Jul).
- **Ordering.** After EXP-2.4, before EXP-2.6.

### EXP-2.6 Own eval and best move
- **Statement.** A shallower pass (EXP-2.3) discards its best eval and best move. Otherwise `pos.best_move = best_move` when set and `pos.eval_cp = best_eval` when not null. The row is saved with explicit fields only (`visits`, `nodes_invested`, `last_analysis`, `analysis_passes`, `best_move`, `eval_cp`, `updated`), so a revocation or closure written earlier in the same loop on this row is not overwritten.
- **Inputs / outputs.** `Position.eval_cp`, `Position.best_move`, `Position.updated`.
- **Code.** `atomicdb/ingest.py:ingest_analysis:984` (1280-1293).
- **Tests.** `test_backed_eval.py::BackedIngestTests::test_a_shallow_repeat_never_undoes_a_deeper_search` and the EXP-2 group. **Gaps:** none.
- **Determinism.** None.
- **History.** `d5267bb` (3-Aug).
- **Ordering.** Last write of stage (a) on the landed row.

### EXP-2.7 A landing on a closed position does nothing
- **Statement.** If the position is not `UNKNOWN` when the landing transaction locks it (or when the caller prepares the proofs), `ingest_analysis` returns `{'skipped': 'already-closed'}` and writes nothing: no visits, no nodes, no pass history, no seeds. `apply_job` still adds `time_invested`, still absorbs requests at or below the arbiter budget (EXP-5.2) and still marks the job `DONE`.
- **Inputs / outputs.** None on the position except `time_invested`.
- **Code.** `atomicdb/ingest.py:ingest_analysis:984` (1021-1034); `ingest_queue.py:apply_job:240`.
- **Tests.** `tests.py` (`already-closed`). **Gaps:** nothing pins that `time_invested` still grows on a skipped landing.
- **Determinism.** Whether a landing is skipped depends on what closed the position first; in v1 that is commit order of concurrent transactions, in v2 log order.
- **History.** First commit.
- **Ordering.** Before everything in EXP-2.

### EXP-5.1 Arbiter budget of a mate stop
- **Statement.** Workers since build 2026111101 stop at the first completed iteration whose best line is a mate. Such a pass arbitrates the showcase (EXP-2.3), `eval_cp`/`best_move` and claim refresh (CLM-2) as if it had run its task's budget: `arbiter_budget(lines, searched, budget)` is the task's `budget_nodes` when the first line has a non-null `mate`, `budget` is an int, and `budget > searched`; otherwise `searched`. `ingest_analysis` then uses `max(nodes_budget, arbiter_budget)`.
- **Inputs / outputs.** Reads `lines[0].mate`, the payload's `nodes`, `AnalysisTask.budget_nodes`.
- **Code.** `atomicdb/ingest_queue.py:arbiter_budget:190`; `ingest.py:ingest_analysis:984` (1029-1030).
- **Tests.** `test_mate_stop.py::ArbiterBudgetTests::test_a_mate_stop_arbitrates_with_its_budget`, `::test_a_full_search_is_unchanged`, `::test_a_mate_in_a_later_line_is_not_a_mate_stop` (40 in EXP-5). **Gaps:** none.
- **Determinism.** None.
- **History.** #225 (11-Nov).
- **Ordering.** Computed by `apply_job` before the landing transaction.

### EXP-5.2 A landed search absorbs the requests it makes useless
- **Statement.** In the landing transaction, after `ingest_analysis`, every **PENDING** `AnalysisTask` on the same position with `budget_nodes <= arbiter`, other than the delivered task, becomes `COMPLETED` with `nodes_searched = 0`, `completed = now`, no machine (`absorb_tasks`). LEASED tasks are never absorbed. This runs even when the landing was skipped as already closed.
- **Inputs / outputs.** `AnalysisTask.state`, `completed`, `nodes_searched`.
- **Code.** `atomicdb/ingest_queue.py:_absorb_shadowed:211`, `ingest.py:absorb_tasks:5770`, `ingest.py:is_absorbed:5789`. The queue file (Q-3.14) is the canonical description of the absorption idiom.
- **Tests.** `test_queue_control.py::AbsorptionStillWorksTests::test_a_cheaper_pending_request_is_absorbed_by_a_deeper_analysis`, `test_ingest_queue.py::ClosureAbsorbsItsQuestionsTests::test_a_mate_witness_closes_the_children_it_leaves_without_work`. **Gaps:** see the queue file.
- **Determinism.** `completed` is wall clock.
- **History.** `d07b172` (3-Aug); #225 absorbs with the arbiter budget.
- **Ordering.** After EXP-2, in the same transaction.

### EXP-6.1 The landing record and its seeds
- **Statement.** After stage (a), `Landing.of(pos, closed_keys, reseeded)` computes, with reads of the edge table only: `others` = the parents of every key this landing closed, excluding the landed position and the closed keys themselves (distinct; the transpositions, CAS-1.4); `parents` = the parents of the landed position (edge order as the database returns it, duplicates kept); `audience` = the re-seeded children plus every parent of a re-seeded child other than the landed position (CLM-3); `source` = the current closure source (CAS-4.1). **Cascade seeds** = `[key, *closed, *others]`; **backed seeds** = `[key, *parents, *others, *audience]`. With deferral, the record is stored as a `PropagationSeed` payload (`cascade`, `backed`, `closed`, `others`, `parents`, `audience`, plus `job` and `verify`).
- **Inputs / outputs.** Reads `Edge`; writes `PropagationSeed` when deferred.
- **Code.** `atomicdb/ingest.py:Landing:1318`, `ingest.py:_other_parents:967`, `ingest.py:_seed_audience:941`.
- **Tests.** `test_closure_reaches_all_parents.py::IngestClosureReachesEveryParentTests::test_a_mate_closure_reaches_the_other_parent_of_the_child`, `test_backprop_wakes.py::SharedChildSeedTests::test_the_other_parent_follows_a_seed_it_did_not_write`, `test_deferred_propagation.py::StageATests::test_a_closure_of_its_own_child_is_stage_a_and_its_parents_are_b` (17 in EXP-6). **Gaps:** none.
- **Determinism.** `parents` and `audience` come back in database order; they become seeds, and seed order is the order of the first backed level only through `dict.fromkeys` (BKD-7.1), which does not change values. The closed keys are in closure order.
- **History.** #188 (8-Nov) the record; #65 (14-Oct) `others`; #49 (4-Oct) audience; #79 (26-Oct) closed keys as cascade seeds.
- **Ordering.** End of stage (a); input to EXP-6.2.

### EXP-6.2 One climb for a batch of landings
- **Statement.** `propagate_landings(landings)` runs, for one landing, exactly the inline climb with its seed lists in their order; for several (a drained batch, one closure source), the union of the cascade seeds and the union of the backed seeds, **sorted by key**, into one `backup_cascade` and one `backup_backed_evals`; then, per landing in landing order, the arms and the recheck registration (section 2.3). The plan's batch-size independence (D2) is not a property of v1: the noise cut (BKD-8.1) and the per-call caps make a batched climb differ from one-at-a-time climbs in rare cases, and v1 relies on the sweep to converge (`propagation_queue.py`, "the noise cut").
- **Inputs / outputs.** As CAS-1, BKD-7, CAS-6.
- **Code.** `atomicdb/ingest.py:propagate_landings:1397`; `atomicdb/propagation_queue.py:drain_batch:398`, `propagation_queue.py:_merge:197`.
- **Tests.** `test_deferred_propagation.py::EquivalenceTests::test_a_closure_that_reaches_another_parent`, `::test_a_line_from_the_start_position` (inline and deferred end in the same state on these shapes). **Gaps:** nothing pins batch-size independence in general (it does not hold).
- **Determinism.** Which landings share a batch depends on timing (the drainer claims what is ready). This is the main obstacle to replay of production: golden cases must record batch boundaries (plan section 7.2).
- **History.** #188, #240, #322.
- **Ordering.** Section 2.3.

---

# 5. Claims: evals seeded on replies (CLM)

### CLM-1.1 A claim never overwrites a measurement
- **Statement.** When a pass's line names a move whose child is `UNKNOWN` and carries a non-null `eval_cp`, the child may receive that number as its `eval_cp` (a "claim", LINE-CLAIM in `docs/value-semantics.md`). The write is a conditional `UPDATE ... WHERE key = child AND (eval_cp IS NULL OR (refresh AND nodes_invested = 0 AND backed_nodes = 0))`, setting `eval_cp` and `updated = now`. So a claim fills an empty child always, and replaces an earlier claim only when `refresh` is true (CLM-2.1); a child with any search of its own (`nodes_invested > 0`) or any weighted backed value (`backed_nodes > 0`) is never touched. The in-memory pre-check uses the same predicate on the loaded row; the database predicate decides. A seed whose value differs from the child's previous `eval_cp` adds the child to `reseeded` (CLM-3.1).
- **Inputs / outputs.** Reads child `status`, `eval_cp`, `nodes_invested`, `backed_nodes`; writes child `eval_cp`, `updated`.
- **Code.** `atomicdb/ingest.py:_seed_child_eval:899`; caller `ingest_analysis:984` (1084-1092).
- **Tests.** `tests.py::SeedNotStompTests::test_parent_lines_do_not_overwrite_child_eval`, `test_backed_eval.py::BackedIngestTests::test_a_seed_refreshes_an_older_seed_because_neither_is_a_search`, `test_backed_eval.py::BackedIngestTests::test_a_new_pass_refreshes_the_seed_the_old_one_left_behind`, `test_tb_distance.py::IngestTests::test_a_tablebase_line_keeps_its_distance` (13 in CLM-1). **Gaps:** none.
- **Determinism.** The predicate in the WHERE exists for READ COMMITTED races (a child's own landing committing between read and write). In v2 the check is plain.
- **History.** `23f67f7` (21-Jul) seeds only empty evals; `e38d9fd` (29-Jul) conditional UPDATE (3,229 of 72,391 seeds had overwritten searches); `c44c3a0` (4-Aug) a seed may refresh a seed.
- **Ordering.** Inside EXP-2.1, before the mate arms of the same line. Plan decision D4 proposes storing claims on the edge instead.

### CLM-2.1 Only the pass that arbitrates refreshes claims
- **Statement.** `seed_arbitrates` is false when the position already has a current showcase and `arbiter < showcase[0]._budget` (read before this pass writes its own showcase). A pass that does not arbitrate may still fill children with no `eval_cp` (`refresh = False`: the predicate is only `eval_cp IS NULL`), but never replaces an existing claim. Note that this test uses the strict "shallower" condition only; a pass at an equal budget that loses the showcase on width (EXP-2.3) still refreshes claims.
- **Inputs / outputs.** Reads `pos.last_analysis` (current lines, `_budget`).
- **Code.** `atomicdb/ingest.py:ingest_analysis:984` (1057-1060).
- **Tests.** `test_backed_eval.py::BackedIngestTests::test_a_shallower_pass_does_not_refresh_the_seed_either`, `::test_a_shallower_pass_still_seeds_the_children_nobody_had_named` (11 in CLM-2). **Gaps:** the equal-budget, narrower pass (refreshes claims, does not take the showcase) is not pinned.
- **Determinism.** None.
- **History.** `718b9a4` (4-Aug).
- **Ordering.** Computed before the line loop.

### CLM-3.1 Who hears about a re-seed
- **Statement.** The children whose claim this pass changed, and every other parent of each of them (distinct by query, landed position excluded), join the backed seeds (EXP-6.1). A child that was walked before it was searched carries a backed value anchored on its old claim, so it is seeded too; the other parents stand on the child's number.
- **Inputs / outputs.** Reads `Edge(child_id__in=reseeded)`.
- **Code.** `atomicdb/ingest.py:_seed_audience:941`.
- **Tests.** `test_backprop_wakes.py::SharedChildSeedTests::test_the_other_parent_follows_a_seed_it_did_not_write` (8 in CLM-3). **Gaps:** none.
- **Determinism.** Database order of the parents (no effect on values).
- **History.** #49 (4-Oct; Eclipsia, "best move doesnt match eval").
- **Ordering.** End of stage (a).

### CLM-4.1 Pending claims of untrusted accounts
- **Statement.** When an open child's line has a prepared proof whose verdict is not `PROVEN` (and not `NO_MATE`, which disputes instead) and the delivering account is not trusted (REV-2), the tree does not move: a `PendingClaim` is recorded with `position_key = child`, `parent_key`, the task, account and engine sha from the closure provenance, `kind = TB` for a TB line else `MATE`, `winner` (`WHITE`/`BLACK`), `pv` = the line after the move, unless an open claim with the same position, account and line already exists. When an exhaustive search later finds no mate on exactly that line, the open claims with that `pv` are resolved `REFUTED` (from the dispute arm, MPV-2.7). When the position closes by any door (CAS-7.1), every open claim on it is resolved `CONFIRMED` if its winner matches the new status's winner, else `CONTRADICTED` (a `DRAW` contradicts every claim).
- **Inputs / outputs.** `PendingClaim` rows (`resolved`, `resolution`).
- **Code.** `atomicdb/ingest.py:_record_pending_claim:5714`, `ingest.py:_refute_pending_claims:5736`, `ingest.py:_resolve_pending_claims:5745`.
- **Tests.** `test_closure_trust.py::CooperativeMateTests::test_a_pending_claim_resolves_when_the_position_closes`, `::test_a_contradicted_claim_says_so`, `test_closure_trust.py::ProvenClosuresTests::test_a_refuted_line_resolves_its_claim` (6 in CLM-4). **Gaps:** none.
- **Determinism.** `resolved` timestamps.
- **History.** #223 (11-Nov), after a cooperative line 1.Nf3 a6 2.Ng5 a5 3.Nxf7 closed the start position.
- **Ordering.** Inside EXP-2.1; resolution inside CAS-7.1.

---

# 6. The status cascade and closures (CAS)

The three-valued minimax of one node (`logic.backup_status`) is CAS-1.1 in
the proofs file: a mover-win child wins the node; an unexpanded node closes on
nothing else; an expanded node closes as a mover-loss when every child is a
mover-loss and as a draw when every child is decided; otherwise it stays open.
This section is the cascade that applies it.

### CAS-1.1 `backup_cascade`: the fixed point of statuses
- **Statement.** Start with `frontier` = the seed keys plus every parent of a seed (one query on `Edge(child_id__in=seeds)`), as a **set**. While the frontier is not empty and fewer than `CASCADE_GUARD_LIMIT` (100,000) pops have happened: pop a key (arbitrary set order), read its row and its edges with their children (`ORDER BY move_uci`). A row with no edges is skipped. Otherwise:
  1. If the row is `UNKNOWN`, `new_status = backup_status(fen, expanded, [supporting status of each child])` (CAS-1.2). If it is decided, the row closes: `status = new_status`, `closure = 'MINIMAX'`, `proof = _minimax_proof(...)` (CAS-3.1), `best_move` = the witness (CAS-1.3), and the closure events fire at once (CAS-7.1), before the row is saved.
  2. If the row's closure is `MINIMAX` and it is decided (just now or before): recompute `proof` (CAS-3.1) and `clock_slack = _minimax_slack(...)` (DTM-5.10); either change marks the row dirty.
  3. If the row is `WHITE_WIN` or `BLACK_WIN` (any closure that has edges: `MINIMAX`, `MATE_PV` with a materialised chain, `SOLVE`, ...): `(mate_in, best_move) = mate_distance_refresh(pos, edges)` (DTM-1, DTM-2, DTM-3); a change marks it dirty.
  4. A dirty row that this call has already written `CASCADE_REWRITE_LIMIT` (32) times is not written and not climbed from again (CAS-5.2). Otherwise it is saved with **only** `status, closure, proof, best_move, mate_in, clock_slack, updated`, counted in `changed`, and **all** its parents are added to the frontier.
  A decided row never reopens here (revocation is REV-1). `DRAW` rows only get steps 2 (for `MINIMAX`: proof; slack is `None` for draws).
  At the end: a `CASCADE_GUARD` event if the frontier is not empty, a `CASCADE_CYCLE` event if any row hit the rewrite limit, `report['changed']` = the written keys in write order, and the proof-number refresh of `seeds + changed` (PN-8.1, or the batch's sink, PN-8.2). Returns the number of writes.
- **Inputs / outputs.** Reads `Position` (all columns but the two JSON ones) and `Edge`; writes the seven columns above; events `NODE_CLOSED` (via CAS-7.1), `CASCADE_GUARD`, `CASCADE_CYCLE`; through CAS-7.1 also `ClosureProvenance`, `PendingClaim`, `AnalysisTask`, `Campaign`.
- **Code.** `atomicdb/ingest.py:backup_cascade:1754`.
- **Tests.** `tests.py::BackupTests`, `test_backed_eval.py::BackedIngestTests::test_partially_expanded_lineage_still_backs_up`, `test_closure_reaches_all_parents.py::IngestClosureReachesEveryParentTests::test_a_closed_link_of_the_won_line_reaches_its_other_parents`, `test_cascade_bulk_reads.py::EquivalenceTests::test_a_loop_of_closed_positions` (78 in CAS-1). **Gaps:** nothing pins the fixed point under a different pop order (see Determinism); nothing pins that the events of a new closure fire before the save.
- **Determinism.** **Pop order is Python set order of string keys, which depends on `PYTHONHASHSEED` (random per process unless set).** For a cascade that converges below both limits the fixed point does not depend on pop order (status is monotone and the distance rules are a function of children). It does when a limit cuts the cascade: which rows reach 32 rewrites, or which 100,000 pops happen first, depends on the order. The edge order (`ORDER BY move_uci`) decides the witness among equal winners (CAS-1.3) and is explicit since #327 (Postgres used the unique index, SQLite used id order). Concurrent transactions at READ COMMITTED can each miss the other's closure (CAS-6).
- **History.** `b409c30` (three closure doors); `e100915` (27-Jul) status only, no eval minimax (it had overwritten `eval_cp`); `66129b9` (29-Jul) writes only its own columns; `abfaab1`, `343a39a`, `e718a8e` distances; #301 (23-Nov) rewrite limit; #327 (27-Nov) wave reads.
- **Ordering.** First step of every climb (section 2.3); before BKD-7 and PN-8.

### CAS-1.2 Fresh context: a decisive child must have clock margin
- **Statement.** When the cascade asks what a child can support, a `DRAW` or `UNKNOWN` child counts as itself; a decisive child counts as itself only if `logic.edge_supports(parent_fen, move, child.clock_slack)` holds (a capture or pawn move always supports; a quiet move needs `clock_slack >= 1`; NULL counts as 0), else it counts as `UNKNOWN`. Turned off by `ATOMICDB_FRESH_CONTEXT = False` (every child counts as itself). Only **new** closures use this; the revocation cascade (REV-1.1) and the witness, proof and distance of an already closed node read raw child statuses. `closable_rows` (CAS-2.1) uses it too.
- **Inputs / outputs.** Reads child `status`, `clock_slack`, the move.
- **Code.** `atomicdb/ingest.py:_supporting_status:2001`, `ingest.py:fresh_context_enabled:1988`; `logic.py:edge_supports:647` (proofs file DTM-5.3).
- **Tests.** `test_clock_slack.py::FreshContextTests::test_a_quiet_edge_with_no_margin_cannot_close_the_parent`, `::test_a_zeroing_edge_carries_a_closure_with_no_margin_at_all`, `::test_the_same_shape_closes_once_the_children_have_margin`. **Gaps:** nothing pins the asymmetry with revocation.
- **Determinism.** A settings switch.
- **History.** `8c04741` (27-Jul, the 50-move rule `atomic-fide-claim-v1`).
- **Ordering.** Inside CAS-1.1 step 1 and CAS-2.1.

### CAS-1.3 The witness of a new MINIMAX closure
- **Statement.** When an open row closes in the cascade, `best_move` is set to the first, in edge order (by move), of: a child with the new status that is verified (`TERMINAL`/`TB` closure or `ANDOR` proof); else any child with the new status; else any decided child. Raw child statuses are used (a child that did not support the closure under CAS-1.2 can still be the witness). The distance rule of the same visit (step 3 of CAS-1.1) may then replace `best_move` (DTM-1.1).
- **Inputs / outputs.** `Position.best_move`.
- **Code.** `atomicdb/ingest.py:backup_cascade:1754` (1805-1814).
- **Tests.** `test_revocation.py::MinimaxWitnessPreferenceTests::test_verified_winning_child_is_preferred_as_the_witness`. **Gaps:** the third fallback (any decided child, used when a draw or a loss closes) is not pinned.
- **Determinism.** Edge order, by move name.
- **History.** `7a437b6`, `bdaddf1`.
- **Ordering.** Before DTM-1 in the same visit.

### CAS-1.4 Every parent of what a landing closes is a seed
- **Statement.** A landing seeds the cascade with the landed key, the keys it closed (each child closed by its line and each link of a materialised chain, in closure order) and the other parents of those keys (EXP-6.1). The closed keys are seeds themselves so that `mate_distance_refresh` compares each with all its children (a link closed with the length of its own part of the line keeps a longer distance otherwise).
- **Code.** `atomicdb/ingest.py:Landing.cascade_seeds:1364`.
- **Tests.** `test_closure_reaches_all_parents.py::IngestClosureReachesEveryParentTests::test_a_mate_closure_reaches_the_other_parent_of_the_child`, `test_chain_link_distance.py::NewClosureTests::test_a_chain_link_takes_a_faster_child_it_already_had`. **Gaps:** none.
- **Determinism.** None.
- **History.** #65 (14-Oct), #79 (26-Oct; 0.15% of closed positions had distances up to 20 plies too long).
- **Ordering.** Input of CAS-1.1.

### CAS-1.5 How the cascade reads (no semantic effect)
- **Statement.** By default the cascade reads a wave of up to `CASCADE_WAVE_KEYS` (512) frontier keys at once and caches row values (up to `CASCADE_CACHE_ROWS`, 200,000, then forgets everything); every visit builds fresh objects from the cached values, and each save copies the saved fields into the cache. With `ATOMICDB_CASCADE_BULK_READS = False` it reads two queries per visit. Both read edges ordered by move; an edge whose child row is missing is left out. In any schedule where no other transaction writes these rows during the call the two readers write the same values; under concurrency a wave read can be a second or two older than a per-key read (allowed at READ COMMITTED).
- **Code.** `atomicdb/ingest.py:_CascadeBulkReader:1667`, `ingest.py:_CascadePerKeyReader:1654`.
- **Tests.** `test_cascade_bulk_reads.py::EquivalenceTests` (15 tests). **Gaps:** none.
- **Determinism.** Which keys share a wave depends on set order; values do not.
- **History.** #327 (27-Nov).
- **Ordering.** Implementation detail of CAS-1.1; v2 reads its columns directly.

### CAS-2.1 `closable_rows`: one cascade step, read only
- **Statement.** For a set of keys: the open rows among them, their edges with children, and for each row with at least one edge the `backup_status` over the supporting statuses (CAS-1.2). Returns `{key: (row, status, edges)}` for the rows that would close. Writes nothing.
- **Code.** `atomicdb/ingest.py:closable_rows:1951`.
- **Tests.** `test_closure_reaches_all_parents.py::RecascadeStatusCommandTests::test_an_open_position_with_every_child_closed_is_found`, `::test_a_quiet_edge_without_clock_slack_is_not_counted` (5 in CAS-2). **Gaps:** none.
- **Determinism.** None.
- **History.** #102 (29-Oct).
- **Ordering.** Used by the rechecks (CAS-6), `recascade_status` and `sweep_stale_backed`.

### CAS-3.1 Proof grade of a MINIMAX closure
- **Statement.** With no edges or an `UNKNOWN` status: NULL. If the status is a mover-win: `ANDOR` when any child with that status is verified, else `ENGINE`. Otherwise (a mover-loss or a draw, which need every child): `ANDOR` when **every** child is verified, else `ENGINE`. Recomputed on every visit of a `MINIMAX` row, so a grade falls when the winning child's grade falls and rises when it rises.
- **Inputs / outputs.** Reads child `status`, `closure`, `proof`; writes `Position.proof`.
- **Code.** `atomicdb/ingest.py:_minimax_proof:5579`.
- **Tests.** `test_proofs.py::MinimaxProofInheritanceTests::test_winning_witness_inherits_andor_then_engine`, `test_closure_reaches_all_parents.py::IngestClosureReachesEveryParentTests::test_a_certified_line_upgrades_every_parent_standing_on_it`, `test_revocation.py::RevokeClosureTests::test_or_node_survives_when_another_winning_edge_remains`. **Gaps:** the draw case (every child verified) is not pinned on its own.
- **Determinism.** None.
- **History.** `3960f81` (21-Jul).
- **Ordering.** Inside CAS-1.1 steps 1 and 2.

### CAS-4.1 Closure source
- **Statement.** Every closure is labelled with the queue its result came from: `USER`, `FILL`, `AUTO` (the task's `source`), `SOLVE` (certificates) or `NONE` (no task: commands, clicks, sweeps, deleted tasks). The label is a context value set by `closure_attribution(source, provenance)` for the duration of an apply, upper-cased and replaced by `NONE` when unknown; it is restored on exit. A deferred climb carries the landing's source (`Landing.source`); a recheck after commit carries the source of the landing that registered it. The label never changes a closure, only the event and the provenance row.
- **Inputs / outputs.** `DBEvent NODE_CLOSED.payload.source`, `ClosureProvenance.source`.
- **Code.** `atomicdb/ingest.py:closure_attribution:5652`, `ingest.py:current_closure_source:5673`, constants `CLOSURE_SOURCES` (5623).
- **Tests.** `test_adversarial_explorer.py::ClosureAttributionTests::test_a_certificate_closure_is_stamped_solve`, `::test_a_closure_nobody_asked_for_says_so`, `::test_a_coverage_closure_is_stamped_fill`, `test_parent_recheck.py::ClosureRaceTests::test_the_landing_that_commits_last_closes_the_parent` (16 in CAS-4). **Gaps:** none.
- **Determinism.** Which landing's source labels a race-closed parent depends on commit order (the one that commits last closes it through its recheck).
- **History.** `7a592e9` (28-Jul); #223 provenance.
- **Ordering.** Set before stage (a); read by CAS-7.1.

### CAS-4.2 Closure provenance
- **Statement.** With the source travels `{task, account, engine_sha}` (task coerced to int or NULL, account and sha truncated to 64 chars). Every closure upserts one `ClosureProvenance` row per position (`key` unique; the latest closure wins) with `closed_at = now`, `status`, `closure`, `proof`, `source`, `task_id`, `account`, `engine_sha`.
- **Code.** `atomicdb/ingest.py:_clean_provenance:5638`, `ingest.py:_record_provenance:5755`.
- **Tests.** `test_closure_trust.py::CooperativeMateTests::test_every_closure_records_task_account_and_engine`, `test_proof_provenance.py` (14 tests). **Gaps:** none.
- **Determinism.** `closed_at` wall clock.
- **History.** #223 (11-Nov).
- **Ordering.** Inside CAS-7.1.

### CAS-5.1 Cascade guard
- **Statement.** A cascade stops after `CASCADE_GUARD_LIMIT` (100,000) pops; the keys still in the frontier are dropped and a `CASCADE_GUARD` event records `iterations`, `remaining`, `seed_count`. Nothing carries them on.
- **Code.** `atomicdb/ingest.py:backup_cascade:1754` (1789, 1876-1880).
- **Tests.** `test_performance_guards.py::CascadeGuardTests::test_guard_exhaustion_emits_event`. **Gaps:** none.
- **Determinism.** Which keys are dropped depends on set order.
- **History.** `3960f81`.
- **Ordering.** Inside CAS-1.1. Plan D2 proposes dropping size guards once propagation is exact.

### CAS-5.2 Cascade rewrite limit
- **Statement.** One cascade call writes one position at most `CASCADE_REWRITE_LIMIT` (32) times. A 33rd dirty visit is not saved, its parents are not added, and the key goes into a `CASCADE_CYCLE` event (first 20 keys sorted, `count`, `limit`, `iterations`, `seed_count`). The next call starts a new count. The backstop against cycles of closed positions whose distances hold each other up (DTM-3 handles the loops it can see within 64 hops).
- **Code.** `atomicdb/ingest.py:backup_cascade:1754` (1853-1858, 1881-1886).
- **Tests.** `test_performance_guards.py::CascadeCycleTests::test_a_cycle_that_never_rests_is_cut_by_the_rewrite_limit`, `::test_a_long_chain_is_not_touched_by_the_limit`. **Gaps:** none.
- **Determinism.** Where the cut falls depends on pop order (set order).
- **History.** #301 (23-Nov: four positions of a bishop-and-rook shuffle counted to mate_in 40,000 inside one batch, a transaction open for an hour).
- **Ordering.** Inside CAS-1.1.

### CAS-6.1 A landing reads its neighbours again after it commits
- **Statement.** Every landing (and every TB landing) registers, for after its commit, `recheck_after_commit(landed, keys, climbed)` with `keys = [landed, *parents, *others]` and `climbed` = the keys its cascade wrote, minus (one landing) the landed key and its closed keys, or (a batch) every landed and closed key of the batch. The recheck: deduplicate `keys` in order; if `climbed` is not empty and fewer than 16 keys, append the parents of the first 16 `climbed` keys (ordered by parent key); cut to `RECHECK_MAX_KEYS` (16); `closing` = the keys `closable_rows` decides (CAS-2.1), sorted; `stale` = the keys `stale_backed_rows` (BKD-12.1) finds, sorted, minus `closing`. Nothing found: return, write nothing. Otherwise, with a 200 ms lock timeout on PostgreSQL: `close_missed(closing)` (CAS-6.3), then `backup_backed_evals_retrying(closed + stale, sleeps=(0.5, 2.0), generation=1)`, and a `LANDING_RECHECK` event with the summary. The closure source and provenance are the landing's. It never raises; a failure is logged and left to the sweep.
- **Inputs / outputs.** As CAS-1.1 and BKD-7; event `LANDING_RECHECK`.
- **Code.** `atomicdb/ingest.py:_recheck_once_committed:4920`, `ingest.py:recheck_after_commit:4943`, `ingest.py:_recheck:5026`, `ingest.py:_climbed_parents:5004`, `ingest.py:_closable_keys:5015`.
- **Tests.** `test_parent_recheck.py::ClosureRaceTests::test_the_landing_that_commits_last_closes_the_parent`, `test_parent_recheck.py::SafetyTests::test_at_most_sixteen_positions_are_read`, `::test_a_quiet_recheck_writes_nothing`, `::test_a_second_recheck_finds_the_parent_closed` (26 in CAS-6). **Gaps:** none.
- **Determinism.** Exists only because of READ COMMITTED: it reads what other transactions committed meanwhile. The 16-key cut depends on the order of `parents` (database order). In v2's single writer there is nothing to recheck; it matters for the golden cases, which record its writes.
- **History.** #102 (29-Oct: two last replies closed in the same second left the parent open for minutes to hours); #240 memo.
- **Ordering.** After the commit of stage (b), after the batch's proof refreshes.

### CAS-6.2 The recheck memo of a batch
- **Statement.** In a batch, the rechecks of its landings share the parents of a `climbed` prefix and the per-key verdicts of `closable_rows` until one of them writes; a recheck that finds anything clears the memo before writing. `stale_backed_rows` is never memoised (its verdict on a key depends on the other keys of its call, BKD-12.1). Answers are the same with and without the memo.
- **Code.** `atomicdb/ingest.py:_RecheckMemo:4967`.
- **Tests.** `test_proof_after_commit.py::RecheckMemoTests::test_with_and_without_the_memo_the_answers_agree`. **Gaps:** none.
- **Determinism.** None beyond CAS-6.1.
- **History.** #240.
- **Ordering.** Inside CAS-6.1.

### CAS-6.3 `close_missed`
- **Statement.** In one transaction: lock the open rows among the keys in key order (`select_for_update ... order_by('key')`), run `backup_cascade(locked)`, and return the locked keys the cascade changed. A lock conflict (SQLSTATE 40P01, 55P03, 40001, or SQLite "database is locked") rolls it back and retries after each of the sleeps, then raises.
- **Code.** `atomicdb/ingest.py:close_missed:4892`, `ingest.py:is_lock_conflict:4757`.
- **Tests.** `test_parent_recheck.py::LockConflictTests::test_a_lock_conflict_is_tried_again`, `test_parent_recheck.py::SweepBackstopTests::test_the_sweep_closes_a_parent_both_rechecks_missed`. **Gaps:** none.
- **Determinism.** Lock waits, sleeps (wall clock).
- **History.** #102.
- **Ordering.** Inside CAS-6.1 and `sweep_stale_backed`.

### CAS-6.4 A committed climb reads its meeting points again
- **Statement.** After the commit of a climb, the rows its backed ascent decided are read again: `rested_keys` (decided and found already stored) reversed, then `written_keys` reversed (top of the climb first), deduplicated, at most `CLIMB_RECHECK_MAX_KEYS` (256). `stale_backed_rows` of those; the stale ones (sorted) get `backup_backed_evals_retrying(..., generation=1)` under the 200 ms lock timeout, and a `LANDING_RECHECK` event with `meeting: true`. Of two climbs that meet at a row, the one that commits last has the row on its list. Never raises.
- **Code.** `atomicdb/ingest.py:_meeting_points:5091`, `ingest.py:_recheck_meeting_points_once_committed:5098`, `ingest.py:recheck_meeting_points:5110`.
- **Tests.** `test_lost_wake.py::MeetingClimbTests::test_the_climb_that_rested_last_reads_the_row_again`, `::test_deferred_climb_that_wrote_last_reads_the_row_again`, `::test_one_climb_after_the_other_needs_no_repair` (5 tests). **Gaps:** none.
- **Determinism.** READ COMMITTED races only; the 256 cut depends on level order and on row order within a level (BKD-7.1).
- **History.** #333 (27-Nov: a row kept 1294 for fourteen hours while its children read 1324 and 1316).
- **Ordering.** Registered last in `propagate_landings`, runs after CAS-6.1 of every landing of the batch.

### CAS-7.1 What every closure does besides the closure
- **Statement.** Every door that closes a position calls `_emit_closure_events(pos, **extra)` exactly once per closure it wins (CAS-1.1, MPV-2.6, MPV-5.3, TB-2.4, SOL-2.2, the TB-line end): (1) a `NODE_CLOSED` event `{key, status, closure, source, **extra}` (TB adds `dtz`, `dtz_verified`, `clock_slack`; SOLVE adds certificate data); (2) the provenance row (CAS-4.2); (3) the open pending claims on the position are resolved (CLM-4.1); (4) **every PENDING `AnalysisTask` on the position is absorbed** (`COMPLETED`, 0 nodes, no machine; LEASED ones are left to finish); (5) every `ACTIVE` `Campaign` whose root is this position gets a `CAMPAIGN_CLOSED` event and moves to `DONE`. Positions born terminal (POS-2.1) do not pass through here.
- **Inputs / outputs.** `DBEvent`, `ClosureProvenance`, `PendingClaim`, `AnalysisTask`, `Campaign.state`.
- **Code.** `atomicdb/ingest.py:_emit_closure_events:5800`.
- **Tests.** `test_adversarial_explorer.py::ClosureAttributionTests::test_a_certificate_closure_emits_one_event_not_two`, `test_ingest_queue.py::ClosureAbsorbsItsQuestionsTests::test_a_mate_witness_closes_the_children_it_leaves_without_work` (29 in CAS-7). **Gaps:** no test pins `CAMPAIGN_CLOSED` by name.
- **Determinism.** Timestamps.
- **History.** `b409c30`; `7a592e9` (28-Jul) single emitter (SOLVE closures had counted twice); `4001003` campaign DONE; `d0070d6` (3-Aug) absorbs pending tasks (318 zombie requests); #223 provenance and claims.
- **Ordering.** Inside the closing transaction, at the moment of the closure (in CAS-1.1 before the row's save).

---

# 7. Mate distance (DTM)

The clock-slack layer (`edge_slack`, `edge_supports`, `minimax_slack`, the
slack of a proof, of a witness, of a DTZ) is DTM-5.1 to DTM-5.9 in the
proofs file. This section is the distance `mate_in` and the slack of a
`MINIMAX` node.

### DTM-1.1 The winner's distance
- **Statement.** For a closed decisive node whose status is the mover-win (the side to move mates), `mate_distance_refresh` returns `(mate_in, best_move)`: `winners` = children with the same status and a non-null `mate_in`; `best` = the minimum of `winners` by `(child.mate_in, 0 if verified else 1)` (`_witness_rank`; Python `min` keeps the first of equal keys, in edge order by move); `own` = the node's own witness length (DTM-4.1); `derived = 1 + best.mate_in`; `new = min(own, derived)` over those that are not null (NULL when both are). If the number comes from the child (`best` exists and `derived == new`) and either the number changes or (`best_move != best.move` and `best` is verified), return `(new, best.move)`. If it does not come from the child and the number changes, and there is an own witness with a stored line, return `(new, first move of won_line)`. Otherwise `(new, stored best_move)`. Consequences: a `MINIMAX` node (no own witness) takes exactly `1 + best child` in both directions, and becomes NULL when no winning child has a distance; a `MATE_PV` node can be shortened by a child but never lengthened past its own line; on an equal-rank tie the stored `best_move` is kept unless the tied best child is verified and different.
- **Inputs / outputs.** Reads node `status`, `fen`, `closure`, `proof`, `won_line`, `mate_in`, `best_move`; children `status`, `mate_in`, `closure`, `proof`. Writes (through CAS-1.1) `mate_in`, `best_move`.
- **Code.** `atomicdb/ingest.py:mate_distance_refresh:2037`, `ingest.py:_witness_rank:2319`.
- **Tests.** `test_mate_distance.py::OwnWitnessTests::test_a_longer_child_cannot_stretch_the_stored_witness`, `::test_a_shorter_child_still_shortens_it`, `test_mate_distance.py::UnnameableDistanceTests::test_a_minimax_or_without_a_measured_winner_loses_its_number`, `test_chain_link_distance.py::EquivalentWitnessTests::test_a_tie_between_verified_witnesses_is_left_alone` (52 in DTM-1). **Gaps:** none found.
- **Determinism.** Edge order (by move) among equal witnesses.
- **History.** `abfaab1` practical DTM; `e718a8e` (29-Jul) recomputed in both directions (Wolfram, 1.Nf3 d5: an OR ancestor kept a stale ≤M2).
- **Ordering.** CAS-1.1 step 3, after the witness (CAS-1.3) on a new closure. Also used by `backfill_mate_distance` (CMD-4) with `loop_check='always'` and an overlay.

### DTM-2.1 The loser's distance
- **Statement.** For a closed decisive node whose status is a mover-loss (`_loser_distance`): `dists` = every child's `mate_in`; `full` = there is at least one child and none has a null distance; `own` = the own witness length (DTM-4.1). If not `full` and no `own`: return the stored `(mate_in, best_move)` unchanged. If `full`: `counted` = all children, no floor. Otherwise: `counted` = children with the same status and a distance, `floor = own`. `candidate = max(1 + child.mate_in over counted, and floor)`. Then the loop check (DTM-3.1) may set some replies aside. Without exclusions: return `(candidate, stored best_move)` (the best move is not changed by the loser rule unless a reply is excluded).
- **Inputs / outputs.** As DTM-1.1.
- **Code.** `atomicdb/ingest.py:_loser_distance:2226`.
- **Tests.** `tests.py::DtmTests::test_loss_takes_max_plus_one`, `tests.py::DtmTests::test_tb_child_leaves_distance_unknown`, `test_mate_distance.py::LoserWitnessFloorTests::test_a_longer_closed_reply_raises_the_witness`, `::test_a_shorter_closed_reply_leaves_the_witness`, `test_mate_distance.py::AndMirrorTests::test_the_and_parent_recomputes_exactly_downwards` (9 in DTM-2). **Gaps:** none.
- **Determinism.** None.
- **History.** `e718a8e`; #284 (20-Nov, quasa: a page said M20 with a reply closed at M22; the floor is over the witness, not the stored number, so a child that later shortens brings the parent down).
- **Ordering.** As DTM-1.1.

### DTM-3.1 A repetition cannot lengthen a mate
- **Statement.** In the loser rule, the replies that would raise the number are checked for a loop: `looks` when the node has a key and (`loop_check == 'always'`, or `loop_check == 'raise'` and (`mate_in` is null or `candidate > mate_in`)). Walking `counted` by `(-child.mate_in, move)`: stop at the first reply with `1 + mate_in <= floor` (the own witness gives the number from there), or, under `'raise'`, with `1 + mate_in <= stored mate_in`; stop at the first reply whose support chain does **not** reach this node within `MATE_SUPPORT_HOPS` (64) (`support_reaches`); otherwise exclude it and continue. With exclusions: `rest` = the other counted replies; under `full` the own witness becomes the floor again; `value = max(1 + mate_in over rest, floor)` or NULL when nothing is left (the status stays, only the number goes). If the stored `best_move` was excluded, it becomes the longest remaining reply (ties by move name, highest) when that reply gives the value, else the first move of the own line when the own witness gives it, else NULL.
  `support_reaches(start, target)` follows `_support_next` from `start` one indexed query per position, stopping (false) at a dead end, a repeated position or after 64 hops; true when it reaches `target`. `_support_next(node, children)`: nothing for an undecided node; with no number (or a mated node with no own witness and no same-status child with a distance) it stands on the child through `best_move` that still has the same status, unless an own witness exists; the mating side stands on its shortest same-status child by `(mate_in, move != best_move, move)` unless its own witness is at least as short; the mated side stands on its longest same-status reply that fits under its stored number (`1 + mate_in <= stored`), by `(mate_in, move == best_move, move)`, or on the longest overall when nothing fits and it has no own witness, unless its own witness is at least as long. `overrides` lets a caller substitute `(mate_in, best_move)` of keys it holds in memory (the backfill's overlay).
- **Inputs / outputs.** Reads `Edge` with child and parent columns along the chain.
- **Code.** `atomicdb/ingest.py:_loser_distance:2226` (2251-2292), `ingest.py:support_reaches:2189`, `ingest.py:_support_next:2140`, `ingest.py:_SupportNode:2127`.
- **Tests.** `test_mate_distance_cycles.py::RepetitionLoopTests::test_a_full_information_anchor_keeps_its_longest_outside_reply`, `::test_a_longer_defence_that_does_not_come_back_is_still_taken`, `::test_a_loop_longer_than_the_hop_limit_is_not_set_aside`, `test_mate_distance.py::BackfillCycleTests::test_an_ungrounded_cycle_comes_to_rest_instead_of_inflating` (11 in DTM-3). **Gaps:** none found.
- **Determinism.** None in the walk (ties by move name). The cascade looks only on raises, the listed repair always: the two can rest on different numbers for a loop already inflated and at rest.
- **History.** #319 (25-Nov: db0d0bb0 read M145, 289 plies over a won line of 30).
- **Ordering.** Inside DTM-2.1. `CASCADE_REWRITE_LIMIT` (CAS-5.2) stays as the backstop for loops longer than 64 hops.

### DTM-4.1 The own witness
- **Statement.** A node's own distance claim: none unless its closure is `MATE_PV`; none for a `TB_LINE` proof (the line ends in a table win, not in mate); else the length of `won_line` in plies, or the stored `mate_in` when the line is empty (historic rows). `MINIMAX`, `TB` and `SOLVE` closures claim nothing of their own.
- **Code.** `atomicdb/ingest.py:_own_witness_plies:2295`.
- **Tests.** `tests.py::WitnessTests::test_mate_pv_closure_records_line`, `test_mate_distance.py::OwnWitnessTests` (9 in DTM-4). **Gaps:** none.
- **Determinism.** None.
- **History.** `e718a8e`; #223 TB_LINE.
- **Ordering.** Read by DTM-1, DTM-2, DTM-3.

### DTM-5.10 Clock slack of a MINIMAX closure
- **Statement.** For a `MINIMAX` node: `None` for a draw. For a mover-win: `minimax_slack(fen, [(move, child.clock_slack) for children with the node's status], [], mover_wins=True)`, the maximum `edge_slack` over those edges (NULL with none). For a mover-loss: `minimax_slack(fen, [], [(move, child.clock_slack) for all children], mover_wins=False)`, the minimum `edge_slack` over all edges. `edge_slack` is 100 through a capture or pawn move, 0 for a NULL child slack, else `child - 1` clamped to 0..100 (proofs file DTM-5.2, DTM-5.4). Recomputed on every visit of a `MINIMAX` row.
- **Code.** `atomicdb/ingest.py:_minimax_slack:2023`; `logic.py:minimax_slack:676`.
- **Tests.** `test_clock_slack.py` (34 tests). **Gaps:** none.
- **Determinism.** None.
- **History.** `8c04741` (27-Jul).
- **Ordering.** CAS-1.1 step 2.

---

# 8. Revocation, trust and disputes (REV)

### REV-1.1 `revoke_closure`: withdraw a closure and what stood on it
- **Statement.** `pending` starts with `(seed, None)`, processed first in first out, at most `REVOKE_GUARD_LIMIT` (100,000) iterations. For each `(key, via_child)`: lock the row (missing: skip); an open row: skip. For an ancestor (not the seed): a `MINIMAX` row is kept if `logic.backup_status(fen, expanded, [raw child statuses])` still equals its status (raw statuses, no fresh-context rule); any other closure is kept unless its own witness runs through `via_child` (REV-1.3). For the seed: a `TERMINAL` closure is never revoked (returns empty); any other closure, independent or not, is revoked because the caller says its evidence failed. A revoked row is cleared (REV-1.2; `mark_disputed` applies to the seed only) and every parent of it is appended to `pending` with `via_child = key`. If nothing was revoked, return. Otherwise lift tombstones (REV-1.4), run `backup_cascade(revoked)` (what still holds closes again, distances and witnesses of survivors are recomputed), and, if `requeue` and the seed is still open, queue the deep reanalysis (REV-3.1). Event `CLOSURE_REVOKED` `{key, reason (default 'evidence-withdrawn'), chain: first 64 revoked keys, revoked, revived, requeued, truncated}`; `REVOKE_GUARD` if the guard ran out. Returns `{'revoked': [keys in revocation order], 'requeued': task id or None, 'revived': n}`.
- **Inputs / outputs.** Reads and writes the closure columns; `Position.priority`, `updated`; `AnalysisTask`; events.
- **Code.** `atomicdb/ingest.py:revoke_closure:5508`.
- **Tests.** `test_revocation.py::RevokeClosureTests::test_event_records_the_affected_chain`, `::test_intermediate_andor_cuts_the_cascade`, `::test_or_node_survives_when_another_winning_edge_remains`, `test_clock_slack.py::ClosureSlackTests::test_revoking_a_closure_also_withdraws_its_slack`, `test_mate_distance.py::OwnWitnessTests::test_the_witness_comes_back_when_the_short_child_goes_away` (29 in REV-1). **Gaps:** nothing pins `REVOKE_GUARD`; nothing pins the asymmetry that the re-closing cascade applies the fresh-context rule while the keep test does not, so a revoked `MINIMAX` ancestor can stay open where the keep test would have kept it.
- **Determinism.** Parents come back in database order; the result is order-independent because revocation is monotone (a row goes from closed to open at most once, and a row kept early is re-examined when another child is revoked).
- **History.** `bdaddf1` (27-Jul); `e100915` chains; `8c04741` slack.
- **Ordering.** Called by MPV-3.1 (online refutation), SOL-2.4 (trusted solver disproof), `verify_mates`/`recertify_mates` (CMD). Runs CAS-1.1 at its end.

### REV-1.2 Clearing a closure
- **Statement.** `status = 'UNKNOWN'`, `closure = NULL`, `mate_in = NULL`, `clock_slack = NULL`; with `mark_disputed`, `proof = 'DISPUTED'` and `won_line` kept (the refuted witness stays visible); otherwise `proof = NULL`, `won_line = NULL`. `eval_cp`, `backed_*`, `visits`, `best_move` are heuristic knowledge and survive. Saved with those six fields and `updated`.
- **Code.** `atomicdb/ingest.py:_clear_closure:5447`.
- **Tests.** `test_revocation.py`, `test_won_line_chain.py` (`mark_disputed`). **Gaps:** none.
- **Determinism.** None.
- **History.** `bdaddf1`, `8c04741`.
- **Ordering.** Inside REV-1.1.

### REV-1.3 A chain link falls with the link below it
- **Statement.** A non-`MINIMAX` ancestor (a link of a materialised won line) is revoked when its closure is `MATE_PV`, its proof is not `ANDOR`, it has a `won_line`, and the edge `(ancestor, first move of won_line)` leads exactly to the revoked child. An `ANDOR` link proved itself and keeps cutting the cascade.
- **Code.** `atomicdb/ingest.py:_witness_runs_through:5426`.
- **Tests.** `test_mate_distance.py` (`_witness_runs_through`), `test_revocation.py`. **Gaps:** none.
- **Determinism.** None.
- **History.** `e100915` (27-Jul).
- **Ordering.** Inside REV-1.1.

### REV-1.4 Lifting tombstones
- **Statement.** After a revocation, every revoked key and every child of a revoked key with `priority <= DEAD/2` gets `priority = 0.0` and `updated = now` (one level only; grandchildren come back when the selector visits their parents). Returns how many rows were revived.
- **Code.** `atomicdb/ingest.py:_revive_tombstones:5474`.
- **Tests.** `test_selector_v2.py` (`_revive_tombstones`), `test_campaigns.py::SelectorBonusTests::test_a_tombstone_is_not_resurrected_by_any_number_of_votes`. **Gaps:** none.
- **Determinism.** `updated` wall clock (read by the selector's delta pass).
- **History.** `bdaddf1`; `d519cfd` stamps `updated`.
- **Ordering.** Inside REV-1.1, before the re-closing cascade. With D5 the tombstone becomes a computed property.

### REV-2.1 The trust gate
- **Statement.** A line the server could not prove (verdict `INCONCLUSIVE` or `TB_LINE`, i.e. anything but `PROVEN` and `NO_MATE`) closes a child only when the account that delivered the search is trusted: `account_trusted(username)` is true when `ATOMICDB_CLOSURE_TRUST_GATE` is off, false for an empty name, else whether a `TrustedAccount` row with that username exists. The account is `AnalysisTask.delivered_by`, falling back to the payload's `username`. Untrusted lines become pending claims (CLM-4.1). Proven closures (terminal, `PROVEN` AND/OR, minimax, tablebase, certificates) close for everybody. The same gate decides whether a solver disproof may revoke (SOL-2.4) and, through `ATOMICDB_TB_TRUSTED`, whether a worker's 6-man table result is accepted without a server re-probe (TB-2.2).
- **Inputs / outputs.** Reads `TrustedAccount`; managed with `manage.py atomicdb_trust`.
- **Code.** `atomicdb/ingest.py:trust_gate_enabled:5701`, `ingest.py:account_trusted:5705`; caller `ingest_queue.py:apply_job:240` (295-299); the gate itself `ingest.py:ingest_analysis:984` (1127-1138).
- **Tests.** `test_closure_trust.py::ProvenClosuresTests::test_a_proven_mate_closes_for_any_account`, `test_closure_trust.py::CooperativeMateTests::test_a_trusted_account_closes_as_before`, `::test_an_untrusted_account_does_not_close_the_start_position` (12 in REV-2). **Gaps:** none.
- **Determinism.** Trust is mutable state outside the log of facts; a replay must take the trust set as of each landing (an input of the log, plan section 2.1).
- **History.** #223 (11-Nov).
- **Ordering.** Read by `apply_job` before the landing.

### REV-3.1 The deep reanalysis of a refuted witness
- **Statement.** `_queue_disputed_reanalysis(pos)`: if the position has a PENDING task (the one with the highest generation), raise its budget to at least `BUDGET_LADDER[-1]` (2,000,000,000) and set `multipv = DEPTH_MULTIPV` (2); otherwise create one with `generation` = the first unused value from `max(visits, 0)` up, `budget_nodes = 2B`, `multipv = 2`, `source = 'AUTO'`. Never rate-limited; the server-picked floor and the interior cap do not apply.
- **Code.** `atomicdb/ingest.py:_queue_disputed_reanalysis:8690`.
- **Tests.** `test_proofs.py::NewDisputedMateTests::test_new_dispute_does_not_close_and_queues_max_budget`, `test_mate_clamp.py::WitnessRefutedTests::test_it_promotes_a_clamped_task_upwards`, `test_arm_rate_budget.py::UnbudgetedDoorsTests::test_the_disputed_witness_rebuy_is_never_rate_limited` (5 in REV-3). **Gaps:** none.
- **Determinism.** None.
- **History.** `3960f81`.
- **Ordering.** Called by MPV-2.7 (new dispute) and REV-1.1 (after revocation).

### REV-4.1 Solver disproof of a closure
- **Statement.** See SOL-2.4 in the proofs file: a trusted solver `DISPROVED` on a closed, uncertified position revokes it (REV-1.1); an untrusted one is recorded and changes nothing.
- **Code.** `atomicdb/ingest.py:_dispute_from_solver:10243`.

---

# 9. Backed values (BKD)

`backed_eval` is the minimax of what a position's children know, kept apart
from the position's own search (`eval_cp`). `docs/value-semantics.md`
(invariants 1 to 10) is the owner's statement of intent; this section is what
the code does. Four columns are written, and only by the backed ascent
(BKD-7): `backed_eval` (White's view), `backed_move` (the edge the value
comes through, "the pointer"), `backed_plies` (plies of the pointer chain down
to the analysis the number comes from), `backed_nodes` (the "quality": search
nodes that support it, or `PROVEN_QUALITY` = 2^60 for an exact value).

### BKD-1.1 What a decided child contributes
- **Statement.** A child whose status is decided contributes its truth value (+10,000, -10,000, 0; section 1.1), quality `PROVEN_QUALITY` (2^60), plies 0, `exact = True`, its `mate_in` (read only by ties, BKD-6.2), no pointer (`spine` none). Its own `backed_eval` and `eval_cp` are never read.
- **Inputs / outputs.** Child `status`, `mate_in`.
- **Code.** `atomicdb/ingest.py:_child_contribution:2492` (2501-2504), `ingest.py:_ChildValue:2420`.
- **Tests.** `test_backed_eval.py::BackedGuardTests::test_solved_children_enter_with_their_truth_value`, `test_backup_echo.py::ContributionTests::test_a_proven_child_is_never_an_echo` (10 in BKD-1). **Gaps:** none.
- **Determinism.** None.
- **History.** `14e9598` (27-Jul).
- **Ordering.** Reads the statuses the cascade (CAS-1.1) has just written in the same climb.

### BKD-1.2 What an open child contributes
- **Statement.** An open child with a `backed_eval` and `backed_plies <= BACKED_ECHO_PLIES` (256) contributes `backed_eval`, quality `max(backed_nodes, nodes_invested)` (everything invested in the child, not only the leaf that backed it), plies `backed_plies`, pointer `spine = backed_move`, and `floor = is_floor(...)` (BKD-3.1). Otherwise, an open child with an `eval_cp` contributes `eval_cp`, quality `nodes_invested`, plies 0, no pointer (a seeded claim has quality 0). Otherwise the child is uninformed (`value = None`, quality 0).
- **Inputs / outputs.** Child `eval_cp`, `backed_eval`, `backed_nodes`, `nodes_invested`, `backed_plies`, `backed_move`.
- **Code.** `atomicdb/ingest.py:_child_contribution:2492`, `ingest.py:_backed_children_by_parent:4380`.
- **Tests.** `test_backup_echo.py::ContributionTests::test_a_chain_up_to_the_bound_is_read_as_before`, `test_quality_tolerance.py::QualityConvergenceTests::test_bought_depth_closes_the_loop_instead_of_going_silent`. **Gaps:** none.
- **Determinism.** None.
- **History.** `14e9598`; `d5965eb` (28-Jul) quality is the maximum of both (chains had jammed at the weakest leaf).
- **Ordering.** Input of BKD-5.

### BKD-1.3 An echo is no value
- **Statement.** A child whose `backed_plies` exceeds `BACKED_ECHO_PLIES` = 4 x `BACKED_MAX_PLIES` = 256 is read as if it had no backed value (its own `eval_cp`, or nothing). A real pointer chain never repeats a position and an ascent is 64 levels deep; a distance past 256 was counted round a cycle by a number that outlived its source.
- **Code.** `atomicdb/ingest.py:_child_contribution:2492` (2505), constant 2489.
- **Tests.** `test_backup_echo.py::ContributionTests::test_an_echo_is_read_as_the_childs_own_measure`, `test_backup_echo.py::EchoFamilyTests::test_the_echo_dies_and_the_ring_takes_its_best_exit`, `::test_without_the_rule_the_echo_holds_and_never_rests`. **Gaps:** none.
- **Determinism.** None.
- **History.** #81 (26-Oct: 63 positions held 1208 at 2,500 to 3,365 plies a week after their source was gone).
- **Ordering.** Part of BKD-1.2.

### BKD-2.1 Comparing values and weights
- **Statement.** `_better_for_mover(value, reference, stm_white)` is strict (`>` for White, `<` for Black); every guard of BKD-5 uses it, so an equal value never displaces. Quality is compared in nodes: `PROVEN_QUALITY` (2^60) beats any search; the quality guard records a discrepancy below `own_quality * BACKED_QUALITY_TOLERANCE` (0.5) and never vetoes (BKD-5.3); only quality <= 0 vetoes (BKD-5.2). A repetition and a seeded claim carry quality 0.
- **Code.** `atomicdb/ingest.py:_better_for_mover:2526`, constants 2397-2398.
- **Tests.** `test_backed_eval.py::BackedGuardTests::test_backed_never_contradicts_a_proven_status`, `test_backed_eval.py::RepetitionZeroContractTests::test_proven_draws_win_the_tie_against_a_walked_one`, `test_quality_tolerance.py::QualityToleranceTests::test_a_genuinely_shallow_child_displaces_and_buys` (18 in BKD-2). **Gaps:** no test pins that an equal value does not displace the own eval under partial coverage.
- **Determinism.** None.
- **History.** `14e9598`; `8fa3a09` (tolerance); `624aaaa` (no ratio veto).
- **Ordering.** Inside BKD-5.

### BKD-3.1 A borrowed number is a floor
- **Statement.** `is_floor(status, nodes_invested, backed_eval)`: the child has a `backed_eval`, has never been searched (`nodes_invested` 0 or null) and is not decided. Its number is the best of the replies somebody opened: what its mover gets at least; one ply up it is a ceiling on the move. A seeded claim is not a floor (no backed value).
- **Code.** `atomicdb/ingest.py:is_floor:2567`.
- **Tests.** `test_backprop_floor.py::FloorTests` (16 tests). **Gaps:** none.
- **Determinism.** None.
- **History.** #62 (11-Oct).
- **Ordering.** Computed in BKD-1.2; judged in BKD-3.3.

### BKD-3.2 The lines of a showcase
- **Statement.** `_showcase_lines(last_analysis)`: empty when there is no showcase bound (BKD-4.1); otherwise the set of moves heading the current lines (`move`, or the first move of `pv`; `prior_pass` lines excluded).
- **Code.** `atomicdb/ingest.py:_showcase_lines:2579`.
- **Tests.** `test_backprop_floor.py::FloorTests::test_a_floor_outside_the_lines_does_not_beat_them`. **Gaps:** none.
- **Determinism.** None.
- **History.** #62.
- **Ordering.** Input of BKD-3.3.

### BKD-3.3 The multipv certificate holds against a floor
- **Statement.** Inside `_backed_for`, an informed child that is not a repetition is **refused** when it is a floor, the parent has a showcase bound, the child's move is not one of the showcase's lines, and its value is strictly better for the mover than the bound (`_beats_certificate`). A refused child is turned into an uninformed one (value none) for everything that follows. Each refused child adds a discrepancy `(move, parent, min(own_quality, REQUEST_BUDGET_LADDER[0] = 128,000,000))` (a search of the move itself settles it, at a visitor's rung). If no informed child is left, the result is `(own eval, no move, 0, own quality)` when there is an own eval, else nothing.
- **Inputs / outputs.** Child value, floor flag, move; parent `last_analysis`, `eval_cp`, `nodes_invested`.
- **Code.** `atomicdb/ingest.py:_beats_certificate:2599`, `ingest.py:_backed_for:3893` (3939-3960).
- **Tests.** `test_backprop_floor.py::FloorTests::test_a_floor_outside_the_lines_does_not_beat_them`, `::test_a_floor_that_respects_the_bound_still_travels`, `::test_once_the_move_is_searched_its_value_backs_up`, `test_backprop_floor.py::FloorAndTheBulkButtonTests::test_a_floor_standing_on_a_search_is_bought_by_the_rule` (27 in BKD-3). **Gaps:** none.
- **Determinism.** None.
- **History.** #62 (11-Oct; Eclipsia, 5-Oct: −363 through an unsearched ...Bh3 whose own search later read +1491).
- **Ordering.** Step 4 of BKD-5.1.

### BKD-3.4 The same predicate for a page
- **Statement.** `refuses_floor(row, child, move)` applies BKD-3.3 to one row of the explorer, with the two positions in hand: the table shows a refused floor muted and the arrow skips it (VIEW-2). One predicate, so the table and the header do not disagree.
- **Code.** `atomicdb/ingest.py:refuses_floor:2605`; caller `views.py:_child_moves:6141`.
- **Tests.** `test_backprop_floor.py` (view tests). **Gaps:** none.
- **Determinism.** None.
- **History.** #62.
- **Ordering.** Read side only.

### BKD-4.1 The showcase bound
- **Statement.** `_showcase_bound(last_analysis)`: with two or more current lines (excluding `prior_pass`), the `eval_cp` of the **last** current line when it is an int; otherwise none (one line is the node's own eval; a mate line without `eval_cp` bounds nothing). It certifies that no move outside the showcase is better for the mover than that number.
- **Code.** `atomicdb/ingest.py:_showcase_bound:3150`.
- **Tests.** `test_backprop_floor.py::FloorTests::test_the_bound_is_the_last_line_and_not_the_own_number`, `::test_without_two_lines_there_is_no_certificate_to_hold`, `test_showcase_bound.py::ShowcaseBoundTests::test_bound_gives_permission_but_never_wins_the_negamax` (33 in BKD-4). **Gaps:** none.
- **Determinism.** None.
- **History.** `624aaaa` (10-Aug, Wolfram); `f33b3d0` (16-Aug) the bound grants permission and never competes.
- **Ordering.** Read by BKD-3.3 and BKD-5.1. `_decide_level` reads `last_analysis` only for rows with a gap (not expanded, or a child uninformed or a floor); for other rows the bound is none, which changes nothing because `bounded` requires a gap (BKD-4.3) and floors are only judged on rows that have one.

### BKD-4.2 Partial coverage
- **Statement.** `coverage_is_partial(row, children)`: true when no child is informed, or the row is not expanded, or some child is uninformed. Complete coverage is an expanded row with every child informed.
- **Code.** `atomicdb/ingest.py:coverage_is_partial:3134`.
- **Tests.** `test_backed_eval.py::BackedGuardTests::test_and_node_ignores_an_optimistic_partial_min`, `::test_and_node_accepts_a_proven_better_defence`, `test_adversarial_explorer.py::FragileMateClaimTests::test_partial_coverage_is_exactly_what_cuts_the_proof_authority`. **Gaps:** none.
- **Determinism.** None.
- **History.** `7a592e9`.
- **Ordering.** BKD-5.1; also defines the fragile mate claims of the fragile arm (queue file).

### BKD-4.3 Complete with a bound
- **Statement.** `bounded` = the row has a bound and (some child is uninformed, or coverage is partial). `complete` = `bounded` or not partial. A bound therefore lets the negamax run over the informed children as if the table were complete; it never supplies a value of its own (a bound with no informed child backs nothing).
- **Code.** `atomicdb/ingest.py:_backed_for:3893` (3982-3990).
- **Tests.** `test_showcase_bound.py::ShowcaseBoundTests::test_bound_gives_permission_but_never_wins_the_negamax`. **Gaps:** none.
- **Determinism.** None.
- **History.** `624aaaa`, `f33b3d0`.
- **Ordering.** BKD-5.1.

### BKD-5.1 `_backed_for`: one position's backed value
- **Statement.** Returns `(value, move, plies, quality)` for `row` given `_ChildValue`s of **all** its edges (uninformed ones carry `value = None`), a bound (BKD-4.1) and the showcase lines (BKD-3.2). It never reads the database. In order:
  1. A decided row returns `(truth value, row.best_move, 0, PROVEN_QUALITY)`: a backed value never contradicts the row's status.
  2. No informed child: `(None, None, 0, 0)`.
  3. Every informed child marked as a repetition (`cycles`, LOOP-1, LOOP-2) is set to value 0, quality 0 (a fact about a walk, with no search weight).
  4. `own = row.eval_cp`, `own_quality = row.nodes_invested` (0 if null). Floors are refused (BKD-3.3).
  5. `bounded`, `complete` (BKD-4.3). `best = _best_child(informed, stm, row.best_move)` (BKD-6.1).
  6. If `best` is a repetition: append `(best.move, row.key, own_quality)` to `cycle_picks` (bought by BKD-11.3, whether or not it decides); if not `complete`, return `(own, None, 0, own_quality)` when `own` exists, else nothing. A repetition never decides a node with moves nobody opened.
  7. The zero-support veto (BKD-5.2), when it applies, replaces `best` or returns.
  8. If not `complete`: with `own`, the directional guard (BKD-5.3); without `own`, the anchorless loss (BKD-5.4).
  9. `quality = best.quality`, cut to `row.nodes_invested` when (not `complete` or `bounded`) and it is a proof's quality (BKD-5.5).
  10. Return `(best.value, best.move, 1 + best.plies, quality)`.
- **Inputs / outputs.** Row `status`, `fen`, `best_move`, `eval_cp`, `nodes_invested`, `expanded`; children; out-parameters `discrepancies`, `cycle_picks`.
- **Code.** `atomicdb/ingest.py:_backed_for:3893`.
- **Tests.** `test_backed_eval.py::RepetitionZeroContractTests::test_a_real_alternative_better_than_the_draw_wins_the_backup`, `::test_a_repetition_does_not_push_a_node_off_its_own_measure`, `test_backup_ties.py::OwnMoveTests::test_2bd63fd6_keeps_its_best_line` (58 in BKD-5; most of `test_backed_eval.py`'s 95 tests exercise it). **Gaps:** none found in the main paths.
- **Determinism.** None: a pure function of its inputs, and ties are total (BKD-6.1).
- **History.** `14e9598` (27-Jul) through #80 (26-Oct): see BKD-5.2 to 5.5 and the history table in section 12.
- **Ordering.** Called by `_decide_level` (BKD-7.2), by the loop rules (LOOP-2.2, LOOP-3.1, LOOP-4.1) and by `_line_verdict`.

### BKD-5.2 The zero-support veto, and a vetoed claim set aside
- **Statement.** Applies when `own` exists, `best.quality <= 0`, (`bounded` or not `complete`), and `best` is strictly better for the mover than `own`: a favourable number with no engine node behind it (a walked line, a seeded claim, a repetition) does not displace a measurement. It adds the discrepancy `(best.move, row.key, own_quality)`. Then `vetoed` = informed children with quality <= 0 better for the mover than `own`. If any vetoed child is a repetition or is the row's own `best_move`, the candidates are only children with quality > 0 that are better for the mover than `own`; otherwise the candidates are every informed child except the vetoed ones. No candidate: return `(own, None, 0, own_quality)`. Else `best = _best_child(candidates)`, and if that is a repetition it is appended to `cycle_picks`. Processing continues with step 8 of BKD-5.1 (under partial coverage the directional guard still keeps `own` unless a candidate beats it; with a bound the candidates decide even below `own`).
- **Code.** `atomicdb/ingest.py:_backed_for:3893` (4009-4072).
- **Tests.** `test_backprop_claims.py::VetoedClaimTests::test_the_searched_reply_backs_up_past_the_refused_claim`, `::test_a_refused_claim_with_nothing_searched_behind_it_changes_nothing`, `test_backup_ties.py::VetoedClaimTests::test_e214fcc0_is_no_longer_frozen_at_its_own_search`, `::test_c8b0b2e2_falls_back_to_its_second_line`, `test_backup_ties.py::VetoGuardTests::test_a_repetition_that_beats_the_own_number_keeps_it`, `::test_a_claim_hiding_a_repetition_hands_over_its_question`. **Gaps:** none.
- **Determinism.** None.
- **History.** `8fa3a09` (28-Jul) quality tolerance; `624aaaa` (10-Aug) only zero support vetoes; #49 (4-Oct) the best measured child backs up after a veto; #80 (26-Oct) a vetoed claim is set aside, with the two exceptions (26 of 40 sampled positions needed the own-move exception).
- **Ordering.** Step 7 of BKD-5.1.

### BKD-5.3 The directional guard under partial coverage
- **Statement.** When coverage is not complete and `own` exists: if `best` is not strictly better for the mover than `own`, return `(own, None, 0, own_quality)` (unopened moves may hide something better, so a partial min never lowers the mover's own number). If it is better, it backs up whatever its support; when `best.quality < own_quality * BACKED_QUALITY_TOLERANCE` (0.5) the discrepancy `(best.move, row.key, own_quality)` is recorded so the missing depth is bought (BKD-11.1).
- **Code.** `atomicdb/ingest.py:_backed_for:3893` (4073-4092).
- **Tests.** `test_quality_tolerance.py::QualityToleranceTests::test_a_genuinely_shallow_child_displaces_and_buys`, `test_backed_eval.py::BackedGuardTests::test_shallow_child_displaces_at_once_and_buys_convergence`. **Gaps:** none.
- **Determinism.** None.
- **History.** `14e9598`; `8fa3a09` (28-Jul, 0.08% support difference had blocked a move); `624aaaa` (8-Aug) the ratio no longer vetoes.
- **Ordering.** Step 8 of BKD-5.1.

### BKD-5.4 Without an anchor, a loss does not back up
- **Statement.** When coverage is not complete and the row has no `eval_cp`: if `best.value` is in the mate band against the mover (`<= -9000` for White to move, `>= 9000` for Black), return nothing. A gain for the mover still backs up from a single child.
- **Code.** `atomicdb/ingest.py:_backed_for:3893` (4093-4111).
- **Tests.** `test_backed_eval.py::BackedIngestTests::test_a_walked_mate_line_stops_at_the_losers_partial_node`. **Gaps:** none.
- **Determinism.** None.
- **History.** `9e40758` (14-Aug: a ghost backed 10,000 through a transposition topped a table).
- **Ordering.** Step 8 of BKD-5.1.

### BKD-5.5 A proof's authority ends at the first partial node
- **Statement.** When the result would carry `PROVEN_QUALITY` and the row is not complete or is complete only by a bound, the quality written is the row's own `nodes_invested` (0 for a walked node). The value still travels; its weight does not.
- **Code.** `atomicdb/ingest.py:_backed_for:3893` (4112-4126).
- **Tests.** `test_adversarial_explorer.py::FragileMateClaimTests::test_partial_coverage_is_exactly_what_cuts_the_proof_authority`. **Gaps:** none.
- **Determinism.** None.
- **History.** `393c6ae` (28-Jul: 9994 BACKED painted over an unsearched walked line).
- **Ordering.** Step 9 of BKD-5.1.

### BKD-6.1 The tie order: which child a position stands on
- **Statement.** `_best_child(children, stm, own_move)`. The children are first sorted by move name (`move or ''`), then the **maximum** of a key is taken (Python `max` keeps the first of equal keys, so a complete tie goes to the alphabetically first move). Normal key: `(sign * value, grounding, proof rank, quality, -plies, move == own_move)` with `sign = +1` for White, `-1` for Black. When **every** child's `_defence_moves` is not none (every informed move is a mate against the mover, BKD-6.3) the key is instead `(grounding, defence length, proof rank, quality, -plies, move == own_move)`: the longest defence, on one scale; the value written is still the chosen child's own number.
- **Inputs / outputs.** `_ChildValue` fields; the row's `best_move` as `own_move`.
- **Code.** `atomicdb/ingest.py:_best_child:3384`.
- **Tests.** `test_backup_ties.py::TieOrderTests::test_a_better_value_still_wins`, `::test_a_full_tie_goes_to_the_own_search_move_first`, `test_pointer_loop_grounding.py::GroundingTests::test_a_better_value_still_wins_whatever_its_chain`, `test_backup_ties.py::LongestDefenceTests::test_a_proven_longer_mate_beats_an_engine_shorter_one`, `::test_an_engine_longer_mate_beats_a_proven_shorter_one`, `test_arrow_tie_order.py` (73 in BKD-6). **Gaps:** none found.
- **Determinism.** None: total order ending on the move name. This is the "ties by name" that D3 asks to generalise.
- **History.** #69 (19-Oct) returning moves lose ties; #70 grounding; #80 (26-Oct) proof rank, support, chain, own move, name (a position had stood on a mate in one while an open move tied, by storage order); #284 (20-Nov) longest defence.
- **Ordering.** BKD-5.1 steps 5 and 7.

### BKD-6.2 Proof rank
- **Statement.** `_proof_rank(child, stm)`: `(0, 0)` for an open child and for a proven draw; for a decided win of the side to move `(1, -mate_in)` (soonest mate first), `(1, -inf)` when the distance is unknown; for a decided loss `(-1, mate_in)` (latest mate first), `(-1, -inf)` when unknown (last of its kind).
- **Code.** `atomicdb/ingest.py:_proof_rank:3341`.
- **Tests.** `test_backup_ties.py::TieOrderTests`. **Gaps:** none.
- **Determinism.** None.
- **History.** #80.
- **Ordering.** Inside BKD-6.1.

### BKD-6.3 Defence length when every move is mated
- **Statement.** `_defence_moves(child, stm)`: none unless the child's value is non-zero and against the mover. A decided child: `ceil(mate_in / 2)` = `(mate_in + 1) // 2` moves, `-inf` when unknown. An open child with `|value| >= 10,000` (a proven reply below an open node): `+inf` (ranks first). An open child above the tablebase ceiling (`|value| > 9500`): `10,000 - |value|` (an engine's mate in N). Anything else: none (so the normal key applies).
- **Code.** `atomicdb/ingest.py:_defence_moves:3357`.
- **Tests.** `test_backup_ties.py::LongestDefenceTests` (test names in BKD-6.1). **Gaps:** no direct test of the `+inf` case.
- **Determinism.** None.
- **History.** #284 (465afa99 stood on an engine #5 while f8e8 was proven to last 25 plies).
- **Ordering.** Inside BKD-6.1.

### BKD-6.4 Grounding
- **Statement.** `_grounding(child)`: 0 when the move's pointer chain comes back to the parent (`returns`), 1 when it runs into a loop elsewhere (`circular`), 2 otherwise (a source: an own search, a proven position, a leaf, or a chain the walk could not follow to its end). Higher wins ties. `returns` and `circular` are set per level by `_decide_level` (BKD-7.2) and by the loop rules.
- **Code.** `atomicdb/ingest.py:_grounding:3279`.
- **Tests.** `test_pointer_loop_grounding.py::SelfProtectingLoopTests::test_a_drain_prefers_the_source_to_the_loop_at_the_same_value` (6 tests). **Gaps:** none.
- **Determinism.** None.
- **History.** #70 (19-Oct: a ring rested at 1208 protected by its neighbours).
- **Ordering.** Inside BKD-6.1.

### BKD-7.1 The backed ascent
- **Statement.** `backup_backed_evals(seed_keys, max_plies=64, open_only=False, generation=0, report=None)`. `frontier` = the seeds deduplicated in order, empty keys dropped. Per level, while the frontier is not empty and fewer than `max_plies` levels have run: read the rows of the frontier (`key IN frontier`, plus `status = 'UNKNOWN'` with `open_only`; **no ORDER BY**); none: stop. Add them to `processed`. Decide the level (BKD-7.2). Then for each row, in the order the database returned them: a row that waits (BKD-7.3) is skipped (its stored pointer goes back into the spine cache); a decision equal to what is stored (all four columns) is recorded in `rested`; otherwise it is classified for waking (BKD-8.1), the four columns are set and the row is queued for one `bulk_update`. After the write: buy quality convergence and cycle disambiguation for the level (BKD-11.1, BKD-11.3). `wake` = the loop positions outside the level not yet looked at in this ascent (sorted) plus the waiting rows (sorted). If nothing changed: with a wake list, the next level is the wake list (no revisit counted); with none, rest, unless rows valued on a returning line were seen and the settling look has not run (BKD-7.6). If something changed: stop with guard `node-budget` when `processed >= BACKED_MAX_NODES` (50,000) (BKD-9.1); otherwise the next frontier is `_parents_to_wake(propagate, standing)` (BKD-8.2) plus the wake list, deduplicated, each key admitted at most `BACKED_MAX_REVISITS` (64) times per ascent (BKD-7.4). When the level bound ends the loop with keys left: guard `ply-guard`. Returns the number of rows written.
- **Inputs / outputs.** Reads `Position` (`key, fen, status, expanded, eval_cp, nodes_invested, best_move, backed_*`, and `last_analysis` of gap rows), `Edge` with child columns; writes the four backed columns (`bulk_update`, which does **not** touch `updated`); tasks and events (BKD-9, BKD-11). `report`: `levels`, `processed`, `changed`, `guard`, `left`, `rested_keys`, `written_keys` (level order), and on abort `aborted`, `frontier`.
- **Code.** `atomicdb/ingest.py:backup_backed_evals:4555`.
- **Tests.** `test_backed_eval.py::BackedIngestTests::test_analysis_of_a_grandchild_reaches_the_root`, `test_backprop_wakes.py::LateWakeTests::test_an_ascent_leaves_nothing_for_the_next_one_to_fix`, `::test_the_last_wave_of_an_ascent_still_reaches_the_node`, `test_backed_eval.py::BackedRepetitionTests::test_a_transposition_that_does_not_come_back_keeps_its_value` (59 in BKD-7). **Gaps:** nothing pins `BACKED_MAX_NODES` by name (the `node-budget` guard is tested only for proof numbers).
- **Determinism.** Row order within a level is database order: it does not change any decision (a level is decided as a whole, BKD-7.2), but it orders the purchases of the level (which ones fit under the arm caps) and the `rested`/`written` lists whose first 256 entries the meeting-point recheck reads (CAS-6.4). `bulk_update` leaves `updated` alone, so the selector's delta pass does not see backed changes (queue file PRI-3).
- **History.** `14e9598` (27-Jul) through #333 (27-Nov); see the history table.
- **Ordering.** After CAS-1.1 in every climb; before CAS-6.

### BKD-7.2 Deciding one level
- **Statement.** `_decide_level(rows, spines, loop_budget)`, which writes nothing and buys nothing: (1) read the children of every row (BKD-1). (2) Mark returning children: `_draw_cycling_children(children, spines, circular=elsewhere)` (LOOP-1.1); `returning` = the open rows with a child marked. (3) For every child, `returns = cycles` and `circular = (parent, move) in elsewhere` (BKD-6.4). (4) Value the returning children of the open rows on the line that returns (LOOP-2.1). (5) Read `last_analysis` for the gap rows (not expanded, or some child uninformed or a floor). (6) For each row in order: tell the spine cache its stored pointer, decide it with `_backed_for` (BKD-5.1) collecting `discrepancies` and `cycle_picks`, tell the cache the decided pointer. (7) Open the pointer loops the level is about to write, or value them at their best exit (LOOP-3.1, LOOP-4.1); `wake` = loop positions outside the level, `ruled` = rows the loop rule decided. (8) `waiting` = the rows that wait (BKD-7.3). Shared by the ascent and by `stale_backed_rows` (BKD-12.1), so "stale" means "the ascent would rewrite it".
- **Code.** `atomicdb/ingest.py:_decide_level:4475`, `ingest.py:_LevelDecision:4468`.
- **Tests.** Through BKD-7.1 and BKD-12.1. **Gaps:** none.
- **Determinism.** The returning-line valuation (step 4) and the loop rule (step 7) stop on wall-clock deadlines (LOOP-5). Everything else is a function of the rows and the spine cache.
- **History.** #85 (26-Oct) factored out.
- **Ordering.** Inside BKD-7.1.

### BKD-7.3 A level never writes a number it is rewriting
- **Statement.** `_rows_that_wait(rows, children, decided, ruled)`: `changed` = rows whose decision differs from what they store; `moved` = changed rows whose value, plies or quality differ (not only the move). A changed row not decided by the loop rule, with a decided move, `stands_on` its child through that move when the child is in `moved` and the child is not a repetition (`cycles`) nor returning. Then, for each start in key order, follow `stands_on` until a node already decided, outside the map, or repeated: a ring (the chain comes back on itself) is written entirely; the rows of the chain above the ring, or above a node outside it, alternate from the bottom: the row standing directly on a row that writes waits, the one above it writes, and so on (the chain starts from "the row below writes" when the walk ended on a row outside `stands_on` or on a ring, and from the decision already computed for the row it ended on otherwise). Returns the rows that do not write now; they keep their stored values and are decided again in the next level (they join `wake`).
- **Code.** `atomicdb/ingest.py:_rows_that_wait:3846`.
- **Tests.** `test_loop_best_exit.py` (the one test file naming it). **Gaps:** the alternation along a chain of three or more rows of one level is not pinned directly.
- **Determinism.** None.
- **History.** #77 (21-Oct: families that never came to rest fell from 82 to 15 of 8,900 replayed).
- **Ordering.** Last step of BKD-7.2.

### BKD-7.4 The revisit cap
- **Statement.** A key may enter the frontier through the wake path at most `BACKED_MAX_REVISITS` (64, the level bound) times per ascent; a refused wake is recorded in `dropped` and reported by a `BACKED_GUARD` event with reason `revisit-cap` (BKD-9.1). Keys entering through the "nothing changed but wake" path are not counted.
- **Code.** `atomicdb/ingest.py:backup_backed_evals:4555` (4663-4670, 4708-4712).
- **Tests.** `test_backprop_wakes.py::LateWakeTests::test_a_wake_is_never_dropped_in_silence`. **Gaps:** none.
- **Determinism.** None.
- **History.** #49 (4-Oct; it was 2 and dropped wakes silently).
- **Ordering.** Inside BKD-7.1.

### BKD-7.5 The node budget
- **Statement.** When a level changed something and the ascent has processed at least `BACKED_MAX_NODES` (50,000) rows in total, it stops; `left` = the parents it would wake plus the wake list, reported with reason `node-budget`. Not carried by the ingest carry (only `ply-guard` is, BKD-9.2); the sweep picks it up from the event.
- **Code.** `atomicdb/ingest.py:backup_backed_evals:4555` (4658-4661).
- **Tests.** none by name. **Gaps:** the node budget of the backed ascent is not pinned.
- **Determinism.** Where the cut falls depends on the shape of the family; deterministic given the input.
- **History.** `14e9598`.
- **Ordering.** Inside BKD-7.1. Plan D2.

### BKD-7.6 The settling look
- **Statement.** When a level changed nothing and has no wake list, but rows valued on a returning line were seen during the ascent (`returning`), the ascent runs one more level over exactly those rows (clearing the set, `settling = True`). If that level changes nothing either, it rests; if it changes something, ascending resumes normally (`settling = False`).
- **Code.** `atomicdb/ingest.py:backup_backed_evals:4555` (4644-4657).
- **Tests.** `test_repetition_progress.py` (`settling`), `test_backprop_repetition.py::ReturningLineTests::test_one_touch_settles_the_whole_loop`. **Gaps:** none.
- **Determinism.** None.
- **History.** #49 / #69.
- **Ordering.** Inside BKD-7.1.

### BKD-7.7 A database error in the middle of an ascent
- **Statement.** On a `DatabaseError` the level being written rolls back. Outside a transaction, the lost frontier is the current frontier plus, if the level had been written before the error, the parents of the written rows; it is recorded in a `BACKED_GUARD` event with reason `aborted`, `report` gets `aborted` and that `frontier`, and the error is raised. Inside a transaction (an ingest) nothing is recorded: the whole job rolls back and is retried.
- **Code.** `atomicdb/ingest.py:backup_backed_evals:4555` (4674-4698).
- **Tests.** `test_stale_backed_sweep.py::LockConflictTests::test_a_retry_climbs_on_from_the_aborted_level`. **Gaps:** none.
- **Determinism.** v1 plumbing (no database errors in v2's writer).
- **History.** #85.
- **Ordering.** Inside BKD-7.1; BKD-13.1 retries from the recorded frontier.

### BKD-8.1 The noise cut
- **Statement.** A written row is classified: **propagate** (wake every parent) when `backed_eval` goes from null to a value or back, when `backed_move` changes, or when the value moves by at least `BACKED_EPSILON_CP` (10 cp); otherwise **standing** (wake only the parents whose `backed_move` is the edge to this row). A row that changes only its plies or quality, or its value by less than 10, therefore does not wake parents standing on a sibling.
- **Code.** `atomicdb/ingest.py:_backed_worth_propagating:4313`, `ingest.py:_backed_stored:4308`.
- **Tests.** `test_backed_eval.py::BackedPropagationCostTests::test_sub_epsilon_drift_still_reaches_whoever_stands_on_it`, `test_backed_eval.py::BackedArgmaxDriftTests::test_a_drift_that_flips_the_parents_edge_is_never_silenced` (7 in BKD-8). **Gaps:** none.
- **Determinism.** None, but it breaks batch-size independence (EXP-6.2): what a parent standing on a sibling reads depends on which updates woke it. The error is bounded by 10 cp and the sweep closes it. Plan D2 proposes dropping it once the port is exact.
- **History.** `14e9598`; `ab4226d` (5-Aug: a 4 cp drift left a parent on an edge no child held; parents standing on the edge are always woken).
- **Ordering.** BKD-7.1, after the decision of each row.

### BKD-8.2 Which parents wake
- **Statement.** `_parents_to_wake(propagate, standing)`: one query on the edges into those rows; a parent is woken when the child is in `propagate`, or when the parent's `backed_move` equals the edge's move (it stands on that child). Returns a set (iteration order is set order; the frontier is deduplicated in that order).
- **Code.** `atomicdb/ingest.py:_parents_to_wake:4352`.
- **Tests.** As BKD-8.1. **Gaps:** none.
- **Determinism.** Set order decides the order of the next frontier, which only matters for the purchase order and the meeting-point list (BKD-7.1).
- **History.** `ab4226d`.
- **Ordering.** BKD-7.1.

### BKD-9.1 Guard events
- **Statement.** An ascent that stops with work left writes one `BACKED_GUARD` event `{reason, processed, plies, seed_count, generation, left, keys: first 2,000 sorted keys}` for `ply-guard` or `node-budget`, plus one for `revisit-cap` (with `dropped`) when wakes were refused, plus one for `aborted` on a database error outside a transaction. The event is the record a later ascent starts from (BKD-9.2, BKD-12.2).
- **Code.** `atomicdb/ingest.py:_guard_event:4419`, `backup_backed_evals:4555` (4699-4712).
- **Tests.** `test_backed_eval.py::BackedChainTests::test_ply_guard_is_recorded`, `test_stale_backed_sweep.py::CutAscentTests::test_a_cut_ascent_leaves_its_frontier_on_record` (11 in BKD-9). **Gaps:** the 2,000-key truncation is not pinned.
- **Determinism.** None.
- **History.** #85.
- **Ordering.** End of BKD-7.1.

### BKD-9.2 An ingest carries its own cut on
- **Statement.** Inside `apply_job`, ascents cut by the **ply guard** add their `left` keys to the job's `carried_cuts` list. After the job commits, `carry_cut_ascents(keys)`: deduplicate; nothing, or more than `CARRY_MAX_KEYS` (64): do nothing (the sweep takes it from the event); otherwise `backup_backed_evals_retrying(keys, open_only=True, generation=1)` outside any transaction. A failure is logged; the job stays applied.
- **Code.** `atomicdb/ingest.py:carried_cuts:4454`, `ingest.py:carry_cut_ascents:4803`; caller `ingest_queue.py:apply_job:240` (366-374) and the drainer.
- **Tests.** `test_stale_backed_sweep.py::IngestCarryTests::test_the_frontiers_of_a_block_are_collected`, `::test_an_ascent_outside_an_ingest_collects_nothing`. **Gaps:** none.
- **Determinism.** None.
- **History.** #89 (27-Oct: the main line was 63 to 66 plies of pointers long).
- **Ordering.** After the job's commit, after the proof refresh, before `then_verify`. In v2, an ascent runs to rest inside its batch (plan 2.2.3), so this rule and the cut disappear only if D2 drops `BACKED_MAX_PLIES`.

### BKD-10.1 The headline value
- **Statement.** `known_eval_of(status, backed_eval, eval_cp)` = the truth value of a decided status, else `backed_eval` if not null, else `eval_cp`. `best_known_eval(pos)` applies it to a row. One precedence everywhere a number is quoted (explore header, the row in the parent's table, the API `score`, the map inspector), and in the selector's regret and the frontier ranking.
- **Code.** `atomicdb/ingest.py:known_eval_of:5135`, `ingest.py:best_known_eval:5152`.
- **Tests.** `test_headline_value.py::MapInspectorPrecedenceTests::test_the_display_field_walks_the_three_rungs`, `test_root_extremes.py::ValueTests::test_the_value_is_the_one_the_explorer_headlines`, `test_selector_leak.py::RegretUsesBestKnownEvalTests::test_the_frontier_ranks_a_backed_child_as_informed` (79 in BKD-10, mostly views). **Gaps:** none.
- **Determinism.** None.
- **History.** `14e9598`; `53567d5` columns version; owner decision 6-Aug (one node, one number).
- **Ordering.** Read side and selector.

### BKD-11.1 Quality convergence: buy the missing depth
- **Statement.** At the end of every level, each discrepancy `(move, parent, own_quality)` (BKD-3.3, BKD-5.2, BKD-5.3) may buy the child a search. Room = `min(hourly room of the 'quality' arm, QUALITY_QUEUE_CAP (50) - PENDING tasks of that arm)`; none: nothing. For each discrepancy in order until `room` purchases: the edge `(parent, move)`; the child must be `UNKNOWN`; `budget = _rung_matching(own_quality)` (BKD-11.2); held at 128M when the interior cap applies (`caps_interior`, queue file); skipped when `child.nodes_invested >= budget`; then the server-picked floor (`server_picked_budget(budget, pos=child)`, 128M minimum). `get_or_create` the task `(position = child, generation = child.visits)` with that budget, `multipv_for(child.visits, budget)`, source `FILL`, arm `quality`; an existing PENDING task with a smaller budget is raised with a compare-and-set on its old budget. Each creation or raise counts. A `QUALITY_CONVERGENCE` event when anything was bought.
- **Inputs / outputs.** `AnalysisTask` rows; event.
- **Code.** `atomicdb/ingest.py:_queue_quality_convergence:4234`; arm room `ingest.py:_arm_budget_room:9143`.
- **Tests.** `test_backed_eval.py::BackedGuardTests::test_shallow_child_displaces_at_once_and_buys_convergence`, `test_backprop_floor.py::FloorTests::test_the_search_that_would_settle_it_is_bought`, `test_arm_rate_budget.py::QualityRateTests::test_the_convergence_stops_at_its_hourly_count` (40 in BKD-11). **Gaps:** none.
- **Determinism.** **The hourly room is a rolling one-hour window on the wall clock** (`ARM_RATE_WINDOW`, tasks of the arm counted on `created`; default `ATOMICDB_ARM_RATE_QUALITY` = 120 an hour, read from settings or the environment; raising an existing task does not add a row, so it counts inside the call but not in later windows); which discrepancies fit depends on discrepancy order (row order of the level, BKD-7.1). Plan D1/D3.
- **History.** `8fa3a09` (28-Jul); `c76fcbe` (17-Aug) hourly rate; #63 (14-Oct) the parent's rung, not the one above; #68 (18-Oct) 128M floor; #224 (11-Nov) interior cap.
- **Ordering.** After each level's write in BKD-7.1. Never from `stale_backed_rows`.

### BKD-11.2 Rungs
- **Statement.** `BUDGET_LADDER` = 8M, 32M, 128M, 512M, 2B. `_rung_matching(nodes)`: the first rung with `nodes <= rung * 1.01` (an engine stops a few nodes past its budget), else 2B. `_rung_at_least(nodes)`: the first rung `>= nodes`, else 2B.
- **Code.** `atomicdb/ingest.py:_rung_matching:4143`, `ingest.py:_rung_at_least:4130`.
- **Tests.** `test_quality_tolerance.py`, `test_idle_repair.py` (`RUNG_OVERSHOOT`). **Gaps:** none.
- **Determinism.** None.
- **History.** #63.
- **Ordering.** BKD-11.1, BKD-11.3.

### BKD-11.3 Cycle disambiguation
- **Statement.** Each `cycle_pick` `(move, parent, own_quality)` (BKD-5.1 step 6, BKD-5.2, LOOP-4.1) may buy the child a deeper pass, from the same arm and the same room as BKD-11.1 (computed again for this call): the child must be `UNKNOWN`; `budget = _rung_at_least(max(own_quality, child.nodes_invested + 1))` (strictly deeper than what the child has), then `server_picked_budget(budget)` **without** the interior cap; the same `get_or_create` / raise as BKD-11.1. A `CYCLE_DISAMBIGUATION` event when anything was bought. Picks are recorded before the "nothing changed" test, so a stale repetition retries its purchase on every pass (task deduplication absorbs the repeats).
- **Code.** `atomicdb/ingest.py:_queue_cycle_disambiguation:4155`.
- **Tests.** `test_cycle_disambiguation.py::CycleDisambiguationTests::test_a_chosen_cycle_zero_buys_the_child_a_deeper_pass`, `::test_a_stale_pin_retries_the_purchase_on_every_pass`, `test_arm_rate_budget.py::QualityRateTests::test_the_cycle_disambiguation_shares_the_same_bag`. **Gaps:** none.
- **Determinism.** As BKD-11.1.
- **History.** `ec871a8` (11-Aug); `8ea0f62` (14-Aug) also when it does not decide; #68.
- **Ordering.** Right after BKD-11.1 in each level.

### BKD-12.1 Stale rows
- **Statement.** `stale_backed_rows(keys)`: the open rows among the keys, decided as one level (`_decide_level` with a fresh spine cache and a fresh 1 s loop budget); returns `{key: (stored, decided)}` for every row whose decision differs from its stored four columns (including rows that would only wait). Writes and buys nothing. A row's verdict depends on the other rows of the same call (waiting rows, loops of the level).
- **Code.** `atomicdb/ingest.py:stale_backed_rows:4722`.
- **Tests.** `test_stale_backed_sweep.py::AtRestTests::test_no_stale_rows_once_the_backup_is_at_rest` (29 tests in the file). **Gaps:** none.
- **Determinism.** The loop rule's wall-clock budget (LOOP-5).
- **History.** #85.
- **Ordering.** Used by CAS-6.1, CAS-6.4, `sweep_stale_backed`.

### BKD-12.2 The sweep of stale backed values
- **Statement.** `manage.py sweep_stale_backed`, every 15 minutes, one at a time (advisory lock `0x5BAC5EED`). Four feeds: (1) the frontiers of `BACKED_GUARD` events since the last run (carried on, not checked, up to `--max-generation` carries); (2) merge points: positions reached within `--recent-depth` plies up by two ingest jobs whose time windows overlap (`_merge_points`); (3) a rotation over the table in physical order, sized so the whole table is read every `--rotation-days`; (4) the support spine of the start position and every open reply along it (down to `BACKED_ECHO_PLIES`). What feeds 2 to 4 find stale (BKD-12.1) is checked again after `--settle-seconds`, and repaired only if it is still stale with the same stored numbers and has no pending ingest. Closures: merge points, handed-over keys and `--keys` are asked `closable_rows` (CAS-2.1) and, still decided after the settle, closed with `close_missed` (CAS-6.3) under source `NONE`. Repairs run `backup_backed_evals_retrying(open_only=True)` in short transactions: confirmed closures first, then confirmed rows in small batches, then continuations. Valves: `--max-changes`, `--max-cut`, `--max-stale-fraction` (the rotation's findings are reported, not repaired, past it), `--max-seconds`. One `BACKED_SWEEP` event per run carries the findings and the cursors.
- **Code.** `atomicdb/management/commands/sweep_stale_backed.py` (`_merge_points:150`, `Command`).
- **Tests.** `test_stale_backed_sweep.py` (29), `test_parent_recheck.py::SweepBackstopTests::test_a_merge_point_of_overlapping_landings_is_closed` (40 in BKD-12, mostly mixed or v1-specific). **Gaps:** none for the repairs; the rotation cursor is plumbing.
- **Determinism.** Everything here is time-driven: overlapping job windows, settle delays, a time-sized rotation slice in physical order, per-run valves. It exists to repair what v1's concurrency and cuts leave behind. In v2 (one writer, ascents to rest) the sweep has nothing to repair; the plan expects it to disappear. Its effects on production values are part of what the shadow compares.
- **History.** #85 (26-Oct), #102 closures, spine feed 2-Nov.
- **Ordering.** Outside the landing pipeline.

### BKD-12.3 Deliberate recascades
- **Statement.** `recascade_backed` reruns the backed ascent over chosen keys or the whole table (off as a nightly job since 5-Oct, it stopped fitting in a night); `recascade_status` asks `closable_rows` of the table and closes what it finds. They apply the same functions; they are tools for a change of rule, which v2 replaces by a replay.
- **Code.** `atomicdb/management/commands/recascade_backed.py`, `recascade_status.py`.
- **Tests.** `test_backed_eval.py::BackedRelicTests::test_recascade_command_sweeps_the_relic`, `test_closure_reaches_all_parents.py::RecascadeStatusCommandTests::test_apply_closes_them_and_a_second_pass_finds_nothing`, `test_recascade_slices.py`. **Gaps:** none.
- **Determinism.** Seed order and slicing (hex key order) change where guards cut.
- **History.** see `docs/value-semantics.md` invariant 4.
- **Ordering.** Outside the pipeline.

### BKD-13.1 Retrying an ascent after a lock conflict
- **Statement.** `backup_backed_evals_retrying(seeds, sleeps=(1, 3, 9), ...)`, outside a transaction only: on an `OperationalError` that is a lock conflict (CAS-6.3), add the aborted frontier (BKD-7.7) in front of the seeds, sleep, and try again; raise when the sleeps run out, inside a transaction, or for any other error. Returns the rows changed by every try.
- **Code.** `atomicdb/ingest.py:backup_backed_evals_retrying:4766`, `ingest.py:is_lock_conflict:4757`.
- **Tests.** `test_stale_backed_sweep.py::LockConflictTests::test_a_conflict_that_does_not_go_away_is_raised`, `test_parent_recheck.py::LockConflictTests::test_a_lock_conflict_is_tried_again` (4 in BKD-13, all v1-specific). **Gaps:** none.
- **Determinism.** v1 plumbing (lock waits, sleeps).
- **History.** #85.
- **Ordering.** Used by BKD-9.2, CAS-6, BKD-12.2.

---

# 10. Loops and repetition (LOOP)

The graph erases the move counters, so a position cannot say it occurred
twice; only a walk can (invariant 6). Every rule here is a walk over
`backed_move` pointers, bounded in plies, in re-read positions and, for two of
them, in **wall-clock seconds**. These are the rules the plan ports last
(section 10) and the only ones whose results depend on a clock in v1 (D1).

### LOOP-1.1 Marking a child whose value comes back
- **Statement.** `_draw_cycling_children(by_parent, cache, line=frozenset(), circular=None)` marks, for every parent and child: a child whose key is in `line` (a position the line under evaluation already went through) is a repetition at once (`cycles = True`, `plies = 0`, `value = 0`). Otherwise, a child with a value and a pointer (`spine`, i.e. a backed value) is walked: from the child, follow the pointer of each node (the cache's current `backed_move`), up to `BACKED_CYCLE_MAX_PLIES` (32) steps, one query per step for all walkers (`_resolve_spine_steps`). A step whose edge no longer exists: no cycle. A step that lands on the parent or on a position of `line`: the child is marked (`cycles = True`, `plies = 0`; its value is set to 0 later by `_backed_for`). A step that lands on a position this walk already passed: no repetition here; `(parent, move)` goes into `circular` when asked (a loop elsewhere, BKD-6.4). A step to a node with no pointer, or to a decided node: no cycle. Running out of steps: no cycle (the safe error). Returns the set of marked `(parent, move)`. The child's stored pointer is put into the cache only if the cache does not have one yet.
- **Inputs / outputs.** Reads `Edge` with child `status` and `backed_move`; mutates `_ChildValue` fields and the spine cache.
- **Code.** `atomicdb/ingest.py:_draw_cycling_children:2724`, `ingest.py:_resolve_spine_steps:2814`, `ingest.py:_SpineCache:2703`.
- **Tests.** `test_backed_eval.py::BackedRepetitionTests::test_a_cycle_beyond_the_guard_is_not_claimed`, `::test_a_real_alternative_beats_the_loop_that_outranked_it`, `test_backed_eval.py::RepetitionZeroContractTests::test_a_partial_ring_stops_lending_the_number_it_invented`, `test_backprop_repetition.py::ReturningLineTests::test_a_loop_nobody_can_leave_is_still_a_draw` (24 in LOOP-1). **Gaps:** none.
- **Determinism.** Reads pointers the same ascent may already have rewritten (the cache is told of each decision, BKD-7.2): the result depends on the order in which levels decide, which is a function of the seeds. None from the clock.
- **History.** `97f866b` (3-Aug, repetition = draw); `ec871a8`, `8ea0f62`; #49 `line`; #70 `circular`.
- **Ordering.** Step 2 of BKD-7.2, and inside LOOP-2.2 one ply deeper.

### LOOP-1.2 The spine cache of one ascent
- **Statement.** For the duration of one ascent (or one `stale_backed_rows` call, or one page's `line_moves`), `steps[(node, move)] = (child, child status)` (facts of the graph) and `spines[node]` = the node's current pointer. Every decision of a level is written into `spines` (`wrote`) as soon as it is made and again when it is kept or replaced, so walks later in the ascent follow the pointers the ascent has decided, not the stored ones. `_resolve_spine_steps` fetches unknown steps with `parent IN (...) AND move IN (...)` and caches every row it gets (including pairs it was not asked for).
- **Code.** `atomicdb/ingest.py:_SpineCache:2703`, `_resolve_spine_steps:2814`, `_resolve_level_steps:3411` (which does not overwrite the pointer of a node the level has decided).
- **Tests.** Through LOOP-1.1 and LOOP-3.1. **Gaps:** none.
- **Determinism.** None beyond LOOP-1.1.
- **History.** `8ea0f62`, #69.
- **Ordering.** Owned by BKD-7.1.

### LOOP-2.1 A returning child is valued on the line that returns
- **Statement.** `_resolve_returning_children(by_parent, cache, line, budget=None, only=None, unresolved=None, deadline=None, verdicts=None)`. A top-level call (`budget is None`) opens a deadline `now + BACKED_CYCLE_MAX_SECONDS` (1.0 s) in a module-level variable, shortened to `deadline` when one is given; nested calls share it. For each parent (restricted to `only` when given) and each marked child not itself on the line (`played = line | {parent}`): value it with `_value_on_line(child, played, cache, budget)` where a top-level call gives each child its own budget of `BACKED_CYCLE_MAX_NODES` (64) re-reads and a nested call shares the caller's. `None` (out of budget or time): the walk's verdict stands (the child stays a repetition worth 0) and `(parent, move)` goes into `unresolved`. A verdict that repeats: the child stays marked. Otherwise the child is unmarked (`cycles = False`) and takes the line's value, quality, plies and pointer. Its `returns` flag, set by `_decide_level` before this call, stays true, so it still loses ties (BKD-6.4).
- **Code.** `atomicdb/ingest.py:_resolve_returning_children:2834`.
- **Tests.** `test_backprop_repetition.py::ReturningLineTests::test_one_touch_settles_the_whole_loop`, `::test_the_losing_side_is_not_handed_a_draw_the_other_can_refuse`, `::test_out_of_budget_the_walk_keeps_its_old_verdict`, `test_cycle_deadline.py::CycleDeadlineTests::test_a_top_level_call_opens_a_fresh_deadline` (32 in LOOP-2). **Gaps:** none.
- **Determinism.** **Wall clock**: the 1 s deadline (CYCLE_DEADLINE_1019) decides between a value and the draw verdict, so a stored backed value can depend on CPU speed and load. Module-level deadline state is per process. Plan D1.
- **History.** #49 (4-Oct: moky's and Eclipsia's positions; two loops sharing positions had no resting state); #71 (19-Oct) the deadline after ten-minute ingests.
- **Ordering.** Step 4 of BKD-7.2 (open rows only); inside LOOP-2.2; in `line_moves` (VIEW-3.1).

### LOOP-2.2 What a position is worth to a line that went through `played`
- **Statement.** `_value_on_line(key, played, cache, budget, verdicts)`: return `None` when the budget is spent, `len(played) >= 32`, or the deadline has passed. Otherwise spend one unit, read the row (`key, fen, status, expanded, eval_cp, nodes_invested, best_move, last_analysis, backed_*`; missing: `None`), read its children (BKD-1), mark them against `played` (LOOP-1.1), value their returning children recursively with the shared budget (LOOP-2.1), and return `_line_verdict(row, children, played)` (LOOP-2.3), recording it in `verdicts[(key, played)]` when asked. Nothing is written or bought.
- **Code.** `atomicdb/ingest.py:_value_on_line:2925`.
- **Tests.** `test_backprop_repetition.py::NestedLoopTests::test_an_ascent_through_a_nest_of_loops_comes_to_rest`, `test_cycle_deadline.py::CycleDeadlineTests::test_a_nested_call_keeps_the_running_deadline`. **Gaps:** none.
- **Determinism.** The deadline (LOOP-2.1).
- **History.** #49; #62 floors inside; #71; #77.
- **Ordering.** Inside LOOP-2.1.

### LOOP-2.3 The verdict of a position on a line
- **Statement.** `_line_verdict(row, kids, played)` returns `(value, quality, plies, move, repeats)`: run `_backed_for(row, kids, bound, cycle_picks=picks, lines)` with the row's own showcase bound and lines. (1) If the result's move is the first repetition pick (the backup chose the repetition): `(0, 0, 0, move, True)`. (2) The draw belongs to whoever can play the repeating move: if some child of the row is on `played` (the row can repeat by itself) and the value is `None` or 0 is strictly better for the mover than the value: `(0, 0, 0, that move, True)`. (3) No value: `(row.eval_cp, row.nodes_invested, 0, None, False)`, the row's own measure. (4) Otherwise `(value, max(quality, row.nodes_invested), plies, move, False)`.
- **Code.** `atomicdb/ingest.py:_line_verdict:2978`.
- **Tests.** `test_explorer_repetition_rows.py::BackupPinTests::test_h6g5_on_the_line_is_worth_its_way_out`, `::test_p_stands_on_e4f5`, `test_loop_best_exit.py::NoWayOutTests::test_a_loop_nobody_wants_to_leave_is_a_draw`. **Gaps:** none.
- **Determinism.** None (pure, given the children).
- **History.** #49 (opabinia's position 8-Sep), #77 factored out.
- **Ordering.** Inside LOOP-2.2 and LOOP-4.1.

### LOOP-3.1 A level never writes a loop of pointers at a value other than 0
- **Statement.** `_open_pointer_loops(rows, children, decided, cache, showcases, cycle_picks, budget, ruled)`, after every row of a level is decided. `starts` = the level's rows, sorted by key, that are open, have a decided move and a non-zero decided value. From each start follow the pointers (the first step from the level's own children; then the cache, one query per ply for unknown steps), up to 32 plies: a missing step ends the walk; a step to a decided child ends it (a proven value is nobody's echo); back to the start: a loop (recorded once per member set); onto another position of the path: a loop elsewhere, ignored. Loops are processed in sorted order of their member lists. A loop no longer closed (an earlier loop of the same level opened it) is skipped. For each member of the loop that belongs to the level, in key order: mark its move to the next member as `returns` and decide it again with `_backed_for` (with its showcase bound and lines when the level read them, i.e. for gap rows only); if the result has the **same value** through **another move** whose grounding is 2 (it reaches a source), that becomes the member's decision (the loop is opened there, at the first member by key that can) and the loop is done. If no member can open it and the loop budget has time left, the loop is valued at its best exit (LOOP-4.1): members of the level whose value changes take the new decision (recorded in `ruled`), and the repetition picks of members in the level join `cycle_picks`. Either way, members outside the level are returned to be woken once per ascent.
- **Inputs / outputs.** Mutates `decided` and the cache; reads edges.
- **Code.** `atomicdb/ingest.py:_open_pointer_loops:3442`, `ingest.py:_loop_still_closed:3432`.
- **Tests.** `test_loop_best_exit.py::UntouchedTests::test_a_loop_opened_for_the_same_value_is_left_to_part_2`, `test_backprop_repetition.py::OpenRingOrderTests::test_entering_from_the_top_or_from_inside_publishes_the_same`, `test_backprop_repetition.py::NestedLoopTests::test_the_rest_does_not_depend_on_how_ties_are_broken`, `test_pointer_loop_grounding.py::SelfProtectingLoopTests` (12 in LOOP-3). **Gaps:** nothing pins that the re-decision uses no showcase bound for a member without a gap (harmless by BKD-4.1, but a port that always passes the bound must check it).
- **Determinism.** The best-exit fallback has a wall-clock budget (LOOP-5.1). Otherwise deterministic (sorted keys, sorted loops).
- **History.** #69 (19-Oct: Rd6-f6 Rf8-d8 Rf6-d6 Rd8-f8, four positions at 1200 each standing on the next); #70 open only with a move that reaches a source; #77 best-exit fallback.
- **Ordering.** Step 7 of BKD-7.2, before the waiting rows (BKD-7.3).

### LOOP-4.1 A loop of pointers is worth its best exit
- **Statement.** `_loop_at_best_exit(members, by_key, decided, cache, deadline, picks)`, `members` in pointer order. `None` (leave the loop as it is) when the deadline has passed at any of the checks, when a member row is missing or decided after a fresh read, or when the result fails the final check. Otherwise: classify every member's moves (LOOP-4.2) into `inside` (to another member), `out` (ways out, at their own number) and `back` (ways out whose chain comes back into the loop: dropped, they only carry the loop's own number). For each member as the `start` of a lap: walk the lap backwards from the last member to the second, computing for each position `here` its verdict on the line `{the members from start up to here-1}`: its children are its ways out plus, for each inside move, either a repetition (worth 0, quality 0, marked `cycles` and `returns`) when the target is at or before `here` on the lap, or the target's lap verdict (marked `returns`, a floor when the target row is a floor) when it is ahead (a target whose verdict repeats is a repetition); `lap[here] = _line_verdict(...)` (LOOP-2.3). Then the start itself: `_backed_for(row, out + inside moves seen from position 0, bound, cycle_picks, lines)` is its new decision; its repetition picks go into `picks`. Final check: following each member's chosen inside move, a cycle of members whose decided values are not all 0 means the rule did not understand the loop: `None`. A loop nobody wants to leave is therefore worth 0 (the repetition); the distance is counted from the way out, never round the loop.
- **Code.** `atomicdb/ingest.py:_loop_at_best_exit:3722`.
- **Tests.** `test_loop_best_exit.py::AboveEveryExitTests::test_a_two_cycle_becomes_its_best_exit`, `::test_before_the_rule_the_level_wrote_the_loop_at_its_own_number`, `test_loop_best_exit.py::PliesTests::test_a_loop_kept_at_its_value_counts_from_the_exit`, `::test_the_distance_is_counted_from_the_exit_and_does_not_grow`, `test_loop_best_exit.py::SideToMoveTests::test_the_mirror_case_is_black_leaving` (38 in LOOP-4). **Gaps:** nothing pins the final "did not understand" check returning `None`.
- **Determinism.** **Wall clock** (deadline from the loop budget, LOOP-5.1), checked four times.
- **History.** #77 (21-Oct, owner decision: three loops held 1208 above every way out in cz_eclipsia's rook ending).
- **Ordering.** Inside LOOP-3.1.

### LOOP-4.2 The ways out of a loop
- **Statement.** `_loop_ways_out(members, children, cache, decided)`, with `children` from `_region_children` (each child that the level has just decided counts with its decided value, re-read through BKD-1.2). For each member and each child: to another member: `inside`. A decided child, an uninformed one, or one with no pointer: `out` (a source, or nothing known). Otherwise walk its pointers (up to 32 plies, one query per ply): back into the loop: `back`; to a decided child or one with no pointer: `out`; onto its own chain again: `out` with `circular = True` (a loop elsewhere: counted at that loop's value, loses ties); still walking after 32 plies: `out`.
- **Code.** `atomicdb/ingest.py:_loop_ways_out:3660`, `ingest.py:_region_children:3640`.
- **Tests.** `test_loop_best_exit.py` (26 tests). **Gaps:** none.
- **Determinism.** None beyond the shared caches.
- **History.** #77; #80 `mate_in` for ties.
- **Ordering.** Inside LOOP-4.1.

### LOOP-5.1 Bounds of the loop walks
- **Statement.** All in one table, because they decide results when they bite:

  | Bound | Value | When it bites | Result |
  |---|---|---|---|
  | `BACKED_CYCLE_MAX_PLIES` | 32 | a pointer walk (LOOP-1.1, LOOP-3.1, LOOP-4.2), a line deeper than 32 positions (LOOP-2.2) | no cycle claimed / the walk's verdict stands / a way out treated as a source |
  | `BACKED_CYCLE_MAX_NODES` | 64 re-reads per returning child (top level) | LOOP-2.2 | `None`: the child stays a repetition worth 0 |
  | `BACKED_CYCLE_MAX_SECONDS` | 1.0 s per top-level valuation, shared by nested calls, shortened by a request deadline | LOOP-2.1, LOOP-2.2 | `None`: as above |
  | `BACKED_LOOP_MAX_SECONDS` | 1.0 s per ascent (a fresh budget per `stale_backed_rows` call) | LOOP-3.1 before LOOP-4.1, and LOOP-4.1's own checks | the loop is left as decided |
  | `MATE_SUPPORT_HOPS` | 64 | DTM-3.1 | the reply is not set aside |
- **Code.** constants at `atomicdb/ingest.py:2402-2415`, `3637`, `2124`; deadline state `_cycle_deadline` (2415).
- **Tests.** `test_cycle_deadline.py::CycleDeadlineTests` (4 tests), `test_loop_best_exit.py::BoundTests::test_no_time_left_leaves_the_loop`, `test_backed_eval.py::BackedRepetitionTests::test_a_cycle_beyond_the_guard_is_not_claimed` (11 in LOOP-5). **Gaps:** none.
- **Determinism.** The two seconds budgets are the wall-clock dependence D1 targets: replace them by counted steps (re-reads, positions walked). The plan recommends yes; it changes results only where the budget bit in v1, which the golden cases will show.
- **History.** #71, #77.
- **Ordering.** —

### LOOP-6.1 A repetition is a draw with no weight, and settles only a complete node
- **Statement.** The value of a repetition is 0 with quality 0 (BKD-5.1 step 3): a proven draw beats it on support, and the zero-support veto (BKD-5.2) applies to it. When the best child of a node is a repetition and the node's coverage is not complete (BKD-4.3), the node keeps its own measure (or publishes nothing without one) and buys the disambiguation (BKD-11.3). With complete coverage the repetition decides: the mover has nothing better than repeating.
- **Code.** `atomicdb/ingest.py:_backed_for:3893` (3927-3936, 3991-4008).
- **Tests.** `test_backed_eval.py::RepetitionZeroContractTests::test_a_repetition_does_not_decide_a_node_with_open_moves`, `::test_with_every_answer_on_the_table_the_draw_is_the_verdict`, `::test_proven_draws_win_the_tie_against_a_walked_one`. **Gaps:** none.
- **Determinism.** None.
- **History.** `8ea0f62` (12-14 Aug, Wolfram).
- **Ordering.** Inside BKD-5.1.

---

# 11. Values served but not stored (VIEW)

These rules decide numbers the site and the public API return without writing
them. They belong in phase 5 (read side) but they share predicates with the
backup, and the API golden file of the plan (section 6) pins them.

### VIEW-1.1 One node, one number
- **Statement.** Every surface that quotes a node's value quotes `best_known_eval` (BKD-10.1): the explore header, the node's row in its parent's table, the query API `score`, the map inspector. The raw own eval survives as context (`point`).
- **Code.** `atomicdb/ingest.py:best_known_eval:5152`; `views.py:api_query:7050`, `views.py:_child_moves:6141`.
- **Tests.** `test_headline_value.py` (7), `test_root_extremes.py::ValueTests`. **Gaps:** none.
- **Determinism.** None.
- **History.** owner decision 6-Aug.

### VIEW-2.1 The move table: one order, by value
- **Statement.** Each row, from the side to move's view: a proven win of the mover `score = 10,000`, `rank = 10,001 + (999 - min(n, 999)) * 0.001` (shorter mates first); a proven loss `score = -10,000`, `rank = -10,001 + min(n, 999) * 0.001` (longer resistance first), where `n = (plies + 1) // 2` moves and `plies = 1 + mate_in`, else 1 for a terminal, else `1 + len(won_line)` for a `MATE_PV` that is not a `TB_LINE`; a proven draw `0`; an open child with a value `score = best_known_eval` flipped to the mover, `rank` clamped to ±9,999; an unanalysed child `rank = -9,999.5`. A row whose child is on the visitor's route, or whose line verdict repeats (VIEW-3.1), is a repetition: `score = 0`, `rank = 0`. A row whose line comes back and is left by somebody is shown at its value on the line, `rank` clamped. A row the backup refuses as a floor (BKD-3.4) keeps its place and is marked `floor`. Sort by `(-rank, floor, uci != first, uci)`, where `first` is the header's move (VIEW-2.2) when it is a row and not a floor, else the position's `best_move`. The `M<n>`/`-M<n>` label uses the same `n`. A proven position ignores the route.
- **Inputs / outputs.** Served rows; nothing written.
- **Code.** `atomicdb/views.py:_child_moves:6141`, `views.py:_first_among_equals`, `views.py:_walked_value:5680`.
- **Tests.** `test_arrow_tie_order.py` (8), `test_explorer_repetition_rows.py` (25), `test_backprop_floor.py` (view tests), `test_explorer_header.py` (35), `test_api_request_children.py`, `test_public_api.py`. **Gaps:** none found.
- **Determinism.** None (ties by move name since 26-Oct; they were storage order before).
- **History.** owner decision 4-Aug (order by value only); #79 tie order; #320, #321 route-dependent rows (26-Nov).

### VIEW-2.2 The arrow and the API `best_move`
- **Statement.** The header's move is the `backed_move` of an open position with a backed value, or its `best_move` when the backed value equals its own eval and has no move; none for a proven position or one with no backed value. The arrow: walking the sorted rows, stop at the first row valued on the line (a repetition or a returning line); before that, if the header's move is a row and not a floor, it is the arrow. Otherwise the first row that is not a floor and carries a score or a mate; if the first non-floor row has neither, nothing; with no row at all, the position's `best_move` unless it is a blocked move. The API's `best_move` is the same move, so the recommended move and the quoted number always describe one move.
- **Code.** `atomicdb/views.py:_arrow_move:9199`, `views.py:_header_move`.
- **Tests.** `test_arrow_tie_order.py`, `test_explorer_repetition_rows.py::DefenderTests::test_castling_is_never_the_arrow_while_the_draw_is_better`. **Gaps:** none.
- **Determinism.** None.
- **History.** 14-Oct (1bbe0b3e), #79, 26-Nov (a repetition is a move too).

### VIEW-3.1 Rows valued on the visitor's route
- **Statement.** `line_moves(row, line=route)` runs the same marking and valuation as the backup (LOOP-1.1, LOOP-2.1) with the visitor's route as the line, under the same budget, a request deadline able to shorten it: `{move: (value, repeats)}` for every move that lands on the route or whose own line comes back to it; a move the budget runs out on is left out (its stored value is shown). With `detail`, each move valued at its way out also gets the returning `path` (`_returning_path`, from the cache, no query) and the `exit` (`_line_exit`: the first position on the path whose choice on the line is not the next step of the loop). Without a route, the line is the position alone, which is exactly how the backup values its moves, so rows and the backed value read the same numbers. Stored values are never changed.
- **Code.** `atomicdb/ingest.py:line_moves:3001`, `ingest.py:repetition_moves:3120`, `ingest.py:_returning_path:3076`, `ingest.py:_line_exit:3098`; `views.py:_child_moves:6141`.
- **Tests.** `test_explorer_repetition_rows.py::ReturningLineChipTests::test_line_moves_reports_the_path_and_the_exit`, `::test_out_of_budget_the_page_still_renders_without_a_line`, `test_explorer_repetition_rows.py::ReturningRowTests::test_stored_values_are_never_touched`, `test_repetition_ui.py` (15). **Gaps:** none.
- **Determinism.** The wall-clock budget (LOOP-5.1) and the request's shared deadline (`views._line_walk_budget`): the same page can show a row at its line value or at its stored value depending on load.
- **History.** #320, #321 (26-Nov, owner decision: rows follow the route, stored values do not).

### VIEW-4.1 Lines shown cut at their first repetition
- **Statement.** A stored PV is displayed only up to its first self-crossing (`views._pv_cut_at_repetition`), and a move that would re-enter the current route is a disabled row with the repetition chip (still valued at 0, VIEW-2.1). The proven-line jump stops where the line comes back on itself.
- **Code.** `atomicdb/views.py:_pv_cut_at_repetition`, `views.py:_walk_proven_line:4624`.
- **Tests.** `test_explorer_queue_and_jump.py::ProvenLineJumpTests::test_the_walk_stops_where_the_line_comes_back_on_itself`, `test_root_extremes.py::MainLineTests::test_the_line_stops_before_it_repeats_a_position`. **Gaps:** none.
- **Determinism.** None.
- **History.** owner decision 6-Aug.

---

# 12. The changes that made the rules what they are

The per-rule History fields name the commits. These are the 25 changes a
porter should know by heart; most rules from 21-Jul to 17-Aug were committed
directly, without a PR, and are listed after them. Built from `git log -L` on
each function, comparing function bodies with docstrings removed.

| PR | Date | What it changed |
|---|---|---|
| #49 | 04-Oct | A repetition is scored where the line comes back (LOOP-2); every parent standing on a changed child is woken, revisit cap = level bound (BKD-7.4); the best measured child backs a position after a vetoed claim (BKD-5.2); the other parents of re-seeded children are seeds (CLM-3.1). |
| #62 | 11-Oct | The multipv certificate holds against an unsearched floor (BKD-3). |
| #65 | 14-Oct | Every parent of what an ingest closes is seeded (EXP-6.1). |
| #69 | 19-Oct | A returning move loses ties; no pointer loop is written at a value other than 0 (BKD-6.1, LOOP-3.1). |
| #70 | 19-Oct | Ties graded by where the pointer chain goes (BKD-6.4); loops opened only with a move that reaches a source. |
| #71 | 19-Oct | One-second deadline on the returning-line valuation (LOOP-5.1), after ten-minute ingests. |
| #77 | 21-Oct | A loop of pointers is worth its best exit (LOOP-4); a row standing on a child its own level rewrites waits (BKD-7.3). |
| #79 | 26-Oct | Closed keys are cascade seeds, so their distance is re-derived (CAS-1.4); arrow tie order (VIEW-2). |
| #80 | 26-Oct | Full tie order (BKD-6.1); a vetoed claim is set aside (BKD-5.2). |
| #81 | 26-Oct | A backed distance past 256 plies is an echo (BKD-1.3). |
| #85 | 26-Oct | `_decide_level`, stale rows, retry from the rolled-back level, the sweep (BKD-7.2, BKD-12, BKD-13). |
| #89 | 27-Oct | An ingest carries its cut ascent on (BKD-9.2). |
| #102 | 29-Oct | A landing reads its neighbours again after commit (CAS-6.1). |
| #112 | 02-Nov | The server closes what its 6-man tables cover (TB-2.3). |
| #116 | 03-Nov | Covered positions are never served (queue file). |
| #186 | 07-Nov | Proof refresh skips locked rows (PN-4.5). |
| #188 | 08-Nov | Stage (a) commits at once; the climb goes through a durable queue (EXP-6, queue Q-4). |
| #223 | 11-Nov | Trust gate, closure provenance, tablebase-line closures (REV-2, CAS-4.2, TB-3). |
| #225 | 11-Nov | A mate stop arbitrates at its task's budget (EXP-5.1). |
| #284 | 20-Nov | The mated side takes its longest defence, in distance and in ties (DTM-2.1, BKD-6.3). |
| #301 | 23-Nov | A cascade rewrites one position at most 32 times (CAS-5.2). |
| #312 | 25-Nov | Distance to the tables kept in stored values (score bands, proofs file). |
| #319 | 25-Nov | Mate distances no longer count round repetitions (DTM-3.1). |
| #322 | 26-Nov | Deadlocked drainer batches retried as a batch; pn/dn refresh pipelined (queue Q-4, PN-8.3). |
| #333 | 27-Nov | A committed climb reads its meeting points again (CAS-6.4). |

Foundational direct commits: `b409c30` (21-Jul, three closure doors, evals
never close); `3960f81` (proof grades, `_minimax_proof`, cascade guard);
`bdaddf1` (27-Jul, revocation); `8c04741` (27-Jul, clock slack and the
50-move rule); `14e9598` (27-Jul, backed values, noise cut, ply bound);
`0de882a` (proof numbers); `e100915` (cascade backs up status only; won-line
chains); `e718a8e` (29-Jul, distances in both directions); `66129b9` (race
fixes: own columns, CAS on `UNKNOWN`); `97f866b`, `8ea0f62` (3 and 14 Aug,
repetition = draw with no weight); `624aaaa`, `f33b3d0` (10 and 16 Aug, the
showcase bound); `ab4226d` (5-Aug, parents standing on a drifting edge always
wake); `7d7e3db`, `98629c0`, `aa63d21` (6-Aug, repetition in pn/dn);
`b72a43e` (28-Jul, sticky OR primary). PR #48 (4-Oct) re-imported production
code identical to the 14-17 Aug commits and adds no rule.

---

# 13. Defects and quirks found while writing this

The port reproduces v1 exactly unless the owner decides otherwise (brief and
plan). These are places where "exactly" needs a decision, or at least a note
in the shadow's list of known differences.

1. **A job can be applied twice** (queue file Q-3.7, OQ-1). `apply_job`
   commits the job `DONE`, then runs `proof.refresh_proof_numbers` outside
   any try block; if it raises, `process_job` calls `_mark_failure`, which
   writes `PENDING` over the committed `DONE`, and the next claim applies the
   landing again (`visits` and `nodes_invested` counted twice, a second pass
   in the history). Checked in the code (`ingest_queue.py:342, 360, 421-425,
   171-184`). Recommended: fix in v1 now (guard `_mark_failure` on
   `state != DONE`, wrap the post-commit steps); v2 applies once.
2. **A refuted closed witness gets two disputes** (proofs file Q9). After
   `_revoke_contradicted_mate` reopens a child, the same line falls into the
   open-child branch: a second `MATE_PROOF_DISPUTED` event, `won_line`
   rewritten to the refuting line, and a second call to
   `_queue_disputed_reanalysis` (which finds the first task and only raises it).
3. **The best eval of a pass skips some lines** (EXP-2.1): a line whose
   prepared proof is `NO_MATE` on an open child, or whose closure lost a race,
   does not compete for `eval_cp`/`best_move`. Port as is.
4. **Revocation and re-closure use different supports** (REV-1.1): the keep
   test reads raw child statuses, the re-closing cascade applies the
   fresh-context rule (CAS-1.2). Port as is.
5. **Terminal positions born by creation emit no `NODE_CLOSED`** and absorb no
   tasks (POS-2.1), while every other closure does (CAS-7.1). Counters and
   feeds depend on it.
6. **`updated` is not a reliable change marker.** `bulk_update` (the backed
   ascent) leaves it alone; `expand` and `link_existing_children` revive
   tombstones without it; `_revive_tombstones` and claims set it by hand. The
   selector's delta pass reads it (queue file PRI-3). The plan replaces it by
   a version per record (section 2.2.4).
7. **Page views write.** The relink on view (POS-3.2), a click
   (`_materialise_child`) and the proven-line jump close positions and climb
   (section 2.4). In v2 they must be inputs to the log.
8. **Production's online mate-proof allowance contradicts its own comment.**
   Production runs `ONLINE_MATE_PROOF_SECONDS = 1.0`; the repo has 2.5 and the
   comment above it records that at 1 s "27 jobs ran out of time before
   verifying a mate PV, so 1s is too short" (`ingest.py:166-175`). A line whose
   move-by-move check runs out of time is dropped, not downgraded (proofs file
   MPV-2.5). Recommended: the owner chooses one value and the repo and
   production agree before golden cases are recorded.
9. **The status cascade pops its frontier from a Python set** (CAS-1.1), whose
   order of strings changes with `PYTHONHASHSEED` (random per process). Values
   are the same whenever no guard bites (CAS-5); where the 32-rewrite limit or
   the 100,000-pop guard cut a cascade, two runs of the same input can leave
   different rows.

---

# 14. Production against the repository

Read-only on 28-Nov-2026: every `atomicdb/**/*.py` outside `migrations/` was
hashed on the production box (`git hash-object`) and compared with
`origin/spell-runner` `f59413e`. Test files are not deployed with the code (65
missing, 56 older copies); that is by design (deploys exclude
`atomicdb/test_*`).
Migrations: production's latest is `0079_opening_name_max_80`, as in the repo.

| File | Production vs repo | Effect on rules |
|---|---|---|
| `ingest.py` | `ONLINE_MATE_PROOF_SECONDS = 1.0` (repo 2.5). Nothing else. | **Semantic**: online mate-proof verdicts and whether a line closes (MPV-2.5). |
| `proof.py` | lacks the `shared_alt.observe(...)` call in `descend_value` and its import | none: a shadow log, off by default (`shared_alt.enabled()`) |
| `shared_alt.py`, `management/commands/shared_alt_report.py` | not deployed | none (shadow only) |
| `ingest_queue.py`, `propagation_queue.py` | production has `perf_events` timing lines (one JSON line per job and per drained batch) that the repo does not | none: no-op unless `ATOMICDB_PERF_DIR` is set; reads no tree |
| `perf_events.py` | production only (phase-0 baseline instrumentation, 28-Nov) | none |
| `views.py` | production lacks three repo changes: `api_my_queue` answering 405 to methods other than GET/POST, `live_request.invalidate_queue_ahead` after a queue bump (two places), the settings-panel redirect of `api_pref_lifo`; and two comments | none on the graph; queue-page cache and HTTP replies |
| `live_request.py`, `testing.py` | production has an older comment block; the repo's `testing.py` resets more process memos | none |

So the only semantic drift is the known hot-patch. Production also runs with
settings that are not in the repo (queue file, "What is live in production",
and OQ-28): they must be read from `/etc/openbench` before golden cases are
recorded.

---

# 15. Determinism register

Everything that makes v1's stored values depend on something other than the
ordered inputs. "D" is the plan decision that settles it.

| # | Hazard | Where | Rules | D | Recommendation |
|---|---|---|---|---|---|
| 1 | Wall-clock budget of the returning-line valuation (1 s per top-level call, module-level deadline, shortened by a request) | `ingest.py:2414`, `2900-2903`, `2957-2959` | LOOP-2.1, LOOP-2.2, VIEW-3.1 | D1 | Count re-reads instead; calibrate from production timings so the count bites as rarely as 1 s does today. |
| 2 | Wall-clock budget of the loop rule (1 s per ascent) | `ingest.py:3637`, `3534-3540`, `3734-3778` | LOOP-3.1, LOOP-4.1, BKD-12.1 | D1 | Same: a step budget per ascent. |
| 3 | Wall-clock online mate-proof allowance (1.0 s production, 2.5 s repo), shared by PV checks, AND/OR proofs, TB lines | `ingest.py:175`, `prepare_mate_proofs` | MPV-2.4, MPV-2.5, TB-3.1 | D1 | Proofs file Q1: PV checks always complete, AND/OR and TB lines get node budgets. |
| 4 | Rolling one-hour arm windows on `created` | `ingest.py:9143` | BKD-11, queue arms, PN-9, SOL-4 | D1 | Record `now` in every input; or count per sequence window. |
| 5 | Set pop order of the status cascade (`PYTHONHASHSEED`) | `ingest.py:1775-1791` | CAS-1.1, CAS-5 | D3 | Pop in a declared order (key order, or FIFO of insertion); equal values when no guard bites. |
| 6 | Database row order inside a backed level (no ORDER BY) | `ingest.py:4598-4603` | BKD-7.1, BKD-11, CAS-6.4 | D3 | Key order. Changes only which purchases fit under a cap and which meeting points are read. |
| 7 | Edge order: cascade witness by move name (explicit); edge id as movegen order and as the proof-number tie-break | `ingest.py:1661`, `1732`, `740`; proof.py | CAS-1.3, DTM-1.1, EXP-1.1, PN | D3 | Keep v1 edge ids at migration, or move every edge tie to move name. |
| 8 | READ COMMITTED races between concurrent landings and climbs; their repairs (rechecks, sweep) | sections 2.3, CAS-6, BKD-12.2 | CAS-6, BKD-9, BKD-12, BKD-13 | (one writer) | Nothing to port; golden cases record their writes; the shadow compares at rest. |
| 9 | Batch composition of the drainer (timing) through the noise cut | `propagation_queue.py`, BKD-8.1 | EXP-6.2, BKD-8 | D2 | Port the cut first, then drop it with D2 and test batch-size independence. |
| 10 | Cut ascents and their carries (64 levels, 50,000 rows, 64 revisits, 64-key carry, sweep generations) | BKD-7, BKD-9 | BKD-7.4, BKD-7.5, BKD-9 | D2 | Run ascents to rest in v2; until D2, reproduce the cuts and their events. |
| 11 | Trust as mutable state (`TrustedAccount`, `ATOMICDB_TB_TRUSTED`) | REV-2.1, TB-2.2 | REV-2, TB-2, SOL-2.4 | — | Make trust changes facts in the log. |
| 12 | Settings switches that change rules | section 1.4; queue file hazard 7; proofs file hazards | many | — | Capture production's values in phase 0; port only the live branches unless the owner wants the others. |
| 13 | Process state: module caches, state files, cache counters, `random.random()` fallback, lease tokens | queue file hazard 6 | Q-10, PRI-3, SOL-4.3 | — | Writer state or logged facts. |
| 14 | Counter derived from the table (highest task id) for descent draws | `ingest.py:_task_counter:7035` | PN-6.6, PRI-4.4 | — | Use the log sequence number (queue OQ-25). |
| 15 | Path dependence of stored state: sticky proof primary, "has a ProofNode row" | proof.py | PN-5.1, PN-4.6, PN-10.1 | D5 | Proofs file Q3. |
| 16 | pyffish move order inside the exhaustive proof | `logic.prove_forced_mate` | MPV-2.1 | — | Same library in v2 (plan open question 3), or a canonical order in both. |
| 17 | Page views that write, throttled by a cache TTL | POS-3.2, section 2.4 | POS-3, MPV-5, CAS-1, BKD-7 | — | Inputs to the log. |

---

# 16. Recommended port order

The plan (section 10) says loops last and D1 before them. The order below
goes from functions of one row to functions of a family, so every step has
the previous ones as tested building blocks, and each step's shim tests are
listed in `test-map.csv` by group.

## Phase 2: rules over the graph columns

| Step | Rules | Why here | Shim tests (groups) |
|---|---|---|---|
| 1 | Conventions: section 1, score-band helpers (SB), `_status_eval`, `known_eval_of` (BKD-10.1) | everything reads them | SB, BKD-10 |
| 2 | Exact layer: `backup_status` (CAS-1.1 in the proofs file), clock slack (DTM-5.1..5.10), fresh context (CAS-1.2), proof grade (CAS-3.1), witness (CAS-1.3) | pure functions of one node and its children | CAS-1, CAS-3, DTM-5 |
| 3 | Distances without loops: own witness (DTM-4.1), winner (DTM-1.1), loser (DTM-2.1) | functions of one node | DTM-1, DTM-2, DTM-4 |
| 4 | The cascade driver (CAS-1.1, CAS-1.4) with its guards (CAS-5) and the closure side effects (CAS-7.1, CAS-4) | first fixed point; decide D3 for pop order here | CAS-1, CAS-4, CAS-5, CAS-7 |
| 5 | Distance loops (DTM-3.1) | a bounded walk, deterministic, no clock | DTM-3 |
| 6 | Child contributions and coverage (BKD-1, BKD-4, BKD-2), floors (BKD-3) | inputs of the backed decision | BKD-1, BKD-2, BKD-3, BKD-4 |
| 7 | `_backed_for` without repetitions (BKD-5.1 steps 1-2, 4-10; BKD-5.2..5.5) and the tie order with grounding fixed at 2 (BKD-6.1..6.3) | the core decision, still loop-free | BKD-5, BKD-6 |
| 8 | The ascent (BKD-7.1, 7.2 without steps 2, 4 and 7, BKD-7.3..7.7), noise cut and wakes (BKD-8), guard events (BKD-9.1), stale rows (BKD-12.1) | second fixed point; batch independence fails here by design until D2 | BKD-7, BKD-8, BKD-9, BKD-12 |
| 9 | Purchases from the ascent (BKD-11) as emitted intents, with the arm windows of D1 | needs only the task model's interface | BKD-11 |
| 10 | Proof numbers (PN-0..PN-5, PN-7, PN-8, PN-10) | read statuses, never feed back; decide D5 first | PN-1, PN-3, PN-4, PN-5, PN-7, PN-8, PN-10 |
| 11 | Repetition marking and its value: LOOP-1.1, LOOP-1.2, LOOP-6.1, grounding with `returns`/`circular` (BKD-6.4), BKD-7.2 step 2, BKD-7.6 | the first walk over pointers; no clock | LOOP-1, LOOP-6 |
| 12 | Returning lines (LOOP-2.1..2.3), BKD-7.2 step 4 | the first clock-bound rule: needs D1 | LOOP-2, LOOP-5 |
| 13 | Pointer loops (LOOP-3.1) | depends on 11-12 | LOOP-3 |
| 14 | Best exit (LOOP-4.1, LOOP-4.2) | hardest, clock-bound, depends on all of the above | LOOP-4 |
| 15 | Batch-size independence and termination as property tests (plan 7.5); golden cases with the rechecks' and the sweep's effects (CAS-6, BKD-9.2, BKD-12.2, BKD-13 are not ported) | closes phase 2 | EXP-6 |

## Phase 3: stage (a)

| Step | Rules |
|---|---|
| 1 | Identity and movegen through the Fairy-Stockfish library: POS-1.1, POS-4.1 |
| 2 | Creation, reachability, expansion: POS-2.1, POS-5.1, EXP-1.1 |
| 3 | Landing a pass: EXP-2.1..2.7, EXP-5.1, EXP-5.2 (with the queue model), EXP-6.1 |
| 4 | Claims: CLM-1..CLM-3 |
| 5 | Mate lines: MPV-1, MPV-2 (after D1), MPV-5 (won-line chains), then MPV-3 / MPV-4 and REV-1, REV-3 |
| 6 | Trust: REV-2, CLM-4 |
| 7 | Tablebases: TB-1..TB-5 (check shakmaty-syzygy against python-chess, plan open question 5) |
| 8 | Certificates: SOL-1..SOL-3, REV-4 |
| 9 | Click and view doors: POS-3, as log inputs |

The read-side rules (VIEW) belong to phase 5 and the queue file to phase 4.

---

# 17. Questions for the owner

Each with options and a recommendation; none blocks the parts of the port
that come before it in section 16.

1. **D3, cascade pop order** (CAS-1.1, register 5). (a) Reproduce Python set
   order: impossible across processes. (b) Pop in key order, or FIFO of
   insertion. Recommend (b), FIFO: it matches the level-by-level shape of the
   other ascents, and values only differ from v1 where a guard cut a cascade,
   which the shadow lists.
2. **D1, loop budgets** (LOOP-5.1, register 1-2). (a) Keep seconds: no replay
   of those values. (b) Count steps. Recommend (b), calibrated on production
   (the CYCLE_DEADLINE events and job timings say how often 1 s bites).
3. **Page views that write** (section 13.7). (a) Keep writing from views, as
   log inputs. (b) Move linking to creation only and drop the relink on view.
   Recommend (a) for the exact port; (b) is a semantic change for later.
4. **The double dispute** (13.2, proofs Q9). Recommend porting as is and
   fixing in v1 if the owner considers the second event noise.
5. **The job applied twice** (13.1, queue OQ-1). Recommend fixing v1 now.
6. **Production's 1.0 s** (13.8). Recommend deciding the value and making the
   repo and production agree before golden cases are recorded.
7. **The noise cut and the guards** (D2, register 9-10). The plan recommends
   dropping them after the exact port passes; this document agrees, and notes
   that `BKD-9.2` (carry), `BKD-12.2` (sweep) and `CAS-6` (rechecks) then have
   nothing to do.
8. **Revocation's asymmetry** (13.4) and **terminal events** (13.5): port as
   is; ask whether either was intended.

The proofs file adds Q1 to Q12 (mate-proof budgets, move order, proof numbers
computed, absence semantics, and seven smaller points) and the queue file
OQ-1 to OQ-39 (defects, wire compatibility, replay inputs, the regret
selector, fairness).

---

# 18. The test map

[`test-map.csv`](test-map.csv) lists every `def test_` method of
`atomicdb/test_*.py` and `atomicdb/tests.py`, 4,412 of them in 179 files
(the row count of each file was checked against a scan of its `def test_`
lines). Columns:

| Column | Meaning |
|---|---|
| `file`, `test_class`, `test_name` | the test |
| `rule_ids` | the rule groups it exercises (`BKD-5`, `Q-2`, ...; `;`-separated), `NONE` for pages, names, votes, notifications and metrics that touch no rule, and `FAM-?` where a test exercises a rule of the family that no group names |
| `functions` | the `atomicdb` functions and constants the test drives or patches |
| `portability` | `portable`: sets up rows, calls a rule, asserts stored values or queued tasks; runs against v2 through the shim. `mixed`: asserts a rule through a view, a command or HTTP; portable once re-expressed. `v1-specific`: asserts SQL query counts, locks, retries, isolation, Django views and templates, caches, command plumbing, migrations or settings |
| `reason` | a few words on the classification |
| `asserts` | what the test pins, in one sentence (partly generated from the test's name, docstring and assertions) |

Totals: 1,314 portable, 858 mixed, 2,240 v1-specific; 1,835 tests are
`NONE`. By family (a test can count in several):

| Family | Tests | Family | Tests |
|---|---:|---|---:|
| Q (queue, leases, requests, arms) | 1,403 | CAS | 171 |
| BKD | 459 | POS | 167 |
| PRI | 325 | MPV | 163 |
| PN | 307 | LOOP | 129 |
| EXP | 286 | DTM | 116 |
| SOL | 230 | TB | 56 |
| | | REV | 51 |
| | | CLM | 38 |

The classification was made by reading each test; the `asserts` column of
part of the files was generated from names, docstrings and assertions and
reads mechanically. A test classified `portable` may still use a Django
fixture helper; what makes it portable is that its assertion is about rule
output, not about how v1 produced it.
