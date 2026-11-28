//! How the rules read the graph: a node's edges with their children's
//! records, ext records with v1's defaults, the witness line.

use crate::conv::uci_order;
use crate::exact::Kid;
use av2_model::{Closure, Core, Edge, Ext, GraphRead, Move, PosId, Proof};

/// The edges of `id` with the children's records, ordered by move name (v1
/// `Edge.objects.filter(parent=pos).select_related('child').order_by('move_uci')`).
pub fn kids_by_move(g: &dyn GraphRead, id: PosId, buf: &mut Vec<Edge>) -> Vec<Kid> {
    buf.clear();
    g.children(id, buf);
    let mut kids: Vec<Kid> = buf.iter().map(|e| Kid { mv: e.mv, id: e.child, core: g.core(e.child) }).collect();
    kids.sort_by_key(|k| uci_order(k.mv));
    kids
}

/// The edges of `id` with the children's records, in edge order (v1 edge id
/// order: movegen order of a fresh expansion).
pub fn kids_in_edge_order(g: &dyn GraphRead, id: PosId, buf: &mut Vec<Edge>) -> Vec<Kid> {
    buf.clear();
    g.children(id, buf);
    buf.iter().map(|e| Kid { mv: e.mv, id: e.child, core: g.core(e.child) }).collect()
}

/// The ext record, or v1's defaults (every counter 0, no blobs).
#[inline]
pub fn ext_of(g: &dyn GraphRead, id: PosId) -> Ext {
    g.ext(id).unwrap_or_default()
}

/// The stored witness line (`Position.won_line`): its length in plies and
/// its first move.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct WonLine {
    pub plies: usize,
    pub first: Move,
}

pub fn won_line(g: &dyn GraphRead, id: PosId) -> WonLine {
    let Some(ext) = g.ext(id) else { return WonLine::default() };
    if ext.won_line.is_none() {
        return WonLine::default();
    }
    let Some(bytes) = g.blob(ext.won_line) else { return WonLine::default() };
    let text = String::from_utf8_lossy(&bytes);
    let mut it = text.split_whitespace();
    let first = it.next().map(|s| Move::parse(s).unwrap_or(Move::NONE)).unwrap_or(Move::NONE);
    let plies = if first.is_none() && text.split_whitespace().next().is_none() { 0 } else { 1 + it.count() };
    WonLine { plies, first }
}

/// DTM-4.1 `_own_witness_plies`: the distance a node claims by itself.
/// None unless its closure is `MATE_PV`; none for a `TB_LINE` proof; else the
/// length of `won_line`, or the stored `mate_in` when the line is empty.
/// Also returns the first move of the line when the line is not empty (read
/// by DTM-1.1 and DTM-3.1 only when the own witness exists).
pub fn own_witness(g: &dyn GraphRead, id: PosId, core: &Core) -> OwnWitness {
    if core.closure() != Closure::MatePv || core.proof() == Proof::TbLine {
        return OwnWitness::default();
    }
    let line = won_line(g, id);
    if line.plies > 0 {
        OwnWitness { plies: Some(line.plies as i64), line_first: Some(line.first) }
    } else {
        OwnWitness { plies: core.mate().map(i64::from), line_first: None }
    }
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct OwnWitness {
    /// `_own_witness_plies`.
    pub plies: Option<i64>,
    /// `won_line.split()[0]` when the line is not empty.
    pub line_first: Option<Move>,
}
