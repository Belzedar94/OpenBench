# Rules port status (`av2-rules`)

Phase 2 of `docs/atomicdb/v2-plan.md`, in the order of `rules-spec.md`
section 16. One row per rule id of the spec. **Ported** means the Rust code
reproduces the v1 function named in the spec; **tests** counts the v1 tests
translated into Rust (unit tests in the module, or `crates/av2-rules/tests/`)
against the portable tests of the group in `test-map.csv`; **deviations**
lists every place where the port does not do exactly what v1 does, and why.

Owner decisions applied (brief, 28-Nov): D1 step budgets, D2 noise cut and
size guards kept behind switches until the exact port passes, D3 ties by id
and FIFO cascade pops.

## Step 1: conventions, score bands, headline value

| Rule | Rust | Ported | Tests | Deviations |
|---|---|---|---|---|
| Spec 1.1 values and signs, `_status_eval`, `_better_for_mover` | `conv::status_eval`, `conv::better_for_mover`, `conv::stm_white` | yes | unit | Side to move from the record flag `F_BLACK_TO_MOVE` instead of the FEN (the loader sets it from the FEN). |
| Spec 1.2 verified / independent closures | `conv::is_verified`, `conv::closure_is_independent` | yes | unit (through CAS-3) | none |
| EXP-4.1 `_mate_moves_to_plies` | `conv::mate_moves_to_plies` | yes | 1 (`test_mate_clamp.py::MateDistanceUnitTests::test_the_worker_formula_decodes_back_to_plies`) | none |
| SB-1 constants | `bands::*` | yes | `test_tb_distance.py::MapTests` 5/5 | none |
| SB-2 `tb_distance` | `bands::tb_distance` | yes | (in MapTests) | none |
| SB-3 `engine_cp_to_stored` | `bands::engine_cp_to_stored` | yes | (in MapTests) | none |
| SB-4 `legacy_value`, `raw_score` | `bands::legacy_value`, `bands::raw_score` | yes | `NormalizeTests::test_bounds_and_wdl_in_the_raw_line` + word-boundary unit test | `raw_score` is a hand-written scan of `\bscore (cp\|mate) (-?\d+)\b` (no regex crate); a digit run too long for an `i64` is skipped where Python would match it (no worker sends one). |
| SB-5 `stored_from_raw` | `bands::stored_from_raw` | yes | NormalizeTests 4/4 | Lines are `serde_json` objects. |
| SB-6 `normalize_line(s)` | `bands::normalize_line`, `bands::normalize_lines` | yes | NormalizeTests 4/4 | Returns `None` for "unchanged" where v1 returns the same object; equality with the legacy value follows Python (`9500 == 9500.0`). |
| SB-7 `band` | `bands::band` | yes | `MapTests::test_band_names` | none |
| SB-8, SB-9, SB-10 | (where normalisation runs; the worker contract; thresholds) | SB-10 constants yes (`conv::MATE_BAND`, `conv::TB_CLAMP_CEILING`); SB-8 belongs to stage (a) | `MapTests::test_the_thresholds_the_code_reads_did_not_move` | none |
| BKD-10.1 `known_eval_of`, `best_known_eval` | `conv::known_eval_of`, `conv::best_known_eval` | yes | `test_selector_leak.py::RegretUsesBestKnownEvalTests::test_the_value_map_prefers_status_then_backed_then_eval` (the other 9 portable BKD-10 tests drive the selector, maps or the ascent: phase 4/5, or step 8) | none |
| Edge order by move name (`ORDER BY move_uci`, `sorted(key=move)`) | `conv::uci_order` | yes | exhaustive unit test against string order | none |

## Step 2: exact layer

| Rule | Rust | Ported | Tests | Deviations |
|---|---|---|---|---|
| CAS-1.1 (proofs file) `backup_status` | `exact::backup_status` | yes | unit table; through the cascade (step 4) | none |
| CAS-1.2 (proofs file) `backup_eval` | `exact::backup_eval` | yes | unit | none (no rule of the port calls it; kept for parity) |
| DTM-5.1 `is_zeroing` | `exact::Zeroing` trait, `exact::BoardZeroing` | yes | `test_clock_slack.py::ZeroingTests` 2/2 | v1 asks the pinned Fairy-Stockfish build for the halfmove counter after the move; the default oracle reads the parent's board (pawn move or capture of an occupied square, which is the atomic rule: en passant and promotions are pawn moves, castling never captures). A caller with the engine library can plug it in through the trait. |
| DTM-5.2 `edge_slack` | `exact::edge_slack` | yes | `SlackRecurrenceTests` (2) | Takes the zeroing answer instead of `(fen, uci)`. |
| DTM-5.3 `edge_supports` | `exact::edge_supports` | yes | unit | as DTM-5.2 |
| DTM-5.4 `minimax_slack` | `exact::minimax_slack` | yes | `SlackRecurrenceTests` (2) | as DTM-5.2 |
| DTM-5.5 `line_clock_slack` | `exact::line_clock_slack` | yes | unit (v1 has none) | Takes the zeroing answers of the line's plies. |
| DTM-5.6 `slack_from_run` | `exact::slack_from_run` | yes | `SlackRecurrenceTests::test_exhaustive_proof_uses_its_worst_reversible_run` | none |
| DTM-5.7 `slack_from_witness_length` | `exact::slack_from_witness_length` | yes | `SlackRecurrenceTests::test_uncertified_witness_uses_the_crude_length_bound` | none |
| DTM-5.8 `slack_from_dtz` | `exact::slack_from_dtz` | yes | `SlackRecurrenceTests::test_tablebase_needs_a_dtz_or_it_is_zero` | none |
| DTM-5.9 clamp, `CLOCK_SLACK_MAX` | `exact::clamp_slack`, `conv::CLOCK_SLACK_MAX` | yes | `SlackRecurrenceTests::test_terminal_takes_the_maximum` | none |
| CAS-1.2 fresh context `_supporting_status` | `exact::supporting_status` | yes | unit; `FreshContextTests` 4/4 through the cascade (step 4) | none |
| CAS-3.1 `_minimax_proof` | `exact::minimax_proof` | yes | unit; `test_proofs.py::MinimaxProofInheritanceTests` and `test_revocation.py::MinimaxWitnessPreferenceTests` through the cascade (step 4) | none |
| DTM-5.10 `_minimax_slack` | `exact::node_minimax_slack` | yes | `FreshContextTests::test_the_same_shape_closes_once_the_children_have_margin` (step 4) | none |
| CAS-1.3 witness of a new MINIMAX closure | `exact::minimax_witness` | yes | unit (all three fallbacks, the third is a v1 gap) | none |

Not portable here (they need the exhaustive prover, the TB probe or a
landing): `ProofRunTests` (MPV-2.1), `TablebaseDtzTests` (TB-2.4),
`ClosureSlackTests` (POS-2.1, REV-1.2), `test_closure_trust.py::TablebaseLineTests`
(TB-3.1), `test_solve.py` (SOL-1.4), `test_won_line_chain.py` (MPV-5): phase 3.

## Step 3: distances without loops

| Rule | Rust | Ported | Tests | Deviations |
|---|---|---|---|---|
| DTM-4.1 `_own_witness_plies` | `read::own_witness` | yes | through DTM-1/2 (`OwnWitnessTests`, `LoserWitnessFloorTests`) | none |
| DTM-1.1 winner's distance, `_witness_rank` | `distance::mate_distance_refresh` | yes | `test_mate_distance.py`: `ReportedShapeTests` 2/2 cascade tests, `OwnWitnessTests` 3/3 (the third with the revocation replaced by the reopened child it leaves), `UnnameableDistanceTests` 1/1; `tests.py::DtmRefineTests` 1/1; `test_chain_link_distance.py::EquivalentWitnessTests` (recurrence half) | Distances are `u16` in the record: a value past 65,534 saturates (only an inflating loop gets there, and DTM-3 plus the rewrite limit stop it long before). |
| DTM-2.1 loser's distance | `distance::loser_distance` | yes | `AndMirrorTests` 2/2, `LoserWitnessFloorTests` 3/3 cascade tests, `tests.py::DtmTests` 2/2 | as DTM-1.1 |

Not ported here: the `backfill_mate_distance` command tests (CMD-4, phase 3:
the command reuses `mate_distance_refresh` with `LoopCheck::Always` and
`Overrides`, both ported) and the `NewClosureTests` of
`test_chain_link_distance.py` (they land a mate line: stage (a)).

## Step 4: the cascade driver, guards, closure side effects

| Rule | Rust | Ported | Tests | Deviations |
|---|---|---|---|---|
| CAS-1.1 `backup_cascade` | `cascade::backup_cascade` | yes | 21 cascade tests (`tests.py::BackupTests` 3/3 portable, `PartialExpansionTests`, `FreshContextTests` 4/4, `MinimaxProofInheritanceTests`, `MinimaxWitnessPreferenceTests`, the DTM tests above); property tests `tests/props_cascade.rs` | **Pop order FIFO (D3)** instead of Python set order; seeds first in their order, then each seed's parents in parent-list order. Events carry position ids; `CASCADE_CYCLE` lists the first 20 by id instead of by key (D3). |
| CAS-1.2 fresh context | `exact::supporting_status`, `RulesConfig::fresh_context` | yes | `FreshContextTests` 4/4 | none |
| CAS-1.3 witness | `exact::minimax_witness` | yes | `MinimaxWitnessPreferenceTests` | none.  **Finding** (property test `a_dag_does_not_depend_on_seed_order`): the witness is the first winning child closed *at the moment* the row closes, and DTM-1.1 keeps it when no child has a distance, so `best_move` of such a closure depends on visit order, in v1 too. Every other column of the fixed point is order-independent on a DAG. |
| CAS-1.4 closed keys and their parents are seeds | input of `backup_cascade` (stage (a) builds the seed list) | n/a here | `test_closure_reaches_all_parents.py` needs a landing: stage (a) | none |
| CAS-1.5 bulk reads | not ported (no semantic effect) | n/a | n/a | v2 reads its columns directly |
| CAS-2.1 `closable_rows` | `cascade::closable_rows` | yes | unit via the cascade's predicate; `RecascadeStatusCommandTests` are command tests (phase 3) | Returns the decided keys in input order instead of a dict in row order. |
| CAS-4.1 closure source | `effects::ClosureSource`, `effects::Attribution` | yes | `one_closure_event_per_closure_with_its_source` | The attribution is passed in `Env` instead of a context variable. |
| CAS-4.2 provenance | `effects::Provenance::clean` | yes (the value; the row is the projection's) | same | none |
| CAS-5.1 cascade guard | `RulesConfig::cascade_guard_limit`, `Event::CascadeGuard` | yes | `test_performance_guards.py::CascadeGuardTests` 1/1 | Switchable (D2). |
| CAS-5.2 rewrite limit | `RulesConfig::cascade_rewrite_limit`, `Event::CascadeCycle` | yes | `CascadeCycleTests` 2/2 | Switchable (D2). |
| CAS-7.1 closure side effects | `effects::Effects::node_closed` (one call per closure, before the row's write, with status, closure, proof, source, provenance) | yes (the call; the event row, provenance row, claim resolution, task absorption and campaign end are the implementer's: task model and projection) | `one_closure_event_per_closure_with_its_source`, `an_already_closed_row_fires_nothing` | none |
| PN-8.1 proof refresh hook | `effects::Effects::proof_refresh(seeds + changed)` | yes (the hook; step 10 is another agent's) | same | none |

The other portable CAS-1/CAS-7 tests go through `ingest_analysis`,
`revoke_closure`, `enqueue_pv_verification`, `materialise_won_line`, the
coverage arm or the drainer: they come with stage (a) and the queue model.

## Step 5: distance loops

| Rule | Rust | Ported | Tests | Deviations |
|---|---|---|---|---|
| DTM-3.1 the repetition check, `_support_next`, `support_reaches` | `distance::loser_distance`, `distance::support_next`, `distance::support_reaches`; hop limit `RulesConfig::mate_support_hops` / `DtmOpts::hops` | yes | `test_mate_distance_cycles.py::RepetitionLoopTests` 9/10 (the listed-backfill one is a command test) and the cascade half of `test_mate_distance.py::BackfillCycleTests` | none |

### Path dependence found by the property tests (v1 behaviour, ported as is)

- A `MATE_PV` row with no `won_line` (historic rows) stands on its stored
  `mate_in` as its own witness (DTM-4.1), so whatever number it first takes
  stays: the fixed point depends on visit order.  **Counted on production
  (28-Nov, read-only, `TABLESAMPLE SYSTEM (1)`, 2,074,102 rows sampled):
  0 of 454,878 sampled `MATE_PV` rows lack a `won_line`** (a 0.2% sample
  with another seed: 0 of 88,794).  With 95% confidence fewer than 7 in a
  million `MATE_PV` rows (under ~300 of ~45M) are such rows: the quirk is
  ported but has no data to act on.
- The witness of a new closure (CAS-1.3) is order-dependent when no distance
  names one; owner decision 28-Nov: **keep the exact port**.  Post-port
  simplification to list: recompute the witness on every visit of a decided
  row (first verified winning child by move, else first winning child),
  which makes `best_move` order-independent too.
- A `MINIMAX` row whose stored slack, grade or distance disagrees with its
  children changes when visited; a parent visited before reads the old
  value (and may close through it), one visited after reads the new one.
- With consistent rows and lines on every `MATE_PV`, the fixed point on a
  DAG is independent of seed order except for CAS-1.3 above (property test,
  400 cases per run, 5,000 checked once).

## Step 6: child contributions, coverage, floors

| Rule | Rust | Ported | Tests | Deviations |
|---|---|---|---|---|
| BKD-1.1 decided child | `backed::child_contribution` | yes | `test_backup_echo.py::ContributionTests::test_a_proven_child_is_never_an_echo`, `test_backed_eval.py::BackedGuardTests::test_solved_children_enter_with_their_truth_value` | none |
| BKD-1.2 open child | `backed::child_contribution` | yes | `ContributionTests` 4/4, `test_quality_tolerance.py` | none |
| BKD-1.3 echo | `config::BACKED_ECHO_PLIES` | yes | `ContributionTests::test_an_echo_is_read_as_the_childs_own_measure`; the two `EchoFamilyTests` need the best-exit rule (LOOP-4, step 14) | The echo bound is a constant (v1's test patches it). |
| BKD-2.1 comparisons, quality tolerance | `conv::better_for_mover`, `backed::BACKED_QUALITY_TOLERANCE_DEN` | yes | `test_quality_tolerance.py` 17/17, ties tests | `quality < own * 0.5` is computed exactly as `2 * quality < own` (same answers for integers). |
| BKD-3.1 `is_floor` | `backed::is_floor` | yes | `test_backprop_floor.py::FloorTests` 8/8 | none |
| BKD-3.2 `_showcase_lines` | `backed::showcase_of` | yes | unit, FloorTests | A head that is not a UCI move is dropped (it can name no child, so no comparison changes). |
| BKD-3.3 the certificate against a floor | `backed::beats_certificate`, inside `backed_for` | yes | FloorTests, `FloorAndTheBulkButtonTests` (floor half) 2/2 | none |
| BKD-3.4 `refuses_floor` | `backed::refuses_floor` | yes | `FloorAndTheBulkButtonTests` | none |
| BKD-4.1 `_showcase_bound` | `backed::showcase_of`, `backed::showcase_from_graph` | yes | `test_showcase_bound.py::ShowcaseBoundTests` 7/7, unit | The bound is parsed from the `last_analysis` blob per level for gap rows only, as v1 reads it. |
| BKD-4.2 `coverage_is_partial` | `backed::coverage_is_partial` | yes | unit, ShowcaseBoundTests | none |
| BKD-4.3 bounded / complete | inside `backed_for` | yes | ShowcaseBoundTests | none |

## Step 7: `_backed_for` and the tie order

| Rule | Rust | Ported | Tests | Deviations |
|---|---|---|---|---|
| BKD-5.1 `_backed_for` | `backed::backed_for` | yes, all ten steps (the repetition branches act on `cycles` once the loop rules set it) | `test_backup_ties.py` 27 tests, `test_backed_eval.py::BackedGuardTests` 8/8, `test_backprop_claims.py` 2/2 | none |
| BKD-5.2 zero-support veto, vetoed claim set aside | inside `backed_for` | yes | `VetoedClaimTests` (2), `VetoGuardTests` 6/6 (repetitions set by hand) | none |
| BKD-5.3 directional guard | inside `backed_for` | yes | `QualityToleranceTests` | none |
| BKD-5.4 anchorless loss | inside `backed_for` | yes | `BackedIngestTests::test_a_walked_mate_line_stops_at_the_losers_partial_node` (rules half) | none |
| BKD-5.5 proof authority | inside `backed_for` | yes | ShowcaseBoundTests, ties | none |
| BKD-6.1 `_best_child` | `backed::best_child` | yes | `TieOrderTests` 10/10, `LongestDefenceTests` 5/5, `OwnMoveTests` 4/4 | none (ties end on the move name, as v1). |
| BKD-6.2 `_proof_rank` | `backed::proof_rank` | yes | unit, TieOrderTests | `-inf` is `i64::MIN`. |
| BKD-6.3 `_defence_moves` | `backed::defence_moves` | yes | unit (the `+inf` case v1 does not test), LongestDefenceTests | `+-inf` are `i64::MAX`/`MIN`. |
| BKD-6.4 grounding | `backed::grounding` | yes (fixed at 2 until the loop rules set `returns`/`circular`) | ties | none |

## Step 8: the ascent, noise cut, wakes, guards, stale rows

| Rule | Rust | Ported | Tests | Deviations |
|---|---|---|---|---|
| BKD-7.1 `backup_backed_evals` | `ascent::backup_backed_evals` | yes | `test_backed_eval.py` (BackedChainTests 6/6, propagation, drift, relic), `test_backprop_wakes.py::LateWakeTests` 3/3, `test_tb_distance.py::BackupTests` 3/3; property tests `tests/props_ascent.rs` | **Row order within a level and the order of woken parents: id order (D3)**; v1 has none (database order, set order). Changes no value; orders purchases and the rested/written lists. `bulk_update` leaves `updated` alone in v1; v2 bumps the record version (the plan's replacement for `updated`). |
| BKD-7.2 `_decide_level` | `ascent::decide_level` | yes, with steps 2, 4 and 7 as `loops::LoopRules` hooks (no-ops in `NoLoops`) | through BKD-7.1 and BKD-12.1 | none |
| BKD-7.3 `_rows_that_wait` | `ascent::rows_that_wait` | yes | through the ascent tests; chains of three or more rows are a v1 gap | none |
| BKD-7.4 revisit cap | `RulesConfig::backed_max_revisits` | yes | `LateWakeTests::test_a_wake_is_never_dropped_in_silence` | Switchable (D2). |
| BKD-7.5 node budget | `RulesConfig::backed_max_nodes` | yes | (v1 has none) | Switchable (D2). |
| BKD-7.6 settling look | inside `backup_backed_evals` | yes (its trigger fills once the loop rules mark returning rows) | step 11 | none |
| BKD-7.7 database error mid-ascent | not ported | n/a | n/a | v1 plumbing: no database errors in the writer; a graph error aborts the batch. |
| BKD-8.1 noise cut | `ascent::worth_propagating`, `RulesConfig::noise_cut` (default on = v1) | yes | `BackedPropagationCostTests` 2, `BackedArgmaxDriftTests` 2/2, plus the same shape with the cut off | Switch for D2. |
| BKD-8.2 parents to wake | `ascent::parents_to_wake` | yes | same | Id order instead of set order (D3). |
| BKD-9.1 guard events | `effects::Event::BackedGuard` | yes | `BackedChainTests::test_ply_guard_is_recorded`, `test_stale_backed_sweep.py::CutAscentTests` | Keys are ids, sorted by id (D3). |
| BKD-9.2 the ingest carries its cut | `AscentReport::carry` (the ply-guard frontier) | the frontier yes; the carry after commit belongs to the writer (and disappears if D2 drops the ply guard) | `IngestCarryTests` 2/2 (as report contents) | none |
| BKD-10.1 headline value | step 1 | | | |
| BKD-12.1 `stale_backed_rows` | `ascent::stale_backed_rows` | yes | `AtRestTests::test_no_stale_rows_once_the_backup_is_at_rest`, `LostUpdateTests` (the check) | Returns `(id, stored, decided)` in id order instead of a dict. |
| BKD-12.2 the sweep, BKD-12.3 recascades, BKD-13 retries | not ported | n/a | command / v1-specific | v1 repairs of concurrency and cuts (spec: "nothing to port"). |

## Step 9: purchases from the ascent

| Rule | Rust | Ported | Tests | Deviations |
|---|---|---|---|---|
| BKD-11.1 quality convergence | `purchase::queue_quality_convergence` | yes: rung, interior cap, the child's own investment, server floor, order, room, event | `test_quality_tolerance.py` purchases, `test_backed_eval.py::BackedGuardTests::test_shallow_child_displaces_at_once_and_buys_convergence`, `test_backprop_floor.py` purchase, `test_arm_rate_budget.py::QualityRateTests` 4/4 | **The task write is an intent on `purchase::TaskSink`** (`quality_room`, `buy`); `get_or_create`/raise and `multipv_for` are the task model's. The arm's hourly room (a rolling window on `created` in v1) is the sink's, counted in log terms under D1. |
| BKD-11.2 rungs | `purchase::rung_matching`, `purchase::rung_at_least` | yes | unit | `nodes <= rung * 1.01` computed exactly as `100 * nodes <= 101 * rung` (same answers: every rung times 1.01 is exact in a double). |
| BKD-11.3 cycle disambiguation | `purchase::queue_cycle_disambiguation` | yes | `test_cycle_disambiguation.py` 3 tests need the repetition walk (step 11): not yet | as BKD-11.1 |
| `server_picked_budget`, `caps_interior` (queue file, read here) | `purchase::server_picked_budget`, `purchase::caps_interior`, `RulesConfig::auto_interior_deep_rungs` | yes (without the clamp argument, which the ascent never passes) | unit | none |

## Hooks left for the next steps

- **Step 10 (proof numbers)**: wired.  `Effects::proof_refresh(seeds +
  changed)` is called at the end of every cascade (PN-8.1); `proofs::ProofCalls`
  wraps the batch's effect sink, collects the calls in order (plus
  `ProofCalls::landing` for PN-8.4) and the writer replays them after the
  commit with `av2_proof::Proofs::after_batch(&committed, &calls, seq, now)`
  (gate-off numbers, `RefreshConfig::default()`).  `tests/proofs.rs`: a
  closure reaches the root's numbers; the property test
  `replay_after_commit_equals_inline` (200 cases) shows that replaying after
  the batch equals refreshing right after the cascade.  `av2-rules` links
  `av2-proof` for tests only (the proof crate's bench feature depends on
  `av2-tools`, which depends on `av2-rules`).
- **Steps 11 to 14 (loops)**: done, `loops::V1Loops` (below).  The writer
  passes `V1Loops::default()` as the ascent's `LoopRules`; `NoLoops` is the
  loop-free behaviour of steps 1-9.
- **Stage (a)**: ends with `climb::climb(cascade_seeds, backed_seeds)`
  (EXP-6.1); a batch merges seeds with `climb::merge_seeds` (EXP-6.2).
- **Not done here**: the zeroing differential check against the engine
  library waits for `stage-a`'s `av2-chess`; the golden cases wait for the
  `shim` agent's `av2-golden` (step 15's second half).

## Steps 11 and 12: repetition marking and returning lines

| Rule | Rust | Ported | Tests | Deviations |
|---|---|---|---|---|
| LOOP-1.1 `_draw_cycling_children` | `repetition::draw_cycling_children` | yes | `tests/loops_repetition.rs` (35 tests: `test_backprop_repetition.py` 10/10 portable, `test_repetition_progress.py` 8/10, `test_cycle_disambiguation.py` 3/3, `test_backed_eval.py` `BackedRepetitionTests` 5/7 and `RepetitionZeroContractTests` 6/9, the counted `test_cycle_deadline.py`) | Parents walked in id order (v1: database order); no value depends on it. |
| LOOP-1.2 spine cache, step queries | `repetition::SpineCache`, `repetition::resolve_steps` | yes, including the cross-product fetch and the stored-pointer overwrite of `_resolve_spine_steps` and the decided-row exception of `_resolve_level_steps` | through LOOP-1.1, LOOP-3.1 | none |
| LOOP-2.1 `_resolve_returning_children` | `repetition::resolve_returning_children`, `loops::V1Loops::value_returning` | yes | ReturningLineTests, NestedLoopTests | **D1: the 1.0 s clock is a counted `Meter`** (below).  Children of a level valued in id order of their parents (v1: database order), which decides who is valued first when the clock runs out. |
| LOOP-2.2 `_value_on_line` | `repetition::value_on_line` | yes | as above | D1 |
| LOOP-2.3 `_line_verdict` | `repetition::line_verdict` | yes | `test_explorer_repetition_rows.py::BackupPinTests` (in `loops_pointer.rs`), `NoWayOutTests` | none |
| LOOP-6.1 a repetition settles only a complete node | `backed::backed_for` (step 7) | yes | `RepetitionZeroContractTests` | none |
| BKD-6.4 grounding with `returns`/`circular`, BKD-7.2 steps 2 and 4, BKD-7.6 settling look | `ascent::decide_level` with `V1Loops` | yes | `test_pointer_loop_grounding.py` 4/4, `test_repetition_progress.py` | none |
| VIEW-3.1 `line_moves`, `repetition_moves` | not ported (read side, phase 5; the tests build it from the ported pieces) | no | | |

## Steps 13 and 14: pointer loops and the best exit

| Rule | Rust | Ported | Tests | Deviations |
|---|---|---|---|---|
| LOOP-3.1 `_open_pointer_loops`, `_loop_still_closed` | `pointer_loops::open_pointer_loops`, `pointer_loops::loop_still_closed` | yes | `tests/loops_pointer.rs` (34 tests: `test_loop_best_exit.py` 24 of 25 portable, `test_pointer_loop_grounding.py` 4, `test_backup_echo.py::EchoFamilyTests` 1 of 2, `test_explorer_repetition_rows.py` backup tests 4) | none |
| LOOP-4.1 `_loop_at_best_exit` | `pointer_loops::loop_at_best_exit` | yes, the four clock checks at their places | AboveEveryExit, Plies, SideToMove, NoWayOut, Drain, OverlappingLoop, Bound | **D1: `BACKED_LOOP_MAX_SECONDS` is a counted budget per ascent** |
| LOOP-4.2 `_loop_ways_out`, `_region_children` | `pointer_loops::loop_ways_out`, `pointer_loops::region_children` | yes | as above | none |
| LOOP-5.1 bounds | `repetition::BACKED_CYCLE_MAX_PLIES`, `BACKED_CYCLE_MAX_NODES`, `LoopCosts` | yes | `Bound` tests, counted deadline tests | D1 |

Skipped (with reasons in the test files): tests through views, the API,
commands, query counts; `WaitingTests::test_the_wheel_of_the_report_in_miniature`
(patches `_rows_that_wait` away); the echo test that patches
`BACKED_ECHO_PLIES`; the request deadline of `line_moves` (read side).

### D1 calibration: the two wall-clock budgets as counted steps

v1 cuts the returning-line valuation 1.0 s after each top-level call
(`BACKED_CYCLE_MAX_SECONDS`, `_cycle_deadline`) and the best-exit rule after
1.0 s per ascent (`BACKED_LOOP_MAX_SECONDS`).  v2 counts instead: every
operation v1 makes at those places charges a `repetition::Meter` with what it
costs v1 on production, in microseconds, and the budgets stay 1,000,000
units, checked exactly where v1 reads the clock (strict `>`).

**How the costs were measured (28-Nov-2026, read-only on production).** A
script ran v1's own `stale_backed_rows` (one level of the backup, writes and
buys nothing) inside a `READ ONLY` transaction that was rolled back, with the
loop functions wrapped to count and time them, on 12,651 positions: the
start position's support spine and every reply along it, a 0.05% page
sample of open backed positions, and 24 loop families named in v1's code and
tests (BFS below each, up to 1,200 open positions), as single rows and as
levels of 32.  Results:

| Measure | Value |
|---|---|
| top-level valuations with a returning child | 48 |
| wall time per valuation, p50 / p90 / max | 0.10 s / 0.22 s / 0.39 s |
| valuations the 1 s clock cut | **0** |
| valuations the 64-re-read cap cut | 13 (ported exactly) |
| fit `secs = a * re-reads + b * step queries` | a = **2.26 ms**, b = **1.16 ms**, R² = 0.983 |
| `_line_verdict` alone | 22-27 µs mean (17-26 µs p50) |
| best-exit calls (loop sizes 20, 20, 4, 4) | 3-17 ms, 0 late; fit 30 µs per lap verdict + 4.0 ms of reads |

So `LOOP_COSTS = { reread: 2,260, step_query: 1,160, verdict: 30,
loop_reads: 4,000 }` µs, budgets 1,000,000.  On the 48 measured valuations
the counted clock predicts 0.27x to 1.27x of the measured time (median
0.97x) and cuts none of them, as v1's clock cut none: the outcomes match.
Under today's load the clock is far from biting (largest valuation 0.39 s,
about 140 re-reads where 1 s is about 440); it bit in v1 on 19-Oct, when a
family full of loops turned valuations into minutes.  A cost per operation
measured on a calmer or busier day moves the bound proportionally; the
constants are in one place (`repetition::LOOP_COSTS`) and the golden cases
will show any valuation where v1's clock and the count disagree.

## Step 15: batch-size independence and termination

`tests/props_batch.rs`.  From a graph at rest, 1 to 7 re-searches land (eval
moves, +128M nodes); climbing after each one or once with the merged seeds
(EXP-6.2) is compared on every rule-visible column.

| Setting | Compared cases | Differ |
|---|---:|---:|
| DAG, noise cut off (D2) | 1,496 | **0** (asserted, plus a 300-case property test) |
| DAG, noise cut on (v1) | 1,496 | 8 (0.5%) |
| cycles, loop rules, noise cut off | 1,414 | 8 (0.6%) |
| cycles, loop rules, noise cut on (v1) | 1,417 | 33 (2.3%) |

So with D2 the climb is batch-size independent on DAGs; v1's noise cut breaks
it in 0.5% of small dense cases, and the loop rules break it on their own
(the spine cache and the loop walks read pointers in the order levels decide
them, LOOP-1.1 "Determinism").  Batch-size independence on graphs with loops
needs more than D2: an owner decision for after the exact port (options:
accept it, as v1 does with its sweep; or make the loop rules read only
stored pointers, which changes results).  Termination: on any graph, cycles
included, every climb ends within the level bound and an ascent cut by a
bound reports a `BACKED_GUARD` event (property test, 300 cases).

### Golden cases (28-Nov, from the `shim` branch, PR #350, before it merged)

`av2-golden --mode v2 --kind drain` on the 158 recorded production climbs
(`propagation_queue.drain_batch`, recorded 07:39-08:07 UTC), in a scratch
merge of #350 with this stack, its adapters switched from `NoLoops` to
`V1Loops`:

| Loop rules | Clean | Differ | Field diffs |
|---|---:|---:|---|
| `NoLoops` (the shim's adapters today) | 145 | 13 | 1,342 extra writes, 320 value diffs, 88 missing writes (`backed_*`) |
| **`V1Loops` (calibrated budgets)** | **154** | **4** | 42 value diffs, 12 missing writes (`backed_*`) |
| `V1Loops`, unlimited budgets | 154 | 4 | identical to the calibrated run |
| `V1Loops`, budgets 0 | 146 | 12 | 444 value diffs, 1,505 extra writes |

So the loop rules close 9 of the 13 differing climbs, and the calibrated
budgets behave like unlimited ones on these cases (as v1's clock did not
bite).  The 4 left are the largest and longest batches of the sample (v1
took 0.8 to 6.5 s and read 2,300 to 15,000 positions; the clean ones
p50 144 ms and 194 positions); their differences are ties between moves of
equal value and the plies and support that follow (one family also 986 vs
968).  Not yet explained: candidates are rows another transaction committed
while v1's batch ran (READ COMMITTED, the races the rechecks exist for),
and the row and parent orders that D3 fixes by id.  The shim's adapters
should pass `V1Loops::default()` once this stack merges.

## Benchmark: climbs on the production slice

`av2-climb` (`crates/av2-tools/src/bin/av2-climb.rs`) on the slice of
`docs/store.md` (1,560,369 positions, 1,994,101 edges, loaded with
`av2-load`), Windows 10, Ryzen 9 5950X, store on the NVMe, release build,
32 climbs per commit, one fsync per commit, 300 commits per row:

| Workload | Noise cut | Climbs/s with commits | Rules only, µs per climb | Rules only, climbs/s | Rows written per climb (cascade + backed) | Levels | Rows processed | Commit p50 / p99 |
|---|---|---:|---:|---:|---:|---:|---:|---|
| re-search of a searched interior position | on (v1) | **15,876** | 31.6 | 31,656 | 0.06 + 1.75 | 2.2 | 5.7 (max 194) | 1.81 / 4.16 ms |
| same | off (D2) | 17,484 | 28.0 | 35,679 | 0.05 + 1.31 | 2.1 | 5.0 (max 56) | 1.73 / 3.40 ms |
| mate line closes a child | on (v1) | **10,449** | 40.1 | 24,919 | 1.60 + 1.83 | 2.1 | 6.9 (max 79) | 1.44 / 2.79 ms |
| same | off (D2) | 10,239 | 38.3 | 26,114 | 1.70 + 1.72 | 2.1 | 6.7 (max 63) | 1.36 / 2.55 ms |

Against the stand-ins: the store's `climb_b32` with stand-in rules (five
blind writes up the first-parent chain, no reads of children) measured
38,100/s; the storage benchmark's mmap store 23,297/s re-searches.  The real
rules read every child of every row they decide and buy as they go, and
still leave the commit (fsync, ~0.8 ms) as a large share of a 32-climb
batch.  The rows differ in seed between the two settings (different
positions), so the D2 columns are not a paired comparison.  The slice has
no FEN, witness lines or showcases (see the tool's header); the
closure-workload count of climbs is lower because a pick that finds no
open child is skipped.
