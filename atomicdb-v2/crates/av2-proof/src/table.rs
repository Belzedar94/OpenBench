//! The in-memory replacement of v1's `ProofNode` table (one per campaign).
//!
//! Owner decision D5: proof numbers are computed in memory, not stored.  v1
//! keeps one row per (campaign, position) "reached by a refresh" with `pn`,
//! `dn`, `expanded_in_proof`, `selected_child` (PN-0.1).  Row presence is
//! meaningful (served "absent", frontier membership), so the table keeps it.
//!
//! Layout, chosen from production's shape (39.4M rows on 206M positions,
//! ~80% of them proved rows `(0, INF)`):
//!
//! - `kind`: 2 bits per position: no row / explicit / implicit proved
//!   `(0, INF, False, NULL)` / implicit refuted `(INF, 0, False, NULL)`.
//!   A closed node's row always holds one of the two implicit values once it
//!   has been refreshed after closing, so it costs 2 bits.
//! - `rows`: every other row (open nodes, and closed nodes whose row is still
//!   stale from before the closure), in a hash map.
//!
//! About 52 MB + 40 B per explicit row on production's 206M positions.

use av2_model::{Move, PosId};
use std::collections::HashMap;
use std::hash::{BuildHasherDefault, Hasher};

use crate::numbers::PROOF_INFINITY;

/// One `ProofNode` row.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Row {
    pub pn: u64,
    pub dn: u64,
    /// v1 `expanded_in_proof`.
    pub expanded: bool,
    /// v1 `selected_child` (`Move::NONE` = NULL).
    pub selected: Move,
    /// Sequence number of the batch that created the row: stands for v1's
    /// `ProofNode.updated`, which in practice is the creation time (PN-0.1)
    /// and is only read to order `open_and_obligations` (PN-10.4).
    pub born: u64,
}

impl Row {
    pub const PROVED: Row = Row { pn: 0, dn: PROOF_INFINITY, expanded: false, selected: Move::NONE, born: 0 };
    pub const REFUTED: Row = Row { pn: PROOF_INFINITY, dn: 0, expanded: false, selected: Move::NONE, born: 0 };

    /// The four fields v1 compares to decide "changed" (PN-4.3).
    #[inline]
    pub fn same_values(&self, o: &Row) -> bool {
        self.pn == o.pn && self.dn == o.dn && self.expanded == o.expanded && self.selected == o.selected
    }
    #[inline]
    pub fn numbers(&self) -> (u64, u64) {
        (self.pn, self.dn)
    }
}

const K_NONE: u8 = 0;
const K_EXPLICIT: u8 = 1;
const K_PROVED: u8 = 2;
const K_REFUTED: u8 = 3;

/// A fast, deterministic hasher for position ids (iteration order of the map
/// is never used for a result).
#[derive(Default, Clone, Copy)]
pub struct IdHasher(u64);
impl Hasher for IdHasher {
    #[inline]
    fn finish(&self) -> u64 {
        self.0
    }
    #[inline]
    fn write(&mut self, bytes: &[u8]) {
        for &b in bytes {
            self.0 = (self.0.rotate_left(5) ^ b as u64).wrapping_mul(0x51_7c_c1_b7_27_22_0a_95);
        }
    }
    #[inline]
    fn write_u64(&mut self, v: u64) {
        self.0 = (v ^ (v >> 29)).wrapping_mul(0xbf58_476d_1ce4_e5b9);
        self.0 ^= self.0 >> 32;
    }
}
pub type IdMap<V> = HashMap<u64, V, BuildHasherDefault<IdHasher>>;
pub type IdSet = std::collections::HashSet<u64, BuildHasherDefault<IdHasher>>;

#[derive(Clone, Default)]
pub struct ProofTable {
    kind: Vec<u8>, // 4 positions per byte
    rows: IdMap<Row>,
    count: u64,
}

impl ProofTable {
    pub fn new() -> ProofTable {
        ProofTable::default()
    }

    #[inline]
    fn kind_of(&self, id: u64) -> u8 {
        self.kind.get((id / 4) as usize).map(|b| (b >> ((id % 4) * 2)) & 3).unwrap_or(K_NONE)
    }
    #[inline]
    fn set_kind(&mut self, id: u64, k: u8) {
        let i = (id / 4) as usize;
        if i >= self.kind.len() {
            self.kind.resize((i + 1).next_power_of_two().max(1024), 0);
        }
        let s = (id % 4) * 2;
        self.kind[i] = (self.kind[i] & !(3 << s)) | (k << s);
    }

    #[inline]
    pub fn has(&self, id: PosId) -> bool {
        self.kind_of(id.0) != K_NONE
    }

    /// The row of `id`, if the position has one.
    #[inline]
    pub fn get(&self, id: PosId) -> Option<Row> {
        match self.kind_of(id.0) {
            K_NONE => None,
            K_EXPLICIT => self.rows.get(&id.0).copied(),
            K_PROVED => Some(Row::PROVED),
            _ => Some(Row::REFUTED),
        }
    }

    /// Write a row (create or overwrite).
    pub fn put(&mut self, id: PosId, row: Row) {
        let old = self.kind_of(id.0);
        if old == K_NONE {
            self.count += 1;
        }
        let implicit = !row.expanded && row.selected.is_none();
        let k = if implicit && row.pn == 0 && row.dn == PROOF_INFINITY {
            K_PROVED
        } else if implicit && row.pn == PROOF_INFINITY && row.dn == 0 {
            K_REFUTED
        } else {
            K_EXPLICIT
        };
        if k == K_EXPLICIT {
            self.rows.insert(id.0, row);
        } else if old == K_EXPLICIT {
            self.rows.remove(&id.0);
        }
        self.set_kind(id.0, k);
    }

    /// Drop a row (only tests and the poison fixtures need this).
    pub fn remove(&mut self, id: PosId) {
        if self.kind_of(id.0) == K_EXPLICIT {
            self.rows.remove(&id.0);
        }
        if self.kind_of(id.0) != K_NONE {
            self.count -= 1;
        }
        self.set_kind(id.0, K_NONE);
    }

    /// Number of rows (v1: `ProofNode.objects.filter(campaign=...).count()`).
    pub fn len(&self) -> u64 {
        self.count
    }
    pub fn is_empty(&self) -> bool {
        self.count == 0
    }
    pub fn explicit_rows(&self) -> usize {
        self.rows.len()
    }

    /// Every position that has a row, in id order.
    pub fn ids(&self) -> impl Iterator<Item = PosId> + '_ {
        self.kind
            .iter()
            .enumerate()
            .flat_map(|(i, &b)| (0..4u64).filter(move |s| (b >> (s * 2)) & 3 != K_NONE).map(move |s| PosId(i as u64 * 4 + s)))
    }

    /// Explicit rows (open nodes and stale closed rows), unordered.
    pub fn explicit(&self) -> impl Iterator<Item = (PosId, &Row)> {
        self.rows.iter().map(|(&k, r)| (PosId(k), r))
    }

    /// Approximate heap bytes.
    pub fn heap_bytes(&self) -> usize {
        self.kind.capacity() + self.rows.capacity() * (std::mem::size_of::<(u64, Row)>() + 1)
    }

    /// Same rows (presence and the four v1 fields) as `other`.
    pub fn same_as(&self, other: &ProofTable) -> bool {
        self.count == other.count
            && self.ids().all(|id| match (self.get(id), other.get(id)) {
                (Some(a), Some(b)) => a.same_values(&b),
                _ => false,
            })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn implicit_and_explicit_rows_round_trip() {
        let mut t = ProofTable::new();
        assert!(t.get(PosId(5)).is_none());
        t.put(PosId(5), Row::PROVED);
        assert_eq!(t.get(PosId(5)), Some(Row::PROVED));
        assert_eq!(t.explicit_rows(), 0);
        let open = Row { pn: 3, dn: 9, expanded: true, selected: Move::parse("e2e4").unwrap(), born: 7 };
        t.put(PosId(5), open);
        assert_eq!(t.get(PosId(5)), Some(open));
        assert_eq!(t.explicit_rows(), 1);
        t.put(PosId(5), Row::REFUTED);
        assert_eq!(t.get(PosId(5)), Some(Row::REFUTED));
        assert_eq!(t.explicit_rows(), 0);
        // A "proved" row that still names a primary is not implicit.
        let held = Row { selected: Move::parse("a2a3").unwrap(), expanded: true, ..Row::PROVED };
        t.put(PosId(1_000_003), held);
        assert_eq!(t.get(PosId(1_000_003)), Some(held));
        assert_eq!(t.len(), 2);
        assert_eq!(t.ids().collect::<Vec<_>>(), vec![PosId(5), PosId(1_000_003)]);
        t.remove(PosId(5));
        assert_eq!(t.len(), 1);
        assert!(!t.has(PosId(5)));
    }
}
