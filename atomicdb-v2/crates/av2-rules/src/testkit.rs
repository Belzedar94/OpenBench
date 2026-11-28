//! An in-memory graph and fixture helpers for the rules' tests, the property
//! tests and the benchmarks.
//!
//! `TestGraph` has the store's semantics (`av2_store::mem::MemGraph` is the
//! store's oracle; this crate cannot depend on the store, which depends on
//! it).  The fixture helpers translate v1's Django fixtures: a position is
//! made from a FEN (`ingest.get_or_create_position`), children by playing a
//! move on it (`logic.apply_move`, here without legality: piece moves, side
//! flips, no en-passant right), edges in the order given (v1 edge ids).

use av2_model::graph::GraphError;
use av2_model::record::F_BLACK_TO_MOVE;
use av2_model::{BlobRef, Closure, Core, Edge, Ext, Fingerprint, GraphRead, GraphWrite, Key, Move, PosId, Proof, Status};
use std::collections::HashMap;

pub const START_FEN: &str = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";

#[derive(Default, Clone, Debug)]
pub struct TestGraph {
    cores: Vec<Core>,
    exts: HashMap<u64, Ext>,
    children: HashMap<u64, Vec<Edge>>,
    parents: HashMap<u64, Vec<u64>>,
    fens: Vec<Option<String>>,
    keys: HashMap<Fingerprint, u64>,
    blobs: HashMap<u64, Vec<u8>>,
    next_blob: u64,
    /// Number of `put_core` / `put_ext` calls (for tests that count writes).
    pub writes: u64,
}

impl TestGraph {
    pub fn new() -> TestGraph {
        TestGraph { next_blob: 1, ..Default::default() }
    }
    fn check(&self, id: PosId) -> Result<(), GraphError> {
        if (id.0 as usize) < self.cores.len() { Ok(()) } else { Err(GraphError::NoSuchPosition(id)) }
    }
}

impl GraphRead for TestGraph {
    fn len(&self) -> u64 {
        self.cores.len() as u64
    }
    fn core(&self, id: PosId) -> Core {
        self.cores.get(id.index()).copied().unwrap_or_default()
    }
    fn ext(&self, id: PosId) -> Option<Ext> {
        self.exts.get(&id.0).copied()
    }
    fn children(&self, id: PosId, out: &mut Vec<Edge>) {
        if let Some(v) = self.children.get(&id.0) {
            out.extend_from_slice(v);
        }
    }
    fn parents(&self, id: PosId, out: &mut Vec<PosId>) {
        if let Some(v) = self.parents.get(&id.0) {
            out.extend(v.iter().map(|&p| PosId(p)));
        }
    }
    fn lookup(&self, fp: &Fingerprint) -> Option<PosId> {
        self.keys.get(fp).map(|&i| PosId(i))
    }
    fn fen(&self, id: PosId) -> Option<String> {
        self.fens.get(id.index()).cloned().flatten()
    }
    fn blob(&self, r: BlobRef) -> Option<Vec<u8>> {
        self.blobs.get(&r.0).cloned()
    }
}

impl GraphWrite for TestGraph {
    fn create(&mut self, fp: Fingerprint, fen: Option<&str>, core: Core) -> Result<PosId, GraphError> {
        if let Some(&id) = self.keys.get(&fp) {
            return Err(GraphError::Duplicate(PosId(id)));
        }
        let id = self.cores.len() as u64;
        self.cores.push(Core { ext: 0, version: 0, ..core });
        self.fens.push(fen.map(str::to_string));
        self.keys.insert(fp, id);
        Ok(PosId(id))
    }
    fn put_core(&mut self, id: PosId, core: Core) -> Result<(), GraphError> {
        self.check(id)?;
        self.writes += 1;
        let c = &mut self.cores[id.index()];
        *c = Core { ext: c.ext, version: c.version, ..core };
        Ok(())
    }
    fn put_ext(&mut self, id: PosId, ext: Ext) -> Result<(), GraphError> {
        self.check(id)?;
        self.writes += 1;
        self.cores[id.index()].ext = 1;
        self.exts.insert(id.0, ext);
        Ok(())
    }
    fn set_children(&mut self, parent: PosId, edges: &[Edge]) -> Result<(), GraphError> {
        self.check(parent)?;
        if let Some(e) = edges.iter().find(|e| e.child.0 >= self.len()) {
            return Err(GraphError::NoSuchPosition(e.child));
        }
        let old = self.children.get(&parent.0).cloned().unwrap_or_default();
        for (k, e) in edges.iter().enumerate() {
            if old.iter().any(|x| x.child == e.child) || edges[..k].iter().any(|x| x.child == e.child) {
                continue;
            }
            self.parents.entry(e.child.0).or_default().push(parent.0);
        }
        self.children.insert(parent.0, edges.to_vec());
        Ok(())
    }
    fn put_blob(&mut self, bytes: &[u8]) -> Result<BlobRef, GraphError> {
        let r = self.next_blob;
        self.next_blob += 1;
        self.blobs.insert(r, bytes.to_vec());
        Ok(BlobRef(r))
    }
}

// ---------------------------------------------------------------- fixtures

pub fn mv(s: &str) -> Move {
    Move::parse(s).unwrap_or_else(|_| panic!("bad move {s}"))
}

/// Play `uci` on `fen` without legality (fixture helper, `logic.apply_move`
/// for the shapes the tests use): the piece moves (a promotion letter
/// replaces it), the side to move flips, the en-passant right is dropped,
/// castling rights are kept, counters `0 1`.
pub fn play(fen: &str, uci: &str) -> String {
    let m = mv(uci);
    let parts: Vec<&str> = fen.split(' ').collect();
    let mut board = [0u8; 64];
    for (ri, rank) in parts[0].split('/').enumerate() {
        let r = 7 - ri;
        let mut f = 0;
        for c in rank.bytes() {
            if c.is_ascii_digit() {
                f += (c - b'0') as usize;
            } else {
                board[r * 8 + f] = c;
                f += 1;
            }
        }
    }
    let mut piece = board[m.from_sq() as usize];
    board[m.from_sq() as usize] = 0;
    if m.promo() != 0 {
        let p = [0u8, b'n', b'b', b'r', b'q', b'k'][m.promo() as usize];
        piece = if piece.is_ascii_uppercase() { p.to_ascii_uppercase() } else { p };
    }
    board[m.to_sq() as usize] = piece;
    let mut out = String::new();
    for r in (0..8).rev() {
        let mut empty = 0;
        for f in 0..8 {
            let c = board[r * 8 + f];
            if c == 0 {
                empty += 1;
            } else {
                if empty > 0 {
                    out.push((b'0' + empty) as char);
                    empty = 0;
                }
                out.push(c as char);
            }
        }
        if empty > 0 {
            out.push((b'0' + empty) as char);
        }
        if r > 0 {
            out.push('/');
        }
    }
    let side = if parts.get(1) == Some(&"w") { "b" } else { "w" };
    let castling = parts.get(2).copied().unwrap_or("-");
    format!("{out} {side} {castling} - 0 1")
}

/// The twenty legal moves of the starting array for one side, in movegen
/// order (pawns a to h, single then double step, then knights): the moves of
/// the start position and of the replies after one knight move.
pub fn opening_moves(white: bool) -> Vec<String> {
    let (r2, r3, r4, kr, kt) = if white { ('2', '3', '4', '1', '3') } else { ('7', '6', '5', '8', '6') };
    let mut v = Vec::new();
    for f in 'a'..='h' {
        v.push(format!("{f}{r2}{f}{r3}"));
        v.push(format!("{f}{r2}{f}{r4}"));
    }
    for (from, tos) in [('b', ['a', 'c']), ('g', ['f', 'h'])] {
        for to in tos {
            v.push(format!("{from}{kr}{to}{kt}"));
        }
    }
    v
}

/// The moves of a side whose king's knight is out (on f3 for White, f6 for
/// Black) and everything else on its starting square: the sixteen pawn
/// moves, the knight's five, the other knight's two and the rook's one
/// (twenty-four, the positions after 1.Nf3 x and 1.Nf3 Nf6 2.Ng1).
pub fn developed_moves(white: bool) -> Vec<String> {
    let mut v: Vec<String> = opening_moves(white).into_iter().take(16).collect();
    let extra: &[&str] = if white {
        &["f3g1", "f3d4", "f3e5", "f3g5", "f3h4", "b1a3", "b1c3", "h1g1"]
    } else {
        &["f6g8", "f6d5", "f6e4", "f6g4", "f6h5", "b8a6", "b8c6", "h8g8"]
    };
    v.extend(extra.iter().map(|s| s.to_string()));
    v
}

/// The FEN after these moves from the start position (`_after(*moves)`).
pub fn after(moves: &[&str]) -> String {
    moves.iter().fold(START_FEN.to_string(), |f, m| play(&f, m))
}

/// A fixture builder over a [`TestGraph`].
#[derive(Default, Debug, Clone)]
pub struct Fixture {
    pub g: TestGraph,
}

impl Fixture {
    pub fn new() -> Fixture {
        Fixture { g: TestGraph::new() }
    }

    /// `get_or_create_position(fen)` (no terminal detection: fixtures set
    /// statuses by hand, as v1's tests do).
    pub fn pos(&mut self, fen: &str) -> PosId {
        let fp = Key::of_canonical_fen(fen).fingerprint();
        if let Some(id) = self.g.lookup(&fp) {
            return id;
        }
        let mut core = Core::default();
        if fen.split(' ').nth(1) == Some("b") {
            core.flags |= F_BLACK_TO_MOVE;
        }
        self.g.create(fp, Some(fen), core).unwrap()
    }

    /// A position known by a name (synthetic graphs): a unique FEN-less node.
    pub fn named(&mut self, name: &str, black_to_move: bool) -> PosId {
        let fp = Key::of_canonical_fen(name).fingerprint();
        if let Some(id) = self.g.lookup(&fp) {
            return id;
        }
        let mut core = Core::default();
        if black_to_move {
            core.flags |= F_BLACK_TO_MOVE;
        }
        self.g.create(fp, None, core).unwrap()
    }

    /// Append the edge `parent --uci--> child` (`Edge.objects.create`).
    pub fn edge(&mut self, parent: PosId, uci: &str, child: PosId) {
        let mut v = Vec::new();
        self.g.children(parent, &mut v);
        let m = mv(uci);
        if v.iter().any(|e| e.mv == m) {
            return;
        }
        v.push(Edge { child, mv: m });
        self.g.set_children(parent, &v).unwrap();
    }

    /// Play `uci` from `parent`'s FEN, create the child and the edge.
    pub fn child(&mut self, parent: PosId, uci: &str) -> PosId {
        let fen = self.g.fen(parent).expect("parent has a FEN");
        let c = self.pos(&play(&fen, uci));
        self.edge(parent, uci, c);
        c
    }

    /// `expand(pos)` with an explicit move list (movegen order): every child
    /// and edge, and `expanded = True`.
    pub fn expand_with(&mut self, parent: PosId, moves: &[String]) -> Vec<PosId> {
        let kids = moves.iter().map(|m| self.child(parent, m)).collect();
        self.set(parent, |c| c.set_flag(av2_model::record::F_EXPANDED, true));
        kids
    }

    /// `expand(pos)` of a position of the opening (twenty moves of its side).
    pub fn expand_opening(&mut self, parent: PosId) -> Vec<PosId> {
        let white = !self.core(parent).black_to_move();
        self.expand_with(parent, &opening_moves(white))
    }

    pub fn core(&self, id: PosId) -> Core {
        self.g.core(id)
    }
    pub fn ext(&self, id: PosId) -> Ext {
        self.g.ext(id).unwrap_or_default()
    }
    pub fn set(&mut self, id: PosId, f: impl FnOnce(&mut Core)) {
        let mut c = self.g.core(id);
        f(&mut c);
        self.g.put_core(id, c).unwrap();
    }
    pub fn set_ext(&mut self, id: PosId, f: impl FnOnce(&mut Ext)) {
        let mut e = self.g.ext(id).unwrap_or_default();
        f(&mut e);
        self.g.put_ext(id, e).unwrap();
    }
    pub fn close(&mut self, id: PosId, s: Status, c: Closure, p: Proof) {
        self.set(id, |core| core.set_status(s, c, p));
    }
    pub fn set_won_line(&mut self, id: PosId, line: &str) {
        let r = self.g.put_blob(line.as_bytes()).unwrap();
        self.set_ext(id, |e| e.won_line = r);
    }
    pub fn set_analysis(&mut self, id: PosId, json: &str) {
        let r = self.g.put_blob(json.as_bytes()).unwrap();
        self.set_ext(id, |e| e.analysis = r);
    }
    pub fn edge_child(&self, parent: PosId, uci: &str) -> PosId {
        let mut v = Vec::new();
        self.g.children(parent, &mut v);
        v.iter().find(|e| e.mv == mv(uci)).map(|e| e.child).unwrap_or_else(|| panic!("no edge {uci}"))
    }
    pub fn children(&self, parent: PosId) -> Vec<Edge> {
        let mut v = Vec::new();
        self.g.children(parent, &mut v);
        v
    }

    /// Write a closure as v1's tests do (`_close(pos, status=..., ...)`).
    #[allow(clippy::too_many_arguments)]
    pub fn closed(&mut self, id: PosId, s: Status, c: Closure, p: Proof, mate_in: Option<u16>, slack: Option<u8>, best: &str) {
        self.set(id, |core| {
            core.set_status(s, c, p);
            core.mate_in = mate_in.unwrap_or(av2_model::record::NO_MATE);
            core.clock_slack = slack.unwrap_or(av2_model::record::NO_SLACK);
            core.best_move = Move::parse_opt(best).unwrap();
        });
    }

    /// `ingest.backup_cascade(seeds)` with v1's settings.
    pub fn cascade(&mut self, seeds: &[PosId]) -> (crate::cascade::CascadeReport, crate::effects::Recorder) {
        self.cascade_cfg(&crate::config::RulesConfig::default(), seeds)
    }

    pub fn cascade_cfg(
        &mut self,
        cfg: &crate::config::RulesConfig,
        seeds: &[PosId],
    ) -> (crate::cascade::CascadeReport, crate::effects::Recorder) {
        let mut rec = crate::effects::Recorder::default();
        let mut z = crate::exact::BoardZeroing;
        let attribution = crate::effects::Attribution::default();
        let mut env = crate::cascade::Env { cfg, effects: &mut rec, zeroing: &mut z, attribution: &attribution };
        let report = crate::cascade::backup_cascade(&mut self.g, seeds, &mut env).unwrap();
        (report, rec)
    }

    /// `ingest.backup_backed_evals(seeds)` with v1's settings, no loop rules
    /// and an in-memory task model.
    pub fn ascent(&mut self, seeds: &[PosId]) -> (crate::ascent::AscentReport, crate::effects::Recorder, crate::purchase::MemTasks) {
        let mut tasks = crate::purchase::MemTasks::default();
        let (r, rec) = self.ascent_with(&crate::config::RulesConfig::default(), seeds, crate::ascent::AscentOpts::default(), &mut tasks);
        (r, rec, tasks)
    }

    pub fn ascent_with(
        &mut self,
        cfg: &crate::config::RulesConfig,
        seeds: &[PosId],
        opts: crate::ascent::AscentOpts,
        tasks: &mut dyn crate::purchase::TaskSink,
    ) -> (crate::ascent::AscentReport, crate::effects::Recorder) {
        let mut rec = crate::effects::Recorder::default();
        let mut loops = crate::loops::NoLoops;
        let mut env = crate::ascent::AscentEnv { cfg, effects: &mut rec, tasks, loops: &mut loops };
        let r = crate::ascent::backup_backed_evals(&mut self.g, seeds, opts, &mut env).unwrap();
        (r, rec)
    }

    /// `_backed_for(row, children, ...)` on the row as stored, with its
    /// children and showcase read from the graph (what one level decides).
    pub fn backed_for(&self, id: PosId) -> (crate::backed::Backed, Vec<crate::backed::Discrepancy>, Vec<crate::backed::Discrepancy>) {
        let row = crate::backed::Row { id, core: self.core(id), nodes_invested: self.ext(id).nodes_invested };
        let mut kids = crate::backed::children_of(&self.g, id, &mut Vec::new());
        let show = crate::backed::showcase_from_graph(&self.g, id);
        let (mut d, mut p) = (Vec::new(), Vec::new());
        let b = crate::backed::backed_for(&row, &mut kids, Some(&mut d), &show, Some(&mut p));
        (b, d, p)
    }

    /// The backed columns as stored.
    pub fn stored(&self, id: PosId) -> crate::backed::Backed {
        crate::backed::Backed::stored(&self.core(id), &self.ext(id))
    }

    /// The children of `parent` as the cascade reads them (by move).
    pub fn kids(&self, parent: PosId) -> Vec<crate::exact::Kid> {
        crate::read::kids_by_move(&self.g, parent, &mut Vec::new())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn play_and_opening_moves() {
        let f = play(START_FEN, "g1f3");
        assert_eq!(f, "rnbqkbnr/pppppppp/8/8/8/5N2/PPPPPPPP/RNBQKB1R b KQkq - 0 1");
        assert_eq!(opening_moves(true).len(), 20);
        assert_eq!(opening_moves(false)[0], "a7a6");
        assert_eq!(play("4k3/P7/8/8/8/8/8/4K3 w - - 0 1", "a7a8q"), "Q3k3/8/8/8/8/8/8/4K3 b - - 0 1");
    }
}
