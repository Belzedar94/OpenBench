set statement_timeout='180s';
copy (
select id, left(position_id,24) as key, state, attempts, extract(epoch from created)::numeric(16,3) as created_s, extract(epoch from updated)::numeric(16,3) as updated_s,
       pg_column_size(payload) as payload_bytes,
       (summary->'propagated'->>'batch')::int as batch, (summary->'propagated'->>'backed_up')::int as backed_up, (summary->'propagated'->>'backed_evals')::int as backed_evals,
       (summary->>'closed_children')::int as closed_children, summary->>'propagation' as propagation, (summary->>'tb') as tb
from atomicdb_ingestjob where id > (select max(id) - 200000 from atomicdb_ingestjob) order by id
) to stdout with (format csv, header true);