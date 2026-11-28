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
  stays: the fixed point depends on visit order.
- A `MINIMAX` row whose stored slack, grade or distance disagrees with its
  children changes when visited; a parent visited before reads the old
  value (and may close through it), one visited after reads the new one.
- With consistent rows and lines on every `MATE_PV`, the fixed point on a
  DAG is independent of seed order except for CAS-1.3 above (property test,
  400 cases per run, 5,000 checked once).

## Steps 6 to 9

Pending (next PRs).
