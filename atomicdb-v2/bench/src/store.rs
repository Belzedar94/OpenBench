//! The interface every candidate implements.
//!
//! Deliberately small.  Reads are batched (a wave of ids per call) so that a
//! client/server store such as Postgres is not charged one round trip per row;
//! embedded stores simply loop.  Writes arrive as one `Batch` that the store
//! must apply atomically and make durable before returning: the single writer
//! computes the batch against the committed state plus a private overlay, so a
//! store never needs to serve reads of uncommitted data.

use crate::dataset::Dataset;
use crate::model::{Batch, Edge, Node};

pub trait Reader {
    /// Pin a consistent snapshot for the reads that follow (no-op where meaningless).
    fn begin(&mut self) {}
    fn end(&mut self) {}
    /// False when the reads since `begin` may have straddled a batch (only the
    /// lock-free mmap readers can say so; the caller then repeats the read).
    fn validate(&mut self) -> bool { true }
    fn node_count(&mut self) -> u32;
    /// `out[i]` is the record of `ids[i]`.
    fn get_nodes(&mut self, ids: &[u32], out: &mut Vec<Node>);
    /// CSR: children of `ids[i]` are `out[offs[i]..offs[i+1]]`, in move order.
    fn get_children(&mut self, ids: &[u32], offs: &mut Vec<u32>, out: &mut Vec<Edge>);
    /// CSR: parents of `ids[i]` are `out[offs[i]..offs[i+1]]` (any order).
    fn get_parents(&mut self, ids: &[u32], offs: &mut Vec<u32>, out: &mut Vec<u32>);
    fn lookup_keys(&mut self, keys: &[[u8; 16]], out: &mut Vec<Option<u32>>);
}

pub trait Store: Reader {
    fn name(&self) -> String;
    /// Apply atomically; durable (fsync or equivalent) when this returns.
    fn apply_batch(&mut self, b: &Batch);
    /// Sequence number of the last durable batch (0 after a bulk load).
    fn last_seq(&mut self) -> u64;
    /// An independent read handle for another thread.
    fn new_reader(&self) -> Box<dyn Reader + Send>;
    /// Bytes on disk right now.
    fn disk_bytes(&mut self) -> u64;
    /// Bytes physically written by the store since it was opened, when the
    /// store can tell better than the process I/O counters can (mmap, Postgres).
    fn physical_bytes_written(&mut self) -> Option<u64> { None }
    /// Store-specific counters as a JSON object (cumulative since open).
    fn extra_stats(&mut self) -> String { "{}".into() }
    /// Flush whatever is buffered (checkpoint / memtable flush) so that
    /// `disk_bytes` and the write counters are comparable.
    fn settle(&mut self) {}
}

pub struct OpenOpts {
    pub path: std::path::PathBuf,
    /// Cache budget in bytes for stores that take one.
    pub cache_bytes: u64,
    /// Capacity to reserve for stores that must size files up front.
    pub cap_nodes: u64,
    pub cap_edges: u64,
    /// Postgres connection string.
    pub pg: String,
    /// false = relax durability to "no fsync per batch" (reported separately).
    pub durable: bool,
}

pub const KINDS: &[&str] = &["mmap", "lmdb", "sqlite", "rocksdb", "redb", "fjall", "pgint", "pgv1", "memwal", "mem"];

pub fn bulk_load(kind: &str, o: &OpenOpts, ds: &Dataset) {
    match kind {
        "mmap" => crate::stores::mmapcol::bulk_load(o, ds),
        "lmdb" => crate::stores::lmdb::bulk_load(o, ds),
        "sqlite" => crate::stores::sqlite::bulk_load(o, ds),
        "rocksdb" => crate::stores::rocks::bulk_load(o, ds),
        "redb" => crate::stores::redbs::bulk_load(o, ds),
        "fjall" => crate::stores::fjalls::bulk_load(o, ds),
        "pgint" => crate::stores::pg::bulk_load_int(o, ds),
        "pgv1" => crate::stores::pg::bulk_load_v1(o, ds),
        "memwal" => crate::stores::mem::bulk_load_wal(o, ds),
        "mem" => {}
        _ => panic!("unknown store {kind}"),
    }
}

pub fn open(kind: &str, o: &OpenOpts, ds: Option<&Dataset>) -> Box<dyn Store> {
    match kind {
        "mmap" => Box::new(crate::stores::mmapcol::MmapStore::open(o)),
        "lmdb" => Box::new(crate::stores::lmdb::LmdbStore::open(o)),
        "sqlite" => Box::new(crate::stores::sqlite::SqliteStore::open(o)),
        "rocksdb" => Box::new(crate::stores::rocks::RocksStore::open(o)),
        "redb" => Box::new(crate::stores::redbs::RedbStore::open(o)),
        "fjall" => Box::new(crate::stores::fjalls::FjallStore::open(o)),
        "pgint" => Box::new(crate::stores::pg::PgInt::open(o)),
        "pgv1" => Box::new(crate::stores::pg::PgV1::open(o)),
        "memwal" => Box::new(crate::stores::mem::MemWalStore::open(o)),
        "mem" => Box::new(crate::stores::mem::MemStore::from_dataset(ds.expect("mem store needs the dataset"))),
        _ => panic!("unknown store {kind}"),
    }
}

/// Size of a directory tree (or a single file) on disk.
pub fn dir_bytes(p: &std::path::Path) -> u64 {
    let Ok(md) = std::fs::metadata(p) else { return 0 };
    if md.is_file() { return md.len(); }
    let mut total = 0;
    if let Ok(rd) = std::fs::read_dir(p) {
        for e in rd.flatten() { total += dir_bytes(&e.path()); }
    }
    total
}
