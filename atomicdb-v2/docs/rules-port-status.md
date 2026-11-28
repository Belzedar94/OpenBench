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

## Steps 3 to 9

Pending (next PRs).
