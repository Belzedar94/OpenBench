-- AtomicDB v1 -> v2 export (plan section 8.1).
--
-- RUN ON A RESTORED BACKUP, NEVER ON THE LIVE BOX: these are full scans of
-- the two 200M-row tables.  psql -d atomicdb -f export.sql, from the
-- directory that should receive the CSV files.
--
-- Format: COPY CSV with a header; NULL is the empty unquoted field.  The
-- loader (av2-load) finds columns by name, so columns may be added later
-- without breaking it.  Ids are not exported: av2-load assigns them in
-- creation order (positions without an incoming edge first, then by first
-- incoming edge id; plan section 8.2), which is why edges are exported in
-- id order.

SET statement_timeout = 0;
SET extra_float_digits = 3;  -- time_invested round-trips exactly

-- Positions: every column of atomicdb_position that v2 keeps (priority and
-- updated are not kept: owner decision D5, and version replaces updated).
\copy (SELECT key, fen, status, closure, proof, eval_cp, backed_eval, backed_move, backed_plies, backed_nodes, best_move, mate_in, clock_slack, expanded, reachable, depth_invested, nodes_invested, time_invested, visits, campaign_id, last_analysis, analysis_passes, won_line FROM atomicdb_position) TO 'position.csv' WITH (FORMAT csv, HEADER true)

-- Edges, in id order (= creation order).
\copy (SELECT id, parent_id AS parent, move_uci, child_id AS child FROM atomicdb_edge ORDER BY id) TO 'edge.csv' WITH (FORMAT csv, HEADER true)

-- Related tables.  Phase 1 loads positions and edges only; these are
-- exported now so that the projection (phase 5) and the queue (phase 4)
-- have their inputs from the same snapshot.
\copy (SELECT * FROM atomicdb_campaign ORDER BY id) TO 'campaign.csv' WITH (FORMAT csv, HEADER true)
\copy (SELECT * FROM atomicdb_campaignvote ORDER BY id) TO 'campaignvote.csv' WITH (FORMAT csv, HEADER true)
\copy (SELECT * FROM atomicdb_closureprovenance) TO 'closureprovenance.csv' WITH (FORMAT csv, HEADER true)
\copy (SELECT * FROM atomicdb_pendingclaim ORDER BY id) TO 'pendingclaim.csv' WITH (FORMAT csv, HEADER true)
\copy (SELECT * FROM atomicdb_trustedaccount ORDER BY id) TO 'trustedaccount.csv' WITH (FORMAT csv, HEADER true)
\copy (SELECT * FROM atomicdb_proofcampaign ORDER BY id) TO 'proofcampaign.csv' WITH (FORMAT csv, HEADER true)
\copy (SELECT * FROM atomicdb_proofnode ORDER BY id) TO 'proofnode.csv' WITH (FORMAT csv, HEADER true)
\copy (SELECT * FROM atomicdb_analysistask WHERE state IN ('PENDING', 'LEASED') ORDER BY id) TO 'analysistask_open.csv' WITH (FORMAT csv, HEADER true)
\copy (SELECT * FROM atomicdb_solvetask WHERE state IN ('PENDING', 'LEASED') ORDER BY id) TO 'solvetask_open.csv' WITH (FORMAT csv, HEADER true)
-- The raw search payloads are the facts of the whole-history replay (plan
-- section 7.3); 6.2M rows, ~8 GB: export separately when needed.
-- \copy (SELECT id, task_id, position_id, payload, state, created FROM atomicdb_ingestjob ORDER BY id) TO 'ingestjob.csv' WITH (FORMAT csv, HEADER true)

-- A random sample for av2-check (field-by-field comparison).  TABLESAMPLE
-- reads ~0.5% of the pages, not the whole table.
\copy (SELECT key, fen, status, closure, proof, eval_cp, backed_eval, backed_move, backed_plies, backed_nodes, best_move, mate_in, clock_slack, expanded, reachable, depth_invested, nodes_invested, time_invested, visits, campaign_id, last_analysis, analysis_passes, won_line FROM atomicdb_position TABLESAMPLE SYSTEM (0.5)) TO 'position_sample.csv' WITH (FORMAT csv, HEADER true)
