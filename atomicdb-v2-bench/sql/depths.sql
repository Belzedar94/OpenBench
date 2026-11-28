set statement_timeout='120s';
copy (
with recursive up(start, key, d) as (
  select s.key, s.key, 0 from (select position_id as key from atomicdb_ingestjob where id > (select max(id)-400 from atomicdb_ingestjob)) s
  union all
  select u.start, (select e.parent_id from atomicdb_edge e where e.child_id = u.key order by e.id limit 1), u.d+1
  from up u where u.key is not null and u.key <> 'b1791d7fc9ae3d38966568c257ffb3a02cbf8394cdb4805bc70f64fc3c0b6879' and u.d < 400
)
select left(start,24) as start, max(d) filter (where key = 'b1791d7fc9ae3d38966568c257ffb3a02cbf8394cdb4805bc70f64fc3c0b6879') as depth_to_root, max(d) as steps from up group by start
) to stdout with (format csv, header true);