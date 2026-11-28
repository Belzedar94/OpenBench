//! PostgreSQL baselines, driven by the same Rust climb through batched queries
//! (one round trip per wave, never per row), so that what is measured is the
//! store and not Python.
//!
//! `pgv1`: the production shape. `position` keyed by a 64-character text key
//! with the same columns and secondary indexes, `edge(id, parent_id, move_uci,
//! child_id)` with the same two indexes on text keys.
//! `pgint`: Postgres "done right" for this workload. Integer ids, a narrow
//! `node(id, rec)` table and `edge(parent, idx, child, mv)`.

use crate::dataset::Dataset;
use crate::model::*;
use crate::store::{OpenOpts, Reader, Store};
use postgres::binary_copy::BinaryCopyInWriter;
use postgres::types::{ToSql, Type};
use postgres::{Client, NoTls, Statement};
use std::collections::HashMap;
use std::io::Write;

fn connect(o: &str) -> Client { Client::connect(o, NoTls).unwrap_or_else(|e| panic!("postgres connect {o}: {e}")) }

fn pg_written(c: &mut Client) -> u64 {
    let wal: i64 = c.query_one("SELECT wal_bytes::bigint FROM pg_stat_wal", &[]).map(|r| r.get(0)).unwrap_or(0);
    let bufs: i64 = c.query_one("SELECT (buffers_checkpoint + buffers_clean + buffers_backend)::bigint * 8192 FROM pg_stat_bgwriter", &[]).map(|r| r.get(0)).unwrap_or(0);
    (wal + bufs) as u64
}
fn db_size(c: &mut Client) -> u64 { c.query_one("SELECT pg_database_size(current_database())", &[]).map(|r| r.get::<_, i64>(0)).unwrap_or(0) as u64 }
fn meta(c: &mut Client, k: i32) -> u64 { c.query_one("SELECT v FROM meta WHERE k = $1", &[&k]).map(|r| r.get::<_, i64>(0)).unwrap_or(0) as u64 }

// ---------------------------------------------------------------- pgint

const INT_SCHEMA: &str = "
DROP TABLE IF EXISTS node, edge, keymap, meta, position CASCADE;
CREATE TABLE node(id int4 NOT NULL, rec bytea NOT NULL) WITH (fillfactor = 85);
ALTER TABLE node ALTER COLUMN rec SET STORAGE PLAIN;
CREATE TABLE edge(parent int4 NOT NULL, idx int2 NOT NULL, child int4 NOT NULL, mv int2 NOT NULL);
CREATE TABLE keymap(key bytea NOT NULL, id int4 NOT NULL);
ALTER TABLE keymap ALTER COLUMN key SET STORAGE PLAIN;
CREATE TABLE meta(k int4 PRIMARY KEY, v int8 NOT NULL);
";

pub fn bulk_load_int(o: &OpenOpts, ds: &Dataset) {
    let mut c = connect(&o.pg);
    c.batch_execute(INT_SCHEMA).unwrap();
    {
        let sink = c.copy_in("COPY node(id, rec) FROM STDIN (FORMAT binary)").unwrap();
        let mut w = BinaryCopyInWriter::new(sink, &[Type::INT4, Type::BYTEA]);
        for id in 0..ds.n as u32 { w.write(&[&(id as i32), &ds.node_bytes(id)]).unwrap(); }
        w.finish().unwrap();
    }
    {
        let sink = c.copy_in("COPY edge(parent, idx, child, mv) FROM STDIN (FORMAT binary)").unwrap();
        let mut w = BinaryCopyInWriter::new(sink, &[Type::INT4, Type::INT2, Type::INT4, Type::INT2]);
        let mut es = Vec::new();
        for id in 0..ds.n as u32 {
            es.clear();
            ds.children(id, &mut es);
            for (i, e) in es.iter().enumerate() { w.write(&[&(id as i32), &(i as i16), &(e.child as i32), &(e.mv as i16)]).unwrap(); }
        }
        w.finish().unwrap();
    }
    {
        let sink = c.copy_in("COPY keymap(key, id) FROM STDIN (FORMAT binary)").unwrap();
        let mut w = BinaryCopyInWriter::new(sink, &[Type::BYTEA, Type::INT4]);
        for id in 0..ds.n as u32 { let k = key_of(id); w.write(&[&&k[..], &(id as i32)]).unwrap(); }
        w.finish().unwrap();
    }
    c.batch_execute(&format!("
        ALTER TABLE node ADD PRIMARY KEY (id);
        ALTER TABLE edge ADD PRIMARY KEY (parent, idx);
        CREATE INDEX edge_child ON edge(child, parent);
        ALTER TABLE keymap ADD PRIMARY KEY (key);
        INSERT INTO meta VALUES (1, 0), (2, {});
    ", ds.n)).unwrap();
    for s in ["VACUUM (ANALYZE) node", "VACUUM (ANALYZE) edge", "VACUUM (ANALYZE) keymap", "CHECKPOINT"] { c.batch_execute(s).unwrap(); }
}

struct IntSt { nodes: Statement, children: Statement, parents: Statement, keys: Statement }
fn int_prepare(c: &mut Client) -> IntSt {
    IntSt {
        nodes: c.prepare("SELECT id, rec FROM node WHERE id = ANY($1)").unwrap(),
        children: c.prepare("SELECT parent, child, mv FROM edge WHERE parent = ANY($1) ORDER BY parent, idx").unwrap(),
        parents: c.prepare("SELECT DISTINCT child, parent FROM edge WHERE child = ANY($1)").unwrap(),
        keys: c.prepare("SELECT key, id FROM keymap WHERE key = ANY($1)").unwrap(),
    }
}
fn to_i32(ids: &[u32]) -> Vec<i32> { let mut v: Vec<i32> = ids.iter().map(|&x| x as i32).collect(); v.sort_unstable(); v.dedup(); v }

fn int_nodes(c: &mut Client, st: &IntSt, ids: &[u32], out: &mut Vec<Node>) {
    out.clear();
    if ids.is_empty() { return; }
    let rows = c.query(&st.nodes, &[&to_i32(ids)]).unwrap();
    let mut m: HashMap<u32, Node> = HashMap::with_capacity(rows.len());
    for r in &rows { m.insert(r.get::<_, i32>(0) as u32, Node::from_bytes(r.get::<_, &[u8]>(1))); }
    out.extend(ids.iter().map(|id| m[id]));
}
fn emit_grouped<T: Copy>(ids: &[u32], groups: &HashMap<u32, Vec<T>>, offs: &mut Vec<u32>, out: &mut Vec<T>) {
    offs.clear(); out.clear(); offs.push(0);
    for id in ids {
        if let Some(g) = groups.get(id) { out.extend_from_slice(g); }
        offs.push(out.len() as u32);
    }
}
fn int_children(c: &mut Client, st: &IntSt, ids: &[u32], offs: &mut Vec<u32>, out: &mut Vec<Edge>) {
    let mut g: HashMap<u32, Vec<Edge>> = HashMap::new();
    if !ids.is_empty() {
        for r in c.query(&st.children, &[&to_i32(ids)]).unwrap() {
            g.entry(r.get::<_, i32>(0) as u32).or_default().push(Edge { child: r.get::<_, i32>(1) as u32, mv: r.get::<_, i16>(2) as u16 });
        }
    }
    emit_grouped(ids, &g, offs, out);
}
fn int_parents(c: &mut Client, st: &IntSt, ids: &[u32], offs: &mut Vec<u32>, out: &mut Vec<u32>) {
    let mut g: HashMap<u32, Vec<u32>> = HashMap::new();
    if !ids.is_empty() {
        for r in c.query(&st.parents, &[&to_i32(ids)]).unwrap() { g.entry(r.get::<_, i32>(0) as u32).or_default().push(r.get::<_, i32>(1) as u32); }
    }
    emit_grouped(ids, &g, offs, out);
}
fn int_keys(c: &mut Client, st: &IntSt, keys: &[[u8; 16]], out: &mut Vec<Option<u32>>) {
    out.clear();
    if keys.is_empty() { return; }
    let ks: Vec<&[u8]> = keys.iter().map(|k| &k[..]).collect();
    let mut m: HashMap<[u8; 16], u32> = HashMap::new();
    for r in c.query(&st.keys, &[&ks]).unwrap() { let k: &[u8] = r.get(0); m.insert(k.try_into().unwrap(), r.get::<_, i32>(1) as u32); }
    out.extend(keys.iter().map(|k| m.get(k).copied()));
}

pub struct PgInt { c: Client, st: IntSt, conn: String, seq: u64, n: u32, w0: u64 }
pub struct PgIntReader { c: Client, st: IntSt, in_txn: bool }

impl PgInt {
    pub fn open(o: &OpenOpts) -> PgInt {
        let mut c = connect(&o.pg);
        if !o.durable { c.batch_execute("SET synchronous_commit = off").unwrap(); }
        let st = int_prepare(&mut c);
        let (seq, n, w0) = (meta(&mut c, 1), meta(&mut c, 2) as u32, pg_written(&mut c));
        PgInt { c, st, conn: o.pg.clone(), seq, n, w0 }
    }
}
impl Reader for PgInt {
    fn node_count(&mut self) -> u32 { self.n }
    fn get_nodes(&mut self, ids: &[u32], out: &mut Vec<Node>) { int_nodes(&mut self.c, &self.st, ids, out) }
    fn get_children(&mut self, ids: &[u32], offs: &mut Vec<u32>, out: &mut Vec<Edge>) { int_children(&mut self.c, &self.st, ids, offs, out) }
    fn get_parents(&mut self, ids: &[u32], offs: &mut Vec<u32>, out: &mut Vec<u32>) { int_parents(&mut self.c, &self.st, ids, offs, out) }
    fn lookup_keys(&mut self, keys: &[[u8; 16]], out: &mut Vec<Option<u32>>) { int_keys(&mut self.c, &self.st, keys, out) }
}
impl Reader for PgIntReader {
    fn begin(&mut self) { if !self.in_txn { self.c.batch_execute("BEGIN ISOLATION LEVEL REPEATABLE READ, READ ONLY").unwrap(); self.in_txn = true; } }
    fn end(&mut self) { if self.in_txn { self.c.batch_execute("COMMIT").unwrap(); self.in_txn = false; } }
    fn node_count(&mut self) -> u32 { meta(&mut self.c, 2) as u32 }
    fn get_nodes(&mut self, ids: &[u32], out: &mut Vec<Node>) { int_nodes(&mut self.c, &self.st, ids, out) }
    fn get_children(&mut self, ids: &[u32], offs: &mut Vec<u32>, out: &mut Vec<Edge>) { int_children(&mut self.c, &self.st, ids, offs, out) }
    fn get_parents(&mut self, ids: &[u32], offs: &mut Vec<u32>, out: &mut Vec<u32>) { int_parents(&mut self.c, &self.st, ids, offs, out) }
    fn lookup_keys(&mut self, keys: &[[u8; 16]], out: &mut Vec<Option<u32>>) { int_keys(&mut self.c, &self.st, keys, out) }
}
impl Store for PgInt {
    fn name(&self) -> String { "PostgreSQL 16 (integer ids)".into() }
    fn apply_batch(&mut self, b: &Batch) {
        let mut tx = self.c.transaction().unwrap();
        if !b.new_nodes.is_empty() {
            let ids: Vec<i32> = (0..b.new_nodes.len() as u32).map(|k| (b.first_new + k) as i32).collect();
            let recs: Vec<Vec<u8>> = b.new_nodes.iter().map(|n| n.to_bytes().to_vec()).collect();
            let keys: Vec<Vec<u8>> = ids.iter().map(|&i| key_of(i as u32).to_vec()).collect();
            tx.execute("INSERT INTO node(id, rec) SELECT * FROM unnest($1::int4[], $2::bytea[])", &[&ids, &recs]).unwrap();
            tx.execute("INSERT INTO keymap(key, id) SELECT * FROM unnest($1::bytea[], $2::int4[])", &[&keys, &ids]).unwrap();
        }
        if !b.puts.is_empty() {
            let ids: Vec<i32> = b.puts.iter().map(|(i, _)| *i as i32).collect();
            let recs: Vec<Vec<u8>> = b.puts.iter().map(|(_, n)| n.to_bytes().to_vec()).collect();
            tx.execute("UPDATE node SET rec = u.rec FROM unnest($1::int4[], $2::bytea[]) AS u(id, rec) WHERE node.id = u.id", &[&ids, &recs]).unwrap();
        }
        if !b.children.is_empty() {
            let (mut ps, mut ix, mut cs, mut ms) = (Vec::new(), Vec::new(), Vec::new(), Vec::new());
            for (p, es) in &b.children { for (i, e) in es.iter().enumerate() { ps.push(*p as i32); ix.push(i as i16); cs.push(e.child as i32); ms.push(e.mv as i16); } }
            tx.execute("INSERT INTO edge(parent, idx, child, mv) SELECT * FROM unnest($1::int4[], $2::int2[], $3::int4[], $4::int2[]) ON CONFLICT DO NOTHING", &[&ps, &ix, &cs, &ms]).unwrap();
        }
        let n = self.n + b.new_nodes.len() as u32;
        tx.execute("UPDATE meta SET v = CASE k WHEN 1 THEN $1::int8 ELSE $2::int8 END WHERE k IN (1, 2)", &[&(b.seq as i64), &(n as i64)]).unwrap();
        tx.commit().unwrap();
        self.seq = b.seq;
        self.n = n;
    }
    fn last_seq(&mut self) -> u64 { self.seq }
    fn new_reader(&self) -> Box<dyn Reader + Send> { let mut c = connect(&self.conn); let st = int_prepare(&mut c); Box::new(PgIntReader { c, st, in_txn: false }) }
    fn disk_bytes(&mut self) -> u64 { db_size(&mut self.c) }
    fn physical_bytes_written(&mut self) -> Option<u64> { Some(pg_written(&mut self.c).saturating_sub(self.w0)) }
    fn settle(&mut self) { self.c.batch_execute("CHECKPOINT").unwrap(); }
}

// ---------------------------------------------------------------- pgv1

const V1_SCHEMA: &str = "
DROP TABLE IF EXISTS node, edge, keymap, meta, position CASCADE;
CREATE TABLE position(
  key varchar(64) NOT NULL, fen text NOT NULL, eval_cp int4, status varchar(10) NOT NULL DEFAULT 'UNKNOWN',
  closure varchar(8), proof varchar(8), best_move varchar(8), backed_eval int4, backed_move varchar(8),
  backed_plies int4 NOT NULL DEFAULT 0, backed_nodes int8 NOT NULL DEFAULT 0, won_line text, mate_in int4, clock_slack int2,
  last_analysis jsonb, analysis_passes jsonb NOT NULL DEFAULT '[]', expanded bool NOT NULL DEFAULT false, reachable bool NOT NULL DEFAULT false,
  depth_invested int4 NOT NULL DEFAULT 0, nodes_invested int8 NOT NULL DEFAULT 0, time_invested float8 NOT NULL DEFAULT 0,
  visits int4 NOT NULL DEFAULT 0, priority float8 NOT NULL DEFAULT 0, campaign_id int8, updated timestamptz NOT NULL DEFAULT now());
CREATE TABLE edge(id bigserial NOT NULL, parent_id varchar(64) NOT NULL, move_uci varchar(8) NOT NULL, child_id varchar(64) NOT NULL);
CREATE TABLE meta(k int4 PRIMARY KEY, v int8 NOT NULL);
";
const V1_INDEXES: &str = "
ALTER TABLE position ADD PRIMARY KEY (key);
CREATE INDEX pos_status ON position(status);
CREATE INDEX pos_priority ON position(priority);
CREATE INDEX pos_campaign ON position(campaign_id);
CREATE INDEX pos_updated ON position(updated);
ALTER TABLE edge ADD PRIMARY KEY (id);
CREATE UNIQUE INDEX uniq_parent_move ON edge(parent_id, move_uci);
CREATE INDEX edge_child ON edge(child_id);
";

fn fen_of(black: bool) -> &'static str {
    if black { "r1bqkb1r/pp1ppppp/2n2n2/2p5/4P3/2N2N2/PPPP1PPP/R1BQKB1R b KQkq - 0 1" } else { "r1bqkb1r/pp1ppppp/2n2n2/2p5/4P3/2N2N2/PPPP1PPP/R1BQKB1R w KQkq - 0 1" }
}
fn status_txt(st: u16) -> &'static str { match st { ST_WHITE => "WHITE_WIN", ST_BLACK => "BLACK_WIN", ST_DRAW => "DRAW", _ => "UNKNOWN" } }
fn closure_txt(c: u16) -> Option<&'static str> { match c { CL_MATE_PV => Some("MATE_PV"), CL_TERMINAL => Some("TERMINAL"), CL_MINIMAX => Some("MINIMAX"), CL_TB => Some("TB"), _ => None } }
fn mv_txt(m: u16) -> String { format!("{m:04x}") }
fn mv_of(s: &str) -> u16 { u16::from_str_radix(s, 16).unwrap_or(0) }
fn opt_cp(v: i16) -> Option<i32> { if v == NO_EVAL { None } else { Some(v as i32) } }
/// About 1.1 KB, the average size of `last_analysis` in production.
fn analysis_filler() -> String {
    let pv: Vec<String> = (0..24).map(|i| format!("\"{}{}{}{}\"", (b'a' + (i % 8) as u8) as char, 1 + i % 8, (b'a' + ((i + 3) % 8) as u8) as char, 1 + (i + 2) % 8)).collect();
    let line = format!("{{\"move\":\"e2e4\",\"eval_cp\":123,\"mate\":null,\"pv\":[{}]}}", pv.join(","));
    format!("{{\"_budget\":128000000,\"lines\":[{}]}}", vec![line; 5].join(","))
}

pub fn bulk_load_v1(o: &OpenOpts, ds: &Dataset) {
    let mut c = connect(&o.pg);
    c.batch_execute(V1_SCHEMA).unwrap();
    let filler = analysis_filler();
    {
        let mut w = c.copy_in("COPY position(key, fen, eval_cp, status, closure, best_move, backed_eval, backed_move, backed_plies, backed_nodes, mate_in, clock_slack, expanded, reachable, nodes_invested, visits, last_analysis) FROM STDIN").unwrap();
        let mut line = String::with_capacity(2048);
        for id in 0..ds.n as u32 {
            let n = ds.node(id);
            use std::fmt::Write as _;
            line.clear();
            let nul = |v: Option<String>| v.unwrap_or_else(|| "\\N".to_string());
            write!(line, "{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\n",
                hex_key_of(id), fen_of(n.black_to_move()), nul(opt_cp(n.eval).map(|v| v.to_string())), status_txt(n.status()),
                nul(closure_txt(n.closure()).map(|s| s.to_string())), nul(if n.best_move != 0 { Some(mv_txt(n.best_move)) } else { None }),
                nul(opt_cp(n.backed).map(|v| v.to_string())), nul(if n.backed != NO_EVAL { Some(mv_txt(n.backed_move)) } else { None }),
                n.backed_plies, n.backed_nodes as i64, nul(if !n.is_open() { Some(n.mate_in.to_string()) } else { None }),
                nul(if !n.is_open() { Some(n.slack.to_string()) } else { None }), if n.expanded() { "t" } else { "f" },
                if n.flags & F_REACHABLE != 0 { "t" } else { "f" }, n.nodes as i64, n.visits, if n.nodes > 0 { filler.as_str() } else { "\\N" }).unwrap();
            w.write_all(line.as_bytes()).unwrap();
        }
        w.finish().unwrap();
    }
    {
        let mut w = c.copy_in("COPY edge(parent_id, move_uci, child_id) FROM STDIN").unwrap();
        let mut es = Vec::new();
        for id in 0..ds.n as u32 {
            es.clear();
            ds.children(id, &mut es);
            if es.is_empty() { continue; }
            let pk = hex_key_of(id);
            for e in &es { w.write_all(format!("{}\t{}\t{}\n", pk, mv_txt(e.mv), hex_key_of(e.child)).as_bytes()).unwrap(); }
        }
        w.finish().unwrap();
    }
    c.batch_execute(V1_INDEXES).unwrap();
    c.batch_execute(&format!("INSERT INTO meta VALUES (1, 0), (2, {});", ds.n)).unwrap();
    for s in ["VACUUM (ANALYZE) position", "VACUUM (ANALYZE) edge", "CHECKPOINT"] { c.batch_execute(s).unwrap(); }
}

struct V1St { nodes: Statement, children: Statement, parents: Statement, keys: Statement }
fn v1_prepare(c: &mut Client) -> V1St {
    V1St {
        nodes: c.prepare("SELECT key, fen, eval_cp, status, closure, best_move, backed_eval, backed_move, backed_plies, backed_nodes, mate_in, clock_slack, expanded, reachable, nodes_invested, visits FROM position WHERE key = ANY($1)").unwrap(),
        children: c.prepare("SELECT parent_id, move_uci, child_id FROM edge WHERE parent_id = ANY($1) ORDER BY parent_id, id").unwrap(),
        parents: c.prepare("SELECT DISTINCT child_id, parent_id FROM edge WHERE child_id = ANY($1)").unwrap(),
        keys: c.prepare("SELECT key FROM position WHERE key = ANY($1)").unwrap(),
    }
}
fn hexes(ids: &[u32]) -> Vec<String> { let mut v: Vec<u32> = ids.to_vec(); v.sort_unstable(); v.dedup(); v.iter().map(|&i| hex_key_of(i)).collect() }

fn v1_nodes(c: &mut Client, st: &V1St, ids: &[u32], out: &mut Vec<Node>) {
    out.clear();
    if ids.is_empty() { return; }
    let mut m: HashMap<u32, Node> = HashMap::new();
    for r in c.query(&st.nodes, &[&hexes(ids)]).unwrap() {
        let mut n = Node::default();
        let st_ = match r.get::<_, &str>(3) { "WHITE_WIN" => ST_WHITE, "BLACK_WIN" => ST_BLACK, "DRAW" => ST_DRAW, _ => ST_UNKNOWN };
        let cl = match r.get::<_, Option<&str>>(4) { Some("MATE_PV") => CL_MATE_PV, Some("TERMINAL") => CL_TERMINAL, Some("MINIMAX") => CL_MINIMAX, Some("TB") => CL_TB, _ => CL_NONE };
        n.set_status(st_, cl);
        if r.get::<_, &str>(1).contains(" b ") { n.flags |= F_BLACK_TO_MOVE; }
        if r.get::<_, bool>(12) { n.flags |= F_EXPANDED; }
        if r.get::<_, bool>(13) { n.flags |= F_REACHABLE; }
        n.eval = r.get::<_, Option<i32>>(2).map_or(NO_EVAL, |v| v as i16);
        n.best_move = r.get::<_, Option<&str>>(5).map_or(0, mv_of);
        n.backed = r.get::<_, Option<i32>>(6).map_or(NO_EVAL, |v| v as i16);
        n.backed_move = r.get::<_, Option<&str>>(7).map_or(0, mv_of);
        n.backed_plies = r.get::<_, i32>(8) as u16;
        n.backed_nodes = r.get::<_, i64>(9) as u64;
        n.mate_in = r.get::<_, Option<i32>>(10).unwrap_or(0) as u16;
        n.slack = r.get::<_, Option<i16>>(11).unwrap_or(0) as u8;
        n.nodes = r.get::<_, i64>(14) as u64;
        n.visits = r.get::<_, i32>(15) as u8;
        m.insert(id_of_hex_key(r.get::<_, &str>(0)), n);
    }
    out.extend(ids.iter().map(|id| m[id]));
}
fn v1_children(c: &mut Client, st: &V1St, ids: &[u32], offs: &mut Vec<u32>, out: &mut Vec<Edge>) {
    let mut g: HashMap<u32, Vec<Edge>> = HashMap::new();
    if !ids.is_empty() {
        for r in c.query(&st.children, &[&hexes(ids)]).unwrap() {
            g.entry(id_of_hex_key(r.get::<_, &str>(0))).or_default().push(Edge { child: id_of_hex_key(r.get::<_, &str>(2)), mv: mv_of(r.get::<_, &str>(1)) });
        }
    }
    emit_grouped(ids, &g, offs, out);
}
fn v1_parents(c: &mut Client, st: &V1St, ids: &[u32], offs: &mut Vec<u32>, out: &mut Vec<u32>) {
    let mut g: HashMap<u32, Vec<u32>> = HashMap::new();
    if !ids.is_empty() {
        for r in c.query(&st.parents, &[&hexes(ids)]).unwrap() { g.entry(id_of_hex_key(r.get::<_, &str>(0))).or_default().push(id_of_hex_key(r.get::<_, &str>(1))); }
    }
    emit_grouped(ids, &g, offs, out);
}
fn v1_keys(c: &mut Client, st: &V1St, keys: &[[u8; 16]], out: &mut Vec<Option<u32>>) {
    out.clear();
    if keys.is_empty() { return; }
    let hs: Vec<String> = keys.iter().map(hex_key).collect();
    let mut found: HashMap<String, u32> = HashMap::new();
    for r in c.query(&st.keys, &[&hs]).unwrap() { let k: &str = r.get(0); found.insert(k.to_string(), id_of_hex_key(k)); }
    out.extend(hs.iter().map(|h| found.get(h).copied()));
}

pub struct PgV1 { c: Client, st: V1St, conn: String, seq: u64, n: u32, w0: u64, filler: String }
pub struct PgV1Reader { c: Client, st: V1St, in_txn: bool }

impl PgV1 {
    pub fn open(o: &OpenOpts) -> PgV1 {
        let mut c = connect(&o.pg);
        if !o.durable { c.batch_execute("SET synchronous_commit = off").unwrap(); }
        let st = v1_prepare(&mut c);
        let (seq, n, w0) = (meta(&mut c, 1), meta(&mut c, 2) as u32, pg_written(&mut c));
        PgV1 { c, st, conn: o.pg.clone(), seq, n, w0, filler: analysis_filler() }
    }
}
impl Reader for PgV1 {
    fn node_count(&mut self) -> u32 { self.n }
    fn get_nodes(&mut self, ids: &[u32], out: &mut Vec<Node>) { v1_nodes(&mut self.c, &self.st, ids, out) }
    fn get_children(&mut self, ids: &[u32], offs: &mut Vec<u32>, out: &mut Vec<Edge>) { v1_children(&mut self.c, &self.st, ids, offs, out) }
    fn get_parents(&mut self, ids: &[u32], offs: &mut Vec<u32>, out: &mut Vec<u32>) { v1_parents(&mut self.c, &self.st, ids, offs, out) }
    fn lookup_keys(&mut self, keys: &[[u8; 16]], out: &mut Vec<Option<u32>>) { v1_keys(&mut self.c, &self.st, keys, out) }
}
impl Reader for PgV1Reader {
    fn begin(&mut self) { if !self.in_txn { self.c.batch_execute("BEGIN ISOLATION LEVEL REPEATABLE READ, READ ONLY").unwrap(); self.in_txn = true; } }
    fn end(&mut self) { if self.in_txn { self.c.batch_execute("COMMIT").unwrap(); self.in_txn = false; } }
    fn node_count(&mut self) -> u32 { meta(&mut self.c, 2) as u32 }
    fn get_nodes(&mut self, ids: &[u32], out: &mut Vec<Node>) { v1_nodes(&mut self.c, &self.st, ids, out) }
    fn get_children(&mut self, ids: &[u32], offs: &mut Vec<u32>, out: &mut Vec<Edge>) { v1_children(&mut self.c, &self.st, ids, offs, out) }
    fn get_parents(&mut self, ids: &[u32], offs: &mut Vec<u32>, out: &mut Vec<u32>) { v1_parents(&mut self.c, &self.st, ids, offs, out) }
    fn lookup_keys(&mut self, keys: &[[u8; 16]], out: &mut Vec<Option<u32>>) { v1_keys(&mut self.c, &self.st, keys, out) }
}
impl Store for PgV1 {
    fn name(&self) -> String { "PostgreSQL 16 (v1 schema, text keys)".into() }
    fn apply_batch(&mut self, b: &Batch) {
        let mut tx = self.c.transaction().unwrap();
        if !b.new_nodes.is_empty() {
            let (mut key, mut fen, mut eval, mut st, mut cl, mut bm, mut mate, mut slack, mut reach) = (Vec::new(), Vec::new(), Vec::new(), Vec::new(), Vec::new(), Vec::new(), Vec::new(), Vec::new(), Vec::new());
            for (k, n) in b.new_nodes.iter().enumerate() {
                key.push(hex_key_of(b.first_new + k as u32));
                fen.push(fen_of(n.black_to_move()));
                eval.push(opt_cp(n.eval));
                st.push(status_txt(n.status()));
                cl.push(closure_txt(n.closure()));
                bm.push(if n.best_move != 0 { Some(mv_txt(n.best_move)) } else { None });
                mate.push(if n.is_open() { None } else { Some(n.mate_in as i32) });
                slack.push(if n.is_open() { None } else { Some(n.slack as i16) });
                reach.push(n.flags & F_REACHABLE != 0);
            }
            let p: [&(dyn ToSql + Sync); 9] = [&key, &fen, &eval, &st, &cl, &bm, &mate, &slack, &reach];
            tx.execute("INSERT INTO position(key, fen, eval_cp, status, closure, best_move, mate_in, clock_slack, reachable)
                        SELECT * FROM unnest($1::varchar[], $2::text[], $3::int4[], $4::varchar[], $5::varchar[], $6::varchar[], $7::int4[], $8::int2[], $9::bool[])", &p).unwrap();
        }
        if !b.puts.is_empty() {
            let (mut key, mut eval, mut st, mut cl, mut bm, mut be, mut bmv, mut bp, mut bn, mut mate, mut slack, mut exp, mut nodes, mut visits) =
                (Vec::new(), Vec::new(), Vec::new(), Vec::new(), Vec::new(), Vec::new(), Vec::new(), Vec::new(), Vec::new(), Vec::new(), Vec::new(), Vec::new(), Vec::new(), Vec::new());
            for (id, n) in &b.puts {
                key.push(hex_key_of(*id));
                eval.push(opt_cp(n.eval));
                st.push(status_txt(n.status()));
                cl.push(closure_txt(n.closure()));
                bm.push(if n.best_move != 0 { Some(mv_txt(n.best_move)) } else { None });
                be.push(opt_cp(n.backed));
                bmv.push(if n.backed != NO_EVAL { Some(mv_txt(n.backed_move)) } else { None });
                bp.push(n.backed_plies as i32);
                bn.push(n.backed_nodes as i64);
                mate.push(if n.is_open() { None } else { Some(n.mate_in as i32) });
                slack.push(if n.is_open() { None } else { Some(n.slack as i16) });
                exp.push(n.expanded());
                nodes.push(n.nodes as i64);
                visits.push(n.visits as i32);
            }
            let p: [&(dyn ToSql + Sync); 15] = [&key, &eval, &st, &cl, &bm, &be, &bmv, &bp, &bn, &mate, &slack, &exp, &nodes, &visits, &self.filler];
            tx.execute("UPDATE position p SET eval_cp = u.eval_cp, status = u.status, closure = u.closure, best_move = u.best_move,
                          backed_eval = u.backed_eval, backed_move = u.backed_move, backed_plies = u.backed_plies, backed_nodes = u.backed_nodes,
                          mate_in = u.mate_in, clock_slack = u.clock_slack, expanded = u.expanded, nodes_invested = u.nodes_invested, visits = u.visits,
                          last_analysis = CASE WHEN u.nodes_invested > 0 AND p.last_analysis IS NULL THEN ($15::text)::jsonb ELSE p.last_analysis END,
                          updated = now()
                        FROM unnest($1::varchar[], $2::int4[], $3::varchar[], $4::varchar[], $5::varchar[], $6::int4[], $7::varchar[], $8::int4[], $9::int8[],
                                    $10::int4[], $11::int2[], $12::bool[], $13::int8[], $14::int4[])
                             AS u(key, eval_cp, status, closure, best_move, backed_eval, backed_move, backed_plies, backed_nodes, mate_in, clock_slack, expanded, nodes_invested, visits)
                        WHERE p.key = u.key", &p).unwrap();
        }
        if !b.children.is_empty() {
            let (mut ps, mut ms, mut cs) = (Vec::new(), Vec::new(), Vec::new());
            for (p, es) in &b.children { let pk = hex_key_of(*p); for e in es { ps.push(pk.clone()); ms.push(mv_txt(e.mv)); cs.push(hex_key_of(e.child)); } }
            tx.execute("INSERT INTO edge(parent_id, move_uci, child_id) SELECT * FROM unnest($1::varchar[], $2::varchar[], $3::varchar[]) ON CONFLICT DO NOTHING", &[&ps, &ms, &cs]).unwrap();
        }
        let n = self.n + b.new_nodes.len() as u32;
        tx.execute("UPDATE meta SET v = CASE k WHEN 1 THEN $1::int8 ELSE $2::int8 END WHERE k IN (1, 2)", &[&(b.seq as i64), &(n as i64)]).unwrap();
        tx.commit().unwrap();
        self.seq = b.seq;
        self.n = n;
    }
    fn last_seq(&mut self) -> u64 { self.seq }
    fn new_reader(&self) -> Box<dyn Reader + Send> { let mut c = connect(&self.conn); let st = v1_prepare(&mut c); Box::new(PgV1Reader { c, st, in_txn: false }) }
    fn disk_bytes(&mut self) -> u64 { db_size(&mut self.c) }
    fn physical_bytes_written(&mut self) -> Option<u64> { Some(pg_written(&mut self.c).saturating_sub(self.w0)) }
    fn settle(&mut self) { self.c.batch_execute("CHECKPOINT").unwrap(); }
}
