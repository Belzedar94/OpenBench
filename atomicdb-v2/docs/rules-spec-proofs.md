# AtomicDB v1 rules: proof numbers, mate proofs, tablebases, certificates, score bands

Part of the v1 rules specification for the v2 port: see
[`rules-spec.md`](rules-spec.md) for the conventions, the landing pipeline,
the value rules, the determinism register and the port order, and
[`rules-spec-queue.md`](rules-spec-queue.md) for the task model, leases,
queues and the selector.

Scope of this file: every rule of AtomicDB v1 that decides a stored or served
value or a state transition in

1. proof numbers (`atomicdb/proof.py`, `atomicdb/solve_estimate.py`, the
   `ProofCampaign`/`ProofNode` storage, the refresh after commit);
2. mate proofs and certificates (`atomicdb/logic.py` exact layer, mate-PV and
   AND/OR online proofs, tablebase closures, PV verification purchases, solver
   certificates, survival certificates, autosolve, and the management commands
   that rewrite proofs);
3. score bands (`atomicdb/score_bands.py`) and the mate-sign conventions.

Source: `atomicdb/` at `origin/spell-runner` `f59413e` (identical on
`origin/atomicdb-v2` `1c4dc43`). Line numbers are those of that commit. Test
references are `<file under atomicdb/>::<Class>::<test_name>`; the full list
per rule group is in [`test-map.csv`](test-map.csv).

Out of scope (in `rules-spec.md`): backed values, loops, `backup_cascade`,
`mate_distance_refresh`, `revoke_closure`, queue/lease. Where a rule here feeds
one of those, the section says so under **Ordering** ("interface").

Conventions used in every section:

- "White POV" means the value is signed from White's point of view (positive is
  good for White). Every stored `eval_cp`, `backed_eval` and line `eval_cp` is
  White POV. Line field `mate` is White POV too (positive: White mates).
- "Canonical FEN" is `logic.canonical_fen`: board, side to move, castling,
  en-passant square only when the capture is executable, counters `0 1`.
- `INF` is `PROOF_INFINITY = 2^62`.
- "pyffish order" is the order in which `pyffish.legal_moves('atomic', fen, [])`
  returns moves. Several rules depend on it (see the determinism sections).

## Index of rule IDs

### PN: proof numbers
- PN-0.1 Proof-number storage model (ProofCampaign, ProofNode)
- PN-0.2 Default campaign and active campaigns
- PN-1.1 Saturating arithmetic
- PN-1.2 Infinity and the leaf ceiling
- PN-1.3 OR/AND classification of a node
- PN-1.4 Terminal (closed-node) numbers
- PN-1.5 Eval bands
- PN-1.6 Leaf numbers (heuristic initialisation)
- PN-1.7 Internal numbers (recurrences)
- PN-2.1 Solve-gate switch and the maintenance field set
- PN-2.2 Annoyance feature: reversible density
- PN-2.3 Annoyance feature: branching
- PN-2.4 Annoyance feature: material tent
- PN-2.5 Annoyance feature: eval band
- PN-2.6 Annoyance combination
- PN-2.7 Gate factor
- PN-2.8 Gated leaf pn
- PN-2.9 Full-row versus edge-view annoyance
- PN-2.10 SOLVE_GATE_DISAGREE log and its time window
- PN-3.1 Cycling-edge detection (carrier walks)
- PN-3.2 Carrier selection
- PN-3.3 Carrier cache
- PN-3.4 Echo edge priced as a leaf with pn = PROOF_MAX_LEAF
- PN-4.1 compute_numbers (one node)
- PN-4.2 refresh_proof_numbers (entry point)
- PN-4.3 _refresh_campaign level loop
- PN-4.4 Refresh caps and PROOF_GUARD
- PN-4.5 Skip rows locked by another transaction
- PN-4.6 Served proof numbers (root, headline, explorer rows, format)
- PN-5.1 Sticky primary (hysteresis) at OR nodes
- PN-6.1 Normalised repertoire policy
- PN-6.2 Deterministic draws (_point, _bucket)
- PN-6.3 Campaign roots and the campaign start draw
- PN-6.4 Ranking children for the proof descent
- PN-6.5 descend (df-pn descent)
- PN-6.6 Interface: the descent counter and the minting loop
- PN-7.1 OR clamp (proved_or_clamp)
- PN-8.1 Proof refresh hook at the end of every cascade
- PN-8.2 Proof refresh after commit (sink)
- PN-8.3 ProofPipeline (refresh on its own thread)
- PN-8.4 Refresh of the landed position after apply_job / drain
- PN-8.5 Other refresh callers
- PN-9.1 Descent mode switch
- PN-9.2 Value ranking of children
- PN-9.3 Spine step and the fallback primary
- PN-9.4 Spine walk
- PN-9.5 Tip plan (spine, bottleneck, rung)
- PN-9.6 Competitive alternatives
- PN-9.7 Value detours (backtrack)
- PN-9.8 Best unexplored child
- PN-9.9 Plan order
- PN-9.10 Long-line weight
- PN-9.11 descend_value selection loop
- PN-9.12 Interface: arm prices (value_budget, value cascade)
- PN-10.1 Proof frontier and frontier_rows
- PN-10.2 AND frontier and dn statistics
- PN-10.3 Saturated open counts
- PN-10.4 Most-proving frontier and oldest obligations
- PN-10.5 recascade_proof command

### MPV: mate proofs and PV verification
- MPV-1.1 verify_mate_pv
- MPV-1.2 walk_pv
- MPV-2.1 prove_forced_mate (bounded exhaustive AND/OR)
- MPV-2.2 Worst reversible run of a proof
- MPV-2.3 Movegen memo does not change results
- MPV-2.4 prepare_mate_proofs (online preparation)
- MPV-2.5 ONLINE_MATE_PROOF_SECONDS
- MPV-2.6 Closing an open child from a prepared line
- MPV-2.7 NO_MATE on an open child: DISPUTED and deep reanalysis
- MPV-2.8 Trust gate on unproven lines (interface)
- MPV-3.1 Online revocation of a refuted closed witness
- MPV-4.1 Certified line adopted by a closed child
- MPV-5.1 won_line materialisation (entry, grade, slack)
- MPV-5.2 Batched chain write versus stepwise walk
- MPV-5.3 Closing one link of the chain
- MPV-5.4 TB_LINE chain: re-check and end node
- MPV-6.1 PV verification: candidates
- MPV-6.2 PV verification: step rule
- MPV-6.3 PV verification: buying one node
- MPV-6.4 PV verification: the walk
- MPV-6.5 PV verification: covered prefix
- MPV-6.6 enqueue_pv_verification (line selection and receipt)
- EXP-4.1 claimed_mate_plies (and move-to-ply conversion)
- EXP-4.2 _short_mate_clamp
- EXP-4.3 Clamp consumers (multipv_for, server_picked_budget, budget_for)

### Exact layer in logic.py
- POS-4.1 terminal_status
- CAS-1.1 backup_status (three-valued minimax of one node)
- CAS-1.2 backup_eval
- DTM-5.1 is_zeroing
- DTM-5.2 edge_slack
- DTM-5.3 edge_supports (fresh-context rule)
- DTM-5.4 minimax_slack
- DTM-5.5 line_clock_slack
- DTM-5.6 slack_from_run
- DTM-5.7 slack_from_witness_length
- DTM-5.8 slack_from_dtz
- DTM-5.9 Slack clamp and constants

### TB: tablebases
- TB-1.1 Applicability
- TB-1.2 probe_wdl (server probe)
- TB-1.3 wdl_to_status
- TB-2.1 Server table horizon (tb_server_max_men)
- TB-2.2 prepare_tb_closure (validation of a worker WDL)
- TB-2.3 Server-side TB closure of a searched position
- TB-2.4 _apply_prepared_tb (the closure write)
- TB-2.5 apply_job routing of TB results
- TB-3.1 prepare_tb_line (line into a won ending)
- TB-4.1 recompute_tb_distance command
- TB-5.1 close_queued_by_tables (TB sweep)

### SOL: solver and survival certificates
- SOL-1.1 Certificate container (gzip, size limits)
- SOL-1.2 SOLVE certificate format and header checks
- SOL-1.3 SOLVE certificate replay
- SOL-1.4 SOLVE certificate report and slack
- SOL-2.1 apply_solve_result (state machine)
- SOL-2.2 Closing an open position by certificate
- SOL-2.3 Upgrading a closed position by certificate
- SOL-2.4 Solver DISPROVED on a closed position
- SOL-2.5 Post-result cascade and attribution
- SOL-2.6 Submit endpoint: accepted outcomes and trust
- SOL-3.1 Survival certificate format and header
- SOL-3.2 Survival certificate checks
- SOL-3.3 Survival verifier routing (native vs reference)
- SOL-3.4 A verified survival never closes a position
- SOL-3.5 Fortress classifier (scheduling only)
- SOL-4.1 Autosolve: what a showcase claims
- SOL-4.2 Autosolve at landing
- SOL-4.3 Autosolve backlog walk
- SOL-4.4 Engine debt arm
- SOL-4.5 Fragile mate claim arm
- SOL-4.6 Solve pilot (goal by sign)
- SOL-5.1 Recycling stale solve leases

### CMD: management commands that rewrite proofs
- CMD-1 verify_mates
- CMD-2 recertify_mates
- CMD-3 verify_certificate
- CMD-4 backfill_mate_distance (interface)
- CMD-5 materialise_mate_lines

### SB: score bands and sign conventions
- SB-1 Stored bands and constants
- SB-2 tb_distance
- SB-3 engine_cp_to_stored
- SB-4 legacy_value and raw_score
- SB-5 stored_from_raw
- SB-6 normalize_line / normalize_lines
- SB-7 band()
- SB-8 Where normalisation is applied
- SB-9 Mate encoding and sign conventions
- SB-10 Thresholds other rules read

---

# Part 1: Proof numbers (PN)

### PN-0.1 Proof-number storage model
- **Statement.** Proof numbers are not columns of `Position`. A `ProofCampaign` (name unique, `root` Position, `goal` in {`WHITE_WIN`,`BLACK_WIN`}, `active` bool, `algorithm_version` int default 1, `repertoire_policy` JSON) owns one `ProofNode` per position it has reached: `(campaign, position)` unique, `pn` BigInteger default 1, `dn` BigInteger default 1, `expanded_in_proof` bool default False, `selected_child` move UCI (max 8) or NULL, `updated` (auto_now). `pn` estimates the effort to PROVE the goal from the node, `dn` the effort to REFUTE it. Both saturate at `PROOF_INFINITY`. `expanded_in_proof` is True exactly when the node is open and has at least one materialised edge at its last refresh (distinct from `Position.expanded`). `selected_child` is the primary move chosen by PN-4.1 (NULL for closed or edge-less nodes). A row exists only once a refresh (PN-4.3) has reached the position; absence is meaningful for serving (PN-4.6) and for frontier membership (PN-10.1). Indexes `(campaign, pn)`, `(campaign, dn)`.
- **Inputs / outputs.** Tables `atomicdb_proofcampaign`, `atomicdb_proofnode`. Written only by PN-4.3 (create via `bulk_create(ignore_conflicts=True)`, update via `bulk_update` of `pn, dn, expanded_in_proof, selected_child`). Note `bulk_update` does not refresh `updated`; `bulk_create` sets it at creation, so `ProofNode.updated` is in practice the creation time.
- **Code.** `atomicdb/models.py:ProofCampaign:593`, `atomicdb/models.py:ProofNode:630`; writes in `atomicdb/proof.py:_refresh_campaign:758`.
- **Tests.** `test_proof_manager.py::CampaignSeedTests::test_migration_created_the_default_campaign`; `test_proof_manager.py::IncrementalMaintenanceTests::test_numbers_are_written_for_the_touched_cone`. **Gaps:** nothing pins that `updated` stays the creation time; nothing pins `algorithm_version` semantics (never read).
- **Determinism hazards.** `updated` is wall clock (`auto_now`) and is read by `open_and_obligations` ordering (PN-10.4). Row presence depends on refresh history (which seeds were ever refreshed), not on graph state alone.
- **History.** Introduced with the proof manager (July 2026). D5 (owner) proposes computing these numbers instead of storing them.
- **Ordering.** Read by PN-4.1/6.5/9.2/10.x; written by PN-4.3.

### PN-0.2 Default campaign and active campaigns
- **Statement.** The default campaign is named `root-white-win`, root = canonical start position, goal `WHITE_WIN`, `algorithm_version` 1, policy `{'primary': 0.8, 'backup': 0.15, 'explore': 0.05}`. `default_campaign(create=True)` returns it (first row by that name) or creates it (creating the start position through `ingest.get_or_create_position`). `active_campaigns()` is every campaign with `active=True`, ordered by `id`. The "headline" campaign used by pages and snapshots is the first active campaign by `id`.
- **Inputs / outputs.** Reads/creates `ProofCampaign`; may create the start `Position`.
- **Code.** `atomicdb/proof.py:default_campaign:399`, `atomicdb/proof.py:active_campaigns:414`; constants `proof.py:100-101`.
- **Tests.** `test_proof_manager.py::CampaignSeedTests::test_migration_created_the_default_campaign`; `test_proof_manager.py::CampaignSeedTests::test_policy_defaults_to_eighty_fifteen_five`; `test_proof_kpi.py::HeadlineNumbersTests::test_the_default_campaign_is_the_headline`; `test_proof_kpi.py::HeadlineNumbersTests::test_no_active_campaign_means_no_headline`.
- **Determinism hazards.** Campaign iteration order is `id` order (row ids).
- **History.** Data migration seeds the default campaign.
- **Ordering.** Before any refresh or descent.

### PN-1.1 Saturating arithmetic
- **Statement.** `saturating_sum(values)`: start at 0; for each value add `max(0, int(value))`; as soon as the running total is `>= INF` return `INF`. Empty → 0. `saturating_min(values)`: minimum of `max(0, int(v))`, starting from `INF`; empty → `INF`. Negative inputs are treated as 0.
- **Inputs / outputs.** Pure.
- **Code.** `atomicdb/proof.py:saturating_sum:188`, `atomicdb/proof.py:saturating_min:197`.
- **Tests.** `test_proof_manager.py::RecurrenceTests::test_saturation_never_overflows`.
- **Determinism hazards.** none (integers; Python ints are unbounded, a port must avoid overflow before the clamp: use 128-bit or saturating adds).
- **History.** Original.
- **Ordering.** Used by PN-1.7.

### PN-1.2 Infinity and the leaf ceiling
- **Statement.** `PROOF_INFINITY = 2^62`; it means "proved" (dn) or "refuted" (pn) and only closed nodes or sums/mins that saturate may reach it. `PROOF_MAX_LEAF = 2^20`: every heuristic leaf number is clamped to `[1, 2^20]` so a leaf can never claim infinity.
- **Inputs / outputs.** Constants.
- **Code.** `atomicdb/proof.py:94`, `atomicdb/proof.py:97`.
- **Tests.** `test_proof_manager.py::LeafInitialisationTests::test_a_leaf_never_claims_infinity`; `test_solve_gate.py::GateArithmeticTests::test_a_gated_leaf_still_never_claims_infinity`; `test_proof_kpi.py::FormatNumberTests::test_saturation_reads_as_infinity_not_as_a_huge_integer`.
- **Determinism hazards.** none.
- **History.** Original.
- **Ordering.** —

### PN-1.3 OR/AND classification of a node
- **Statement.** The attacker is White iff goal is `WHITE_WIN`. A node is an OR node iff the side to move (second FEN field `w`/`b`) is the attacker; otherwise AND.
- **Inputs / outputs.** `Position.fen`, `ProofCampaign.goal`.
- **Code.** `atomicdb/proof.py:attacker_is_white:312`, `atomicdb/proof.py:is_or_node:316`.
- **Tests.** `test_proof_manager.py::RecurrenceTests::test_goal_reversal_swaps_the_roles`.
- **Determinism hazards.** none.
- **History.** Original.
- **Ordering.** —

### PN-1.4 Terminal (closed-node) numbers
- **Statement.** `terminal_numbers(status, goal)`: `UNKNOWN` → None (open). `status == goal` → `(0, INF)`. Any other closed status (the other win or `DRAW`) → `(INF, 0)`. Binary on purpose: a draw refutes both win goals.
- **Inputs / outputs.** `Position.status`.
- **Code.** `atomicdb/proof.py:terminal_numbers:331`.
- **Tests.** `test_proof_manager.py::RecurrenceTests::test_proved_node_is_a_leaf_with_its_truth_value`; `test_solve_gate.py::GateArithmeticTests::test_a_closed_leaf_keeps_its_truth_value`; `test_proof_manager.py::IncrementalMaintenanceTests::test_a_proved_child_drives_the_parent_to_zero_pn`.
- **Determinism hazards.** none.
- **History.** Original.
- **Ordering.** First check of PN-1.6 and PN-4.1.

### PN-1.5 Eval bands
- **Statement.** `attacker_score = eval_cp` if the attacker is White else `-eval_cp` (eval_cp is White POV). `eval_band(score)`: None if score is None; otherwise the first `(threshold, pn_w, dn_w)` of `EVAL_BANDS` with `score >= threshold`, in this order: `(9000, 1, 64)`, `(800, 2, 16)`, `(300, 4, 8)`, `(-300, 8, 8)`, `(-800, 16, 4)`, `(-9000, 32, 2)`; below -9000 → `DEFAULT_EVAL_BAND = (64, 1)`. Weights are ordinal, monotone in the attacker's favour.
- **Inputs / outputs.** Pure.
- **Code.** `atomicdb/proof.py:EVAL_BANDS:136`, `atomicdb/proof.py:eval_band:322`.
- **Tests.** `test_proof_manager.py::LeafInitialisationTests::test_attacker_advantage_makes_proving_look_cheaper`; `test_proof_manager.py::LeafInitialisationTests::test_eval_is_read_from_the_attacker_side`.
- **Determinism hazards.** none.
- **History.** Original. Note the top band (>= 9000) covers the whole stored band (big evals, TB distances, engine mates) since the score-band change of 24-Nov (SB-1).
- **Ordering.** Inside PN-1.6.

### PN-1.6 Leaf numbers (heuristic initialisation)
- **Statement.** `leaf_numbers(fen, status, eval_cp, goal, legal_moves=None, annoyance=None, key=None)`:
  1. closed status → PN-1.4 numbers;
  2. `band = eval_band(attacker_score)`; if `band is None and not legal_moves` → `(1, 1)` (classic PNS, never gated);
  3. `(pn_w, dn_w) = band or (1, 1)`; `branching = max(1, int(legal_moves or 1))`;
  4. OR node: `base = (1, branching)`; AND node: `base = (branching, 1)`;
  5. `pn = min(2^20, max(1, base_pn * pn_w))`, `dn = min(2^20, max(1, base_dn * dn_w))`;
  6. return `(gated_pn(pn, annoyance, attacker_score, key, eval_cp), dn)` (PN-2.8).
  **In production `legal_moves` is never passed** (no caller in non-test code), so `branching` is always 1 and an informed leaf is exactly `(pn_w, dn_w)` (gated) and an uninformed leaf is `(1, 1)`.
- **Inputs / outputs.** Pure, except that a call with `key` may write a `SOLVE_GATE_DISAGREE` DBEvent (PN-2.10).
- **Code.** `atomicdb/proof.py:leaf_numbers:342`.
- **Tests.** `test_proof_manager.py::LeafInitialisationTests::test_no_information_falls_back_to_classic_one_one`; `test_proof_manager.py::LeafInitialisationTests::test_branching_pushes_the_side_that_must_answer_everything` (exercises the unused `legal_moves` path); `test_proof_manager.py::DescentTests::test_an_unexplored_sibling_keeps_the_classic_one_one`; `test_solve_gate.py::GateOffIsTheHistoricBehaviourTests::test_the_numbers_are_the_documented_ones`; `test_solve_gate.py::GateArithmeticTests::test_the_classic_one_one_is_exempt`. **Gaps:** nothing pins that production never passes `legal_moves`.
- **Determinism hazards.** Only via PN-2.10 (event write).
- **History.** Original; gate added 30-Jul (merge `56d1370`, "Merge solve-gate").
- **Ordering.** Used by PN-4.1, PN-4.6, PN-6.4, PN-9.2.

### PN-1.7 Internal numbers (recurrences)
- **Statement.** `internal_numbers(fen, goal, child_numbers)`: empty → None. OR node: `(saturating_min(pns), saturating_sum(dns))`. AND node: `(saturating_sum(pns), saturating_min(dns))`.
- **Inputs / outputs.** Pure.
- **Code.** `atomicdb/proof.py:internal_numbers:386`.
- **Tests.** `test_proof_manager.py::RecurrenceTests::test_or_node_takes_min_pn_and_sum_dn`; `test_proof_manager.py::RecurrenceTests::test_and_node_takes_sum_pn_and_min_dn`; `test_proof_manager.py::RecurrenceTests::test_goal_reversal_swaps_the_roles`.
- **Determinism hazards.** none.
- **History.** Original.
- **Ordering.** Inside PN-4.1.

### PN-2.1 Solve-gate switch and the maintenance field set
- **Statement.** The "double gate" is OFF unless `settings.ATOMICDB_SOLVE_GATE` is truthy (env `ATOMICDB_SOLVE_GATE` in `1/true/yes`). Off: `gate_annoyance` and `shallow_annoyance` return None and every leaf is computed exactly as without the gate. The refresh loads positions with `.only('key','fen','status','eval_cp')`, plus `('last_analysis','mate_in')` when the gate is on.
- **Inputs / outputs.** Setting `ATOMICDB_SOLVE_GATE`.
- **Code.** `atomicdb/proof.py:solve_gate_enabled:215`, `atomicdb/proof.py:maintenance_fields:220`.
- **Tests.** `test_solve_gate.py::GateOffIsTheHistoricBehaviourTests::test_the_flag_defaults_to_off`; `test_solve_gate.py::GateOnTests::test_the_flag_turns_it_on`; `test_solve_gate.py::GateOffIsTheHistoricBehaviourTests::test_the_maintenance_only_stays_narrow`; `test_solve_gate.py::GateOnTests::test_the_maintenance_asks_for_the_pv_it_is_going_to_read`; `test_solve_gate.py::GateOffIsTheHistoricBehaviourTests::test_no_annoyance_is_computed_at_all_when_it_is_off`.
- **Determinism hazards.** Settings/env switch changes stored pn.
- **History.** `56d1370` (30-Jul).
- **Ordering.** Before PN-2.9.

### PN-2.2 Annoyance feature: reversible density
- **Statement.** `current_line(row)`: the first element of `row.last_analysis` that is a dict without `prior_pass`; None if none. Deferred fields (not loaded by `.only`) read as None (never trigger a query). `reversible_feature(row)`: pv = current line's `pv`; if no pv or no fen → `NEUTRAL = 0.5`. `zeroing_plies(fen, pv, max_plies=32)`: build a board map `{square: piece char}` from the FEN board field (ranks 8..1, files a..h); for each UCI move in `pv[:32]`: stop at the first non-string or string shorter than 4; `read += 1`; zeroing if `len(uci) > 4` (promotion) or the destination square is occupied in the map or the moving piece is `P`/`p`; then remove the origin; if the destination was occupied remove it too (atomic: capturer and captured vanish; neighbours are NOT exploded); else place the moving piece (if any) on the destination. If `read == 0` → NEUTRAL; else `clamp01(1 - zeroing/read)`. The proxy is exact on ply 1 and drifts later (no explosion, castling read as capture only if the destination is occupied).
- **Inputs / outputs.** `Position.fen`, `Position.last_analysis`.
- **Code.** `atomicdb/solve_estimate.py:_stored:134`, `:current_line:149`, `:_board_map:195`, `:zeroing_plies:213`, `:reversible_feature:234`.
- **Tests.** `test_solve_gate.py::ReversibleFeatureTests::*` (all 10: `test_a_capture_counts_even_when_no_pawn_moves`, `test_a_malformed_pv_stops_the_walk_instead_of_crashing`, `test_a_prior_pass_line_is_not_the_current_verdict`, `test_a_promotion_counts_as_zeroing`, `test_a_pv_of_manoeuvres_is_the_tedious_end`, `test_a_pv_of_pawn_moves_and_captures_is_the_cheap_end`, `test_no_pv_is_neutral_not_a_guess`, `test_the_density_is_a_fraction_not_a_count`, `test_the_first_ply_is_exact_deeper_ones_are_a_proxy`, `test_the_scan_is_bounded`).
- **Determinism hazards.** none (pure; float division).
- **History.** `56d1370`.
- **Ordering.** Inside PN-2.6.

### PN-2.3 Annoyance feature: branching
- **Statement.** `branching_feature(row, branching=None)`: if `branching` (edge count) is given → `clamp01(branching / 40)`. Else `lines` = number of dict lines of `last_analysis` without `prior_pass`; 0 → NEUTRAL; else `max(0.5, clamp01(lines / 5))` (the MultiPV proxy can only push up). In the refresh and the descent `branching` is never passed.
- **Inputs / outputs.** `Position.last_analysis`.
- **Code.** `atomicdb/solve_estimate.py:current_lines:163`, `:branching_feature:249`.
- **Tests.** `test_solve_gate.py::BranchingFeatureTests::test_no_information_at_all_is_neutral`; `::test_the_edge_count_is_trusted_in_both_directions`; `::test_the_multipv_proxy_can_only_push_upwards`.
- **Determinism hazards.** none.
- **History.** `56d1370`.
- **Ordering.** Inside PN-2.6.

### PN-2.4 Annoyance feature: material tent
- **Statement.** `men = logic.piece_count(fen)` (letters in the board field, kings included). No fen → NEUTRAL. `men <= 6` → 0.0; `men <= 12` → `clamp01((men - 6)/6)`; else `clamp01((32 - men)/20)`.
- **Inputs / outputs.** `Position.fen`.
- **Code.** `atomicdb/solve_estimate.py:material_feature:269`.
- **Tests.** `test_solve_gate.py::MaterialFeatureTests::test_a_full_board_is_tactical_not_tedious`; `::test_below_the_tablebase_horizon_the_answer_is_a_lookup`; `::test_the_curve_is_a_tent_not_a_ramp`; `::test_the_peak_is_the_bare_endgame_above_that_horizon`.
- **Determinism hazards.** none.
- **History.** `56d1370`.
- **Ordering.** Inside PN-2.6.

### PN-2.5 Annoyance feature: eval band
- **Statement.** `eval_band_feature(row)`: 0.0 if `mate_in` is not None (read without query); 0.0 if the current line has a truthy `mate`; NEUTRAL if `eval_cp` is None; else with `s = |eval_cp|`: `s >= 9000` → 0.0, `s >= 800` → 0.2, `s >= 300` → 0.5, else 1.0. Colour-blind.
- **Inputs / outputs.** `Position.mate_in`, `last_analysis`, `eval_cp`.
- **Code.** `atomicdb/solve_estimate.py:eval_band_feature:284`.
- **Tests.** `test_solve_gate.py::EvalBandFeatureTests::test_no_eval_is_neutral`; `::test_seen_mate_is_the_fast_lane`; `::test_the_band_is_ordinal_and_monotone`; `::test_the_sign_does_not_matter`.
- **Determinism hazards.** none.
- **History.** `56d1370`.
- **Ordering.** Inside PN-2.6.

### PN-2.6 Annoyance combination
- **Statement.** `annoyance(pos, branching=None) = clamp01(0.35*reversible + 0.25*branching + 0.20*material + 0.20*eval_band)`, summed in exactly that order as float64 (weights in `FEATURE_WEIGHTS`, they sum to 1.0). Pure: no engine, no query.
- **Inputs / outputs.** Pure.
- **Code.** `atomicdb/solve_estimate.py:features:306`, `:FEATURE_WEIGHTS:316`, `:annoyance:324`.
- **Tests.** `test_solve_gate.py::AnnoyanceTests::*` (4); `test_solve_gate.py::WeightsTests::*` (3); `test_solve_gate.py::DumpSolveCostsTests::test_the_annoyance_column_is_the_estimator`.
- **Determinism hazards.** Float summation order must be replicated bit for bit (it feeds `int(pn * factor)`).
- **History.** `56d1370`.
- **Ordering.** Before PN-2.7.

### PN-2.7 Gate factor
- **Statement.** `gate_factor(a) = 1.0 + (8 - 1.0) * clamp01(a)` (`MAX_FACTOR = 8`). Inputs outside [0,1] are clamped.
- **Inputs / outputs.** Pure.
- **Code.** `atomicdb/solve_estimate.py:gate_factor:352`.
- **Tests.** `test_solve_gate.py::GateFactorTests::*` (4).
- **Determinism hazards.** none.
- **History.** `56d1370`.
- **Ordering.** Inside PN-2.8.

### PN-2.8 Gated leaf pn
- **Statement.** `gated_pn(pn, annoyance, attacker_score, key=None, eval_cp=None)`: annoyance None → pn. `attacker_score is not None and attacker_score >= 9000` (`SOLVE_GATE_MATE_BAND = EVAL_BANDS[0][0]`) → pn (mate band exempt). Else `factor = gate_factor(annoyance)`, `gated = min(2^20, max(1, int(pn * factor)))` (float64 product, truncation toward zero). If `key` is truthy and `gated > pn` and `attacker_score >= 300` (`SOLVE_GATE_OPTIMISTIC`) → log (PN-2.10). Return `gated`. `dn` is never gated. The classic `(1,1)` leaf never reaches this function (PN-1.6 step 2).
- **Inputs / outputs.** May write DBEvent `SOLVE_GATE_DISAGREE`.
- **Code.** `atomicdb/proof.py:gated_pn:284`.
- **Tests.** `test_solve_gate.py::GateArithmeticTests::test_annoyance_multiplies_the_pn`; `::test_dn_is_never_touched`; `::test_the_gate_is_monotone_in_the_annoyance`; `::test_the_mate_band_is_exempt`; `::test_zero_annoyance_changes_nothing`; `test_solve_gate.py::GateOnTests::test_a_tedious_frontier_leaf_gets_more_expensive`; `::test_turning_it_off_again_restores_the_numbers`; `test_solve_gate.py::GateOnTests::test_the_descent_ranks_the_tedious_child_last`.
- **Determinism hazards.** Float multiply then `int()`; replicate float64 semantics.
- **History.** `56d1370`.
- **Ordering.** Last step of PN-1.6.

### PN-2.9 Full-row versus edge-view annoyance
- **Statement.** `gate_annoyance(row, branching=None)` = `solve_estimate.annoyance(row, branching)` on a full Position row (gate on), used ONLY for (a) a frontier leaf in PN-4.1 (a position with no materialised edge; this call also passes `key=position.key`, the only path that may log) and (b) `root_numbers` (no key). `shallow_annoyance(fen, eval_cp)` = annoyance of `Leaf(fen, eval_cp, None, None)` (reversible and branching fall to NEUTRAL, eval-band feature sees only eval_cp), used for every child priced from an edge (PN-4.1 children without a row or on a cycling edge, PN-6.4, PN-9.2). Both return None with the gate off.
- **Inputs / outputs.** Pure except via PN-2.10.
- **Code.** `atomicdb/proof.py:gate_annoyance:227`, `atomicdb/proof.py:shallow_annoyance:239`, `atomicdb/solve_estimate.py:shallow:125`.
- **Tests.** `test_solve_gate.py::DisagreementQuadrantTests::test_the_degraded_edge_view_does_not_write_the_dataset`; `test_solve_gate.py::GateOnTests::test_a_level_still_costs_a_fixed_number_of_statements`.
- **Determinism hazards.** none beyond PN-2.10.
- **History.** `56d1370`.
- **Ordering.** Inside PN-4.1.

### PN-2.10 SOLVE_GATE_DISAGREE log and its time window
- **Statement.** `_log_disagreement(key, eval_cp, annoyance, factor)`: `now = time.monotonic()`; if the process-level dict `_solve_gate_logged` has `key` with `now - ts < 900.0` s → no event, return False. If the dict holds `>= 4096` entries: drop entries older than `now - 900`; if still `>= 4096` clear it entirely. Store `key → now`; create `DBEvent(kind='SOLVE_GATE_DISAGREE', payload={'key', 'eval_cp' (White POV, as stored), 'annoyance': round(a, 4), 'factor': round(f, 3)})`; return True. The log does not change any number.
- **Inputs / outputs.** DBEvent `SOLVE_GATE_DISAGREE`; module cache `_solve_gate_logged`; `reset_solve_gate_log()` clears it.
- **Code.** `atomicdb/proof.py:_log_disagreement:258`, `:reset_solve_gate_log:253`, constants `proof.py:161-174`.
- **Tests.** `test_solve_gate.py::DisagreementQuadrantTests::*` (12, e.g. `test_the_same_leaf_is_not_logged_once_per_pass`, `test_the_cache_does_not_grow_without_bound`, `test_the_rate_limit_cache_can_be_reset`, `test_the_eval_in_the_payload_is_white_pov_like_the_column`, `test_the_mate_band_never_disagrees`, `test_the_real_maintenance_pass_logs_the_frontier_leaf`, `test_nothing_is_logged_with_the_gate_off`).
- **Determinism hazards.** Wall clock (`time.monotonic`), per-process cache (several processes log duplicates), DBEvent timestamps. Event emission depends on refresh scheduling.
- **History.** `56d1370`.
- **Ordering.** Inside PN-2.8.

### PN-3.1 Cycling-edge detection (carrier walks)
- **Statement.** `_cycling_edges(campaign, parents, children_by_parent, node_rows, cache)` returns the set of `(parent_key, move)` whose value travels back into the parent. Setup: for each parent in `parents` order, for each edge `(move, child, status, eval, fen)` in edge-id order: skip if `status != 'UNKNOWN'`; skip if the child has neither a row in `node_rows` nor non-None numbers in `cache.numbers`; otherwise create two walks, dn first then pn: `[parent, move, node=child, fen=child_fen, want_dn, seen={child}]`. Then up to `PROOF_CYCLE_MAX_PLIES = 32` rounds while walks remain: fetch carrier steps for all current nodes (PN-3.3); for each walk: skip if `(parent, move)` is already in the result; `below = _carrier_next(goal, cache, node, fen, want_dn)` (PN-3.2, `fen` is the current node's fen); None → drop walk; `below == parent` → add `(parent, move)` and drop; `below in seen` → drop (a cycle not through this parent); else find the first cached child row of `node` with id `below` (none → drop), add `below` to `seen`, move to it (node, fen) and keep the walk. Every doubtful stop claims no cycle.
- **Inputs / outputs.** Reads `Edge` (parent, child, child status, child fen) and `ProofNode(pn, dn)` through the cache; returns a set.
- **Code.** `atomicdb/proof.py:_cycling_edges:609`.
- **Tests.** `test_proof_cycles.py::CycleRuleTests::test_cycling_edge_is_detected_along_value_carriers`; `::test_a_cycle_within_the_cap_is_claimed`; `::test_long_cycles_beyond_the_cap_claim_nothing`; `::test_the_walk_follows_values_not_the_sticky_primary`; `::test_the_dn_ratchet_is_dead`; `::test_the_rule_is_idempotent_and_never_oscillates`; `::test_poisoned_shuttle_drains_to_a_fixpoint`; `::test_fresh_component_does_not_ratchet`.
- **Determinism hazards.** Ties depend on edge-id order (PN-3.2). The cache may hold numbers computed earlier in the same refresh, including rows later skipped as locked (PN-4.5), so the detector can see values that were never committed.
- **History.** `7d7e3db` (6-Aug, invariant 6, first as `(INF,0)`), changed the same day by `98629c0` to the leaf-estimate rule (PN-3.4) after measured oscillation. See `docs/value-semantics.md` invariant 6.
- **Ordering.** Runs once per level in PN-4.3 BEFORE any node of the level is computed.

### PN-3.2 Carrier selection
- **Statement.** `_carrier_next(goal, cache, node, fen, want_dn)`: among `cache.children[node]` in edge-id order, consider only children with status `UNKNOWN` and non-None cached numbers; value = dn if `want_dn` else pn. dn wanted: at an OR node the larger dn carries (sum dominated by its largest term), at an AND node the smaller (min). pn wanted: OR → smaller (min), AND → larger (sum). Replace the current best only on strict improvement, so ties keep the first in edge order. Returns the child id or None.
- **Inputs / outputs.** Cache only.
- **Code.** `atomicdb/proof.py:_carrier_next:578`.
- **Tests.** covered by PN-3.1 tests. **Gaps:** no test pins the tie rule.
- **Determinism hazards.** Edge-id order for ties.
- **History.** `7d7e3db`.
- **Ordering.** Inside PN-3.1.

### PN-3.3 Carrier cache
- **Statement.** One `_CarrierCache` per `_refresh_campaign` call: `children[node] = ((child_id, status, fen), ...)` in edge-id order, `numbers[node] = (pn, dn) or None`. `_resolve_carrier_steps` loads, for cursors not in `children`, their edges (ordered by edge id) and, for children not yet in `numbers`, their `ProofNode` numbers of this campaign (`setdefault`, missing → None). At each level the refresh seeds `numbers` with every row of `existing` (`setdefault`, so values written earlier in the call win), and after computing each node calls `wrote(key, pn, dn)`.
- **Inputs / outputs.** Reads `Edge`, `ProofNode`; in-memory only.
- **Code.** `atomicdb/proof.py:_CarrierCache:533`, `:_resolve_carrier_steps:553`; seeding at `proof.py:778-779`, `wrote` at `proof.py:792`.
- **Tests.** `test_proof_after_commit.py::SameProofNumbersTests::test_a_repetition_cycle`; `test_propagation_throughput.py::HandOverTests::test_a_repetition_cycle`.
- **Determinism hazards.** Edge and child status are read once per call (a concurrent closure is not seen). `wrote` runs before the skip-locked filter (PN-4.5).
- **History.** `7d7e3db`; cold-cache cost discussed in PR #240.
- **Ordering.** Inside PN-3.1 and PN-4.3.

### PN-3.4 Echo edge priced as a leaf with pn = PROOF_MAX_LEAF
- **Statement.** In PN-4.1 an edge in `cycling` contributes, instead of the child's stored numbers, the child's static leaf numbers from the edge view (`leaf_numbers(child_fen, child_status, child_eval, goal, annoyance=shallow)`), and then `pn` is overwritten with `PROOF_MAX_LEAF` (2^20); `dn` keeps the leaf value. Not `(INF, 0)`: a stored refutation would assert something about the node rather than about one path, and made the detector oscillate.
- **Inputs / outputs.** Pure inside PN-4.1.
- **Code.** `atomicdb/proof.py:compute_numbers:700-723`.
- **Tests.** `test_proof_cycles.py::CycleRuleTests::test_cycling_edge_contributes_a_leaf_estimate_not_the_loop_number`; `::test_the_dn_ratchet_is_dead`; `::test_the_rule_is_idempotent_and_never_oscillates`.
- **Determinism hazards.** none.
- **History.** `98629c0` (6-Aug, "la repeticion se COBRA, no se declara probada").
- **Ordering.** Requires PN-3.1 of the same level.

### PN-4.1 compute_numbers (one node)
- **Statement.** `compute_numbers(campaign, position, children, child_nodes, previous, cycling)` → `(pn, dn, expanded_in_proof, selected_child)`:
  1. closed position → `(*terminal_numbers, False, None)`;
  2. no edges → frontier leaf: `leaf_numbers(position.fen, status, eval_cp, goal, annoyance=gate_annoyance(position), key=position.key)`, `(pn, dn, False, None)`;
  3. else for every edge in edge-id order: if the child has a row in `child_nodes` and the edge is not cycling → `(row.pn, row.dn)` **even if the child is closed** (the stored row is trusted); otherwise leaf numbers from the edge view (closed child without a row → exact PN-1.4 numbers) and, if cycling, pn := 2^20 (PN-3.4);
  4. `(pn, dn) = internal_numbers(...)`;
  5. OR node: `index = argmin_i (pn_i, i)`; `held = index of previous` if `previous` is one of the moves else None; `index = sticky_index(index, held, numbers)` (PN-5.1). AND node: `index = argmin_i (dn_i, i)` (not sticky);
  6. return `(pn, dn, True, moves[index])`. The selected child may be a closed child.
- **Inputs / outputs.** Position `key, fen, status, eval_cp` (+ `last_analysis, mate_in` with the gate); child tuples; child rows.
- **Code.** `atomicdb/proof.py:compute_numbers:660`.
- **Tests.** `test_proof_manager.py::IncrementalMaintenanceTests::test_a_proved_child_drives_the_parent_to_zero_pn`; `test_proof_manager.py::SelectedChildHysteresisTests::*`; `test_proof_cycles.py::CycleRuleTests::test_cycling_edge_contributes_a_leaf_estimate_not_the_loop_number`; `test_proof_kpi.py::HeadlineNumbersTests::test_a_proved_root_reads_zero_and_infinity`.
- **Determinism hazards.** Ties by edge index (edge id order). The value read for a child row may be the in-memory value of a row already recomputed earlier in the same level (see PN-4.3, "in-place mutation").
- **History.** Hysteresis `b72a43e` (28-Jul); cycles `7d7e3db`/`98629c0` (6-Aug).
- **Ordering.** Called by PN-4.3 after PN-3.1.

### PN-4.2 refresh_proof_numbers (entry point)
- **Statement.** `refresh_proof_numbers(seed_keys, campaigns=None, max_plies=64)`: seeds = `seed_keys` deduplicated in first-seen order, falsy keys dropped; empty → 0. `campaigns` None → `active_campaigns()` (id order); none → 0. Runs `_refresh_campaign(campaign, seeds, max_plies)` for each campaign in order and returns the total rows written (created + updated). Never raises on budget exhaustion (PN-4.4); stale numbers "order worse, never lie".
- **Inputs / outputs.** See PN-4.3.
- **Code.** `atomicdb/proof.py:refresh_proof_numbers:736`.
- **Tests.** `test_proof_manager.py::IncrementalMaintenanceTests::test_an_idempotent_refresh_stops_at_the_first_level`; `::test_a_transposition_with_two_parents_updates_both`.
- **Determinism hazards.** Campaign order = id order.
- **History.** Original.
- **Ordering.** Called by PN-8.x.

### PN-4.3 _refresh_campaign level loop
- **Statement.**
  ```
  frontier = seeds; visits = {}; processed = plies = written = 0; guard = None
  carriers = new _CarrierCache
  while frontier and plies < max_plies:            # max_plies default 64
      plies += 1
      positions = Position rows with key in frontier (fields PN-2.1)   # NO ORDER BY
      if no positions: break
      keys = their keys
      children = edges of keys, by edge id: (move, child, child.status, child.eval_cp, child.fen)
      existing = ProofNode rows of this campaign for keys ∪ all child ids
      seed carriers.numbers from existing (setdefault)
      cycling = _cycling_edges(campaign, keys, children, existing, carriers)
      create, update, propagate = [], [], []
      for row in positions:                          # DB order
          processed += 1
          node = existing.get(row.key)
          (pn, dn, exp, sel) = compute_numbers(campaign, row, children[row.key] or (),
                                  existing, previous = node.selected_child if node else None,
                                  cycling)
          carriers.wrote(row.key, pn, dn)
          if node is None: create new ProofNode(pn, dn, exp, sel); propagate += key; continue
          if all four fields equal: continue
          set node.pn, node.dn, node.expanded_in_proof, node.selected_child IN PLACE
          update += node; propagate += key
      bulk_create(create, ignore_conflicts=True); written += len(create)
      skip-locked filter on update (PN-4.5), removing skipped keys from propagate
      bulk_update(update, [pn, dn, expanded_in_proof, selected_child], batch 500); written += len(update)
      if not propagate: break
      if processed >= PROOF_MAX_NODES (50_000): guard = 'node-budget'; break
      frontier = []
      for key in set(parent ids of edges whose child is in propagate):
          if visits.get(key, 0) < PROOF_MAX_REVISITS (2): visits[key] += 1; frontier += key
  else:  (the while condition became false without break)
      if frontier: guard = 'ply-guard'
  if guard: PROOF_GUARD event (PN-4.4)
  return written
  ```
  **In-place mutation (must be reproduced or decided):** `existing` holds model instances; an updated row is mutated before later rows of the same level are computed, so a later parent in the same level reads the NEW numbers of a child updated earlier in that level (Gauss-Seidel), while a child row CREATED in this level is not in `existing` and is still priced by its leaf estimate (Jacobi). Which case applies depends on the database's row order of `positions`. Seeds are not counted in `visits`; any key can be re-entered at most twice after the seed level. A created row counts as changed. Frontier keys with no Position row are ignored.
- **Inputs / outputs.** Reads `Position(key, fen, status, eval_cp[, last_analysis, mate_in])`, `Edge`, `ProofNode`; writes `ProofNode.pn/dn/expanded_in_proof/selected_child`; may write DBEvent `PROOF_GUARD`, `SOLVE_GATE_DISAGREE`.
- **Code.** `atomicdb/proof.py:_refresh_campaign:758`, `:_node_rows:440`, `:_children_by_parent:445`.
- **Tests.** `test_proof_manager.py::IncrementalMaintenanceTests::test_numbers_are_written_for_the_touched_cone`; `::test_an_idempotent_refresh_stops_at_the_first_level`; `::test_a_transposition_with_two_parents_updates_both`; `::test_one_level_costs_a_fixed_number_of_statements`; `::test_guard_emits_an_event_and_stops`; `test_proof_manager.py::SelectedChildHysteresisTests::test_the_stored_primary_survives_a_refresh`; `test_proof_cycles.py::CycleRuleTests::test_poisoned_shuttle_drains_to_a_fixpoint`. **Gaps:** nothing pins the in-place (Gauss-Seidel) effect or its dependence on row order; nothing pins `bulk_create(ignore_conflicts)` dropping a concurrently created row while counting it as written.
- **Determinism hazards.** Unordered `Position` query (row order changes results within a level, see above); `set(...)` of parent keys (order irrelevant here: visits are per key); concurrent writers (READ COMMITTED reads per statement); `ignore_conflicts` silently loses this call's numbers if another process created the row first; skip-locked (PN-4.5). Called inside other transactions (cascade hook) or outside (after commit), see PN-8.
- **History.** Level-wise maintenance from the start; skip-locked PR #186 (7-Nov); after-commit PR #240 (13-Nov); pipeline PR #322 (26-Nov).
- **Ordering.** After the status cascade (the proof reads status; status never reads pn/dn).

### PN-4.4 Refresh caps and PROOF_GUARD
- **Statement.** Caps per campaign call: `PROOF_MAX_PLIES = 64` levels (parameter `max_plies`), `PROOF_MAX_REVISITS = 2` re-entries per key, `PROOF_MAX_NODES = 50_000` processed rows (checked only after a level that changed something). On `node-budget` (break) or `ply-guard` (loop ended with a non-empty frontier) the call writes `DBEvent(kind='PROOF_GUARD', payload={'campaign': name, 'reason', 'processed', 'plies', 'seed_count': len(seeds)})` and stops; what was not refreshed stays stale.
- **Inputs / outputs.** DBEvent `PROOF_GUARD`.
- **Code.** `atomicdb/proof.py:122-124`, `:_refresh_campaign:844-861`.
- **Tests.** `test_proof_manager.py::IncrementalMaintenanceTests::test_guard_emits_an_event_and_stops`. **Gaps:** `ply-guard` path has no direct test.
- **Determinism hazards.** Truncation leaves results dependent on seed batching (which seeds share a call), contrary to batch-size independence (plan §2.2.6).
- **History.** Original.
- **Ordering.** End of PN-4.3.

### PN-4.5 Skip rows locked by another transaction
- **Statement.** When the backend supports `SELECT ... FOR UPDATE SKIP LOCKED` and the refresh runs inside an atomic block, the rows to update are re-selected `FOR UPDATE SKIP LOCKED`; rows not obtained are dropped from `update` and their keys from `propagate` (their parents are not climbed from them). Outside a transaction (after-commit, pipeline) nothing is skipped. SQLite keeps every row.
- **Inputs / outputs.** Row locks on `ProofNode`.
- **Code.** `atomicdb/proof.py:_refresh_campaign:812-836`.
- **Tests.** `test_proof_skip_locked.py::SkipLockedTests::test_a_row_held_elsewhere_is_skipped_not_waited_for`; `test_proof_skip_locked.py::OutsideTransactionTests::test_numbers_are_still_written`.
- **Determinism hazards.** Depends on concurrent lock state; the in-memory row was already mutated and cached (PN-3.3, PN-4.3) even if not written.
- **History.** PR #186 (7-Nov, 52 deadlocks in one hour).
- **Ordering.** Between create and update in PN-4.3. Disappears with a single writer.

### PN-4.6 Served proof numbers (root, headline, explorer rows, format)
- **Statement.** `root_numbers(campaign)`: the root's ProofNode `(pn, dn)` if a row exists, else `leaf_numbers(root.fen, root.status, root.eval_cp, goal, annoyance=gate_annoyance(root))` (no key, never logs). `headline_numbers()`: `root_numbers` of the first active campaign by id, else `(None, None)`. Explorer rows (`views._proof_numbers_for`): `{key: (pn, dn)}` of the first active campaign, **absent** (not `(1,1)`) for positions without a row. Campaign page (`views` ~8939): per root, `proven = (pn == 0)`, `refuted = (dn == 0)`. `format_number(v)`: None → `'-'`, `>= INF` → `'∞'`, else thousands with `,`. `ProgressSnapshot` stores root pn/dn (saturated at INF) and frontier stats hourly (`capture_atomicdb_progress`).
- **Inputs / outputs.** Read-only (snapshot command writes `ProgressSnapshot`).
- **Code.** `atomicdb/proof.py:root_numbers:892`, `:headline_numbers:879`, `:format_number:865`; `atomicdb/views.py:_proof_numbers_for:5812`, `views.py:8939-8956`; `management/commands/capture_atomicdb_progress.py:157-161`.
- **Tests.** `test_proof_kpi.py::HeadlineNumbersTests::*`; `test_proof_kpi.py::FormatNumberTests::test_saturation_reads_as_infinity_not_as_a_huge_integer`; `test_campaign_proof_number.py::CampaignProofNumberTests::*` (3); `test_proof_manager.py::ProofStatusCommandTests::*`.
- **Determinism hazards.** Served "absent" depends on row presence (refresh history).
- **History.** —
- **Ordering.** Read side.

### PN-5.1 Sticky primary (hysteresis) at OR nodes
- **Statement.** `sticky_index(best, held, numbers)`: if `held` is None or equals `best` → `best`; if `numbers[best].pn * 3 < numbers[held].pn` (strict; `SELECTED_CHILD_HYSTERESIS = 3`) → `best`; else `held`. A held child with pn 0 can never be dethroned; a held child with pn = INF loses to any finite child. AND nodes are not sticky. A `previous` move that is no longer an edge is ignored.
- **Inputs / outputs.** `ProofNode.selected_child` (previous) → new `selected_child`.
- **Code.** `atomicdb/proof.py:sticky_index:457`, used at `proof.py:727-730`.
- **Tests.** `test_proof_manager.py::SelectedChildHysteresisTests::*` (all 12: `test_a_flicker_does_not_move_the_primary`, `test_crossing_the_threshold_does_move_it`, `test_the_boundary_itself_is_not_a_change`, `test_a_proved_primary_is_never_dethroned`, `test_a_refuted_primary_hands_the_seat_over`, `test_an_and_node_is_not_sticky`, `test_a_primary_that_is_no_longer_a_child_is_not_honoured`, `test_with_no_primary_yet_the_best_pn_takes_it`, `test_the_declared_constant_is_the_approved_one`, `test_the_stored_primary_survives_a_refresh`, `test_the_backup_bucket_still_gets_an_alternative`, `test_the_descent_follows_the_sticky_primary`).
- **Determinism hazards.** History-dependent by design: the stored `selected_child` is an input (path dependence; a recompute from scratch can choose differently). Relevant to D5.
- **History.** `b72a43e` (28-Jul, "Stop the descent dancing").
- **Ordering.** Inside PN-4.1; read by PN-6.5 and PN-9.3.

### PN-6.1 Normalised repertoire policy
- **Statement.** `normalized_policy(campaign)`: for name in (`primary`, `backup`, `explore`): `w = max(0.0, float(policy.get(name, default)))`, on `TypeError/ValueError` use the default (0.8, 0.15, 0.05). If the sum `<= 0` use the defaults. Return each weight divided by the float sum.
- **Inputs / outputs.** `ProofCampaign.repertoire_policy`.
- **Code.** `atomicdb/proof.py:normalized_policy:419`.
- **Tests.** `test_proof_manager.py::CampaignSeedTests::test_malformed_policy_is_normalised_not_trusted`; `::test_policy_defaults_to_eighty_fifteen_five`.
- **Determinism hazards.** Float division; NaN/inf strings in JSON behave as Python `float()` does (porter: reproduce `max(0.0, nan) == 0.0`).
- **History.** Original.
- **Ordering.** Before PN-6.2.

### PN-6.2 Deterministic draws (_point, _bucket)
- **Statement.** `_point(counter, salt='') = int.from_bytes(sha256(f'{salt}{int(counter)}'.encode()).digest()[:8], 'big') / 2^64` as float64 (the int→double conversion rounds to nearest). `_bucket(counter, policy)`: `p = _point(counter)`; `p < primary` → `primary`; `p < primary + backup` → `backup`; else `explore`. The empty salt is the historic repertoire draw; `'campaign:'` is the campaign draw (PN-6.3).
- **Inputs / outputs.** Pure.
- **Code.** `atomicdb/proof.py:_point:918`, `:_bucket:932`.
- **Tests.** `test_proof_manager.py::DescentTests::test_allocation_is_deterministic_not_random`; `::test_soft_repertoire_spends_roughly_eighty_fifteen_five`; `test_solver_allocation.py::DeterministicDrawTests::*` (3).
- **Determinism hazards.** none (pure), provided the counter is reproducible (PN-6.6).
- **History.** Salt domains `b57bc07` (5-Aug).
- **Ordering.** Used by PN-6.5 and PN-9.11.

### PN-6.3 Campaign roots and the campaign start draw
- **Statement.** `campaign_roots()`: `[]` if `settings.ATOMICDB_CAMPAIGN_DESCENT` is false (default True; env values `0/false/no` turn it off). Else every community `Campaign` with `state == ACTIVE` and `root.status == 'UNKNOWN'`, by id, with weight `ln(1 + max(1, int(votes or 0)))` (`CAMPAIGN_ACTIVATION_VOTES = 1`). `campaign_start(counter, roots=None)`: total = sum of weights; no roots or total `<= 0` → None (global root). `p = _point(counter, 'campaign:')`; walk roots accumulating `edge += 0.35 * w / total` (`CAMPAIGN_DESCENT_SHARE`); return the first root with `p < edge`; else None. So at most 35% of descents start at a campaign root.
- **Inputs / outputs.** Reads `Campaign(state, votes, root.status)`.
- **Code.** `atomicdb/proof.py:campaign_descent_enabled:974`, `:campaign_roots:979`, `:campaign_start:997`.
- **Tests.** `test_solver_allocation.py::CampaignShareTests::*` (8); `test_solver_allocation.py::AllocationSwitchTests::*` (3); `test_solver_allocation.py::CampaignMintingTests::*` (3).
- **Determinism hazards.** Float accumulation in id order; `math.log1p`.
- **History.** `b57bc07` (5-Aug).
- **Ordering.** Before PN-6.5 / PN-9.11 in the minting loop (PN-6.6).

### PN-6.4 Ranking children for the proof descent
- **Statement.** `_ranked_children(campaign, position, children, child_nodes)`: for each edge (index = position in edge-id order) whose child status is `UNKNOWN`: numbers = child row `(pn, dn)` or edge-view leaf numbers; key = `(pn, index)` at an OR node, `(dn, index)` at an AND node; sort ascending. `_primary_index(ranked, node)`: index in `ranked` of the node's `selected_child` if present among the (filtered) candidates, else 0.
- **Inputs / outputs.** Reads ProofNode rows.
- **Code.** `atomicdb/proof.py:_ranked_children:1098`, `:_primary_index:1116`.
- **Tests.** `test_proof_manager.py::DescentTests::test_or_descent_follows_the_smallest_pn`; `test_solve_gate.py::GateOnTests::test_the_descent_ranks_the_tedious_child_last`.
- **Determinism hazards.** Edge-id order for ties.
- **History.** Original.
- **Ordering.** Inside PN-6.5.

### PN-6.5 descend (df-pn descent)
- **Statement.** `descend(campaign, counter=0, max_plies=96, avoid=(), start=None)` → `(Position or None, plies)`:
  ```
  policy = normalized_policy(campaign); node = start or campaign.root
  reserved = set(avoid); visited = {node.key}; plies = 0
  while plies < max_plies (DESCENT_MAX_PLIES = 96):
      if node.status != UNKNOWN: return None, plies
      children = edges of node (edge-id order)
      if none: return (None if node in reserved else node), plies
      rows = ProofNode rows of node + children
      ranked = _ranked_children(...) minus visited minus reserved
      if not ranked: return (None if node in reserved else node), plies
      bucket = _bucket(counter + plies, policy)        # re-drawn every ply
      primary = _primary_index(ranked, rows[node])
      primary bucket: ranked[primary]
      backup bucket:  first of ranked without the primary index (ranked[primary] if none)
      explore bucket: ranked[-1]                       # the WORST-ranked child
      visited += chosen; node = Position(chosen); plies += 1
  return node, plies                                   # cap reached: current node
  ```
  An interior node whose children are all visited/reserved/closed is itself returned (unless reserved).
- **Inputs / outputs.** Reads Position, Edge, ProofNode. No writes.
- **Code.** `atomicdb/proof.py:descend:1132`.
- **Tests.** `test_proof_manager.py::DescentTests::*` (7, incl. `test_a_reserved_node_is_not_handed_out_twice`, `test_descent_reaches_an_unexpanded_frontier_node`, `test_a_closed_root_yields_nothing`, `test_descent_never_loops_through_a_transposition`); `test_selector_leak.py::ProofDescentIsImmuneTests::test_a_closed_child_is_never_descended_into`; `test_solver_allocation.py::CampaignShareTests::test_a_descent_that_starts_at_a_campaign_stays_under_it`; `test_value_descent.py::ProductionRootTests::test_the_proof_descent_still_goes_for_the_cheap_line`.
- **Determinism hazards.** Edge-id ties; counter (PN-6.6).
- **History.** Original; `start` from `b57bc07`.
- **Ordering.** Called by `ingest._next_tasks_by_proof` (PN-6.6) when the descent mode is `proof` or the value arm's hourly budget is spent.

### PN-6.6 Interface: the descent counter and the minting loop
- **Statement.** `ingest._next_tasks_by_proof(n)` (queue catalog owns it) drives both descents: `campaigns = active_campaigns()`; `by_value = descent_mode()=='value' and _arm_budget_room('descend') > 0`; `roots = campaign_roots()`; `base = _task_counter()` = highest `AnalysisTask.id` (0 if none); for attempt in `0 .. 4*max(1,n)-1` while fewer than n tasks: `campaign = campaigns[attempt % len]`, `counter = base + attempt`, `start = campaign_start(counter, roots)`, then `descend_value` or `descend` with `avoid = seen`. The chosen position goes through `_still_reachable` (else priority DEAD), `_short_mate_clamp` (EXP-4.2), `budget_for` or `value_budget` (PN-9.12), `proved_or_clamp` (PN-7.1: `budget = min(budget, 250_000)`, clamp = `(250_000, 1)`, overriding the short-mate clamp), `server_picked_budget`, and `AnalysisTask.get_or_create(position, generation=visits)` with arm `campaign` if started at a campaign root, `descend` if by value, else `''`.
- **Inputs / outputs.** Creates `AnalysisTask` rows.
- **Code.** `atomicdb/ingest.py:_task_counter:7035`, `atomicdb/ingest.py:_next_tasks_by_proof:7046`.
- **Tests.** `test_proof_manager.py::SelectorFlagTests::test_pn_selector_produces_tasks_from_the_descent`; `test_bug_hunt_findings.py::ProofSelectorAttemptCapTests::test_the_descent_loop_stops_at_its_attempt_cap`; `test_solver_allocation.py::OrClampMintingTests::*`; `test_arm_rate_budget.py::DescendRateTests::*`.
- **Determinism hazards.** The counter is the max auto-increment id of `AnalysisTask` (depends on sequence allocation, deleted rows, concurrent inserts); `_arm_budget_room` reads an hourly wall-clock window.
- **History.** `b57bc07`, `b3087dc` (value descent).
- **Ordering.** Queue catalog.

### PN-7.1 OR clamp (proved_or_clamp)
- **Statement.** `proved_or_clamp(position_key, campaigns=None)` → `(250_000, 1)` or None. None if `settings.ATOMICDB_OR_CLAMP` is false (default True) or there is no active campaign. `parent_ids` = distinct parents of the position, at most 33 fetched; none or more than 32 (`OR_CLAMP_MAX_PARENTS`) → None. For EVERY active campaign: `proved` = parents having at least one child with `status == campaign.goal`; if `proved` is empty or not `_settled_for(goal, parents, proved)` → None. `_settled_for`: every parent is either closed, or an OR node (for this goal) with a proved child; any other parent (an open AND node, or an open OR node without a proved child) keeps the position alive. Otherwise return `(OR_CLAMP_NODES = 250_000, OR_CLAMP_MULTIPV = 1)`.
- **Inputs / outputs.** Reads Edge and parent `Position(key, fen, status)`.
- **Code.** `atomicdb/proof.py:_settled_for:1049`, `:proved_or_clamp:1068`.
- **Tests.** `test_solver_allocation.py::OrClampTests::*` (8); `test_solver_allocation.py::OrClampMintingTests::*`; `test_idle_pool_budget.py::ProofSelectorTests::test_the_proved_or_clamp_still_buys_its_cheap_look`; `test_value_descent.py::ClampsStillWinTests::test_the_proved_or_clamp_still_cheapens_a_settled_sibling`; `test_idle_pool_budget.py::RuleTests::test_a_clamp_that_is_the_purchase_keeps_its_budget`.
- **Determinism hazards.** `.distinct()[:33]` without ORDER BY (harmless: more than 32 returns None anyway).
- **History.** `b57bc07` (5-Aug).
- **Ordering.** In PN-6.6 after the budget is computed and before `server_picked_budget`.

### PN-8.1 Proof refresh hook at the end of every cascade
- **Statement.** At the end of every `ingest.backup_cascade(seed_keys, report)` call (owned by the cascade catalog), with `seeds = list(seed_keys) + changed_keys` (the keys whose status/closure the cascade rewrote, in rewrite order): if a refresh sink is active in the current context (PN-8.2) the seed list is appended to it; otherwise `proof.refresh_proof_numbers(seeds)` runs immediately, inside the caller's transaction. The proof is refreshed after the exact fixpoint: proof numbers read status, status never reads proof numbers.
- **Inputs / outputs.** See PN-4.3.
- **Code.** `atomicdb/ingest.py:backup_cascade:1889-1900`.
- **Tests.** `test_proof_manager.py::IncrementalMaintenanceTests::test_backup_cascade_maintains_the_numbers`.
- **Determinism hazards.** Inline mode runs inside a transaction that holds locks (hence PN-4.5).
- **History.** Original hook; sink from PR #240.
- **Ordering.** Interface: after the cascade's own writes. Every caller of `backup_cascade` (landing climb, TB apply, solve result, PV walk, commands) therefore refreshes proof numbers.

### PN-8.2 Proof refresh after commit (sink)
- **Statement.** `proof_refresh_after_commit()` is a context manager setting a ContextVar sink (a list) for its duration; cascades inside append their seed lists. `refresh_collected_proof_numbers(calls)` replays `refresh_proof_numbers(seeds)` for each list in call order. The propagation drainer uses it when `proof_after_commit_enabled()` = `settings.ATOMICDB_PROOF_AFTER_COMMIT` (default True) AND the drainer is not inside an outer atomic block: it registers `transaction.on_commit(refresh_proof, robust=True)` before any recheck, so the refresh is the first thing after the batch's commit; failures (`DatabaseError`) are logged, never re-raised. Justification in code: between the hook and the commit the batch writes none of `key, fen, status, eval_cp` (only backed columns and tasks), so each deferred call reads what it would have read inside.
- **Inputs / outputs.** Same writes as PN-4.3, outside the batch transaction.
- **Code.** `atomicdb/ingest.py:_proof_refresh_sink:1924`, `:proof_refresh_after_commit:1929`, `:refresh_collected_proof_numbers:1945`; `atomicdb/propagation_queue.py:proof_after_commit_enabled:262`, `:_refresh_proof_after_commit:275`, `:drain_batch:398-439`.
- **Tests.** `test_proof_after_commit.py::WhenTheRefreshRunsTests::*` (3); `test_proof_after_commit.py::SameProofNumbersTests::*` (4: start-position line, transposition, closure reaching another parent, repetition cycle).
- **Determinism hazards.** After commit, other transactions may commit in between; the refresh reads newer status than the batch did. Not skipping locked rows outside a transaction (PN-4.5) means results can differ from the inline mode.
- **History.** PR #240 (13-Nov, refresh was 65% of drainer time).
- **Ordering.** After the batch commit, before rechecks' own cascades (which refresh inline, since the sink is gone).

### PN-8.3 ProofPipeline (refresh on its own thread)
- **Statement.** With `settings.ATOMICDB_PROOF_PIPELINE` (default True) the drainer service opens `proof_pipeline(depth=1)`: one daemon thread with a FIFO queue of max size `max(1, depth)`. `_hand_over_proof(calls)` submits the batch's collected calls (blocking while the queue is full: backpressure) or runs them inline if no pipeline is open. The thread runs refreshes strictly in submit (commit) order; an exception is logged and the thread continues; `close()` enqueues a sentinel and joins (pending refreshes run before exit). A process killed loses queued refreshes (numbers stay stale until the next refresh reaches them). Nested `proof_pipeline` blocks reuse the open one.
- **Inputs / outputs.** Same as PN-4.3, on a separate DB connection.
- **Code.** `atomicdb/propagation_queue.py:PIPELINE_DEPTH:313`, `:ProofPipeline:316`, `:proof_pipeline:361`, `:_hand_over_proof:380`.
- **Tests.** `test_propagation_throughput.py::PipelineTests::*` (5); `test_propagation_throughput.py::HandOverTests::*` (2).
- **Determinism hazards.** Threading: the refresh of batch k runs concurrently with the climb of batch k+1; what it reads depends on timing. Process death loses work.
- **History.** PR #322 (26-Nov).
- **Ordering.** After PN-8.2's commit hook.

### PN-8.4 Refresh of the landed position after apply_job / drain
- **Statement.** `ingest_queue.apply_job` (inline climb, deferral off): after its transaction commits, if the climb was not deferred it calls `refresh_proof_numbers([job.position_id])` even if nothing cascaded (rejected TB, closed position), so every submit refreshes its own position's leaf (its `eval_cp` changed). With deferral, `propagation_queue._after_commit` refreshes the landed keys only when the batch did NOT already refresh after commit (`proof_refreshed=False`); errors are logged.
- **Inputs / outputs.** As PN-4.3.
- **Code.** `atomicdb/ingest_queue.py:apply_job:355-360`; `atomicdb/propagation_queue.py:_after_commit:481-496`.
- **Tests.** `test_proof_after_commit.py::SameProofNumbersTests::test_a_line_from_the_start_position`. **Gaps:** no direct test for the "nothing cascaded" refresh in `apply_job`.
- **Determinism hazards.** Runs outside a transaction, after commit.
- **History.** Original (apply_job), PR #240.
- **Ordering.** After commit of the landing.

### PN-8.5 Other refresh callers
- **Statement.** `recascade_proof` (PN-10.5) and `rekey_en_passant` (re-keying migration) call `refresh_proof_numbers` directly. No other code writes ProofNode.
- **Inputs / outputs.** As PN-4.3.
- **Code.** `management/commands/recascade_proof.py:81`; `management/commands/rekey_en_passant.py:124`.
- **Tests.** `test_proof_cycles.py::RecascadeProofCommandTests::*`.
- **Determinism hazards.** As PN-4.3.
- **History.** —
- **Ordering.** Manual.

### PN-9.1 Descent mode switch
- **Statement.** `descent_mode()` = `'value'` iff `str(settings.ATOMICDB_DESCENT).lower() == 'value'` exactly; anything else (default `'proof'`, typos) → `'proof'`. The value walker is only used when the mode is `value` AND the hourly room of arm `descend` is positive (PN-6.6); otherwise the df-pn descent runs.
- **Inputs / outputs.** Setting/env `ATOMICDB_DESCENT`.
- **Code.** `atomicdb/proof.py:descent_mode:1328`.
- **Tests.** `test_value_descent.py::ValueDescentSwitchTests::*` (3); `test_arm_rate_budget.py::DescendRateTests::test_a_spent_hour_falls_back_to_the_proof_descent`.
- **Determinism hazards.** Env switch; hourly arm budget (wall clock).
- **History.** `b3087dc` (12-Aug).
- **Ordering.** Before PN-6.5/PN-9.11.

### PN-9.2 Value ranking of children
- **Statement.** `_value_children(parent)`: edges by edge id with `move_uci, child_id, child status, fen, eval_cp, backed_eval, backed_move, backed_nodes, nodes_invested, backed_plies` (index = edge order). `_value_ranked(goal, fen, children, child_nodes)`: keep children with status `UNKNOWN`; `child.value = ingest.known_eval_of(status, backed_eval, eval_cp)` (status eval for closed, else backed if not None, else eval_cp); `pn` = child row pn or edge-view leaf pn; order key `(1, 0, pn, index)` when value is None, else `(0, -value if White to move else value, pn, index)`; ascending. Valued children first, best for the mover first, then smaller pn, then edge order.
- **Inputs / outputs.** Reads Edge, child Position columns, ProofNode.
- **Code.** `atomicdb/proof.py:_value_children:1403`, `:_value_ranked:1410`; `atomicdb/ingest.py:known_eval_of:5135`.
- **Tests.** `test_value_descent.py::ProductionRootTests::test_the_value_descent_goes_for_the_backed_move`; `test_value_descent.py::ReservationTests::test_the_explore_bucket_never_buys_the_worst_child`.
- **Determinism hazards.** Edge-id ties.
- **History.** `b3087dc`.
- **Ordering.** Inside PN-9.4.

### PN-9.3 Spine step and the fallback primary
- **Statement.** `_spine_step(backed_move, ranked, node_row)`: None if `backed_move` is falsy or `ranked` is empty (the node is the leaf whose own eval holds the value). If a ranked child has `move_uci == backed_move` → that child. Otherwise (the spine points at a closed or already-visited child) `_value_primary`: the ranked child whose move equals the node's `ProofNode.selected_child`, else `ranked[0]`.
- **Inputs / outputs.** `Position.backed_move`, `ProofNode.selected_child`.
- **Code.** `atomicdb/proof.py:_spine_step:1465`, `:_value_primary:1451`.
- **Tests.** `test_value_descent.py::SpineWalkTests::test_the_walk_ends_on_the_leaf_without_backing`.
- **Determinism hazards.** none beyond inputs.
- **History.** `b3087dc`.
- **Ordering.** Inside PN-9.4.

### PN-9.4 Spine walk
- **Statement.** `_walk_spine(campaign, key, fen, backed_move, nodes, visited, max_plies, backed_plies=0)` → `(path, cycled_child)`: loop: read children and rows, rank (PN-9.2), append a step `(key, fen, nodes, children, ranked, backed_plies)`; if `plies >= max_plies` → `(path, None)`; `chosen = _spine_step(...)`; None → `(path, None)`; `chosen.key in visited` → `(path, chosen)` (the spine bites its tail); else `step.primary = chosen`, add to `visited` (shared, mutated), move to `chosen` with its `backed_move`, `nodes_invested`, `backed_plies`; `plies += 1`.
- **Inputs / outputs.** Reads only.
- **Code.** `atomicdb/proof.py:_walk_spine:1486`.
- **Tests.** `test_value_descent.py::SpineWalkTests::*`; `test_value_descent.py::CyclingSpineTests::test_the_pre_cycle_node_buys_a_deeper_pass`; `test_value_descent.py::CampaignRootTests::*`.
- **Determinism hazards.** none beyond inputs.
- **History.** `b3087dc`.
- **Ordering.** First step of PN-9.11 and of each detour (PN-9.7).

### PN-9.5 Tip plan (spine, bottleneck, rung)
- **Statement.** `_tip_plan(path, arm, plies)` → `(key, arm, floor_nodes, plies, line)` with `tip = path[-1]` and `line = plies + tip.backed_plies`: if `tip.nodes < VALUE_SPINE_NODES` (= 128,000,000, read from ingest) → `(tip, arm, None, plies, line)`. Else, scanning the path from the tip upwards, the first step with any child that `ingest.is_unjudged` (status UNKNOWN, eval_cp None, backed_eval None) → `(step, 'bottleneck', None, plies, line)` (plies and line stay the tip's). Else `(tip, 'rung', tip.nodes, plies, line)`.
- **Inputs / outputs.** Pure over the path.
- **Code.** `atomicdb/proof.py:_tip_plan:1524`, `:_has_unjudged:1518`, `:_saturated_nodes:1341`; `atomicdb/ingest.py:is_unjudged:9619`.
- **Tests.** `test_value_descent.py::SaturatedTipTests::*` (2); `test_value_descent.py::CampaignRootTests::test_a_saturated_tip_climbs_inside_the_campaign_subtree`; `::test_a_campaign_spine_without_bottlenecks_deepens_its_own_tip`.
- **Determinism hazards.** none.
- **History.** `b3087dc`.
- **Ordering.** Inside PN-9.9.

### PN-9.6 Competitive alternatives
- **Statement.** `_competitive(step)`: `primary = step.primary or ranked[0]`; none or `primary.value is None` → []. Rivals, in ranked order, are the other ranked children with a value and `loss < 150` (`VALUE_ALTERNATIVE_MARGIN_CP`), where `loss = primary.value - child.value` if White to move else `child.value - primary.value` (a better child has negative loss and qualifies).
- **Inputs / outputs.** Pure.
- **Code.** `atomicdb/proof.py:_competitive:1546`.
- **Tests.** `test_value_descent.py::PoolVarietyTests::test_a_far_alternative_is_not_variety_it_is_dispersion`; `::test_a_batch_fills_with_distinct_positions_around_the_spine`.
- **Determinism hazards.** none.
- **History.** `b3087dc`.
- **Ordering.** Inside PN-9.7.

### PN-9.7 Value detours (backtrack)
- **Statement.** `_value_detours(campaign, path, max_plies)` is a lazy generator: `tried = 0`; for `depth` from the deepest step to 0: `prefix` = keys of `path[:depth+1]`; for each competitive child of `path[depth]`: stop the generator if `tried >= 64` (`VALUE_BACKTRACK_MAX`); skip if the child is in `prefix`; `tried += 1`; walk its spine with `visited = prefix ∪ {child}` and `max_plies' = max(0, max_plies - depth - 1)`; yield `(path[:depth+1] + branch, depth + len(branch))`.
- **Inputs / outputs.** Reads only.
- **Code.** `atomicdb/proof.py:_value_detours:1572`.
- **Tests.** `test_value_descent.py::ReservationTests::test_a_live_task_on_the_tip_diverts_to_the_alternative`; `::test_the_batch_reservation_moves_the_walk_too`; `::test_a_walk_with_nothing_left_to_offer_comes_back_empty`. **Gaps:** the 64 cap is untested.
- **Determinism hazards.** none beyond inputs.
- **History.** `b3087dc`.
- **Ordering.** Inside PN-9.9.

### PN-9.8 Best unexplored child
- **Statement.** `_best_unexplored(path)`: from the deepest step upwards, the first step having children with `ingest.is_unexplored(child)` (status UNKNOWN, `nodes_invested` 0/None, `backed_nodes` 0/None) AND `eval_cp is not None` (a seeded claim); among them the maximum of `eval_cp` (White to move) or `-eval_cp` (first maximum in edge order wins ties); returns `(child, step_index + 1)`; None if none.
- **Inputs / outputs.** Pure.
- **Code.** `atomicdb/proof.py:_best_unexplored:1605`; `atomicdb/ingest.py:is_unexplored:9571`.
- **Tests.** `test_value_descent.py::ReservationTests::test_the_explore_bucket_buys_the_best_seeded_virgin`; `::test_the_explore_bucket_never_buys_the_worst_child`.
- **Determinism hazards.** Edge order ties.
- **History.** `b3087dc`.
- **Ordering.** Inside PN-9.9.

### PN-9.9 Plan order
- **Statement.** `_value_plans(campaign, path, cycled, bucket, max_plies)` yields, in order:
  1. if `cycled`: `(path[-1], 'cycle', cycled.nodes_invested + 1, len(path)-1, len(path)-1 + path[-1].backed_plies)`;
  2. bucket `backup`: the first detour (if any) as `_tip_plan(branch, 'backup', plies)`; bucket `explore`: the best unexplored child as `(child, 'explore', None, p, p + child.backed_plies)`;
  3. `_tip_plan(path, 'spine', len(path)-1)`;
  4. every remaining detour (the same generator, so the one consumed by `backup` is not repeated) as `_tip_plan(branch, 'spine', plies)`.
- **Inputs / outputs.** Generator.
- **Code.** `atomicdb/proof.py:_value_plans:1635`.
- **Tests.** `test_value_descent.py::ReservationTests::test_the_backup_bucket_spends_on_the_alternative`; `test_value_descent.py::CyclingSpineTests::test_the_pre_cycle_node_buys_a_deeper_pass`.
- **Determinism hazards.** none.
- **History.** `b3087dc`; plan scoring PR #237.
- **Ordering.** Consumed by PN-9.11.

### PN-9.10 Long-line weight
- **Statement.** `long_line_weight(line_plies) = 1.0` if `(line_plies or 0) - 200 <= 0` (`LONG_LINE_PLIES`), else `0.5 ** ((line_plies - 200) / 40)` (`LONG_LINE_HALF_LIFE_PLIES`). Used only to rank plans; never changes a value.
- **Inputs / outputs.** Pure.
- **Code.** `atomicdb/proof.py:long_line_weight:1320`, constants `proof.py:1314-1317`.
- **Tests.** `test_value_descent.py::LongLineWeightTests::*` (3); `test_value_descent.py::LongLinePenaltyTests::*` (6, incl. `test_values_are_not_touched`, `test_the_line_length_counts_what_the_cut_hides`).
- **Determinism hazards.** `pow` with a fractional exponent: libm last-bit differences across platforms could flip exact ties (low risk).
- **History.** PR #237 (12-Nov).
- **Ordering.** Inside PN-9.11.

### PN-9.11 descend_value selection loop
- **Statement.** `descend_value(campaign, counter=0, max_plies=96, avoid=(), start=None)` → `ValueTarget(position, plies, arm, floor_nodes, line_plies)` or None:
  ```
  node = start or campaign.root; if node.status != UNKNOWN: return None
  reserved = set(avoid); bucket = _bucket(counter, normalized_policy(campaign))   # ONE draw per walk
  path, cycled = _walk_spine(campaign, node.key, node.fen, node.backed_move,
                             node.nodes_invested, {node.key}, max_plies, node.backed_plies)
  shared_alt.observe(campaign, path, node.backed_move)   # shadow, off by default, returns nothing used
  best, best_score, usable = None, 0.0, 0
  for (key, arm, floor, plies, line) in _value_plans(...):
      if best and (0.5**usable <= best_score or usable > 8): break     # LONG_LINE_PLAN_DECAY, LONG_LINE_SCAN_PLANS
      if key in reserved or position has a PENDING/LEASED AnalysisTask: continue
      position = Position(key); if missing or status != UNKNOWN: continue
      score = 0.5**usable * long_line_weight(line); usable += 1
      if best is None or score > best_score: best = ValueTarget(position, plies, arm, floor, line); best_score = score
      if best_score >= 1.0: break
  return best
  ```
  A first usable plan that is not long returns at once; a long plan is never dropped if nothing beats it.
- **Inputs / outputs.** Reads Position, Edge, ProofNode, AnalysisTask state. No writes (shadow may write `SHARED_ALT` events when enabled).
- **Code.** `atomicdb/proof.py:descend_value:1677`, `:_has_live_task:1663`; `atomicdb/shared_alt.py:observe`.
- **Tests.** `test_value_descent.py::ReservationTests::*`; `test_value_descent.py::LongLinePenaltyTests::*`; `test_value_descent.py::PoolVarietyTests::*`; `test_shared_alt.py::SameAnswerTests::test_every_walk_selects_the_same_target`; `test_shared_alt.py::SwitchTests::test_an_exception_never_reaches_the_walk`.
- **Determinism hazards.** Reads live queue state (`_has_live_task`); `shared_alt` has wall-clock caps but its output is not used.
- **History.** `b3087dc` (12-Aug); PR #237 (long lines).
- **Ordering.** Called by PN-6.6.

### PN-9.12 Interface: arm prices (value_budget, value cascade)
- **Statement.** `ingest.value_budget(target)` (queue catalog): `rung` → `_next_rung(floor)` (first of 128M/512M/2B strictly above floor, else 2B; floor None → 128M); `cycle` → `_rung_at_least(floor)` (first `BUDGET_LADDER` rung ≥ floor: 8M,32M,128M,512M,2B; else 2B); `backup` → 32M; `explore` → 8M; `spine`/`bottleneck` → 128M; then floored to 128M by `server_picked_budget` unless a clamp applies. `_queue_value_cascade(pos)` after each landing (value mode only, open position): up to `min(16, hourly room, 16 - pending cascade tasks)` AUTO tasks at `server_picked_budget(8M)` on `unjudged_children(pos)` (edge-id order), arm `cascade`, event `VALUE_CASCADE`.
- **Inputs / outputs.** Creates AnalysisTask rows.
- **Code.** `atomicdb/ingest.py:value_budget:6963`, `:_queue_value_cascade:6984`.
- **Tests.** `test_idle_pool_budget.py::ValueWalkerTests::*`; `test_value_descent.py::ValueCascadeTests::*`.
- **Determinism hazards.** Hourly window (wall clock).
- **History.** `b3087dc`.
- **Ordering.** Queue catalog.

### PN-10.1 Proof frontier and frontier_rows
- **Statement.** A node is on a campaign's proof frontier iff it has a ProofNode row in that campaign, its Position is `UNKNOWN`, and both `pn < INF` and `dn < INF`. `frontier_rows(campaign, limit=20_000)` returns `(position_id, fen, pn, dn)` of frontier nodes ordered by `(pn, position_id)` (text order of the sha256 hex key), first `limit`. Implementation (result identical to a join): `proved_side` = ids with `pn <= 0` (unordered); if more than 2000 (`FRONTIER_LOOKUP_CHUNK`) page the whole candidate set; else page the `pn <= 0` subset first and then `pn > 0`. `_frontier_pages`: keyset pagination by `(pn, position_id)` in pages of `limit` rows, each page joined to `Position` with `status='UNKNOWN'` in chunks of 2000 keys, appending until `limit`.
- **Inputs / outputs.** Read-only.
- **Code.** `atomicdb/proof.py:frontier_rows:1782`, `:_frontier_pages:1829`.
- **Tests.** `test_adversarial_explorer.py::ProofFrontierTests::*` (8, incl. `test_same_rows_as_the_join`, `test_the_scan_is_bounded`, `test_a_closed_node_is_a_leaf_of_the_proof_not_frontier`, `test_a_refuted_node_stops_being_an_obligation`).
- **Determinism hazards.** Membership depends on row presence (refresh history); key ordering depends on DB collation (hex digits and a-f sort the same under C and en_US).
- **History.** PR #296 (23-Nov, capture time).
- **Ordering.** Read side; also feeds the dn-repair arm (queue catalog).

### PN-10.2 AND frontier and dn statistics
- **Statement.** `frontier_and_rows` = frontier rows whose node is NOT an OR node (defender to move). `frontier_dn_stats(campaign, floor, limit)` → `{'and_nodes': count, 'dn_median': median, 'thin': count of dn <= floor, 'saturated': saturated_open_count(campaign, 'impossible')}`; median of the sorted dn list: odd → middle element, even → `(a + b) // 2`; empty → zeros (saturated still computed). `frontier_dn_headline(floor)` = stats of the first active campaign by id, None if none. Callers pass `floor = ingest.DN_REPAIR_FLOOR = 2`.
- **Inputs / outputs.** Read-only; stored hourly in `ProgressSnapshot`.
- **Code.** `atomicdb/proof.py:frontier_and_rows:1867`, `:frontier_dn_stats:1926`, `:frontier_dn_headline:1959`.
- **Tests.** `test_adversarial_explorer.py::ProofFrontierTests::test_the_median_is_a_median`; `::test_an_empty_frontier_reports_zeroes_not_a_gap`; `::test_an_attacker_node_is_not_an_obligation`; `test_proof_cycles.py::SaturationVisibilityTests::test_frontier_stats_carry_the_impossible_count`.
- **Determinism hazards.** As PN-10.1.
- **History.** —
- **Ordering.** Read side.

### PN-10.3 Saturated open counts
- **Statement.** `saturated_open_count(campaign=None, column=None)` counts ProofNode rows whose Position is `UNKNOWN` and: `column='dn'` → `dn >= INF`; `'pn'` → `pn >= INF`; `'impossible'` → `dn >= INF and 0 < pn < INF` (the only state sound recurrences cannot produce); otherwise `pn >= INF or dn >= INF`. Campaign filter optional (None = all campaigns).
- **Inputs / outputs.** Read-only.
- **Code.** `atomicdb/proof.py:saturated_open_count:1880`.
- **Tests.** `test_proof_cycles.py::SaturationVisibilityTests::*` (4).
- **Determinism hazards.** none.
- **History.** `7d7e3db` (6-Aug).
- **Ordering.** Read side.

### PN-10.4 Most-proving frontier and oldest obligations
- **Statement.** `most_proving_frontier(campaign, limit=10)`: rows with Position `UNKNOWN` ordered by `(pn, position_id)` (no finiteness filter). `open_and_obligations(campaign, limit=10)`: rows with Position `UNKNOWN` whose FEN contains `' b '` (attacker White) or `' w '` (attacker Black), ordered by `ProofNode.updated` ascending. Used by `proof_status` (display).
- **Inputs / outputs.** Read-only.
- **Code.** `atomicdb/proof.py:most_proving_frontier:1967`, `:open_and_obligations:1737`; `management/commands/proof_status.py`.
- **Tests.** `test_proof_manager.py::ProofStatusCommandTests::*` (3). **Gaps:** ordering by `updated` untested.
- **Determinism hazards.** `updated` is a wall-clock creation time (PN-0.1); FEN substring test.
- **History.** —
- **Ordering.** Read side.

### PN-10.5 recascade_proof command
- **Statement.** Manual re-baseline: before, print census (impossible, dn, pn counts of PN-10.3). For pass `n` in `1..max_passes` (default 12): seeds = `--keys` if given, else distinct position ids of open rows with `pn >= INF or dn >= INF`; none → stop ("nada saturado"); call `refresh_proof_numbers` in chunks of `--chunk` (default 500); stop when a pass writes 0 rows (fixpoint). The stop criterion is "nothing changed", never "nothing saturated".
- **Inputs / outputs.** ProofNode writes via PN-4.3.
- **Code.** `management/commands/recascade_proof.py:handle`.
- **Tests.** `test_proof_cycles.py::RecascadeProofCommandTests::test_command_drains_the_ratchet_and_reports_the_three_columns`; `::test_command_with_nothing_to_do_says_so`.
- **Determinism hazards.** Seed query unordered (affects chunking, hence PN-4.4 truncation and PN-4.3 order effects).
- **History.** `7d7e3db` (6-Aug).
- **Ordering.** Manual.

---

# Part 2: Mate proofs, PV verification and the exact layer

### MPV-1.1 verify_mate_pv
- **Statement.** `verify_mate_pv(root_fen, pv, winner_is_white, deadline=None)`: `fen = canonical_fen(root_fen)`, `seen = {fen}`. For each move: if a monotonic `deadline` is set and reached → False; if `terminal_status(fen)` is not None (terminal before the PV is exhausted) → False; if the move is not in `legal_moves(fen)` → False; `fen = apply_move(fen, move)` (canonical); if `fen in seen` (repetition inside the line, by canonical FEN) → False; add. After the loop: deadline check again → False; the final position must be terminal (`terminal_status`) with status `WHITE_WIN` if `winner_is_white` else `BLACK_WIN`. An empty PV checks the root itself.
- **Inputs / outputs.** Pure (pyffish).
- **Code.** `atomicdb/logic.py:verify_mate_pv:388`.
- **Tests.** `tests.py::LogicTests::test_verify_mate_pv_accepts_real_mate`; `::test_verify_mate_pv_rejects_illegal`; `::test_verify_mate_pv_rejects_nonterminal`; `test_en_passant_identity.py::RepetitionGuardTests::test_the_witness_verifier_now_rejects_the_repetition`; `test_revocation.py::RecertifyMatesTests::test_recovery_rejects_a_pv_that_no_longer_verifies`.
- **Determinism hazards.** Wall clock when `deadline` is passed (online path, MPV-2.4): a deadline hit returns False, i.e. "not a mate line" (the line is dropped, not degraded).
- **History.** Original; deadline PR #10 (22-Jul); canonical v2 (ghost e.p.) changed repetition detection.
- **Ordering.** Before MPV-2.1 in MPV-2.4; also in MPV-5.1 (verify), CMD-2 recovery.

### MPV-1.2 walk_pv
- **Statement.** `walk_pv(root_fen, pv, deadline=None)` → list of canonical FENs `[root, after ply 1, ...]` or None, with the same per-ply checks as MPV-1.1 (deadline, no terminal before the end, legality, no repetition). It does not judge the end.
- **Inputs / outputs.** Pure.
- **Code.** `atomicdb/logic.py:walk_pv:413`.
- **Tests.** indirect via TB-3.1 tests (`test_closure_trust.py::TablebaseLineTests::*`). **Gaps:** no direct unit test.
- **Determinism hazards.** Wall clock deadline.
- **History.** PR #223 (11-Nov).
- **Ordering.** Used by TB-3.1 and MPV-5.4.

### MPV-2.1 prove_forced_mate (bounded exhaustive AND/OR)
- **Statement.** `prove_forced_mate(fen, winner_is_white, max_plies, budget_positions=200_000, hint_pv=None, deadline=None, return_run=False)` → verdict `PROVEN | NO_MATE | INCONCLUSIVE` (or `(verdict, worst_run)` with `return_run`). Negative `max_plies` or budget → ValueError. Depth-first search from `root = canonical_fen(fen)` with `path = {root}` (canonical FENs of the current branch) and a global counter `visited`:
  ```
  search(node, plies_left, path, hint, run):
      if visited >= budget_positions or (deadline and monotonic() >= deadline): raise Exhausted
      visited += 1                                  # counted on entry, root included
      moves = legal_moves(node)                     # pyffish order
      if not moves or immediate_end(node):          # terminal
          t = terminal_status(node); return (t and t.status == winner, run)
      if plies_left == 0: return (False, run)       # non-terminal leaf at the horizon fails
      hinted = hint[0] if hint else None
      if hinted in moves: move it to the front
      if winner to move (OR):
          for move in moves:
              child = apply_move(node, move)
              if child in path: continue            # repetition: skip, costs no budget
              child_hint = hint[1:] if move == hinted else ()
              child_run = 0 if zeroing(node, move) else run + 1
              proved, worst = search(child, plies_left-1, path ∪ {child}, child_hint, child_run)
              if proved: return (True, max(run, worst))   # FIRST success wins
          return (False, run)
      else (AND):
          worst_here = run
          for move in moves:
              child = apply_move(node, move)
              if child in path: return (False, run)        # defender can repeat: refuted
              ... same hint/run ...
              proved, worst = search(...)
              if not proved: return (False, run)
              worst_here = max(worst_here, worst)
          return (True, worst_here)
  verdict = PROVEN if proved else NO_MATE; Exhausted → INCONCLUSIVE; worst only when PROVEN
  ```
  No transposition table (repetition depends on branch history). `hint_pv` only reorders; it never changes the set of moves or a verdict that fits in the budget. NO_MATE means "no forced mate within `max_plies` plies under the repetition-is-failure rule", not "no mate".
- **Inputs / outputs.** Pure (pyffish).
- **Code.** `atomicdb/logic.py:prove_forced_mate:459`.
- **Tests.** `test_proofs.py::ForcedMateProofTests::*` (incl. `test_proven_real_mate_in_two_uses_hint_first`, `test_no_mate_for_cooperative_but_non_forced_line`, `test_inconclusive_when_position_budget_is_exhausted`, `test_inconclusive_when_wall_deadline_has_expired`); `test_proofs.py::MovegenMemoTests::*`; `test_en_passant_identity.py::RepetitionGuardTests::test_a_forced_mate_search_treats_it_as_a_dead_branch`; `test_proofs.py::MovegenShortcutTests::test_a_proof_never_asks_the_flag_with_both_kings`.
- **Determinism hazards.** **Move order is pyffish's** (after the hint): it decides which OR branch is found first, hence `worst_run` (clock slack) and the number of visited nodes, hence PROVEN vs INCONCLUSIVE at the budget edge. A port on another move generator (e.g. shakmaty) must reproduce pyffish's order exactly or accept differences. Wall-clock deadline (online) makes the verdict timing-dependent.
- **History.** Original (July); run tracking `8c04741` (27-Jul); deadline PR #10; memo PR #280 (18-Nov).
- **Ordering.** Called by MPV-2.4, CMD-1, CMD-2.

### MPV-2.2 Worst reversible run of a proof
- **Statement.** With `return_run=True`, each node carries `run` = consecutive non-zeroing plies leading to it (reset to 0 by a capture or pawn move, `is_zeroing`); a terminal or horizon leaf returns its `run`; an OR node returns `max(run, worst of the FIRST proven child)`; an AND node returns `max(run, max over all children's worst)`. The result is the longest reversible stretch in the strategy tree actually proven (not the minimum over alternatives). Converted to slack by DTM-5.6.
- **Inputs / outputs.** Pure.
- **Code.** `atomicdb/logic.py:prove_forced_mate:547-577`.
- **Tests.** `test_clock_slack.py::ProofRunTests::test_a_proof_reports_its_worst_reversible_run`; `::test_a_failed_proof_reports_no_run`; `::test_the_old_signature_is_untouched`; `test_proofs.py::MovegenMemoTests::test_real_lines_keep_their_verdict_run_and_node_count`.
- **Determinism hazards.** Same move-order dependence as MPV-2.1.
- **History.** `8c04741` (27-Jul).
- **Ordering.** Feeds MPV-2.6 / MPV-4.1 `clock_slack`.

### MPV-2.3 Movegen memo does not change results
- **Statement.** `logic` memoises pure pyffish questions per process: `_legal_moves(fen)` and `_immediate_end_movegen(fen)` and `_game_result(fen)` (32,768 entries each), `_fen_after(fen, uci)` (65,536), `apply_move` and `_en_passant_right_exists` (16,384). `legal_moves` returns a fresh list. `_immediate_end(fen)` is False without asking pyffish when both `K` and `k` are on the board (the only way atomic's immediate-end fires with an empty move list is a missing king). The memo never changes an answer or a node count.
- **Inputs / outputs.** Process caches; `clear_caches()`.
- **Code.** `atomicdb/logic.py:76-89`, `:_legal_moves:198`, `:_immediate_end:232`, `:_fen_after:256`, `:apply_move:266`.
- **Tests.** `test_proofs.py::MovegenMemoTests::*` (4); `test_proofs.py::MovegenShortcutTests::*`.
- **Determinism hazards.** none (pure), but performance-dependent wall-clock budgets (MPV-2.5) do change with cache warmth.
- **History.** PR #280 (18-Nov); kings shortcut 23-Nov profile.
- **Ordering.** —

### MPV-2.4 prepare_mate_proofs (online preparation)
- **Statement.** `prepare_mate_proofs(parent_fen, lines, budget_positions=200_000, deadline_seconds=ONLINE_MATE_PROOF_SECONDS)` → `{line_index: (winner_white, pv_rest, verdict, extra)}`, computed outside the write transaction. `deadline = monotonic() + max(0, deadline_seconds)` shared by everything below (None → no deadline). `legal = set(legal_moves(parent_fen))`.
  1. Candidates: for each line (index i) with `mate` not None, `pv` a non-empty list, `move in legal`, `pv[0] == move`: `child = apply_move(parent, move)` (exception → skip); `winner_white = mate > 0`; `pv_rest = pv[1:]`; keep only if `verify_mate_pv(child, pv_rest, winner_white, deadline)`.
  2. Sort candidates by `(len(pv_rest), index)`; for each: if the deadline has passed → verdict `INCONCLUSIVE`, run None; else `prove_forced_mate(child, winner_white, max_plies=len(pv_rest)+2, budget_positions, hint_pv=pv_rest, deadline, return_run=True)`. Store `(winner_white, pv_rest, verdict, worst_run)`.
  3. For each line index not prepared, in index order: stop when the deadline has passed; `prepare_tb_line(parent, line, legal, deadline)` (TB-3.1); a result is stored as `(winner_white, pv_rest, 'TB_LINE', slack)`.
  Deadline exhaustion never manufactures ANDOR or a dispute; but a deadline hit during step 1 removes the line entirely (no closure), and during step 3 skips TB lines.
- **Inputs / outputs.** Pure except DBEvent `TB_LINE_REJECTED` from TB-3.1.
- **Code.** `atomicdb/ingest.py:prepare_mate_proofs:767`.
- **Tests.** `test_proofs.py::ForcedMateProofTests::test_online_proof_deadline_is_shared_and_fail_safe`; `test_mate_sign.py::MateWinnerPerspectiveTests::*` (4); `test_fencing.py::SubmitPreparationBoundaryTests::test_andor_preparation_runs_outside_atomic_block`.
- **Determinism hazards.** **Wall clock** decides (a) whether a mate line is a candidate at all, (b) PROVEN vs INCONCLUSIVE (ANDOR vs ENGINE grade, slack), (c) whether TB lines are tried. Machine load and cache warmth change outcomes. See D1.
- **History.** 20 s → 5 s (`9f09726`, 28-Jul) → 2.5 s (PR #280, 18-Nov); TB lines PR #223 (11-Nov).
- **Ordering.** Before the landing transaction (apply_job), after score normalisation (SB-8) and after the server TB check (TB-2.5). Consumed by MPV-2.6/2.7/3.1/4.1.

### MPV-2.5 ONLINE_MATE_PROOF_SECONDS
- **Statement.** The shared wall-clock allowance of MPV-2.4: `2.5` seconds in the repository; production is hot-patched to `1.0` (owner, not in the repo). Replay on 677 jobs: 2.5 s gave byte-identical output to 5 s; 1 s lost no PROVEN but 27 jobs ran out of time before verifying a mate PV (those lines were dropped).
- **Inputs / outputs.** Constant.
- **Code.** `atomicdb/ingest.py:175`.
- **Tests.** none pin the value. **Gaps:** the production value differs from the repo.
- **Determinism hazards.** It is the wall-clock budget itself.
- **History.** PR #10 (22-Jul, created), `9f09726` (5 s), PR #280 (2.5 s); production 1.0 hot patch.
- **Ordering.** —

### MPV-2.6 Closing an open child from a prepared line
- **Statement.** Inside `ingest_analysis` (landing transaction, position locked), for each normalised line i whose move has an edge `(pos, move) → child`, with `prepared = mate_proofs.get(i)`, after MPV-3.1 and MPV-4.1 (if they apply) and when `child.status == 'UNKNOWN'` and `prepared` exists and verdict != `NO_MATE` (MPV-2.7) and the trust gate passes (MPV-2.8):
  - `status = WHITE_WIN if winner_white else BLACK_WIN`, `closure = 'MATE_PV'`;
  - `proof = 'ANDOR'` if verdict PROVEN; `'TB_LINE'` if verdict TB_LINE; else `'ENGINE'` (INCONCLUSIVE);
  - `won_line = ' '.join(pv_rest)`; `mate_in = len(pv_rest)` (plies of the stored witness; TB_LINE → NULL);
  - `clock_slack = slack_from_run(worst_run)` if PROVEN and run known, else `slack_from_witness_length(len(pv_rest))`; TB_LINE → the line's measured slack (TB-3.1);
  - `best_move = pv_rest[0]` if pv_rest is non-empty (else unchanged);
  - written with a compare-and-swap `UPDATE ... WHERE key=child AND status='UNKNOWN'` (status, closure, proof, won_line, mate_in, clock_slack, best_move, updated=now); if 0 rows (someone closed it first) nothing else happens for this line;
  - on success: `_emit_closure_events(child)` (CAS-7), child added to `closed_keys`, `materialise_won_line(child)` (MPV-5.1) and its closed keys added, `closed_children += 1`.
  `ingest_analysis` with `mate_proofs=None` (direct callers) first re-reads the position, returns `{'skipped': 'already-closed'}` if closed, normalises lines and calls MPV-2.4 itself.
- **Inputs / outputs.** Writes `Position.status/closure/proof/won_line/mate_in/clock_slack/best_move/updated` of the child; events via CAS-7; positions/edges via MPV-5.
- **Code.** `atomicdb/ingest.py:ingest_analysis:1093-1192`.
- **Tests.** `tests.py::IngestTests::test_mate_pv_closure_emits_event`; `test_clock_slack.py::ClosureSlackTests::test_a_proven_mate_uses_the_run_and_an_engine_one_uses_the_bound`; `test_mate_sign.py::MateWinnerPerspectiveTests::*`; `test_deferred_propagation.py::StageATests::test_a_closure_of_its_own_child_is_stage_a_and_its_parents_are_b`; `test_closure_trust.py::ProvenClosuresTests::test_a_proven_mate_closes_for_any_account`; `test_closure_trust.py::TablebaseLineTests::test_a_line_into_a_won_ending_closes_like_a_mate_line`.
- **Determinism hazards.** CAS against concurrent landings (first committer wins, verdict of the loser is discarded even if stronger); `updated=timezone.now()`.
- **History.** Original; trust gate PR #223; TB_LINE PR #223.
- **Ordering.** Stage (a) of the landing; the parent climb (cascade, backed) follows in stage (b) with `closed_keys` as cascade seeds (interface).

### MPV-2.7 NO_MATE on an open child: DISPUTED and deep reanalysis
- **Statement.** When the child is `UNKNOWN` and the prepared verdict is `NO_MATE` (any account): set `child.proof = 'DISPUTED'`, `child.won_line = ' '.join(pv_rest)` (saved with `updated`); `DBEvent(kind='MATE_PROOF_DISPUTED', payload={'key': child, 'parent': pos, 'winner': 'WHITE'|'BLACK', 'max_plies': len(pv_rest)+2})`; `_refute_pending_claims(child, pv_rest)` (claims catalog); `_queue_disputed_reanalysis(child)`: if a PENDING task exists for the child (the one with the highest `generation`), raise its `budget_nodes` to `max(budget, 2_000_000_000)` and `multipv = 2`; else create `AnalysisTask(position=child, generation = first unused ≥ max(visits,0), budget_nodes = 2B, multipv = 2, source = 'AUTO')` (not floored or capped by `server_picked_budget`). The child stays open. A child revoked by MPV-3.1 in the same line falls into this branch too, so it gets a second `MATE_PROOF_DISPUTED` event (without `was_closed`).
- **Inputs / outputs.** `Position.proof/won_line`; DBEvent `MATE_PROOF_DISPUTED`; AnalysisTask create/update.
- **Code.** `atomicdb/ingest.py:ingest_analysis:1112-1125`, `:_queue_disputed_reanalysis:8690`.
- **Tests.** `test_proofs.py::NewDisputedMateTests::test_new_dispute_does_not_close_and_queues_max_budget`; `test_multipv_policy.py::MultipvAtCreationSitesTests::test_a_disputed_reanalysis_asks_for_depth`.
- **Determinism hazards.** "highest generation PENDING" via `.first()` on an ordered query (deterministic); generation search depends on existing rows.
- **History.** Original; revocation interplay `bdaddf1` (27-Jul).
- **Ordering.** Same loop as MPV-2.6.

### MPV-2.8 Trust gate on unproven lines (interface)
- **Statement.** If the child is `UNKNOWN`, the prepared verdict is not PROVEN (INCONCLUSIVE or TB_LINE) and the delivering account is not trusted, the line does not close anything: `_record_pending_claim(child, parent, winner_white, pv_rest, tb_line)` (claims catalog). `account_trusted(username)`: True when `settings.ATOMICDB_CLOSURE_TRUST_GATE` is false (default True); False for an empty username; else a `TrustedAccount` row exists. `ingest_analysis(trusted=True)` by default (internal callers); the ingest queue passes the real standing of `task.delivered_by` or the payload username. PROVEN closes for any account; NO_MATE disputes for any account.
- **Inputs / outputs.** `PendingClaim` rows (claims catalog).
- **Code.** `atomicdb/ingest.py:ingest_analysis:1127-1138`, `:account_trusted:5705`, `:trust_gate_enabled:5701`; `atomicdb/ingest_queue.py:apply_job:295-299`.
- **Tests.** `test_closure_trust.py::CooperativeMateTests::*`; `test_closure_trust.py::ProvenClosuresTests::*`; `test_closure_trust.py::TablebaseLineTests::test_an_untrusted_account_keeps_it_as_a_pending_claim`.
- **Determinism hazards.** Depends on the relational trust table at apply time.
- **History.** PR #223 (11-Nov).
- **Ordering.** Same loop; claims resolved by the claims catalog when the child closes.

### MPV-3.1 Online revocation of a refuted closed witness
- **Statement.** In the landing loop, when the child is already closed and the prepared verdict is `NO_MATE`: `_revoke_contradicted_mate(child, parent, prepared)` acts only if `child.status` equals the claimed winner, `child.closure == 'MATE_PV'`, `child.proof != 'ANDOR'`, `child.mate_in is not None` and `child.mate_in <= len(pv_rest) + 2` (the refutation covers the stored distance). Then `DBEvent('MATE_PROOF_DISPUTED', {key, parent, winner, max_plies, 'was_closed': True, 'claimed_mate_in'})` and `revoke_closure(child, reason='mate-witness-refuted', mark_disputed=True)` (revocation catalog); returns the revoked keys, and the child is re-read (then MPV-2.7 applies to it). TB_LINE children (mate_in NULL) are never revoked this way.
- **Inputs / outputs.** DBEvent; revocation writes.
- **Code.** `atomicdb/ingest.py:_revoke_contradicted_mate:1487`, call at `ingest.py:1094-1105`.
- **Tests.** `test_revocation.py::OnlineDisputeRevokesTests::test_refuted_witness_on_an_already_closed_child_is_withdrawn`; `::test_a_deeper_claim_than_the_search_is_left_alone`.
- **Determinism hazards.** Inherits MPV-2.4's wall-clock dependence (a NO_MATE needs the search to finish).
- **History.** `bdaddf1` (27-Jul); `b69ab74`/`a4960ff` (29-Jul).
- **Ordering.** First check in the per-line loop, before MPV-4.1 and MPV-2.6/2.7.

### MPV-4.1 Certified line adopted by a closed child
- **Statement.** When the child is already closed and the prepared verdict is `PROVEN`: `_adopt_certified_line` acts only if `child.status` equals the winner, `closure == 'MATE_PV'`, `proof != 'ANDOR'`, and `pv_rest` is non-empty. It replaces the witness entirely, in either direction (longer or shorter): `proof = 'ANDOR'`, `won_line = pv_rest`, `mate_in = len(pv_rest)`, `best_move = pv_rest[0]`, `clock_slack = slack_from_run(run)` if run known else `slack_from_witness_length(len)`; saves; `DBEvent('MATE_WITNESS_CERTIFIED', {key, parent, winner, mate_in, was_mate_in, was_proof, line_changed})`; materialises the new line; returns `[child, *materialised closed keys]` which join `closed_keys`. An INCONCLUSIVE line never replaces a closed witness.
- **Inputs / outputs.** Child `proof/won_line/mate_in/clock_slack/best_move/updated`; DBEvent.
- **Code.** `atomicdb/ingest.py:_adopt_certified_line:1548`, call at `ingest.py:1106-1111`.
- **Tests.** `test_mate_distance.py::CertifiedLineAdoptionTests::*` (4); `test_closure_reaches_all_parents.py::IngestClosureReachesEveryParentTests::test_a_certified_line_upgrades_every_parent_standing_on_it`.
- **Determinism hazards.** Not a CAS (plain save on the loaded object, under the parent's lock only).
- **History.** `a4960ff` (29-Jul, mate-distance honesty).
- **Ordering.** After MPV-3.1, before MPV-2.6.

### MPV-5.1 won_line materialisation (entry, grade, slack)
- **Statement.** `won_line_materialisable(pos)`: `closure == 'MATE_PV'`, status decisive, `proof != 'DISPUTED'`, non-empty `won_line`. `materialise_won_line(pos, verify=False)`: not materialisable → zero counts. If `verify`: TB_LINE witnesses are re-checked with MPV-5.4, others with `verify_mate_pv(pos.fen, line, pos.status == 'WHITE_WIN')`; failure → `{'rejected': 'witness-does-not-verify'}` and nothing written. `grade = pos.proof` if in (ANDOR, ENGINE, TB_LINE) else NULL; `slack = pos.clock_slack` (inherited unchanged by every link: conservative). Then MPV-5.2; returns `{created_edges, closed, plies, closed_keys}`.
- **Inputs / outputs.** Creates Position/Edge rows; closes chain links (MPV-5.3).
- **Code.** `atomicdb/ingest.py:won_line_materialisable:5180`, `:materialise_won_line:5195`.
- **Tests.** `test_won_line_chain.py::MaterialiseWonLineTests::*` (incl. `test_andor_keeps_its_grade_down_the_chain`, `test_engine_stays_engine_down_the_chain`, `test_a_disputed_witness_materialises_nothing`, `test_a_witness_that_no_longer_verifies_is_refused`, `test_slack_is_inherited_conservatively`, `test_the_distance_shrinks_one_ply_at_a_time`, `test_the_chain_is_not_expanded`, `test_an_existing_better_closure_is_not_overwritten`).
- **Determinism hazards.** none beyond MPV-5.2/5.3.
- **History.** `a4960ff` era; TB_LINE PR #223.
- **Ordering.** After MPV-2.6 / MPV-4.1; CMD-5.

### MPV-5.2 Batched chain write versus stepwise walk
- **Statement.** `_won_line_chain(pos, line)`: walk `line[:64]` (`WON_LINE_MAX_PLIES`) from `pos.fen`, stopping at the first move not in `legal_moves(fen)`; each step `(index, uci, parent_key, child_fen = canonical(apply_move), child_key = key_of)`. An exception anywhere → None (fall back to stepwise). Existing rows among the child keys: if any stored FEN differs from the computed one → None. New positions inherit `campaign_id` from the previous node in the line (existing rows pass on their own campaign). Create missing positions (`create_positions`), then edges `(parent, uci, child)` for pairs without an existing `(parent, uci)` edge, `bulk_create(ignore_conflicts=True)`. Returns links, rows, created edge count. `_materialise_stepwise`: same walk with `get_or_create_position` and `Edge.get_or_create` per ply, closing each link as it goes. Both close links with MPV-5.3. Chain nodes are NOT expanded.
- **Inputs / outputs.** Position and Edge inserts.
- **Code.** `atomicdb/ingest.py:_won_line_chain:5241`, `:_materialise_stepwise:5303`.
- **Tests.** `test_ingest_batched_writes.py::MaterialiseTests::*` (4); `test_won_line_chain.py::MaterialiseWonLineTests::test_a_transposition_into_the_line_inherits_the_closure`.
- **Determinism hazards.** `ignore_conflicts` with concurrent writers; a position born terminal by POS-2 rules is closed at creation (not by this rule).
- **History.** Batched version Nov-2026 (perf).
- **Ordering.** Inside MPV-5.1.

### MPV-5.3 Closing one link of the chain
- **Statement.** `_close_won_line_link(pos, child, suffix, tb_line, grade, slack)` where `suffix = line[index+1:]`: (a) TB_LINE and empty suffix and child UNKNOWN → MPV-5.4 end closure. (b) Non-empty suffix and child UNKNOWN → CAS `UPDATE WHERE status='UNKNOWN'`: `status = pos.status`, `closure = 'MATE_PV'`, `proof = grade`, `won_line = suffix`, `mate_in = NULL if tb_line else len(suffix)`, `best_move = suffix[0]`, `clock_slack = slack`, `updated = now`; on success `_emit_closure_events(child)`. A child already closed is never overwritten. The last link of a mate line (empty suffix) is the terminal position, closed at creation.
- **Inputs / outputs.** Position closure columns; events.
- **Code.** `atomicdb/ingest.py:_close_won_line_link:5327`.
- **Tests.** `test_won_line_chain.py::MaterialiseWonLineTests::test_an_existing_better_closure_is_not_overwritten`; `test_closure_reaches_all_parents.py::IngestClosureReachesEveryParentTests::test_a_closed_link_of_the_won_line_reaches_its_other_parents`; `test_ingest_queue.py::ClosureAbsorbsItsQuestionsTests::test_materialising_a_won_line_absorbs_what_it_closes`.
- **Determinism hazards.** CAS race with other closers; `updated` wall clock.
- **History.** —
- **Ordering.** Inside MPV-5.2. The cascade seeds include the closed links (interface).

### MPV-5.4 TB_LINE chain: re-check and end node
- **Statement.** `_tb_line_still_wins(pos, line)`: `walk_pv(pos.fen, line)` must give at least 2 positions and `server_tables_wdl(end)` must map (`wdl_to_status`, end's side to move) to `pos.status`. `_close_tb_line_end(end, status)`: `server_tables_wdl(end.fen)` maps to `status` → CAS `status=status, closure='TB', clock_slack=0, updated=now` (proof, mate_in untouched); emit closure events with `dtz=None, dtz_verified=False, clock_slack=0`.
- **Inputs / outputs.** End node closure; events; TB probe.
- **Code.** `atomicdb/ingest.py:_tb_line_still_wins:5356`, `:_close_tb_line_end:5366`.
- **Tests.** `test_closure_trust.py::TablebaseLineTests::test_a_line_into_a_won_ending_closes_like_a_mate_line`; `::test_the_line_names_no_mate_distance_upwards`.
- **Determinism hazards.** Tablebase files and the server horizon setting (TB-2.1).
- **History.** PR #223.
- **Ordering.** Inside MPV-5.3.

### MPV-6.1 PV verification: candidates
- **Statement.** `_verify_lines(pos)`: dict lines of `last_analysis` without `prior_pass`, in stored order. `_verify_candidates(pos)` → `[(number, pv, from_tree)]`: for `i, line` in the first 3 (`PV_VERIFY_MAX_LINES`) current lines (numbered from 1): `slot = i`; lines without a non-empty list `pv` are skipped (but consume their number); add `(i, pv, False)` and mark `pv[0]` taken. If fewer than 3 candidates, add tree moves `_verify_tree_moves(pos, taken)` up to the remaining room, numbered `slot+1, slot+2, ...`, each as `(n, [uci], True)`. `_verify_tree_moves`: edges of pos (any order) whose move is not taken and whose child is UNKNOWN with a non-None `best_known_eval`; sorted by `(-(known if White to move else -known), uci)`.
- **Inputs / outputs.** Read-only.
- **Code.** `atomicdb/ingest.py:_verify_lines:9855`, `:_verify_tree_moves:9867`, `:_verify_candidates:9896`.
- **Tests.** `test_pv_verify.py::TreeCandidateTests::*` (7); `test_pv_verify.py::EnqueuePvVerificationTests::test_an_earlier_wider_pass_is_not_the_current_line`; `::test_without_a_stored_analysis_there_is_nothing_to_verify`.
- **Determinism hazards.** none (sort by value then UCI).
- **History.** `49769c3` (30-Jul); tree candidates Aug.
- **Ordering.** Inside MPV-6.6.

### MPV-6.2 PV verification: step rule
- **Statement.** `_verify_step(node, spine, trust_own)` → `(uci, rest)` or `(None, [])`: `legal = legal_moves(node.fen)`; if `trust_own` and the node's current line (`solve_estimate.current_line`) has a pv whose first element is a legal string → `(own[0], own[1:])`; else if `spine[0]` is a legal string → `(spine[0], spine[1:])`; else stop.
- **Inputs / outputs.** Pure.
- **Code.** `atomicdb/ingest.py:_verify_step:9937`.
- **Tests.** `test_pv_verify.py::EnqueuePvVerificationTests::test_the_walk_descends_through_covered_ground_and_buys_below`; `::test_an_illegal_move_cuts_the_walk_without_an_error`.
- **Determinism hazards.** none.
- **History.** `49769c3`.
- **Ordering.** Inside MPV-6.4.

### MPV-6.3 PV verification: buying one node
- **Statement.** `_buy_verification(child, requested_by, route)` → `(state, budget)`: `clamp = _short_mate_clamp(child)`; `budget = 128M` (`REQUEST_BUDGET_LADDER[0]`) if no clamp else `budget_for(child)`; `done = largest budget_nodes of a COMPLETED task of the child`; `done >= budget` → `('covered', budget)`. `AnalysisTask.get_or_create(position=child, generation=child.visits, defaults={budget, multipv_for(visits, budget, clamp), source USER, requested_by, route, requested_at=now})`: created → LIFO preference, `'queued'`; existing CANCELLED → revived (`_revive_withdrawn`) + LIFO, `'queued'`; existing PENDING not USER → promoted to USER (requested_by/route filled if empty, request stamp) + LIFO, `'queued'`; otherwise `'in-flight'`.
- **Inputs / outputs.** AnalysisTask create/update.
- **Code.** `atomicdb/ingest.py:_buy_verification:9961`.
- **Tests.** `test_pv_verify.py::EnqueuePvVerificationTests::test_the_budget_is_the_request_floor_not_a_coverage_seed`; `::test_only_a_completed_budget_that_reaches_the_request_skips`; `::test_a_node_already_analysed_deeper_is_not_bought_again`; `::test_a_withdrawn_request_on_the_line_is_bought_again`; `::test_an_autonomous_pending_task_is_promoted_and_counted`; `test_request_time.py::RequestTimeTests::test_a_verify_walk_promoting_server_work_is_a_fresh_wait`; `test_then_verify.py::VerifyHonoursNewestFirstTests::test_a_bought_verification_is_offered_to_newest_first`.
- **Determinism hazards.** `requested_at = timezone.now()`.
- **History.** `49769c3`; 10B cut 25-Aug.
- **Ordering.** Inside MPV-6.4.

### MPV-6.4 PV verification: the walk
- **Statement.** `_walk_pv_frontier(pos, pv, requested_by, route)`: counts `{queued, in_flight, covered_plies, plies, budget, move, move_fen}`; `visited = {pos}`; `node = pos`, `spine = pv`, `trust_own = False`, `walked_route = route`. While `plies < 16` (`PV_VERIFY_MAX_PLIES`): `(uci, spine) = _verify_step(...)`; none → stop. `child, closed_below = get_or_create_position_linked(apply_move(node.fen, uci), campaign_id=node.campaign_id)`; `Edge.get_or_create(node, uci, child)`. If `closed_below` → `settle_linked_children(child, [node])` (POS-3); elif child closed and node open → `backup_cascade([node])` + `backup_backed_evals([node])`. If `child.priority <= DEAD/2` → priority 0. Extend `walked_route` with `,uci` when route given. `plies += 1`. Child already visited → stop. Child closed → `trust_own = False`, mark covered (MPV-6.5), continue. Else `_buy_verification`: `trust_own = (state == 'covered')`; covered → mark covered; in-flight → count; queued → record budget/move/move_fen of the FIRST purchase, `queued += 1`. Nothing is bought on `pos` itself. Returns counts with `plies`.
- **Inputs / outputs.** Positions, edges, tasks; may cascade.
- **Code.** `atomicdb/ingest.py:_walk_pv_frontier:10040`.
- **Tests.** `test_pv_verify.py::EnqueuePvVerificationTests::*`; `test_pv_verify.py::PvWalkDiscoversATerminalTests::*` (4); `test_link_children_at_creation.py::SolvedChildClimbsTests::test_the_pv_walk_closes_the_node_it_makes_on_its_solved_child`.
- **Determinism hazards.** none beyond inputs.
- **History.** `49769c3`; terminal discovery 29-Jul.
- **Ordering.** Inside MPV-6.6.

### MPV-6.5 PV verification: covered prefix
- **Statement.** `_mark_verified(counts, plies)`: `covered_plies = plies` only while nothing has been queued yet and `covered_plies == plies - 1` (contiguous prefix before the first purchase).
- **Inputs / outputs.** Counts only (served in the receipt).
- **Code.** `atomicdb/ingest.py:_mark_verified:10029`.
- **Tests.** `test_pv_verify.py::PvVerifyEndpointTests::test_a_purchase_below_verified_ground_says_how_deep_it_walked`.
- **Determinism hazards.** none.
- **History.** —
- **Ordering.** Inside MPV-6.4.

### MPV-6.6 enqueue_pv_verification (line selection and receipt)
- **Statement.** `enqueue_pv_verification(pos, requested_by, route, line=0)` → `VerifyOutcome(int, **detail)`. Candidates from MPV-6.1; `line != 0` keeps only the candidate with that number; none → `VerifyOutcome(0, ..., unknown_line=True)` (never rounded). For each candidate in order: walk it (MPV-6.4); track max covered/plies; if the walk queued or found in-flight work → return `VerifyOutcome(queued, line=number, lines_tried, from_tree, covered_plies, plies, in_flight, budget, move, move_fen)`. If none bought anything → `VerifyOutcome(0, line=line, lines_tried, from_tree = bool(line) and candidates[0].from_tree, covered_plies=max, plies=max, ...)`. With `line=0` a line in flight stops the fall-through (a second click buys nothing). Also called after a landing for requests with `then_verify` (queue catalog), on open positions only.
- **Inputs / outputs.** Tasks/positions/edges via MPV-6.4.
- **Code.** `atomicdb/ingest.py:enqueue_pv_verification:10143`, `:verify_candidates:10132`; `atomicdb/ingest_queue.py:_verify_after:384`.
- **Tests.** `test_pv_verify.py::ChosenLineTests::*` (7); `test_pv_verify.py::ChosenLineEndpointTests::*` (6); `test_pv_verify.py::EnqueuePvVerificationTests::*`; `test_pv_verify.py::PvVerifyEndpointTests::*`; `test_then_verify.py::ThenVerifyIngestTests::*`; `test_deferred_propagation.py::DurableDeferralTests::test_the_requested_verification_waits_for_the_climb`.
- **Determinism hazards.** none beyond inputs.
- **History.** `49769c3`; chosen line 14-Aug; then_verify 4-Nov.
- **Ordering.** Caller owns the transaction.

### EXP-4.1 claimed_mate_plies (and move-to-ply conversion)
- **Statement.** `_mate_moves_to_plies(moves, winner_white, stm_white)`: `moves <= 0` → 0; the side to move mates (`winner_white == stm_white`) → `2*moves - 1`; else `2*moves`. `claimed_mate_plies(pos)`: (1) `mate_in` not None → `max(0, mate_in)` (already plies); (2) the first dict line of `last_analysis` without `prior_pass`: if its `mate` is truthy → `_mate_moves_to_plies(|mate|, mate > 0, stm_white)`; if that line has no mate, stop looking at lines; (3) `eval_cp` with `9500 < |eval_cp| < 10000` → `_mate_moves_to_plies(10000 - |eval_cp|, eval_cp > 0, stm_white)`; else None. TB-band, big evals and ±10000 carry no distance.
- **Inputs / outputs.** `Position.mate_in`, `last_analysis`, `eval_cp`, `fen`.
- **Code.** `atomicdb/ingest.py:_mate_moves_to_plies:262`, `:claimed_mate_plies:275`.
- **Tests.** `test_mate_clamp.py::MateDistanceUnitTests::*` (11, incl. `test_the_mover_mates_on_its_own_move`, `test_the_defender_mating_costs_the_extra_ply`, `test_the_declared_mate_field_beats_the_decoded_eval`, `test_a_tablebase_clamp_carries_no_distance`, `test_a_proven_win_without_a_stated_distance_is_not_a_claim`, `test_a_line_from_a_previous_pass_does_not_speak`).
- **Determinism hazards.** none.
- **History.** `b69ab74` (29-Jul); ±10000 rule `da18ee6` (30-Jul).
- **Ordering.** Used by EXP-4.2.

### EXP-4.2 _short_mate_clamp
- **Statement.** `_short_mate_clamp(pos)` → `(budget_nodes, multipv)` or None: `plies = claimed_mate_plies(pos)`; None or `plies > 6` (`MATE_CLAMP_PLIES`) → None; else `(min(32_000_000, max(2_000_000, 4_000_000 * plies)), 1)` (`MATE_CLAMP_CAP`, `MATE_CLAMP_FLOOR`, `MATE_CLAMP_PER_PLY`, `MATE_CLAMP_MULTIPV`). No query on a loaded row.
- **Inputs / outputs.** Pure over the row.
- **Code.** `atomicdb/ingest.py:_short_mate_clamp:318`, constants `ingest.py:126-130`.
- **Tests.** `test_mate_clamp.py::ShortMateClampShapeTests::*` (6).
- **Determinism hazards.** none.
- **History.** `b69ab74` (29-Jul).
- **Ordering.** Before EXP-4.3 consumers.

### EXP-4.3 Clamp consumers (multipv_for, server_picked_budget, budget_for)
- **Statement.** `multipv_for(visits, budget_nodes=None, seeding=False, clamp=None)`: seeding → 5; clamp given and `budget_nodes <= clamp[0]` → `clamp[1]`; `visits >= 1` and `budget >= 512M` → 2; else 5 if `visits < 3` else 3. `server_picked_budget(budget, clamp=None, pos=None)`: clamp given and `budget <= clamp[0]` → budget unchanged; else if `pos` is interior (`backed_move` set) and `budget > 128M` and the deep-rungs switch is off → 128M; then `max(budget, SERVER_PICKED_MIN_NODES = 128M)`. `budget_for(pos)`: `BUDGET_LADDER[min(visits, 4)]` (8M, 32M, 128M, 512M, 2B); with a short-mate clamp → `max(ladder, clamp budget)`; else if `|eval_cp| >= 9000` → `max(ladder, 128M)`. (Queue catalog owns the ladders; listed here because the mate clamp decides them.)
- **Inputs / outputs.** Pure.
- **Code.** `atomicdb/ingest.py:multipv_for:334`, `:server_picked_budget:446`, `:caps_interior:440`, `:budget_for:6778`.
- **Tests.** `test_mate_clamp.py::MultipvPolicyTests::*`; `test_mate_clamp.py::BudgetForCarveOutTests::*`; `test_mate_clamp.py::SelectorTaskTests::*`; `test_mate_clamp.py::RequestedTaskTests::*`; `test_idle_pool_budget.py::RuleTests::*`; `test_sweep.py::InteriorRungRuleTests::*`; `test_multipv_policy.py::*`.
- **Determinism hazards.** Env switch `ATOMICDB_AUTO_INTERIOR_DEEP_RUNGS`.
- **History.** `b69ab74`, `d045ea8` (29-Jul); floor poll (Oct); interior cap (11-Nov audit).
- **Ordering.** Queue catalog.

### POS-4.1 terminal_status
- **Statement.** `terminal_status(fen)`: if `legal_moves(fen)` is non-empty and `_immediate_end(fen)` is False → None (not terminal). Otherwise `r = pyffish.game_result('atomic', fen, [])` (side-to-move POV): `r > 0` → side to move wins; `r < 0` → side to move loses; `r == 0` → `DRAW`; returns `(status, 'terminal')`. The halfmove clock is ignored (mate/explosion precede the fifty-move claim; ruleset `atomic-fide-claim-v1`).
- **Inputs / outputs.** Pure (pyffish).
- **Code.** `atomicdb/logic.py:terminal_status:290`.
- **Tests.** `test_clock_slack.py::ClosureSlackTests::test_a_terminal_closure_is_born_with_the_maximum`; `::test_a_terminal_draw_carries_no_slack`; `test_survive.py::SurvivalCertificateTests::test_the_claim_automaton_is_blind_to_the_clock`; `test_proofs.py::MovegenShortcutTests::*`.
- **Determinism hazards.** pyffish build (identity audited separately).
- **History.** Original; shortcut 23-Nov.
- **Ordering.** Used by every exact rule here.

### CAS-1.1 backup_status (three-valued minimax of one node)
- **Statement.** `backup_status(fen, expanded, child_statuses)`: if any child status is a win for the side to move → that win. If not `expanded` → `UNKNOWN`. If the list is non-empty and every child is a loss for the side to move → that loss. If non-empty and none is `UNKNOWN` → `DRAW`. Else `UNKNOWN`. (The cascade feeds it the fresh-context statuses of DTM-5.3; cascade catalog.)
- **Inputs / outputs.** Pure.
- **Code.** `atomicdb/logic.py:backup_status:708`.
- **Tests.** cascade catalog (`tests.py::DtmTests::*`, `test_clock_slack.py::FreshContextTests::*`).
- **Determinism hazards.** none.
- **History.** Original.
- **Ordering.** Interface to `backup_cascade`.

### CAS-1.2 backup_eval
- **Statement.** `backup_eval(fen, child_evals)`: drop None; empty → None; White to move → max; Black → min (White POV).
- **Inputs / outputs.** Pure.
- **Code.** `atomicdb/logic.py:backup_eval:725`.
- **Tests.** cascade catalog.
- **Determinism hazards.** none.
- **History.** Original.
- **Ordering.** Interface to the cascade.

### DTM-5.1 is_zeroing
- **Statement.** `is_zeroing(fen, uci)`: True iff the halfmove counter of `pyffish.get_fen(canonical_fen(fen), [uci])` (field 5) is `'0'` (capture, pawn move, promotion; on a canonical FEN the result is 0 or 1).
- **Inputs / outputs.** Pure.
- **Code.** `atomicdb/logic.py:is_zeroing:270`.
- **Tests.** `test_clock_slack.py::ZeroingTests::*` (2).
- **Determinism hazards.** none (pyffish).
- **History.** `8c04741`.
- **Ordering.** —

### DTM-5.2 edge_slack
- **Statement.** `edge_slack(fen, uci, child_slack)`: zeroing edge → 100 (`CLOCK_SLACK_MAX`); child slack None → 0; else `clamp(min(child_slack - 1, 100))` into [0, 100].
- **Inputs / outputs.** Pure.
- **Code.** `atomicdb/logic.py:edge_slack:630`.
- **Tests.** `test_clock_slack.py::SlackRecurrenceTests::test_a_zeroing_edge_restores_the_full_range`; `::test_an_unmeasured_child_is_treated_as_zero`.
- **Determinism hazards.** none.
- **History.** `8c04741`.
- **Ordering.** Used by DTM-5.4/5.5.

### DTM-5.3 edge_supports (fresh-context rule)
- **Statement.** `edge_supports(fen, uci, child_slack)`: zeroing edge → True; else True iff `child_slack is not None and child_slack >= 1`. The cascade (`_supporting_status`, cascade catalog) treats a decisive child that does not support as `UNKNOWN` when `ATOMICDB_FRESH_CONTEXT` is on (default). Known accepted residue: repetition (graph-history interaction) is not handled by this rule.
- **Inputs / outputs.** Pure.
- **Code.** `atomicdb/logic.py:edge_supports:647`; consumer `atomicdb/ingest.py:_supporting_status:2001`.
- **Tests.** `test_clock_slack.py::FreshContextTests::*` (4).
- **Determinism hazards.** Settings switch.
- **History.** `8c04741`.
- **Ordering.** Interface to the cascade.

### DTM-5.4 minimax_slack
- **Statement.** `minimax_slack(fen, winning_edges, all_edges, mover_wins)` with edges as `(uci, child_slack)`: mover wins → `max(edge_slack)` over winning edges (None if none); mover loses → `min(edge_slack)` over all edges (None if none). Draws carry no slack (callers pass nothing for draws).
- **Inputs / outputs.** Pure.
- **Code.** `atomicdb/logic.py:minimax_slack:676`; consumer `atomicdb/ingest.py:_minimax_slack:2023`.
- **Tests.** `test_clock_slack.py::SlackRecurrenceTests::test_minimax_win_takes_the_best_usable_winning_edge`; `::test_minimax_loss_takes_the_worst_of_every_edge`.
- **Determinism hazards.** none.
- **History.** `8c04741`.
- **Ordering.** Interface to the cascade.

### DTM-5.5 line_clock_slack
- **Statement.** `line_clock_slack(fens, pv, end_slack)`: walk back from the last ply: for index from `len(pv)-1` down to 0 with `fen = fens[index]`: if not `edge_supports(fen, pv[index], slack)` → None (the line holds for no entry clock); `slack = edge_slack(fen, pv[index], slack)`; `smallest = min(smallest, slack)`. Returns the smallest slack of the nodes before the end (every node of the line can inherit it), or None for an empty pv.
- **Inputs / outputs.** Pure.
- **Code.** `atomicdb/logic.py:line_clock_slack:438`.
- **Tests.** `test_closure_trust.py::TablebaseLineTests::test_a_quiet_move_into_the_ending_is_refused`. **Gaps:** no unit test.
- **Determinism hazards.** none.
- **History.** PR #223.
- **Ordering.** Used by TB-3.1.

### DTM-5.6 slack_from_run
- **Statement.** `slack_from_run(run)`: None → None (not measured); else `clamp(100 - run)`.
- **Inputs / outputs.** Pure.
- **Code.** `atomicdb/logic.py:slack_from_run:589`.
- **Tests.** `test_clock_slack.py::SlackRecurrenceTests::test_exhaustive_proof_uses_its_worst_reversible_run`.
- **Determinism hazards.** none.
- **History.** `8c04741`.
- **Ordering.** MPV-2.6, MPV-4.1, SOL-1.4.

### DTM-5.7 slack_from_witness_length
- **Statement.** `slack_from_witness_length(plies) = clamp(100 - max(0, plies))` (assumes every ply reversible: crude bound for ENGINE witnesses).
- **Inputs / outputs.** Pure.
- **Code.** `atomicdb/logic.py:slack_from_witness_length:606`.
- **Tests.** `test_clock_slack.py::SlackRecurrenceTests::test_uncertified_witness_uses_the_crude_length_bound`.
- **Determinism hazards.** none.
- **History.** `8c04741`.
- **Ordering.** MPV-2.6, MPV-4.1.

### DTM-5.8 slack_from_dtz
- **Statement.** `slack_from_dtz(dtz)`: None → 0; else `clamp(100 - (|dtz| + 1))`. The DTZ is accepted unverified (from the worker).
- **Inputs / outputs.** Pure.
- **Code.** `atomicdb/logic.py:slack_from_dtz:616`.
- **Tests.** `test_clock_slack.py::SlackRecurrenceTests::test_tablebase_needs_a_dtz_or_it_is_zero`; `test_clock_slack.py::TablebaseDtzTests::*` (3).
- **Determinism hazards.** none.
- **History.** `8c04741`.
- **Ordering.** TB-2.4.

### DTM-5.9 Slack clamp and constants
- **Statement.** `_clamp_slack(v) = max(0, min(100, int(v)))`; `CLOCK_SLACK_MAX = FIFTY_MOVE_PLIES = 100`. A TERMINAL closure is born with 100; draws carry NULL; NULL is read as 0 by consumers.
- **Inputs / outputs.** —
- **Code.** `atomicdb/logic.py:107-108`, `:_clamp_slack:602`.
- **Tests.** `test_clock_slack.py::SlackRecurrenceTests::test_terminal_takes_the_maximum`.
- **Determinism hazards.** none.
- **History.** `8c04741`.
- **Ordering.** —

---

# Part 3: Tablebases (TB)

### TB-1.1 Applicability
- **Statement.** `logic.tb_applicable(fen, max_men=6)`: piece count (letters of the board field, kings included) `<= max_men`, castling field `'-'` and en-passant field `'-'`. `tb.is_applicable(fen, max_pieces=6)` is the same test with exceptions (malformed FEN) → False. Canonical counters are 0, so WDL ±2 is decisive under the fifty-move rule and |WDL| <= 1 (cursed/blessed) is a practical draw.
- **Inputs / outputs.** Pure.
- **Code.** `atomicdb/logic.py:tb_applicable:740`, `:piece_count:736`; `atomicdb/tb.py:is_applicable:28`.
- **Tests.** `tests.py::TablebaseTests::test_applicability`; `test_tb.py::AtomicTablebaseTests::test_worker_compatible_applicability_guards`; `test_en_passant_identity.py::IdentityOfEverythingElseTests::test_a_phantom_position_becomes_tablebase_applicable`.
- **Determinism hazards.** none.
- **History.** Original; canonical v2 makes ghost e.p. positions applicable.
- **Ordering.** Gate of every TB rule.

### TB-1.2 probe_wdl (server probe)
- **Statement.** `tb.probe_wdl(fen, max_pieces=6)`: not applicable → None; the tablebase object is opened lazily once per process from `settings.ATOMICDB_TB_PATHS` (list, or `;`-separated string; empty entries dropped) with python-chess `chess.syzygy.open_tablebase(first, VariantBoard=AtomicBoard)` plus `add_directory` for the rest; an open failure is logged and cached as None until restart/`reset_cache()`; no paths → None. Probe: `tablebase.probe_wdl(AtomicBoard(fen))`, any exception → None. Returns the raw python-chess WDL integer (normally -2..2), side-to-move POV. The server never derives DTZ.
- **Inputs / outputs.** Files on disk; module-level cache with a lock.
- **Code.** `atomicdb/tb.py:probe_wdl:92`, `:_get_tablebase:73`, `:_configured_paths:66`, `:reset_cache:116`.
- **Tests.** `test_tb.py::AtomicTablebaseTests::*` (7).
- **Determinism hazards.** Files and settings; a failed open is cached for the process lifetime (fail-closed). Plan open question 5: python-chess vs shakmaty-syzygy answers must be compared.
- **History.** Original; 6-man tables deployed 2-Nov (PR #112).
- **Ordering.** Used by TB-2.x, TB-3.1, MPV-5.4.

### TB-1.3 wdl_to_status
- **Statement.** `wdl_to_status(wdl, stm_white)`: `wdl >= 2` → side to move wins; `wdl <= -2` → side to move loses; otherwise `DRAW`.
- **Inputs / outputs.** Pure.
- **Code.** `atomicdb/logic.py:wdl_to_status:750`.
- **Tests.** `tests.py::TablebaseTests::test_wdl_mapping`.
- **Determinism hazards.** none.
- **History.** Original.
- **Ordering.** —

### TB-2.1 Server table horizon (tb_server_max_men)
- **Statement.** `tb_server_max_men() = int(settings.ATOMICDB_TB_SERVER_MAX_MEN)` (settings default env `'5'`; 6 when the six-man WDL tables are installed). Positions up to this count are always re-probed by the server; above it (up to 6) only trusted submitters are believed (TB-2.2).
- **Inputs / outputs.** Setting.
- **Code.** `atomicdb/ingest.py:tb_server_max_men:10301`; `OpenSite/settings.py:124`.
- **Tests.** `test_tb_integration.py::TablebaseClosureTrustTests::test_six_piece_result_is_reprobed_with_six_piece_tables`; `::test_server_tables_leave_positions_beyond_them_alone`.
- **Determinism hazards.** Deployment setting (production value must be captured).
- **History.** PR #112 (2-Nov), PR #116 (3-Nov), PR #223.
- **Ordering.** —

### TB-2.2 prepare_tb_closure (validation of a worker WDL)
- **Statement.** `prepare_tb_closure(position_key, wdl, user=None, dtz=None)`, outside the write transaction: position missing → None. Position not `UNKNOWN` or not `tb_applicable(fen)` (6 men) → None silently. `wdl = int(wdl)` (failure → 99); not in {-2,-1,0,1,2} → `TB_REJECTED` reason `invalid-wdl` (payload `worker_wdl`), None. `men <= server_max`: `server_wdl = probe_wdl(fen, max_pieces=server_max)`; None → `TB_REJECTED` `server-probe-unavailable`; different from wdl → `TB_REJECTED` `wdl-mismatch` (payloads `worker_wdl`, `server_wdl`); either → None. `men > server_max`: accept only if `user.is_staff` or `user.username in settings.ATOMICDB_TB_TRUSTED` (settings default env `'belzedar'`), else `TB_REJECTED` `untrusted-six-piece` (payload `worker_wdl`, `username`), None. `dtz = int(dtz)` or None on failure. Returns `{'key', 'fen', 'wdl', 'dtz'}`. Every `TB_REJECTED` payload also has `key` and `reason`.
- **Inputs / outputs.** DBEvent `TB_REJECTED`.
- **Code.** `atomicdb/ingest.py:prepare_tb_closure:10306`, `:_tb_rejected:10296`.
- **Tests.** `test_tb_integration.py::TablebaseClosureTrustTests::*` (7); `tests.py::TablebaseTests::test_close_by_tb`; `tests.py::TablebaseTests::test_close_by_tb_rejects_castling`; `test_tb.py::AtomicTablebaseTests::test_trusted_default_contains_owner`; `test_fencing.py::SubmitFencingTests::test_rejected_tb_does_not_account_elapsed_time`. **Gaps:** `invalid-wdl` and `server-probe-unavailable` have no direct test.
- **Determinism hazards.** Tablebase files/settings; the trusted list is a setting.
- **History.** Original (`c87c95c` trust hardening, 21-Jul); PR #112, #223.
- **Ordering.** Before TB-2.4.

### TB-2.3 Server-side TB closure of a searched position
- **Statement.** `server_tables_wdl(fen)`: `tb_applicable(fen, server_max)` else None; then `probe_wdl(fen, max_pieces=server_max)`. `prepare_server_tb_closure(key, fen)`: `wdl = server_tables_wdl(fen)`; None → None; else `prepare_tb_closure(key, wdl)` (no user: always within server tables).
- **Inputs / outputs.** As TB-2.2.
- **Code.** `atomicdb/ingest.py:server_tables_wdl:10373`, `:prepare_server_tb_closure:10358`.
- **Tests.** `test_tb_integration.py::TablebaseClosureTrustTests::test_server_tables_settle_a_searched_six_piece_position`; `test_tb_on_pick.py::TablesSettleQueuedPositionsTests::*`.
- **Determinism hazards.** Files/settings.
- **History.** PR #112 (2-Nov).
- **Ordering.** TB-2.5, TB-5.1, TB-3.1.

### TB-2.4 _apply_prepared_tb (the closure write)
- **Statement.** `_apply_prepared_tb(key, prepared)`: None or key mismatch → False. In a (nested) atomic block, lock the position; if it is no longer `UNKNOWN`, its FEN differs from `prepared['fen']`, or it is not `tb_applicable` → False. Else `status = wdl_to_status(wdl, stm)`, `closure = 'TB'`, `clock_slack = NULL` for `DRAW` else `slack_from_dtz(dtz)`; save `status, closure, clock_slack, updated` (proof, mate_in, won_line, best_move are NOT touched); `_emit_closure_events(pos, dtz=dtz, dtz_verified=False, clock_slack=...)`. After the inner block: `backup_cascade([key])`, `backup_backed_evals([key])`, `_recheck_once_committed(key, [key], [key, *cascade changed])` (cascade/backed/recheck catalogs). Returns True. `close_by_tb(key, wdl, user, dtz)` = prepare then apply.
- **Inputs / outputs.** Position closure columns; events; cascade.
- **Code.** `atomicdb/ingest.py:_apply_prepared_tb:10387`, `:close_by_tb:10418`.
- **Tests.** `test_clock_slack.py::TablebaseDtzTests::*` (3); `test_parent_recheck.py::ClosureRaceTests::test_a_tb_landing_rechecks_its_parent`; `test_adversarial_explorer.py::ClosureAttributionTests::test_a_closure_nobody_asked_for_says_so`.
- **Determinism hazards.** `updated` wall clock; DTZ unverified (worker word).
- **History.** Original; DTZ slack `8c04741`.
- **Ordering.** Inside apply_job's transaction (TB-2.5) or TB-5.1; cascade after.

### TB-2.5 apply_job routing of TB results
- **Statement.** In `ingest_queue.apply_job`, before the transaction: if the payload has `tb_wdl` → `prepare_tb_closure(job.position, tb_wdl, dtz=tb_dtz, user=submitting user)`; a None result marks the job `rejected` (the lines of the search are NOT ingested at all: no eval, no seeds, no mate closures). Else `prepare_server_tb_closure(position, fen)`; if it returns a closure the search lines are ignored and the TB closure applies; otherwise lines are normalised (SB-8) and `prepare_mate_proofs` runs. In the transaction: rejected → summary `{'tb_rejected': True}`; prepared → `_apply_prepared_tb` (False also reports `tb_rejected`); else `ingest_analysis`. Elapsed engine time is only credited when not rejected. After commit, a non-deferred job refreshes its position's proof numbers (PN-8.4).
- **Inputs / outputs.** Job summary; Position writes via TB-2.4 or ingest.
- **Code.** `atomicdb/ingest_queue.py:apply_job:240-341`.
- **Tests.** `test_fencing.py::SubmitPreparationBoundaryTests::test_tb_probe_runs_outside_atomic_block`; `test_fencing.py::SubmitFencingTests::test_rejected_tb_does_not_account_elapsed_time`; `test_tb_integration.py::TablebaseClosureTrustTests::test_false_five_piece_result_is_rejected`.
- **Determinism hazards.** Files/settings; order "server tables before mate proofs".
- **History.** PR #112 (server tables), PR #116.
- **Ordering.** Before MPV-2.4.

### TB-3.1 prepare_tb_line (line into a won ending)
- **Statement.** `prepare_tb_line(parent_fen, line, legal=None, deadline=None)` → `(winner_white, pv[1:], 'TB_LINE', slack)` or None. Requirements: `line['mate']` is None; `eval_cp` is an int (not bool) with `|eval_cp| >= 9000` (`MATE_BAND`, a decided claim without mate distance); `pv` a list of length >= 2 with `pv[0] == move`; `move` legal in the parent. `fens = walk_pv(parent_fen, pv, deadline)` (None → None). End = last FEN: if terminal, or not `tb_applicable(end, tb_server_max_men())` → None. `wdl = server_tables_wdl(end)`; None → None. `winner_white = eval_cp > 0`; claimed status vs `wdl_to_status(wdl, end stm)`: different → `TB_LINE_REJECTED` (`reason 'wdl-contradicts-claim'`, `key` = parent key, `move`, `claimed`, `server_wdl`), None. `slack = line_clock_slack(fens[1:], pv[1:], 0)` (end slack 0: a bare WDL holds from a fresh counter only); None → `TB_LINE_REJECTED` (`'fifty-move-margin'`, key, move, `plies = len(pv)`), None. Closes like an uncertified mate line (MPV-2.6, grade TB_LINE, mate_in NULL), subject to the trust gate.
- **Inputs / outputs.** DBEvent `TB_LINE_REJECTED`.
- **Code.** `atomicdb/ingest.py:prepare_tb_line:853`, `:_tb_line_rejected:848`.
- **Tests.** `test_closure_trust.py::TablebaseLineTests::*` (7).
- **Determinism hazards.** Wall-clock deadline (shared with MPV-2.4); tables/settings.
- **History.** PR #223 (11-Nov).
- **Ordering.** Step 3 of MPV-2.4.

### TB-4.1 recompute_tb_distance command
- **Statement.** One-off rewrite of stored values to the 24-Nov scale from the raw lines. Candidates: open positions with `9000 <= |eval_cp| <= 9500` (by `--keys/--keys-file`, or a scan by physical block/rowid ranges with a resumable cursor kept in `TB_DISTANCE_RECOMPUTE` events). Per row: **own search** (`nodes_invested` or non-empty `last_analysis`): `plan_own` rewrites every dict line (current and prior pass) with SB-6; `old_best` = best current line by legacy value (SB-5 second value when the line's eval is one of the pair), `new_best` = best renewed current line (first of equals, best for the side to move); if `row.eval_cp in (old_best, new_best)` and `new_best != row.eval_cp` → `eval_cp = new_best`, and `best_move = new_move` when it differs and `row.best_move in (old_move, None)`. **Seed** (no own search, `backed_nodes == 0`): for each parent edge into it, parent budget = `_budget` of the parent's first current line (0 if absent); each parent line with the edge's move whose raw values contain `row.eval_cp` gives `(is_current_pass, budget, new_value)`; `eval_cp = max(...)[2]` (current pass first, then larger budget, then larger value) if different. Rows with backing but no own search are skipped. `analysis_passes` is never rewritten. Writes (with `--apply`, default dry run): per chunk of 200 in one transaction, rows locked, skipped if status or `eval_cp` changed meanwhile (seeds also if now searched/backed; own rows re-planned on the locked row). Then the parents of written keys are climbed with `backup_backed_evals_retrying(open_only=True)` in batches of 50 (no status cascade). Summary event `TB_DISTANCE_RECOMPUTE`.
- **Inputs / outputs.** `Position.last_analysis/eval_cp/best_move/updated`; backed values via the backed catalog; DBEvent.
- **Code.** `management/commands/recompute_tb_distance.py:plan_own:103`, `:plan_seed:142`, `Command._plan:283`, `Command._write:325`, `Command._climb:360`.
- **Tests.** `test_tb_distance.py::RecomputeCommandTests::*` (4).
- **Determinism hazards.** Physical scan order (ctid/rowid), `--max-seconds` wall-clock valve (default 3000 s), statement/lock timeouts, concurrent ingest.
- **History.** `7a6f5d3` (PR #312, 25-Nov).
- **Ordering.** Manual, after the SB change.

### TB-5.1 close_queued_by_tables (TB sweep)
- **Statement.** Run by the ingest processor every `TB_SWEEP_SECONDS = 30` s: iterate PENDING `AnalysisTask` rows of open positions by task id; collect positions with `tb_applicable(fen, server_max)`, at most `TB_SWEEP_LIMIT = 200` distinct positions (all their tasks kept); for each position in collection order: `prepare_server_tb_closure`; if prepared, in one transaction under `closure_attribution(source of the first task)` apply TB-2.4 and notify each task's requester. The lease never serves a task whose position the server tables cover.
- **Inputs / outputs.** Closures; notifications.
- **Code.** `atomicdb/ingest_queue.py:close_queued_by_tables:455`; `management/commands/process_ingest_queue.py:79`.
- **Tests.** `test_tb_on_pick.py::TablesSettleQueuedPositionsTests::*` (8).
- **Determinism hazards.** Wall-clock period; task id order.
- **History.** PR #116 (3-Nov).
- **Ordering.** Independent sweep.

---

# Part 4: Solver and survival certificates (SOL)

### SOL-1.1 Certificate container (gzip, size limits)
- **Statement.** Certificates travel and are stored gzip-compressed (`compress`: level 6). `decompress(blob, max_bytes=64 MiB)`: None → error "empty"; `len(blob) > 4 MiB` (`MAX_COMPRESSED_BYTES`) → error; gunzip reading at most `max_bytes + 1` bytes (invalid gzip → error); more than `max_bytes` → error; UTF-8 decode (failure → error). The submit endpoint refuses uploads over 4 MiB with HTTP 413 before parsing. Survival certificates use the same container and limits.
- **Inputs / outputs.** Pure. Errors are `CertificateError` (message strings).
- **Code.** `atomicdb/solve.py:compress:72`, `:decompress:79`; `atomicdb/views.py:1909`, `views.py:2058-2062`.
- **Tests.** `test_solve.py::CertificateVerifierTests::test_a_decompression_bomb_is_refused`; `::test_garbage_is_not_gzip`; `test_solve.py::SolveProtocolTests::test_an_oversized_certificate_is_refused_before_parsing`; `test_survive.py::SurvivalCertificateTests::test_round_trips_through_gzip`.
- **Determinism hazards.** none.
- **History.** P1c (July).
- **Ordering.** Before SOL-1.2 / SOL-3.1.

### SOL-1.2 SOLVE certificate format and header checks
- **Statement.** Text, `\n` lines. Line 1 (stripped) must be `# atomicdb-proof/1`. Header lines from index 1 to 31 (`MAX_HEADER_LINES = 32`): blank skipped; `---` ends the header; otherwise `name value` split at the first space (missing name or value → error; repeated names: last wins); no `---` within the window → error. Checks: `ruleset == 'atomic-fide-claim-v1'`; `goal` in {WHITE_WIN, BLACK_WIN}; equals the expected goal if given; `root` present; `canonical_fen(root) == canonical_fen(expected root)` if given. Optional `nodes` checked after replay.
- **Inputs / outputs.** Pure.
- **Code.** `atomicdb/solve.py:parse_header:128`, `:verify_certificate:159-183`.
- **Tests.** `test_solve.py::CertificateVerifierTests::test_an_unknown_format_is_refused`; `::test_a_foreign_ruleset_is_rejected`; `::test_a_certificate_for_another_goal_is_rejected`; `::test_a_certificate_for_another_position_is_rejected`; `::test_a_wrong_node_count_is_rejected`.
- **Determinism hazards.** none.
- **History.** P1c.
- **Ordering.** Before SOL-1.3.

### SOL-1.3 SOLVE certificate replay
- **Statement.** DFS preorder with an explicit stack, starting from `(root as written in the header, counters included; path = {canonical(root)}; run 0; depth 0)`. Pop a node: `deepest = max(deepest, depth)`; `depth > 512` (`MAX_DEPTH`) → error; `nodes += 1`, `> 2,000,000` (`MAX_NODES`) → error; read the next non-blank line (none → "ended before its tree did"). Kind `T`: `terminal_status(fen)` must exist and equal the goal; `worst = max(worst, run)`. Any other kind is an interior node: `terminal_status(fen)` must be None; halfmove counter (FEN field 5, default 0) `>= 100` → error (fifty-move horizon before the goal); attacker to move iff `(stm == White) == (goal == WHITE_WIN)`; `legal = logic.legal_moves(fen)`. `O <move>`: must be attacker to move; move legal; child = `pyffish.get_fen(fen, [move])` (counters kept); child run = 0 if the child's halfmove counter is 0 else `run + 1`; `canonical(child) in path` → error; push. `A <n> <m1..mn>`: must be defender to move; `n` an int equal to the number of moves; `n > 256` (`MAX_FANOUT`) → error; duplicates → error; `set(moves) == set(legal)` exactly (missing/extra listed in the error); each child computed and checked for repetition; pushed in reverse so they pop in listed order. Unknown kind → error. After the stack empties: any remaining non-blank line → error; declared `nodes` must equal the count.
- **Inputs / outputs.** Pure (pyffish).
- **Code.** `atomicdb/solve.py:verify_certificate:185-309`, `:_advance:321`, `:_fifty_move_counter:147`.
- **Tests.** `test_solve.py::CertificateVerifierTests::*` (21: e.g. `test_a_real_certificate_verifies`, `test_a_deep_certificate_with_real_and_branching_verifies`, `test_an_incomplete_and_node_is_rejected`, `test_an_and_node_with_an_extra_move_is_rejected`, `test_a_duplicated_reply_is_rejected`, `test_swapping_or_and_and_is_rejected`, `test_a_repetition_inside_the_proof_is_rejected`, `test_a_non_terminal_leaf_is_rejected`, `test_an_illegal_witness_move_is_rejected`, `test_a_truncated_certificate_is_rejected`, `test_trailing_content_is_rejected`, `test_the_depth_limit_is_enforced`, `test_the_fanout_limit_is_enforced`, `test_the_node_limit_is_enforced`).
- **Determinism hazards.** pyffish move generator (a different generator may disagree on legal sets: the design intends an independent lineage).
- **History.** P1c (July).
- **Ordering.** After SOL-1.2.

### SOL-1.4 SOLVE certificate report and slack
- **Statement.** Report: `goal`, `root` (as written), `nodes`, `depth` (deepest), `worst_reversible_run` (max run over all `T` leaves), `clock_slack = slack_from_run(worst)` (never None).
- **Inputs / outputs.** Pure.
- **Code.** `atomicdb/solve.py:verify_certificate:311-318`.
- **Tests.** `test_solve.py::CertificateVerifierTests::test_a_real_certificate_verifies`.
- **Determinism hazards.** none.
- **History.** P1c.
- **Ordering.** Feeds SOL-2.2/2.3.

### SOL-2.1 apply_solve_result (state machine)
- **Statement.** `apply_solve_result(task, outcome, certificate_blob, advisory_pn, advisory_dn, searched_nodes, elapsed_seconds, solver_build, telemetry, trusted_submitter)`:
  1. `elapsed = clamp(float(elapsed_seconds or 0), 0, 86_400)` (bad input → 0).
  2. Outside the transaction: for `PROVED` → `solve.verify_certificate(decompress(blob), root_fen=task.position.fen, goal=task.goal)`; for `DISPROVED_WHITE_WIN` → `survive.verify_certificate_auto(decompress(blob), root_fen=task.position.fen)`; no blob → reason `"<outcome> without a certificate"`; `CertificateError` → its message; any other exception → `"<Type>: <msg>"`. `verify_seconds` measured (wall clock, telemetry only).
  3. In a transaction under closure source `SOLVE`: lock the SolveTask; already `COMPLETED` → return `{'dup': True}` (nothing written). Set `outcome, advisory_pn, advisory_dn, searched_nodes = max(0, int)`, `elapsed_seconds`, `solver_build[:64]`, `telemetry` (if truthy), `completed = now`.
  4. PROVED/survival not verified → `state = FAILED`, `verified = False`, `reject_reason = reason[:2000]`, save, `DBEvent('SOLVE_REJECTED', {task, key, reason[:500], machine})`, return `{'rejected': True, 'reason': reason[:200]}`. The tree is not touched.
  5. Else `state = COMPLETED`, `verified`, `reject_reason = ''`; if verified store `certificate` (blob), `certificate_bytes`, `certificate_nodes` (survival: `states`; else `nodes`), `certificate_format` (`atomic-survival-threshold-v1` or `atomicdb-proof/1`), and for survival `survival_tau = root_tau`, `survival_states`. Save (full row).
  6. Verified survival → SOL-3.4 (event only). Verified proof → `SOLVE_VERIFIED` event `{task, key, seconds, nodes, depth, bytes, solver_seconds}`; position open → SOL-2.2; else SOL-2.3. `DISPROVED` (unverified by nature) → SOL-2.4. `UNKNOWN` → nothing more.
  7. SOL-2.5. Return `{'verified', 'closed', 'upgraded', 'disputed', 'certificate_nodes'}`.
- **Inputs / outputs.** `SolveTask.*`; Position via SOL-2.2..2.4; DBEvents `SOLVE_REJECTED`, `SOLVE_VERIFIED`, `SURVIVE_VERIFIED`.
- **Code.** `atomicdb/ingest.py:apply_solve_result:8723`.
- **Tests.** `test_solve.py::SolveProtocolTests::*` (incl. `test_acquire_leases_and_submit_closes`, `test_a_false_certificate_fails_the_task_and_mutates_nothing`, `test_proved_without_a_certificate_is_refused`, `test_unknown_is_recorded_and_closes_nothing`, `test_a_second_submit_is_a_duplicate_not_a_reclosure`); `test_engine_debt.py::TelemetryIsAdvisoryTests::*`; `test_survive_fleet.py::SurvivalSubmissionTests::*`.
- **Determinism hazards.** `completed = timezone.now()`; verification time; native verifier subprocess timeout (SOL-3.3) can turn a valid survival certificate into FAILED.
- **History.** P1c (July); survival `3bdc095` (28-Jul).
- **Ordering.** Called by the submit view (SOL-2.6).

### SOL-2.2 Closing an open position by certificate
- **Statement.** `_close_by_certificate(task, report)`: lock the position; not `UNKNOWN` → False. `status = task.goal`, `closure = 'SOLVE'`, `proof = 'ANDOR'`, `clock_slack = report['clock_slack']`; save those + `updated` (mate_in, won_line, best_move untouched); `_emit_closure_events(pos, certificate_nodes, depth, clock_slack, task)`.
- **Inputs / outputs.** Position closure columns; events.
- **Code.** `atomicdb/ingest.py:_close_by_certificate:10279`.
- **Tests.** `test_engine_debt.py::UpgradeNotRecloseTests::test_an_open_position_still_takes_the_closing_path`; `test_autosolve.py::ServingTests::test_a_verified_certificate_closes_the_position`; `test_adversarial_explorer.py::ClosureAttributionTests::test_a_certificate_closure_is_stamped_solve`; `::test_a_certificate_closure_emits_one_event_not_two`.
- **Determinism hazards.** `updated` wall clock.
- **History.** P1c.
- **Ordering.** Inside SOL-2.1.

### SOL-2.3 Upgrading a closed position by certificate
- **Statement.** `_upgrade_by_certificate(task, report)`: lock; status is not `task.goal` (or is UNKNOWN) → False; already `proof == 'ANDOR'` → False; else `proof = 'ANDOR'`, `clock_slack = max(stored or 0, report clock_slack)` when the report has one; save `proof, clock_slack, updated`; `DBEvent('PROOF_UPGRADED', {key, status, closure, certificate_nodes, clock_slack, task})`. Status and closure kind are unchanged (any closure kind with a non-ANDOR proof qualifies, e.g. MATE_PV ENGINE, MINIMAX, TB with NULL proof).
- **Inputs / outputs.** `Position.proof/clock_slack/updated`; DBEvent.
- **Code.** `atomicdb/ingest.py:_upgrade_by_certificate:10214`.
- **Tests.** `test_engine_debt.py::UpgradeNotRecloseTests::*` (4).
- **Determinism hazards.** none beyond `updated`.
- **History.** `204d17e` (28-Jul).
- **Ordering.** Inside SOL-2.1.

### SOL-2.4 Solver DISPROVED on a closed position
- **Statement.** `_dispute_from_solver(task, trusted)`: lock; status is not `task.goal` (including any UNKNOWN position) → False with NO event; independent closure (`closure in (TERMINAL, TB)` or `closure != MINIMAX and proof == ANDOR`) → False; else `DBEvent('SOLVE_DISPUTE_SIGNAL', {key, status, closure, proof, task, trusted, machine})`; untrusted → False; trusted → `revoke_closure(key, reason='solver-disproved-the-goal', mark_disputed=True)` (revocation catalog), True. A DISPROVED carries no certificate.
- **Inputs / outputs.** DBEvent; revocation.
- **Code.** `atomicdb/ingest.py:_dispute_from_solver:10243`, `:_closure_is_independent:5420`.
- **Tests.** `test_engine_debt.py::DisputeFromSolverTests::*` (4); `test_adversarial_explorer.py::FragileMateClaimTests::test_a_disproved_answer_still_closes_nothing`.
- **Determinism hazards.** Trust from settings (SOL-2.6).
- **History.** `7a592e9` (28-Jul).
- **Ordering.** Inside SOL-2.1.

### SOL-2.5 Post-result cascade and attribution
- **Statement.** After the SOL-2.1 transaction, if `verified or disputed`: under `closure_attribution('SOLVE')` run `backup_cascade([task.position])` and `backup_backed_evals([task.position])` (cascade/backed catalogs; this also refreshes proof numbers, PN-8.1). Runs for verified survival results too (nothing changed, harmless).
- **Inputs / outputs.** Interface.
- **Code.** `atomicdb/ingest.py:apply_solve_result:8845-8851`.
- **Tests.** `test_solve.py::SolveClosurePropagationTests::test_a_solve_closure_backs_up_like_any_other`.
- **Determinism hazards.** As the cascade.
- **History.** `7a592e9`.
- **Ordering.** Last step of SOL-2.1.

### SOL-2.6 Submit endpoint: accepted outcomes and trust
- **Statement.** `POST /atomicdb/api/solve/submit` (`views.api_solve_submit`): authenticated account; `outcome` must be one of `PROVED`, `DISPROVED`, `UNKNOWN` (anything else, **including `DISPROVED_WHITE_WIN`**, is 400 malformed); COMPLETED task → `{'ok': True, 'dup': True}`; task must be LEASED to the same `machine` with a matching `lease_token`; certificate file read up to 4 MiB (+1 → 413). Fortress telemetry fields `fortress_{tt_hit,quiet_scc,reset_rate,stagnation,score}` are parsed as floats clamped to [0, 1e9] and stored without the prefix. `trusted_submitter = user.is_staff or username in ATOMICDB_TB_TRUSTED`.
- **Inputs / outputs.** HTTP; calls SOL-2.1.
- **Code.** `atomicdb/views.py:api_solve_submit:2029`.
- **Tests.** `test_solve.py::SolveProtocolTests::*`; `test_engine_debt.py::TelemetryIsAdvisoryTests::*`; `test_survive_fleet.py::FortressClassifierTests::test_it_fires_on_telemetry_that_travelled_the_real_wire`. **Gaps:** survival results reach `apply_solve_result` only from tests, never through this endpoint.
- **Determinism hazards.** none.
- **History.** P1c.
- **Ordering.** Queue/lease catalog owns the protocol.

### SOL-3.1 Survival certificate format and header
- **Statement.** First line `# atomic-survival-threshold-v1`; header (same parser rules as SOL-1.2, stable rejection codes) must have `ruleset atomic-fide-claim-v1`, `repetition NO_REPETITION_SHORTCUTS`, `terminal_precedence terminal-before-clock/1`, `canonical 2` (`CANONICAL_VERSION`), `root` (canonical FEN must match the expected root if given), `entry_clock` an int in [0, 100] equal to `min(halfmove counter of the expected root (or of the header root when none is given), 100)`; optional `states`, `edges` counts. Body: `S <id> <tau> <canonical fen>` records first (ids 0.. contiguous, tau in [0,100], distinct FENs, at most 20,000 states), then `W <id> <move> <target>` (White to move: one per legal move, fan-out <= 256) and `B <id> <move> <target>` (exactly one per Black state); target `#<id>` or `T`; at most 200,000 edges; a state after an edge is an error.
- **Inputs / outputs.** Pure. Errors carry `code` strings (e.g. `entry-clock-mismatch`, `quiet-tau-inequality`).
- **Code.** `atomicdb/survive.py:parse_header:349`, `:_check_header:368`, `:_parse_body:415`, `:_resolve:492`.
- **Tests.** `test_survive.py::SurvivalCertificateTests::*` (format/header/record tests); `test_survive_native.py::NativeDifferentialTests::*`.
- **Determinism hazards.** On the server the root FEN is canonical (`0 1`), so `entry_clock` must be 0 and only certificates with `tau(root) = 0` can verify through `apply_solve_result`.
- **History.** `a20609a` (28-Jul).
- **Ordering.** Before SOL-3.2.

### SOL-3.2 Survival certificate checks
- **Statement.** A movegen budget counts pyffish constructions (`MAX_POSITIONS = 200,000`; `legal_moves` 1, `advance` 1, `canonical` 2 extra only when the FEN declares e.p., `terminal_status` 3); exceeding it → `budget-exceeded`. Pass 1, per state: FEN must equal its canonical form; not terminal; White state: no Black reply, listed moves without duplicates and exactly equal to the legal set; Black state: no White moves, a reply that is legal. Pass 2, per edge: `child = get_fen(fen, [move])`; zeroing iff child halfmove == 0; target `T` → child must be terminal and not `WHITE_WIN`; target state → `canonical(child)` must equal the state's FEN; zeroing edge → `tau(child) == 0`; quiet edge → `tau(child) <= tau(parent) + 1`. Root (canonical) must be a state with `tau(root) <= entry_clock`. Report: `result DISPROVED_WHITE_WIN`, root, entry_clock, root_tau, states, edges, reachable (DFS from root), zeroing_edges, terminal_exits, max_tau, positions. Repeated positions are allowed (fortress).
- **Inputs / outputs.** Pure.
- **Code.** `atomicdb/survive.py:_Movegen:307`, `:verify_certificate:507`.
- **Tests.** `test_survive.py::SurvivalCertificateTests::*` (~33); `test_survive_mined.py::MinedCertificateTests::*`.
- **Determinism hazards.** Budget counted in calls (deterministic); pyffish.
- **History.** `a20609a`.
- **Ordering.** After SOL-3.1.

### SOL-3.3 Survival verifier routing (native vs reference)
- **Statement.** `verify_certificate_auto(text, root_fen)`: if the native binary `tools/survive50-verify/survive50-verify[.exe]` exists and the header's declared `states >= 100` (`NATIVE_VERIFIER_STATE_THRESHOLD`; header untrusted for routing only) → run it as a subprocess with `--budget max_positions` (and `--root`), timeout 120 s; the last stdout line is JSON; `ok` false → `CertificateError(message, code)`; timeout or no output → `CertificateError`. Else the reference verifier; report gets `verifier` and, for the reference, `native_unavailable`. Both must reject with the same code.
- **Inputs / outputs.** Subprocess, temp file.
- **Code.** `atomicdb/survive.py:verify_certificate_auto:744`, `:verify_certificate_native:705`, `:declared_states:687`.
- **Tests.** `test_survive_fleet.py::VerifierRoutingTests::*` (4); `test_survive_native.py::NativeDifferentialTests::*` (5).
- **Determinism hazards.** File presence of the binary; wall-clock timeout (120 s) can reject a valid certificate.
- **History.** `3bdc095`.
- **Ordering.** Inside SOL-2.1.

### SOL-3.4 A verified survival never closes a position
- **Statement.** A verified `DISPROVED_WHITE_WIN` refutes only the boolean goal WHITE_WIN (compatible with a draw and with a Black win). It never writes `Position.status`, closure or proof: the fact is stored on the SolveTask (SOL-2.1 step 5) and in `DBEvent('SURVIVE_VERIFIED', {task, key, seconds, verifier, tau, entry_clock, states, edges, reachable, positions, bytes, stage, solver_seconds})`.
- **Inputs / outputs.** SolveTask, DBEvent only.
- **Code.** `atomicdb/ingest.py:apply_solve_result:8804-8824`.
- **Tests.** `test_survive_fleet.py::SurvivalSubmissionTests::test_a_verified_survival_does_not_close_the_position`; `::test_a_verified_survival_is_stored_as_its_own_typed_result`; `::test_it_lands_in_the_event_log_for_the_orchestrator`; `test_survive_mined.py::MinedCertificateTests::test_a_mined_certificate_is_still_only_a_disproof`.
- **Determinism hazards.** none.
- **History.** `3bdc095`.
- **Ordering.** Inside SOL-2.1.

### SOL-3.5 Fortress classifier (scheduling only)
- **Statement.** `fortress_indicators(telemetry)` (stored keys `tt_hit, quiet_scc, reset_rate, stagnation`): `tt_hit >= 0.65`, `quiet_scc >= 0.50`, `few_resets: reset_rate <= 0.05`, `stagnant: 0 < stagnation < 2.0`; missing/non-numeric → False. `fortress_suspected` = at least 3 of 4. Never authorises a result. Stage budgets `F0 2M, F1 5M, F2 20M, F3 50M, F4 100M`.
- **Inputs / outputs.** Pure.
- **Code.** `atomicdb/survive.py:fortress_indicators:255`, `:fortress_suspected:284`, `:STAGE_BUDGETS:223`.
- **Tests.** `test_survive_fleet.py::FortressClassifierTests::*` (6); `test_engine_debt.py::TelemetryIsAdvisoryTests::test_a_perfect_fortress_score_still_closes_nothing`.
- **Determinism hazards.** none.
- **History.** `3bdc095`.
- **Ordering.** —

### SOL-4.1 Autosolve: what a showcase claims
- **Statement.** `mate_goal(line)`: `eval_cp` must be an int (not bool) with `9500 < |eval_cp| < 10000` (an engine mate; TB band and ±10000 excluded); a non-zero int `mate` whose sign contradicts `eval_cp` → None; goal = `WHITE_WIN` if `eval_cp > 0` else `BLACK_WIN`. `claims(last_analysis)`: current lines (dict, no `prior_pass`); none → `(None, [])`; own goal = `mate_goal(first current line)`; moves = for each current line with a goal and a string move not seen yet, `(move, goal)` ordered by `|eval_cp|` descending (shortest mate first; stable for ties).
- **Inputs / outputs.** Pure.
- **Code.** `atomicdb/autosolve.py:mate_goal:113`, `:claims:131`.
- **Tests.** `test_autosolve.py::MateRuleTests::*` (7).
- **Determinism hazards.** none.
- **History.** PR #101 (29-Oct).
- **Ordering.** SOL-4.2, SOL-4.3.

### SOL-4.2 Autosolve at landing
- **Statement.** `queue_from_landing(pos)` (end of every landing's stage b; failures logged, never propagated): arm rate `ATOMICDB_ARM_RATE_AUTOSOLVE` (default 60/h) `<= 0` → 0. Claims of `pos.last_analysis` (as stage a left it); nothing → 0 (no query). In a savepoint: children = edges `(pos, move)` for claimed moves; `alive` = those among pos and children with `status UNKNOWN` and `priority > DEAD/2`; targets: pos first (if alive and own goal), then each claimed child (alive, not pos, not already listed) with its goal; only while pos itself is alive. Drop targets already asked (`_already_asked`: any SolveTask of arm `autosolve95` in any state; any PENDING/LEASED SolveTask of any arm; a COMPLETED one of any arm with `budget_nodes >= 8M` and the same goal). Room = `min(32 - pending autosolve PENDING, hourly room)`; mint `SolveTask(goal, stage F0, budget 8M, arm autosolve95)` for the first `room` targets (no uniqueness constraint); event `AUTOSOLVE95_ENQUEUED {queued, own, child, keys, pending_before, cap, via='landing', key}`.
- **Inputs / outputs.** SolveTask inserts; DBEvent.
- **Code.** `atomicdb/autosolve.py:queue_from_landing:263`, `:_targets:177`, `:_already_asked:196`, `:_room:222`, `:_mint:237`.
- **Tests.** `test_autosolve.py::LandingTests::*`; `::DirectLandingTests::*`; `::NeverAgainTests::*`; `::CapAndRateTests::*`; `::FailureContainmentTests::*`.
- **Determinism hazards.** Hourly arm window (wall clock); concurrent minting may duplicate (accepted).
- **History.** PR #101.
- **Ordering.** After the climb (propagate_landings).

### SOL-4.3 Autosolve backlog walk
- **Statement.** `drip_backlog(target=16, scan=200, window=2000)` (one step per selector cycle when `--autosolve-cap > 0`): target capped at 16; room = `_room(target, reserve = rate - rate//2)` (half the hourly ceiling kept for landings). Cursor from `ATOMICDB_STATE_DIR/autosolve.json` (loaded once per process; unreadable → top); None → top = max task id + 1. Read up to `scan` COMPLETED AnalysisTasks with `floor <= id < cursor` (`floor = max(0, cursor - window)`), newest first; resume = last id read if `scan` rows were read, else `floor`. Claims of the still-open positions; targets per task in order (the newest search of each position only); a position claimed twice keeps the first goal; skip already asked; stop when the batch reaches `room` (resume = that task id + 1). Mint; event with `via='backlog'`, `cursor`, `scanned`. Save cursor (`resume` if > 0 else None, which restarts from the newest).
- **Inputs / outputs.** SolveTask inserts; state file; DBEvent.
- **Code.** `atomicdb/autosolve.py:drip_backlog:380`, `:_backlog_state:334`, `:_save_backlog:355`.
- **Tests.** `test_autosolve.py::BacklogTests::*` (14); `::BacklogStateTests::*` (4); `::SelectorStepTests::*`; `test_selector_service.py::RefreshSelectorCommandTests::test_autosolve_cap_is_forwarded_and_reported`.
- **Determinism hazards.** A file on disk holds the cursor; hourly window; task ids.
- **History.** PR #101.
- **Ordering.** Selector cycle (queue catalog).

### SOL-4.4 Engine debt arm
- **Statement.** `enqueue_engine_debt(cap=500, limit=None)` (selector cycle): room = `cap - PENDING SolveTasks of arm 'debt'` (optionally `min(limit)`); `taken` = positions with a debt task PENDING/LEASED/COMPLETED. Candidates: `closure MATE_PV`, status decisive, `won_line` non-null and non-empty, `proof ENGINE or NULL`, not taken, ordered by key, first `2*room` rows; mint `SolveTask(goal = position.status, stage F0, 2,000,000 nodes, arm 'debt')` up to room (`bulk_create(ignore_conflicts)`); event `DEBT_ENQUEUED {created, pending_before, cap}`. Results go through SOL-2.3 (upgrade) or SOL-2.4.
- **Inputs / outputs.** SolveTask inserts; DBEvent.
- **Code.** `atomicdb/ingest.py:enqueue_engine_debt:8924`.
- **Tests.** `test_engine_debt.py::EnqueueDebtTests::*` (9); `test_engine_debt.py::DebtIsServedLastTests::*`.
- **Determinism hazards.** none beyond state.
- **History.** `204d17e` (28-Jul); cap per arm fixed 20-Aug.
- **Ordering.** Selector cycle after recycling stale leases (SOL-5.1).

### SOL-4.5 Fragile mate claim arm
- **Statement.** Only when the adversarial arms are on (`ATOMICDB_ADVERSARIAL`, default off; checked by `refresh_selector`). `fragile_mate_claims(scan=2000)`: open positions with `|backed_eval| >= 9000`, newest `updated` first, at most 2000; keep those whose coverage is partial (`coverage_is_partial`, backed catalog); goal by the sign of `backed_eval`. `enqueue_fragile_mate_solves(cap=50)`: room = `cap - PENDING fragile`; skip positions with a fragile task in PENDING/LEASED/COMPLETED or any PENDING/LEASED solve task; mint `SolveTask(goal, F0, 2M, arm 'fragile')`; event `FRAGILE_ENQUEUED {created, pending_before, cap, candidates, white, black}`. A DISPROVED answer on an open position does nothing (SOL-2.4).
- **Inputs / outputs.** SolveTask inserts; DBEvent.
- **Code.** `atomicdb/ingest.py:fragile_mate_claims:9477`, `:enqueue_fragile_mate_solves:9500`; `management/commands/refresh_selector.py:319-328`.
- **Tests.** `test_adversarial_explorer.py::FragileMateClaimTests::*` (12).
- **Determinism hazards.** `order_by('-updated')` (wall clock of last write).
- **History.** `7a592e9` (28-Jul).
- **Ordering.** Selector cycle.

### SOL-4.6 Solve pilot (goal by sign)
- **Statement.** `solve_pilot` command (manual experiment): `goal_for(position, campaign)`: `known = backed_eval if not None else eval_cp`; `<= -900` → BLACK_WIN; `>= 900` → WHITE_WIN; else the campaign goal (default WHITE_WIN). Samples open expanded positions in strata, alternates analyze/solve arms, mints AnalysisTasks (512M, 2B) or SolveTasks (10M, 100M at F4; fortress stratum 2M at F0) and events `SOLVE_PILOT_QUEUED/ARM`.
- **Inputs / outputs.** Task inserts; DBEvents.
- **Code.** `management/commands/solve_pilot.py:goal_for:62`, `Command._queue:169`.
- **Tests.** `test_engine_debt.py::PilotRoundTwoTests::*` (4); `test_solve.py::SolvePilotTests::*` (5).
- **Determinism hazards.** none relevant.
- **History.** July pilot.
- **Ordering.** Manual.

### SOL-5.1 Recycling stale solve leases
- **Statement.** `recycle_stale_solve_leases(now=None, minutes=20)`: SolveTasks with `leased_at < now - 20 min` and (`lease_heartbeat_at` NULL or older than that) and state LEASED go back to PENDING (`machine=''`, heartbeat NULL, token and session emptied; `attempts` unchanged); event `SOLVE_LEASE_RECYCLED {freed, minutes}` only when something was freed. Called by the selector cycle and by the solve acquire endpoint.
- **Inputs / outputs.** SolveTask updates; DBEvent.
- **Code.** `atomicdb/ingest.py:recycle_stale_solve_leases:8878`, `:requeue_solve_leases:8865`.
- **Tests.** `test_engine_debt.py::RecycleStaleSolveLeasesTests::*` (5); `test_lease_release.py::SharedRecyclerTests::*`.
- **Determinism hazards.** Wall clock.
- **History.** 20-Aug fix.
- **Ordering.** Queue catalog.

---

# Part 5: Management commands that rewrite proofs (CMD)

### CMD-1 verify_mates
- **Statement.** Classifies `MATE_PV` closures with `proof IS NULL`. Rows without `won_line` are only reported (`MISSING`). Pending rows (`won_line` non-empty) are processed by `(Length(won_line), key)` ascending, resumable keyset. Per row: status must be decisive (else ERROR); `hint = won_line.split()`; `prove_forced_mate(fen, winner, max_plies = len(hint)+2, budget_positions = --budget-positions (default 200,000), hint)` (no deadline). Map `PROVEN→ANDOR`, `INCONCLUSIVE→ENGINE`, `NO_MATE→DISPUTED`. Persist with CAS `UPDATE ... WHERE key, proof IS NULL, closure, status, fen, won_line unchanged` (proof, updated); not 1 row → SKIPPED. DISPUTED: `DBEvent('MATE_PROOF_DISPUTED', {key, status, closure, max_plies})` and `revoke_closure(key, 'mate-witness-refuted', mark_disputed=True)`. Finally `backup_cascade` over every MATE_PV with proof in (ANDOR, ENGINE, DISPUTED).
- **Inputs / outputs.** `Position.proof`; events; revocations; cascade.
- **Code.** `management/commands/verify_mates.py:Command.handle:28`.
- **Tests.** `test_proofs.py::VerifyMatesCommandTests::*` (6); `test_revocation.py::VerifyMatesRevokesTests::test_disputed_now_reopens_the_position`.
- **Determinism hazards.** Concurrent writers (CAS); pyffish move order (MPV-2.1).
- **History.** July.
- **Ordering.** Manual.

### CMD-2 recertify_mates
- **Statement.** Re-proves `MATE_PV` with `proof = 'ENGINE'` (non-empty `won_line`) excluding positions with a live debt SolveTask (PENDING/LEASED), by `(Length(won_line), key)`. Budget by witness length: `<= 20` plies → 2,000,000 positions, else 20,000,000 (`--budget-positions` overrides). Skip when an earlier `MATE_RECERTIFIED` event for the key recorded a budget `>=` this one (unless `--force`). NO_MATE (CAS-checked row unchanged) → `MATE_PROOF_DISPUTED` (with budget, source) + `revoke_closure(mark_disputed=True)` + `MATE_RECERTIFIED`. PROVEN/INCONCLUSIVE → CAS update `proof` (ANDOR/ENGINE) + `MATE_RECERTIFIED`; ANDOR → `backup_cascade([key])`. With `--include-missing`, unwitnessed `MATE_PV` (`proof NULL`, no won_line) recover a witness from `last_analysis`: lines with `mate` of the closure's sign, a non-empty list of strings as pv, shortest first, first one that `verify_mate_pv` accepts; recovered → proven as above and the CAS also writes `won_line` and `mate_in = len`; not recovered → reported, revoked (`'mate-witness-unrecoverable'`, not marked disputed) only with `--revoke-unwitnessed`. `--dry-run` writes nothing.
- **Inputs / outputs.** `Position.proof/won_line/mate_in`; events; revocations; cascade.
- **Code.** `management/commands/recertify_mates.py:budget_for_witness:54`, `:recover_witness:62`, `Command._prove_and_persist:296`.
- **Tests.** `test_revocation.py::RecertifyMatesTests::*` (8); `test_engine_debt.py::RecertifyDefersToTheFleetTests::*` (2).
- **Determinism hazards.** Resume state read from DBEvents; concurrency (CAS); pyffish order.
- **History.** `a4960ff` (29-Jul).
- **Ordering.** Manual.

### CMD-3 verify_certificate
- **Statement.** `--file` verifies a certificate file (`.gz` or plain) with SOL-1.x and prints the report. `--task N` / `--all` replays stored certificates (`SolveTask` with a certificate, by pk) with `solve.verify_certificate(root_fen=task.position.fen, goal=task.goal)`; failure → `verified=False`, `reject_reason`, `DBEvent('SOLVE_REJECTED', {..., source: 'verify_certificate'})`. `--close` closes still-open positions with SOL-2.2 and cascades. It does not filter by `certificate_format`: a stored survival certificate would be "rejected" (unknown format) and its task marked unverified.
- **Inputs / outputs.** SolveTask, Position, events.
- **Code.** `management/commands/verify_certificate.py:Command._verify_task:81`.
- **Tests.** `test_solve.py::SolveClosurePropagationTests::test_verify_certificate_command_replays_a_stored_proof`; `::test_verify_certificate_command_reports_a_bad_stored_proof`. **Gaps:** survival certificates under `--all`.
- **Determinism hazards.** none.
- **History.** P1c.
- **Ordering.** Manual.

### CMD-4 backfill_mate_distance (interface)
- **Statement.** Recomputes `mate_in` (and `best_move`) over the closed subgraph with `mate_distance_refresh` (DTM catalog) to a fixpoint, writing only at the end in chunks with a snapshot CAS, and writes `DBEvent('MATE_DISTANCE_BACKFILL')`. Owned by the DTM catalog; listed because it rewrites proof-carrying rows (`Position.mate_in`, `best_move`). It never changes status, closure or proof.
- **Inputs / outputs.** `Position.mate_in/best_move/updated`.
- **Code.** `management/commands/backfill_mate_distance.py:571`.
- **Tests.** DTM catalog.
- **Determinism hazards.** DTM catalog.
- **History.** 26-Oct.
- **Ordering.** Manual.

### CMD-5 materialise_mate_lines
- **Statement.** For `MATE_PV` closures with decisive status, non-empty `won_line`, proof not DISPUTED (optional `--proof` filter), by key: one transaction per witness running `materialise_won_line(row, verify = not --no-verify)` (MPV-5.1; verify defaults ON), retried on SQLite busy/locked with backoffs 1, 3, 10, 30 s; then `backup_cascade` over the witnesses that changed and their closed links, sliced by `--batch-size`; event `WON_LINES_MATERIALISED` with counts. Idempotent.
- **Inputs / outputs.** Positions, edges, closures (MPV-5.3), cascade, event.
- **Code.** `management/commands/materialise_mate_lines.py:Command.handle`, `_materialise`, `_batch`.
- **Tests.** `test_won_line_chain.py::BackfillCommandTests::*` (3); `test_closure_reaches_all_parents.py::MaterialisedLinksReachEveryParentTests::test_the_backfill_command`.
- **Determinism hazards.** Sleep/retry timing.
- **History.** July.
- **Ordering.** Manual.

---

# Part 6: Score bands and sign conventions (SB)

### SB-1 Stored bands and constants
- **Statement.** Stored values (White POV, symmetric in sign), by absolute value: `0..8999` plain centipawns; `9000..9249` a large ordinary eval, compressed; `9250..9500` a tablebase win reached `d` plies down, stored `9500 - d` (`d` capped at 250; 9500 is d = 0 or a legacy clamped value); `9501..9999` an engine mate, `10000 - moves`; `10000` proven (status). Constants: `ENGINE_TB_CP = 20000`, `ENGINE_MAX_PLY = 246`, `ENGINE_CP_MAX = 15200`, `PLAIN_MAX = 8999`, `BIG_LOW = 9000`, `BIG_HIGH = 9249`, `TB_TOP = 9500`, `TB_DISTANCE_MAX = 250`, `TB_LOW = 9250`, `MATE_BASE = 10000`, `ENGINE_TB_MIN_CP = 19750`, `LEGACY_CLAMP = 9500`. The map is monotone: bands are ordered and within a band a better engine score never stores smaller.
- **Inputs / outputs.** Constants.
- **Code.** `atomicdb/score_bands.py:52-71`.
- **Tests.** `test_tb_distance.py::MapTests::*` (5); `test_worker_console.py::NumberTests::test_the_tablebase_constants_follow_score_bands`.
- **Determinism hazards.** none.
- **History.** `7a6f5d3` (PR #312, 25-Nov; owner approval 24-Nov).
- **Ordering.** —

### SB-2 tb_distance
- **Statement.** `tb_distance(cp)` (raw engine cp, side-to-move POV): `|cp| < 19750` → None; else `min(250, max(0, 20000 - |cp|))`.
- **Inputs / outputs.** Pure.
- **Code.** `atomicdb/score_bands.py:tb_distance:80`.
- **Tests.** `test_tb_distance.py::MapTests::test_the_examples`.
- **Determinism hazards.** none.
- **History.** PR #312.
- **Ordering.** Inside SB-3.

### SB-3 engine_cp_to_stored
- **Statement.** `engine_cp_to_stored(cp)` (same POV in and out), with `m = |cp|`, `s = -1 if cp < 0 else 1`: `m <= 8999` → `cp`; TB distance `d` (SB-2) not None → `s * (9500 - d)`; `m >= 15200` → `s * 9249`; else `s * (9000 + (m - 9000) * 249 // 6200)` (integer floor division). Examples: 19981 → 9481, 20000 → 9500, 19754 → 9254, 15200..19749 → 9249.
- **Inputs / outputs.** Pure.
- **Code.** `atomicdb/score_bands.py:engine_cp_to_stored:88`.
- **Tests.** `test_tb_distance.py::MapTests::test_the_examples`; `::test_monotone_and_symmetric`; `::test_bands_are_strictly_ordered`.
- **Determinism hazards.** none.
- **History.** PR #312.
- **Ordering.** Inside SB-5.

### SB-4 legacy_value and raw_score
- **Statement.** `legacy_value(cp) = clamp(int(cp), -9500, 9500)` (what workers send as `eval_cp` for a `score cp`). `raw_score(raw)`: non-string or empty → None; first match of `\bscore (cp|mate) (-?\d+)\b` → `(kind, int)`; no match → None. Bound and WDL tokens elsewhere in the line do not matter.
- **Inputs / outputs.** Pure.
- **Code.** `atomicdb/score_bands.py:legacy_value:103`, `:raw_score:108`.
- **Tests.** `test_tb_distance.py::NormalizeTests::test_bounds_and_wdl_in_the_raw_line`.
- **Determinism hazards.** none.
- **History.** PR #312.
- **Ordering.** Inside SB-5.

### SB-5 stored_from_raw
- **Statement.** `stored_from_raw(line, stm_white)` → `(new, legacy)` White POV, or None when the line is not a dict, carries a `mate`, has no raw line, or its raw score is not `cp`. With `sign = 1 if stm_white else -1`: `(sign * engine_cp_to_stored(n), sign * legacy_value(n))`.
- **Inputs / outputs.** Pure.
- **Code.** `atomicdb/score_bands.py:stored_from_raw:134`.
- **Tests.** `test_tb_distance.py::NormalizeTests::test_white_and_black_to_move`; `test_tb_distance.py::IngestTests::test_black_to_move`.
- **Determinism hazards.** none.
- **History.** PR #312.
- **Ordering.** Inside SB-6.

### SB-6 normalize_line / normalize_lines
- **Statement.** `normalize_line(line, stm_white)`: `values = stored_from_raw(line, stm_white)`; None → the same object unchanged. `current = line.get('eval_cp')`; if `current == new` or `current not in (legacy, None)` → unchanged (a value the raw line does not account for is kept). Else return a copy with `eval_cp = new`. Idempotent. `normalize_lines(lines, stm_white)`: non-list → returned as is; else `[normalize_line(l) for l in lines]` (non-dicts pass through).
- **Inputs / outputs.** Pure.
- **Code.** `atomicdb/score_bands.py:normalize_line:150`, `:normalize_lines:167`.
- **Tests.** `test_tb_distance.py::NormalizeTests::*` (4); `test_tb_distance.py::IngestTests::*` (3).
- **Determinism hazards.** none.
- **History.** PR #312.
- **Ordering.** SB-8.

### SB-7 band()
- **Statement.** `band(value)`: None → None; `|v| <= 8999` → `plain`; `<= 9249` → `big`; `<= 9500` → `tb`; `< 10000` → `mate`; else `proven`. Reporting only.
- **Inputs / outputs.** Pure.
- **Code.** `atomicdb/score_bands.py:band:118`.
- **Tests.** `test_tb_distance.py::MapTests::test_band_names`.
- **Determinism hazards.** none.
- **History.** PR #312.
- **Ordering.** —

### SB-8 Where normalisation is applied
- **Statement.** Every search result's lines are normalised with the side to move of the searched position: in `apply_job` before `prepare_mate_proofs` (only when no TB closure pre-empts the search), and again (idempotently) inside `ingest_analysis` before anything reads them (and before `prepare_mate_proofs` when called without prepared proofs). Lines without a raw line keep the worker's value (9500 then means "capped engine win, distance unknown"). The pass history (`analysis_passes`) records what was printed at the time and is never rewritten. Old rows are moved by TB-4.1.
- **Inputs / outputs.** Line `eval_cp` before storage in `last_analysis`, seeds, `eval_cp`.
- **Code.** `atomicdb/ingest_queue.py:apply_job:274-277`; `atomicdb/ingest.py:ingest_analysis:1025-1026`, `ingest.py:1043`.
- **Tests.** `test_tb_distance.py::IngestTests::test_a_tablebase_line_keeps_its_distance`; `::test_a_line_without_raw_keeps_its_value`; `::test_black_to_move`.
- **Determinism hazards.** none.
- **History.** PR #312.
- **Ordering.** Before MPV-2.4 and the landing.

### SB-9 Mate encoding and sign conventions
- **Statement.** The worker parses `info ... score (cp|mate) N ... pv ...` (side-to-move POV) into lines `{move, pv, eval_cp, mate, raw}` in White POV: `score cp v` → `eval_cp = clamp(v, ±9500)` negated when Black is to move (then re-derived by SB-6 from `raw`); `score mate N` → `mate = N` if White to move else `-N` (positive: White mates), `eval_cp = (10000 - |N|) * (1 if mate > 0 else -1)`. On the server: `|eval_cp|` in `9501..9999` decodes to a mate in `10000 - |eval_cp|` MOVES by the side the sign names; `±10000` is the exact value of a proven status (`_status_eval`: WHITE_WIN 10000, BLACK_WIN -10000, DRAW 0), never "mate in 0"; `Position.mate_in` is PLIES (length of the stored witness; TERMINAL = 0). Mate moves convert to plies by EXP-4.1. A mate in 500 or more moves would encode at or below 9500 and collide with the TB band (unreachable: engine `MAX_PLY` 246).
- **Inputs / outputs.** Contract between worker and server.
- **Code.** `Client/atomicdb_worker.py:1790-1798`; `atomicdb/ingest.py:_status_eval:1984`, `:_mate_moves_to_plies:262`.
- **Tests.** `test_worker_search.py::IterationFilterTests::*`; `test_mate_sign.py::MateWinnerPerspectiveTests::*`; `test_mate_clamp.py::MateDistanceUnitTests::test_the_worker_formula_decodes_back_to_plies`; `test_public_api.py::QueryApiAnalysisTests::test_the_scores_are_in_the_same_perspective_as_the_rest`.
- **Determinism hazards.** none.
- **History.** `da18ee6` (30-Jul, ±10000), PR #312.
- **Ordering.** —

### SB-10 Thresholds other rules read
- **Statement.** `MATE_BAND = 9000` (`ingest.MATE_BAND = BIG_LOW`): `|eval| >= 9000` is "decided" (budget jump in `budget_for`, TB-line claims, fragile claims on `backed_eval`, pilot strata). `TB_CLAMP_CEILING = 9500`: `|eval| > 9500` is an engine mate with a known distance (EXP-4.1, autosolve `9500 < |eval| < 10000`, `AUTOSOLVE95_THRESHOLD = 9500`). Proof eval bands use attacker-POV thresholds 9000/800/300/-300/-800/-9000 (PN-1.5), and the annoyance eval-band feature uses 9000/800/300 on `|eval|` (PN-2.5). These thresholds did not move with SB-1.
- **Inputs / outputs.** Constants.
- **Code.** `atomicdb/ingest.py:94`, `:134`, `:9004`; `atomicdb/score_bands.py`.
- **Tests.** `test_tb_distance.py::MapTests::test_the_thresholds_the_code_reads_did_not_move`; `test_autosolve.py::MateRuleTests::test_the_clamp_is_not_a_mate`.
- **Determinism hazards.** none.
- **History.** PR #312.
- **Ordering.** —

---

# Determinism hazards, summary

| Hazard | Rules | Effect on stored values |
|---|---|---|
| Wall-clock online budget (2.5 s repo, 1.0 s production) shared by mate verification, AND/OR proofs and TB lines | MPV-1.1, MPV-2.1, MPV-2.4, MPV-2.5, TB-3.1 | Whether a child closes at all, ANDOR vs ENGINE grade, `clock_slack`, whether a TB line is tried |
| pyffish move order inside `prove_forced_mate` (first OR success wins) | MPV-2.1, MPV-2.2, CMD-1, CMD-2 | Which strategy is found: `worst_run` → `clock_slack`; node count → PROVEN vs INCONCLUSIVE at the budget edge |
| Unordered `Position` query plus in-place mutation of `existing` rows inside one refresh level | PN-4.3 | A parent can read a sibling-level child's new or old numbers depending on DB row order: pn/dn and `selected_child` differ |
| Hysteresis reads the stored `selected_child` | PN-5.1, PN-6.4, PN-9.3 | Path dependence: recomputing from scratch can pick a different primary |
| Skip-locked, `bulk_create(ignore_conflicts)`, refresh after commit, refresh thread | PN-4.5, PN-4.3, PN-8.2, PN-8.3 | Which rows get written, and against which committed state, depends on concurrency |
| Caps (64 plies, 2 revisits, 50,000 nodes) | PN-4.4 | Results depend on how seeds are batched |
| Row presence = "reached by a refresh" | PN-0.1, PN-4.6, PN-10.1 | Served "absent" vs numbers; frontier membership |
| Descent counter = max `AnalysisTask.id` | PN-6.6 | Which bucket/campaign each descent draws |
| Hourly arm windows (`_arm_budget_room`) | PN-6.6, PN-9.1, PN-9.12, SOL-4.2, SOL-4.3 | Value walker vs df-pn, autosolve minting |
| Process cache + monotonic window of `SOLVE_GATE_DISAGREE` | PN-2.10 | Event emission only |
| Tablebase files, `ATOMICDB_TB_PATHS`, `ATOMICDB_TB_SERVER_MAX_MEN`, `ATOMICDB_TB_TRUSTED`, cached open failure | TB-1.2, TB-2.x, TB-3.1, MPV-5.4 | Closures accepted or rejected |
| Native verifier binary presence and 120 s timeout | SOL-3.3 | A valid survival certificate can be rejected |
| File cursor `autosolve.json` | SOL-4.3 | Which backlog passes are minted |
| `updated` timestamps read for ordering | PN-10.4, SOL-4.5 | Order of obligations / fragile candidates |
| Settings switches: `ATOMICDB_SOLVE_GATE`, `ATOMICDB_DESCENT`, `ATOMICDB_OR_CLAMP`, `ATOMICDB_CAMPAIGN_DESCENT`, `ATOMICDB_PROOF_AFTER_COMMIT`, `ATOMICDB_PROOF_PIPELINE`, `ATOMICDB_CLOSURE_TRUST_GATE`, `ATOMICDB_FRESH_CONTEXT`, `ATOMICDB_AUTO_INTERIOR_DEEP_RUNGS`, `ATOMICDB_ADVERSARIAL`, `ATOMICDB_ARM_RATE_*` | many | Different numbers and closures per deployment |
| Edge-id order as the universal tie-break | PN-3.2, PN-4.1, PN-6.4, PN-9.2, PN-9.8 | Ties: v2 must keep v1 edge ids (or creation order) to reproduce |

---

# Open questions for the owner

### Q1 (D1) Wall-clock budgets of the online mate proofs
The online preparation (MPV-2.4) gives every result one shared monotonic deadline (`ONLINE_MATE_PROOF_SECONDS`, 2.5 s in the repo, 1.0 s hot-patched in production). Time decides three things: whether a mate line is verified at all (a line whose `verify_mate_pv` hits the deadline is dropped, not degraded), PROVEN vs INCONCLUSIVE (ANDOR vs ENGINE, run-based vs length-based slack), and whether TB lines are tried. Replay cannot reproduce this.
- **Options.** (a) Keep the wall clock in v2 (no replay of these verdicts; whole-history replay compares only "a closure exists"). (b) Replace by a step budget: count pyffish-equivalent calls (or proof nodes) across the whole preparation, e.g. a shared budget of N movegen questions, with N calibrated so that the production distribution matches 1.0 s / 2.5 s (the 677-job replay in PR #280 is the calibration set). (c) Separate budgets: verification of PV lines always completes (bounded by PV length, cheap), AND/OR proofs get a shared node budget, TB lines a separate small budget.
- **Recommendation.** (c): verification of the witness is O(plies) and should never be cut by time; give the AND/OR proofs a shared node budget (e.g. 200,000 per line as today plus a shared cap per job) and TB lines their own. It removes the "line dropped by time" case, makes ANDOR/ENGINE replayable, and leaves the fleet debt (SOL-4.4) to certify the rest. Decide also which value production should match during shadow (1.0 s or 2.5 s): recommend switching production back to the repo value or porting the hot patch into the repo before phase 2, so v1 and its source agree.

### Q2 (D1) Move order of the exhaustive proof
`prove_forced_mate` returns at the first proven OR child in pyffish order. With a step budget, the order also decides PROVEN vs INCONCLUSIVE. A Rust port on another move generator (shakmaty or a Fairy-Stockfish library build) will order moves differently.
- **Options.** (a) Require the v2 move generator to reproduce pyffish's order (use the same Fairy-Stockfish library, plan open question 3). (b) Sort moves canonically (by UCI string) after the hint, in v2 and in v1 at the same time (a v1 PR), so the order is defined by the spec. (c) Accept differences in slack and grade and classify them in the shadow.
- **Recommendation.** (b) if v1 can take a small PR during the freeze: a defined order is part of making the proof replayable; otherwise (a).

### Q3 (D5) Proof numbers computed, not stored
Today pn/dn live in `ProofNode` rows, written incrementally, history-dependent in three ways: (1) the sticky primary reads the previously stored `selected_child` (PN-5.1); (2) "has a row" means "was reached by a refresh", which decides serving (PN-4.6: absent vs numbers) and the frontier (PN-10.1); (3) the level loop's in-place mutation and unordered rows (PN-4.3), caps (PN-4.4), skip-locked and after-commit refreshes (PN-4.5, PN-8.x) mean the stored numbers are not a function of the graph.
- **Options.** (a) Port exactly: keep a stored ProofNode view with the incremental refresh, with v1's order effects pinned (e.g. process each level's rows in key order and decide Jacobi vs Gauss-Seidel explicitly). (b) Compute in memory as a pure function of the graph per campaign: define pn/dn as the fixpoint of PN-1.x/PN-3.x recurrences over the whole reachable subgraph (Jacobi iteration to rest, cycles handled by PN-3), keep `selected_child` as the only persisted state (hysteresis needs memory), and define "reached" as "reachable from the campaign root through open nodes" instead of "has a row". (c) Compute in memory and drop hysteresis persistence (recompute the primary each time with a deterministic rule), accepting that the "dance" may come back.
- **Recommendation.** (b). Pin these choices in the spec: Jacobi per level (no in-place reads) or full fixpoint, ties by edge id, `selected_child` stored per (campaign, node) and versioned like any other value, the frontier defined by reachability. Expect pn/dn differences against v1 on every node where v1's order effects or caps applied; the shadow should compare `selected_child` and the descent's chosen positions rather than raw pn/dn. Also decide whether the cycle rule must keep reading "numbers written earlier in this pass" (PN-3.3), which a fixpoint makes moot.

### Q4 (D5) Absence semantics on the read side
The explorer shows no pn/dn for positions without a row, and the campaign page says "proven"/"refuted" from `pn == 0`/`dn == 0`. With computed numbers every position has numbers.
- **Options.** (a) Show numbers for every reachable position. (b) Show only for positions the proof has "expanded" (open with at least one edge, i.e. today's `expanded_in_proof`) plus their children.
- **Recommendation.** (b): closest to today's display without depending on refresh history.

### Q5 Unused branching input of leaf numbers
`leaf_numbers` supports `legal_moves` (branching) but no production caller passes it, so informed leaves are `(pn_w, dn_w)` and branching never matters (PN-1.6). A test pins the unused path.
- **Options.** (a) Port as is (branching always 1). (b) Pass the edge count or legal move count (it is free in v2) and accept new numbers.
- **Recommendation.** (a) for the port; (b) is a semantic change to decide after the shadow is clean.

### Q6 Solve gate state in production
`ATOMICDB_SOLVE_GATE` (default off), `ATOMICDB_DESCENT` (default `proof`), `ATOMICDB_SELECTOR`, `ATOMICDB_OR_CLAMP`, `ATOMICDB_CAMPAIGN_DESCENT`, `ATOMICDB_TB_SERVER_MAX_MEN`, `ATOMICDB_TB_TRUSTED`, `ATOMICDB_ADVERSARIAL`, `ATOMICDB_CLOSURE_TRUST_GATE`, `ATOMICDB_FRESH_CONTEXT` decide numbers and closures. The repo defaults are known; production values are in the env file.
- **Recommendation.** Capture the production env in phase 0 and port only the branches production runs; keep the others behind the same switches only if the owner wants them.

### Q7 Survival certificates are unreachable through the API
`apply_solve_result` handles `DISPROVED_WHITE_WIN`, but `api_solve_submit` accepts only `PROVED/DISPROVED/UNKNOWN` (SOL-2.6). Also, on the server the root FEN is canonical (clock 0), so only `tau(root) = 0` certificates can verify (SOL-3.1), and `verify_certificate --all` would mark a stored survival certificate rejected (CMD-3).
- **Options.** (a) Port as is (survival path dead on the wire). (b) Port the verifier and wire the outcome in v2 (and fix CMD-3 to route by `certificate_format`).
- **Recommendation.** (a) for parity; record (b) as a later feature.

### Q8 Search lines discarded when a worker's WDL is rejected
When a worker sends `tb_wdl` and the server rejects it, the whole job is DONE with `tb_rejected` and its search lines are never ingested (TB-2.5): no eval, no seeds, no mate closures from that search.
- **Options.** (a) Port as is. (b) Fall back to ingesting the lines.
- **Recommendation.** (a) for the port (whole-history replay must match); (b) is a separate v1 decision.

### Q9 Double MATE_PROOF_DISPUTED event on online revocation
A closed child revoked online (MPV-3.1) then falls through to the open-child branch (MPV-2.7) and gets a second `MATE_PROOF_DISPUTED` event, `won_line` rewritten to the refuting line and a deep reanalysis queued.
- **Recommendation.** Port as is (events are part of the projection); flag for a v1 fix if the owner considers it noise.

### Q10 DISPROVED on an open position leaves no signal
`_dispute_from_solver` returns before writing `SOLVE_DISPUTE_SIGNAL` when the position is open, although the fragile-arm docstring says the signal is left (SOL-2.4, SOL-4.5).
- **Recommendation.** Port the code's behaviour (no event); ask whether the event was intended.

### Q11 Upgrade by certificate on non-mate closures
`_upgrade_by_certificate` sets `proof = 'ANDOR'` on any closed position with the goal status and a non-ANDOR proof, not only on `MATE_PV ENGINE` debt (SOL-2.3). On a MINIMAX closure the grade changes but the closure stays revocable (`_closure_is_independent` is false for MINIMAX); on a TB closure (proof NULL) it only adds the grade.
- **Recommendation.** Port as is; ask the owner whether MINIMAX/TB rows should be excluded.

### Q12 Unverified DTZ
A TB closure's `clock_slack` comes from the worker's DTZ, accepted without verification (DTM-5.8, TB-2.4). The server tables are WDL only.
- **Options.** (a) Port as is. (b) Probe DTZ on the server when the tables exist (shakmaty-syzygy can).
- **Recommendation.** (a) for parity, (b) as a later hardening.
