set statement_timeout='1200s';
copy (
with recursive bfs(key, depth) as (
  select 'b1791d7fc9ae3d38966568c257ffb3a02cbf8394cdb4805bc70f64fc3c0b6879'::varchar, 0
  union
  select e.child_id, b.depth+1 from bfs b join atomicdb_edge e on e.parent_id = b.key where b.depth < 6
), nd as (select key, min(depth) as depth from bfs group by key)
select left(p.key,24) as key, nd.depth, p.status, p.closure, p.proof, p.eval_cp, p.backed_eval, p.backed_move, p.backed_plies, p.backed_nodes, p.best_move, p.mate_in, p.clock_slack,
       p.expanded, p.reachable, p.nodes_invested, p.visits, p.priority, p.campaign_id, length(p.fen) as fen_len, split_part(p.fen,' ',2) as stm,
       length(p.won_line) as wl_len, pg_column_size(p.last_analysis) as la_bytes, extract(epoch from p.updated)::bigint as updated_s
from nd join atomicdb_position p on p.key = nd.key
) to stdout with (format csv, header true);