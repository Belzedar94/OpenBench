set statement_timeout='180s';
copy (
with s as materialized (
  select key, status, closure, proof, eval_cp, backed_eval, backed_move, backed_plies, backed_nodes, best_move, mate_in, clock_slack,
         expanded, reachable, nodes_invested, visits, priority, campaign_id, length(fen) as fen_len, split_part(fen,' ',2) as stm,
         length(won_line) as wl_len, pg_column_size(last_analysis) as la_bytes, pg_column_size(analysis_passes) as ap_bytes,
         extract(epoch from updated)::bigint as updated_s
  from atomicdb_position tablesample system (0.02) repeatable (42)
)
select left(s.key,24) as key, s.status, s.closure, s.proof, s.eval_cp, s.backed_eval, s.backed_move, s.backed_plies, s.backed_nodes, s.best_move, s.mate_in, s.clock_slack,
       s.expanded, s.reachable, s.nodes_invested, s.visits, s.priority, s.campaign_id, s.fen_len, s.stm, s.wl_len, s.la_bytes, s.ap_bytes, s.updated_s,
       (select count(*) from atomicdb_edge e where e.parent_id = s.key) as outdeg,
       (select count(*) from atomicdb_edge e where e.child_id = s.key) as indeg
from s
) to stdout with (format csv, header true);