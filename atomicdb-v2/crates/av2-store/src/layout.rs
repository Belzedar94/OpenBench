//! File layout of `graph/`: which files exist, their strides and the control
//! block.  See `docs/store.md` for the rationale.

use av2_model::fen::FEN_SIZE;
use av2_model::record::{CORE_SIZE, EXT_SIZE};

/// Magic at the start of `ctl`: "AV2GRAPH".
pub const MAGIC: u64 = 0x4850_4152_4732_5641;
/// Format version of the files in `graph/`.  Bump on any layout change.
pub const FORMAT_VERSION: u64 = 2;

pub type FileId = u8;
pub const F_CTL: FileId = 0;
pub const F_CORE: FileId = 1;
pub const F_EXT: FileId = 2;
pub const F_OUT: FileId = 3;
pub const F_PARENT: FileId = 4;
pub const F_FEN: FileId = 5;
pub const F_OUT_ARENA: FileId = 6;
pub const F_INX_ARENA: FileId = 7;
pub const F_BLOB_ARENA: FileId = 8;
pub const F_KM_DIR: FileId = 9;
pub const F_KM_ARENA: FileId = 10;
pub const NFILES: usize = 11;
/// Not a file: the logical redo record of a key-map shard rebuild (see
/// `batch::rebuild_region`).  Offset = shard index, payload = region, slots.
pub const F_KM_REBUILD: FileId = 0x80;
/// Not a file: the logical record of a key-map insert (slot + filter bits).
pub const F_KM_INSERT: FileId = 0x81;
/// Not a file: the logical record of a key-map compaction into a new
/// generation of `keymap.arena`.
pub const F_KM_COMPACT: FileId = 0x82;

/// File name of generation `generation` of the key-map arena.
pub fn arena_name(generation: u64) -> String {
    if generation == 0 { "keymap.arena".to_string() } else { format!("keymap.arena.{generation}") }
}

/// Generation of a key-map arena file name, if it is one.
pub fn arena_generation(name: &str) -> Option<u64> {
    if name == "keymap.arena" {
        return Some(0);
    }
    name.strip_prefix("keymap.arena.").and_then(|g| g.parse().ok())
}

pub const NAMES: [&str; NFILES] = [
    "ctl",
    "core.col",
    "ext.col",
    "out.col",
    "parent.col",
    "fen.col",
    "out.arena",
    "inx.arena",
    "blob.arena",
    "keymap.dir",
    "keymap.arena",
];

/// Bytes per position in each per-position column.
pub const OUT_SIZE: usize = 5; // offset + 1 of the out block, 40 bits
pub const PARENT_SIZE: usize = 5; // parent id + 1, or LIST | cell
pub const EDGE_SIZE: usize = 7; // child 40 bits + move 16 bits
pub const INX_CELL: usize = 10; // parent 40 bits + next cell 40 bits
pub const BLOCK_HEADER: usize = 2; // edge count u16
/// Marks a parent-column value as the head of a parent list (bit 39).
pub const PARENT_LIST: u64 = 1 << 39;

pub fn stride(f: FileId) -> usize {
    match f {
        F_CORE => CORE_SIZE,
        F_EXT => EXT_SIZE,
        F_OUT => OUT_SIZE,
        F_PARENT => PARENT_SIZE,
        F_FEN => FEN_SIZE,
        _ => 1,
    }
}

// ---- ctl: 4 KiB, u64 fields at fixed offsets.  Everything here except the
// constants written at creation is updated through redo records.
pub const CTL_SIZE: u64 = 4096;
pub const C_MAGIC: usize = 0;
pub const C_FORMAT: usize = 8;
pub const C_SHARD_BITS: usize = 16;
/// Positions (ids `0..n`).
pub const C_POSITIONS: usize = 24;
/// Ext records allocated.
pub const C_EXTS: usize = 32;
/// Bytes used in `out.arena`.
pub const C_OUT_TAIL: usize = 40;
/// Cells used in `inx.arena` (cell 0 is never used, so this starts at 1).
pub const C_INX_TAIL: usize = 48;
/// Bytes used in `blob.arena` (starts at 8 so that offset 0 means "none").
pub const C_BLOB_TAIL: usize = 56;
/// Bytes used in `keymap.arena`.
pub const C_KM_TAIL: usize = 64;
/// Bytes of `keymap.arena` in live shard regions (the rest is garbage left
/// by rebuilds, reclaimed by compaction).
pub const C_KM_LIVE: usize = 72;
/// The counters a batch rewrites, as one contiguous run.
pub const C_COUNTERS: usize = C_POSITIONS;
pub const C_COUNTERS_LEN: usize = 56;
/// Generation of the key-map arena file (written by compaction).
pub const C_KM_GEN: usize = 80;

// ---- key map
/// Bytes per key-map slot: 96-bit fingerprint + id+1 in 40 bits.
pub const SLOT: usize = 17;
/// Bytes per directory entry: region offset u64, slots u32, count u32.
pub const DIR_ENTRY: usize = 16;
/// A shard is regrown (doubled) before its load passes this.
pub const MAX_LOAD_NUM: u64 = 3;
pub const MAX_LOAD_DEN: u64 = 4;
/// Load a shard is built at by the bulk loader (dense: the graph is loaded once).
pub const BUILD_LOAD: f64 = 0.6;
/// Load a shard is rebuilt at when an insert pushes it past the maximum
/// (doubling: the amortised cost of a rebuild stays constant per insert).
pub const REBUILD_LOAD: f64 = 0.375;
pub const MIN_SLOTS: u64 = 16;
/// Bloom filter: bits per entry at the maximal load, 6 probes in one 64-byte block.
pub const FILTER_BITS_PER_KEY: u64 = 10;
pub const FILTER_PROBES: u32 = 6;

/// Filter blocks of a shard with `slots` slots.
pub fn filter_blocks(slots: u64) -> u64 {
    (slots * MAX_LOAD_NUM / MAX_LOAD_DEN * FILTER_BITS_PER_KEY).div_ceil(512).max(1)
}

/// Bytes of a shard region with `slots` slots: the filter, then the slots.
pub fn region_bytes(slots: u64) -> u64 {
    filter_blocks(slots) * 64 + slots * SLOT as u64
}

/// Granularity of file growth (also the Windows allocation granularity).
pub const GROW_ALIGN: u64 = 64 << 10;

/// Chunk size of the page-checksum manifest.
pub const CHUNK: u64 = 1 << 20;
