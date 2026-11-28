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

-- Positions: every column of atomicdb_position that v2 keeps (updated is not
-- kept: version replaces it).  `priority` is not a graph column (D5): the
-- loader writes it to `<store>/v1prio.col`, the stored values the queue orders
-- AUTO/FILL leases by until computed priority is switched on (owner, 28-Nov).
-- `ladder_bought`/`ladder_searched`: max budget_nodes of the COMPLETED tasks
-- the queue does not import (generation < visits; absorbed rows count for
-- bought, only rows that searched count for searched), queue file Q-6.2.
\copy (SELECT p.key, p.fen, p.status, p.closure, p.proof, p.eval_cp, p.backed_eval, p.backed_move, p.backed_plies, p.backed_nodes, p.best_move, p.mate_in, p.clock_slack, p.expanded, p.reachable, p.depth_invested, p.nodes_invested, p.time_invested, p.visits, p.campaign_id, p.last_analysis, p.analysis_passes, p.won_line, p.priority, l.ladder_bought, l.ladder_searched FROM atomicdb_position p LEFT JOIN (SELECT t.position_id, max(t.budget_nodes) AS ladder_bought, max(t.budget_nodes) FILTER (WHERE t.nodes_searched > 0) AS ladder_searched FROM atomicdb_analysistask t JOIN atomicdb_position q ON q.key = t.position_id WHERE t.state = 'COMPLETED' AND t.generation < q.visits GROUP BY t.position_id) l ON l.position_id = p.key) TO 'position.csv' WITH (FORMAT csv, HEADER true)

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
-- The task rows the queue keeps in memory (av2-queue state.rs, retention):
-- open rows, every CANCELLED row (undo works on any), rows on the position's
-- current or a later generation (revivable absorbed rows, delivered rows not
-- applied yet), and rows served within the hour (served_rank, Q-2.19).
-- Everything else is folded into the position's ladder columns above.
\copy (SELECT t.* FROM atomicdb_analysistask t JOIN atomicdb_position p ON p.key = t.position_id WHERE t.state IN ('PENDING', 'LEASED', 'CANCELLED') OR t.generation >= p.visits OR t.completed >= now() - interval '1 hour' ORDER BY t.id) TO 'analysistask_queue.csv' WITH (FORMAT csv, HEADER true)
-- Every SolveTask: the arms exclude positions by their COMPLETED and FAILED
-- rows too (Q-8.6, Q-8.14, SOL-4.4).
\copy (SELECT * FROM atomicdb_solvetask ORDER BY id) TO 'solvetask.csv' WITH (FORMAT csv, HEADER true)
-- The people side the queue reads: pings (deep cap, contributor window),
-- serving preferences and per-machine overrides.
\copy (SELECT * FROM atomicdb_workerping ORDER BY id) TO 'workerping.csv' WITH (FORMAT csv, HEADER true)
\copy (SELECT * FROM atomicdb_contributorpref ORDER BY id) TO 'contributorpref.csv' WITH (FORMAT csv, HEADER true)
\copy (SELECT * FROM atomicdb_workerserving ORDER BY id) TO 'workerserving.csv' WITH (FORMAT csv, HEADER true)
-- The raw search payloads are the facts of the whole-history replay (plan
-- section 7.3); 6.2M rows, ~8 GB: export separately when needed.
-- \copy (SELECT id, task_id, position_id, payload, state, created FROM atomicdb_ingestjob ORDER BY id) TO 'ingestjob.csv' WITH (FORMAT csv, HEADER true)

-- A random sample for av2-check (field-by-field comparison).  TABLESAMPLE
-- reads ~0.5% of the pages, not the whole table.
\copy (SELECT key, fen, status, closure, proof, eval_cp, backed_eval, backed_move, backed_plies, backed_nodes, best_move, mate_in, clock_slack, expanded, reachable, depth_invested, nodes_invested, time_invested, visits, campaign_id, last_analysis, analysis_passes, won_line FROM atomicdb_position TABLESAMPLE SYSTEM (0.5)) TO 'position_sample.csv' WITH (FORMAT csv, HEADER true)
