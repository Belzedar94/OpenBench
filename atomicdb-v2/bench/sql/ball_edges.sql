set statement_timeout='600s';
copy (
with recursive bfs(key, depth) as (
  select 'b1791d7fc9ae3d38966568c257ffb3a02cbf8394cdb4805bc70f64fc3c0b6879'::varchar, 0
  union
  select e.child_id, b.depth+1 from bfs b join atomicdb_edge e on e.parent_id = b.key where b.depth < 5
), nd as (select key, min(depth) as depth from bfs group by key)
select e.id, left(e.parent_id,24) as parent, e.move_uci, left(e.child_id,24) as child, nd.depth as pdepth
from nd join atomicdb_edge e on e.parent_id = nd.key
) to stdout with (format csv, header true);