//! Ports of v1's tree-level proof tests (they need a graph), plus property
//! tests.  v1 file/class/test names are kept in the function names so that
//! `test-map.csv` rows can be found here by grep.
//!
//! v1 builds its fixtures with `ingest.expand` (pyffish).  There is no move
//! generator in v2 yet (phase 3), and none of these rules looks at the
//! moves themselves: a fixture "expanded start position" is a white-to-move
//! node with 20 black-to-move children, edges in creation order.
#![allow(non_snake_case)]

use crate::campaign::Campaign;
use crate::clamp::{OR_CLAMP_MULTIPV, OR_CLAMP_NODES, proved_or_clamp};
use crate::frontier::{self, Saturation};
use crate::gate::GateConfig;
use crate::maintain::{Proofs, RecascadeEnd};
use crate::numbers::{Goal, PROOF_INFINITY, PROOF_MAX_LEAF};
use crate::rebuild::{Scope, rebuild};
use crate::refresh::{ChildEdge, Cycling, GuardReason, ProofEvent, RefreshConfig, compute_numbers, cycling_edges, load_children};
use crate::table::{ProofTable, Row};
use av2_model::record::{F_BLACK_TO_MOVE, NO_CP};
use av2_model::{Core, Edge, Ext, Fingerprint, GraphRead, GraphWrite, Move, PosId, Rng, Status};
use av2_store::mem::MemGraph;

const INF: u64 = PROOF_INFINITY;
const START: &str = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";
const AFTER_E4: &str = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1";
const BARE_BLACK: &str = "4k3/pp4pp/8/8/8/8/PP4PP/3RK2R b K - 0 1";
const MANOEUVRE: [&str; 6] = ["d1d2", "e8e7", "d2d1", "e7e8", "d1d2", "e8e7"];

/// A small graph builder over `MemGraph`.
struct T {
    g: MemGraph,
    next: u64,
}

fn mv(s: &str) -> Move {
    Move::parse(s).unwrap()
}

/// 20 distinct moves for fixture edges.
fn moves20() -> Vec<Move> {
    (0..20u8).map(|i| Move::new(i, i + 20, 0)).collect()
}

impl T {
    fn new() -> T {
        T { g: MemGraph::new(), next: 0 }
    }
    fn pos(&mut self, black: bool, status: Status, eval: Option<i16>) -> PosId {
        self.pos_fen(None, black, status, eval)
    }
    fn pos_fen(&mut self, fen: Option<&str>, black: bool, status: Status, eval: Option<i16>) -> PosId {
        let mut c = Core::default();
        c.set_status(status, Default::default(), Default::default());
        c.set_flag(F_BLACK_TO_MOVE, black);
        c.eval_cp = eval.unwrap_or(NO_CP);
        let mut fp = [0u8; 12];
        fp[..8].copy_from_slice(&self.next.to_be_bytes());
        self.next += 1;
        self.g.create(Fingerprint(fp), fen, c).unwrap()
    }
    fn edge(&mut self, parent: PosId, child: PosId, m: Move) {
        let mut es = Vec::new();
        self.g.children(parent, &mut es);
        es.push(Edge { child, mv: m });
        self.g.set_children(parent, &es).unwrap();
    }
    fn set_eval(&mut self, id: PosId, eval: Option<i16>) {
        let mut c = self.g.core(id);
        c.eval_cp = eval.unwrap_or(NO_CP);
        self.g.put_core(id, c).unwrap();
    }
    fn set_status(&mut self, id: PosId, s: Status) {
        let mut c = self.g.core(id);
        c.set_status(s, Default::default(), Default::default());
        self.g.put_core(id, c).unwrap();
    }
    fn set_analysis(&mut self, id: PosId, json: &serde_json::Value) {
        let r = self.g.put_blob(json.to_string().as_bytes()).unwrap();
        let e = Ext { analysis: r, ..self.g.ext(id).unwrap_or_default() };
        self.g.put_ext(id, e).unwrap();
    }
    fn kids(&self, id: PosId) -> Vec<Edge> {
        let mut es = Vec::new();
        self.g.children(id, &mut es);
        es
    }
    /// The "expanded start position": root (White to move) with 20 children.
    fn expanded_root(&mut self) -> (PosId, Vec<PosId>) {
        let root = self.pos_fen(Some(START), false, Status::Unknown, None);
        let kids = self.expand(root, true);
        (root, kids)
    }
    /// 20 children with the opposite side to move.
    fn expand(&mut self, parent: PosId, children_black: bool) -> Vec<PosId> {
        let fen = if children_black { AFTER_E4 } else { START };
        let mut ids = Vec::new();
        for m in moves20() {
            let c = self.pos_fen(Some(fen), children_black, Status::Unknown, None);
            self.edge(parent, c, m);
            ids.push(c);
        }
        ids
    }
}

fn campaign() -> Campaign {
    Campaign::default_for(1, PosId(0))
}

fn proofs(cfg: RefreshConfig) -> Proofs {
    Proofs::new(vec![campaign()], cfg)
}

fn row(p: &Proofs, id: PosId) -> Option<Row> {
    p.table(1).unwrap().get(id)
}

// ---------------------------------------------------------------- IncrementalMaintenanceTests

#[test]
fn test_proof_manager__numbers_are_written_for_the_touched_cone() {
    let mut t = T::new();
    let (root, kids) = t.expanded_root();
    let mut p = proofs(RefreshConfig::default());
    p.refresh(&t.g, &[kids[0]], 1, 0.0);
    let r = row(&p, root).unwrap();
    assert!(r.expanded);
    assert!(!r.selected.is_none());
    assert!(r.pn >= 1);
}

#[test]
fn test_proof_manager__one_level_costs_a_fixed_number_of_statements() {
    // v1 pins 4 SQL statements per level; the in-memory port pins that one
    // level processes exactly the level's rows, however wide the node is.
    let mut t = T::new();
    let (_root, kids) = t.expanded_root();
    let mut p = proofs(RefreshConfig::default());
    p.refresh(&t.g, &[kids[0]], 1, 0.0);
    p.cfg.max_plies = 1;
    let r = p.refresh(&t.g, &[kids[0]], 2, 0.0);
    assert_eq!((r.processed, r.plies), (1, 1));
}

#[test]
fn test_proof_manager__an_idempotent_refresh_stops_at_the_first_level() {
    let mut t = T::new();
    let (_root, kids) = t.expanded_root();
    let mut p = proofs(RefreshConfig::default());
    p.refresh(&t.g, &[kids[0]], 1, 0.0);
    let before = p.table(1).unwrap().len();
    let r = p.refresh(&t.g, &[kids[0]], 2, 0.0);
    assert_eq!(r.written, 0);
    assert_eq!(r.plies, 1);
    assert_eq!(p.table(1).unwrap().len(), before);
}

#[test]
fn test_proof_manager__a_transposition_with_two_parents_updates_both() {
    let mut t = T::new();
    let _ = t.expanded_root();
    let shared = t.pos(false, Status::Unknown, None);
    let first = t.pos(true, Status::Unknown, None);
    let second = t.pos(true, Status::Unknown, None);
    t.edge(first, shared, mv("g1h1"));
    t.edge(second, shared, mv("g1h1"));
    let mut p = proofs(RefreshConfig::default());
    p.refresh(&t.g, &[shared], 1, 0.0);
    assert!(row(&p, first).is_some());
    assert!(row(&p, second).is_some());
}

#[test]
fn test_proof_manager__guard_emits_an_event_and_stops() {
    let mut t = T::new();
    let (_root, kids) = t.expanded_root();
    let mut p = proofs(RefreshConfig { max_nodes: 1, ..RefreshConfig::default() });
    let r = p.refresh(&t.g, &[kids[0]], 1, 0.0);
    assert_eq!(r.guard, Some(GuardReason::NodeBudget));
    match &r.events[..] {
        [ProofEvent::Guard { reason, campaign, processed, plies, seed_count }] => {
            assert_eq!(reason.v1(), "node-budget");
            assert_eq!(campaign, "root-white-win");
            assert_eq!((*processed, *plies, *seed_count), (1, 1, 1));
        }
        other => panic!("{other:?}"),
    }
}

#[test]
fn ply_guard_is_reported_when_the_level_cap_ends_the_loop() {
    // PN-4.4: no direct v1 test exists for this path (spec gap).
    let mut t = T::new();
    let (root, kids) = t.expanded_root();
    let mut p = proofs(RefreshConfig { max_plies: 1, ..RefreshConfig::default() });
    let r = p.refresh(&t.g, &[kids[0]], 1, 0.0);
    assert_eq!(r.guard, Some(GuardReason::PlyGuard));
    assert!(row(&p, root).is_none());
}

#[test]
fn test_proof_manager__a_proved_child_drives_the_parent_to_zero_pn() {
    let mut t = T::new();
    let (root, kids) = t.expanded_root();
    t.set_status(kids[0], Status::WhiteWin);
    let mut p = proofs(RefreshConfig::default());
    p.refresh(&t.g, &[kids[0]], 1, 0.0);
    let r = row(&p, root).unwrap();
    assert_eq!(r.pn, 0);
    assert_eq!(r.selected, t.kids(root)[0].mv);
}

// test_proof_skip_locked.py::OutsideTransactionTests
#[test]
fn test_proof_skip_locked__numbers_are_still_written() {
    let mut t = T::new();
    let (root, _) = t.expanded_root();
    let mut p = proofs(RefreshConfig::default());
    p.refresh(&t.g, &[root], 1, 0.0);
    assert!(row(&p, root).is_some());
}

// ---------------------------------------------------------------- SelectedChildHysteresisTests

/// `_selected`: the root's primary with these numbers on its first children.
fn selected(t: &T, root: PosId, numbers: &[(u64, u64)], previous: Option<Move>) -> Move {
    let edges = t.kids(root);
    let children: Vec<ChildEdge> = edges[..numbers.len()]
        .iter()
        .map(|e| ChildEdge { mv: e.mv, child: e.child, node: crate::Node { status: Status::Unknown, eval_cp: None, black_to_move: true } })
        .collect();
    let rows: Vec<(PosId, Row)> = children
        .iter()
        .zip(numbers)
        .map(|(c, &(pn, dn))| (c.child, Row { pn, dn, expanded: false, selected: Move::NONE, born: 0 }))
        .collect();
    let (c, _) = compute_numbers(
        &t.g,
        Goal::WhiteWin,
        GateConfig::default(),
        root,
        crate::graph::ProofGraph::node(&t.g, root),
        &children,
        |id| rows.iter().find(|r| r.0 == id).map(|r| r.1),
        previous.unwrap_or(Move::NONE),
        &Cycling::new(),
    );
    c.selected
}

fn hysteresis_fixture() -> (T, PosId, Vec<Move>) {
    let mut t = T::new();
    let (root, _) = t.expanded_root();
    let moves = t.kids(root).iter().map(|e| e.mv).collect();
    (t, root, moves)
}

#[test]
fn test_proof_manager__a_flicker_does_not_move_the_primary() {
    let (t, r, m) = hysteresis_fixture();
    assert_eq!(selected(&t, r, &[(60, 5), (30, 5)], Some(m[0])), m[0]);
}
#[test]
fn test_proof_manager__crossing_the_threshold_does_move_it() {
    let (t, r, m) = hysteresis_fixture();
    assert_eq!(selected(&t, r, &[(60, 5), (19, 5)], Some(m[0])), m[1]);
}
#[test]
fn test_proof_manager__the_boundary_itself_is_not_a_change() {
    let (t, r, m) = hysteresis_fixture();
    assert_eq!(selected(&t, r, &[(60, 5), (20, 5)], Some(m[0])), m[0]);
}
#[test]
fn test_proof_manager__a_proved_primary_is_never_dethroned() {
    let (t, r, m) = hysteresis_fixture();
    assert_eq!(selected(&t, r, &[(0, INF), (0, INF)], Some(m[1])), m[1]);
    assert_eq!(selected(&t, r, &[(0, INF), (1, 5)], Some(m[0])), m[0]);
}
#[test]
fn test_proof_manager__a_refuted_primary_hands_the_seat_over() {
    let (t, r, m) = hysteresis_fixture();
    assert_eq!(selected(&t, r, &[(INF, 0), (7, 5)], Some(m[0])), m[1]);
}
#[test]
fn test_proof_manager__with_no_primary_yet_the_best_pn_takes_it() {
    let (t, r, m) = hysteresis_fixture();
    assert_eq!(selected(&t, r, &[(60, 5), (30, 5)], None), m[1]);
}
#[test]
fn test_proof_manager__a_primary_that_is_no_longer_a_child_is_not_honoured() {
    let (t, r, m) = hysteresis_fixture();
    assert_eq!(selected(&t, r, &[(60, 5), (30, 5)], Some(mv("h7h8"))), m[1]);
}
#[test]
fn test_proof_manager__an_and_node_is_not_sticky() {
    let mut t = T::new();
    let black = t.pos_fen(Some(AFTER_E4), true, Status::Unknown, None);
    let replies = t.expand(black, false);
    let edges = t.kids(black);
    let children: Vec<ChildEdge> =
        edges[..2].iter().map(|e| ChildEdge { mv: e.mv, child: e.child, node: crate::graph::ProofGraph::node(&t.g, e.child) }).collect();
    let rows = [
        (replies[0], Row { pn: 5, dn: 60, expanded: false, selected: Move::NONE, born: 0 }),
        (replies[1], Row { pn: 5, dn: 30, expanded: false, selected: Move::NONE, born: 0 }),
    ];
    let (c, _) = compute_numbers(
        &t.g,
        Goal::WhiteWin,
        GateConfig::default(),
        black,
        crate::graph::ProofGraph::node(&t.g, black),
        &children,
        |id| rows.iter().find(|r| r.0 == id).map(|r| r.1),
        edges[0].mv,
        &Cycling::new(),
    );
    assert_eq!(c.selected, edges[1].mv);
}
#[test]
fn test_proof_manager__the_stored_primary_survives_a_refresh() {
    let mut t = T::new();
    let (root, kids) = t.expanded_root();
    for &k in &kids {
        t.set_eval(k, Some(-400));
    }
    t.set_eval(kids[9], Some(3_000));
    let mut p = proofs(RefreshConfig::default());
    p.refresh(&t.g, &kids, 1, 0.0);
    let edges = t.kids(root);
    assert_eq!(row(&p, root).unwrap().selected, edges[9].mv);
    t.set_eval(kids[2], Some(3_000));
    p.refresh(&t.g, &[kids[2]], 2, 0.0);
    assert_eq!(row(&p, root).unwrap().selected, edges[9].mv);
}

// ---------------------------------------------------------------- test_proof_kpi.py::HeadlineNumbersTests

#[test]
fn test_proof_kpi__the_default_campaign_is_the_headline() {
    let mut t = T::new();
    let (root, _) = t.expanded_root();
    let mut p = proofs(RefreshConfig::default());
    p.refresh(&t.g, &[root], 1, 0.0);
    let r = row(&p, root).unwrap();
    assert_eq!(p.headline_numbers(&t.g), (Some(r.pn), Some(r.dn)));
}
#[test]
fn test_proof_kpi__no_active_campaign_means_no_headline() {
    let t = {
        let mut t = T::new();
        t.expanded_root();
        t
    };
    let mut p = proofs(RefreshConfig::default());
    p.campaigns[0].active = false;
    assert_eq!(p.headline_numbers(&t.g), (None, None));
}
#[test]
fn test_proof_kpi__a_proved_root_reads_zero_and_infinity() {
    let mut t = T::new();
    let (_root, kids) = t.expanded_root();
    t.set_status(kids[0], Status::WhiteWin);
    let mut p = proofs(RefreshConfig::default());
    p.refresh(&t.g, &[kids[0]], 1, 0.0);
    let (pn, dn) = p.headline_numbers(&t.g);
    assert_eq!(pn, Some(0));
    assert_eq!(crate::numbers::format_number(dn), "\u{221e}");
}
#[test]
fn root_numbers_without_a_row_are_the_root_leaf() {
    // PN-4.6: no row -> leaf numbers of the root (classic 1/1 with no eval).
    let mut t = T::new();
    let (_root, _) = t.expanded_root();
    let p = proofs(RefreshConfig::default());
    assert_eq!(p.root_numbers(&t.g, 1), Some((1, 1)));
    assert_eq!(p.numbers_for(&[PosId(0)]), vec![None]);
}

// ---------------------------------------------------------------- test_proof_cycles.py

struct Shuttle {
    t: T,
    a: PosId,
    b: PosId,
    c: PosId,
    d: PosId,
    p1: PosId,
}

fn shuttle() -> Shuttle {
    let mut t = T::new();
    // id 0 is a dummy campaign root so the shuttle is not the root.
    t.pos(false, Status::Unknown, None);
    let a = t.pos(false, Status::Unknown, Some(900));
    let b = t.pos(true, Status::Unknown, Some(950));
    let c = t.pos(false, Status::Unknown, Some(950));
    let d = t.pos(true, Status::Unknown, Some(900));
    let e = t.pos(true, Status::Unknown, Some(400));
    let q = t.pos(true, Status::Unknown, Some(-300));
    let p1 = t.pos(false, Status::WhiteWin, None);
    let p2 = t.pos(false, Status::WhiteWin, None);
    t.edge(a, e, mv("d2d4"));
    t.edge(a, b, mv("e8f7"));
    t.edge(b, p1, mv("e6f6"));
    t.edge(b, c, mv("e6d7"));
    t.edge(c, q, mv("a2a3"));
    t.edge(c, d, mv("f7e8"));
    t.edge(d, p2, mv("d7d8"));
    t.edge(d, a, mv("d7e6"));
    Shuttle { t, a, b, c, d, p1 }
}

impl Shuttle {
    fn poison(&self, p: &mut Proofs) {
        for (n, s) in [(self.a, "e8f7"), (self.b, "e6d7"), (self.c, "f7e8"), (self.d, "d7e6")] {
            p.table_mut(1).unwrap().put(n, Row { pn: 1, dn: INF, expanded: true, selected: mv(s), born: 0 });
        }
    }
    fn seeds(&self) -> Vec<PosId> {
        vec![self.a, self.b, self.c, self.d]
    }
    fn children(&self, id: PosId) -> Vec<ChildEdge> {
        let (mut e, mut o) = (Vec::new(), Vec::new());
        load_children(&self.t.g, &[id], &mut e, &mut o);
        e
    }
    fn cycling_of_a(&self, table: &ProofTable) -> Cycling {
        let kids = self.children(self.a);
        cycling_edges(&self.t.g, Goal::WhiteWin, &[self.a], &[&kids], table, 32)
    }
}

fn until_fixpoint(p: &mut Proofs, g: &MemGraph, seeds: &[PosId], cap: usize) -> bool {
    (0..cap).any(|i| p.refresh(g, seeds, i as u64 + 10, 0.0).written == 0)
}

#[test]
fn test_proof_cycles__cycling_edge_is_detected_along_value_carriers() {
    let s = shuttle();
    let mut p = proofs(RefreshConfig::default());
    s.poison(&mut p);
    assert_eq!(s.cycling_of_a(p.table(1).unwrap()), [(s.a.0, mv("e8f7").0)].into_iter().collect());
}

#[test]
fn test_proof_cycles__the_walk_follows_values_not_the_sticky_primary() {
    let s = shuttle();
    let mut p = proofs(RefreshConfig::default());
    s.poison(&mut p);
    let t = p.table_mut(1).unwrap();
    let ra = t.get(s.a).unwrap();
    t.put(s.a, Row { selected: mv("d2d4"), ..ra });
    let rc = t.get(s.c).unwrap();
    t.put(s.c, Row { selected: mv("a2a3"), ..rc });
    assert_eq!(s.cycling_of_a(p.table(1).unwrap()), [(s.a.0, mv("e8f7").0)].into_iter().collect());
}

#[test]
fn test_proof_cycles__cycling_edge_contributes_a_leaf_estimate_not_the_loop_number() {
    let s = shuttle();
    let mut p = proofs(RefreshConfig::default());
    s.poison(&mut p);
    let kids = s.children(s.a);
    let b_row = row(&p, s.b);
    let only_b = |id: PosId| if id == s.b { b_row } else { None };
    let node = crate::graph::ProofGraph::node(&s.t.g, s.a);
    let (poisoned, _) =
        compute_numbers(&s.t.g, Goal::WhiteWin, GateConfig::default(), s.a, node, &kids, only_b, mv("e8f7"), &Cycling::new());
    assert!(poisoned.dn >= INF);
    let cyc: Cycling = [(s.a.0, mv("e8f7").0)].into_iter().collect();
    let (clean, _) = compute_numbers(&s.t.g, Goal::WhiteWin, GateConfig::default(), s.a, node, &kids, only_b, mv("e8f7"), &cyc);
    assert!(clean.pn < INF && clean.dn < INF);
}

#[test]
fn test_proof_cycles__poisoned_shuttle_drains_to_a_fixpoint() {
    let s = shuttle();
    let mut p = proofs(RefreshConfig::default());
    s.poison(&mut p);
    assert!(until_fixpoint(&mut p, &s.t.g, &s.seeds(), 16), "the ratchet did not reach a fixpoint");
    for n in s.seeds() {
        let r = row(&p, n).unwrap();
        assert!(r.pn < INF && r.dn < INF, "{n}");
    }
    assert_eq!(row(&p, s.a).unwrap().selected, mv("d2d4"));
    assert_eq!(p.refresh(&s.t.g, &s.seeds(), 99, 0.0).written, 0);
}

#[test]
fn test_proof_cycles__the_rule_is_idempotent_and_never_oscillates() {
    let s = shuttle();
    let mut p = proofs(RefreshConfig::default());
    s.poison(&mut p);
    assert!(until_fixpoint(&mut p, &s.t.g, &s.seeds(), 16));
    let settled: Vec<Row> = s.seeds().iter().map(|&n| row(&p, n).unwrap()).collect();
    for i in 0..8 {
        assert_eq!(p.refresh(&s.t.g, &s.seeds(), 100 + i, 0.0).written, 0);
        let now: Vec<Row> = s.seeds().iter().map(|&n| row(&p, n).unwrap()).collect();
        assert!(now.iter().zip(&settled).all(|(a, b)| a.same_values(b)));
    }
}

#[test]
fn test_proof_cycles__the_dn_ratchet_is_dead() {
    let s = shuttle();
    let mut p = proofs(RefreshConfig::default());
    s.poison(&mut p);
    until_fixpoint(&mut p, &s.t.g, &s.seeds(), 16);
    let before = row(&p, s.a).unwrap().dn;
    for i in 0..6 {
        p.refresh(&s.t.g, &s.seeds(), 200 + i, 0.0);
    }
    assert_eq!(row(&p, s.a).unwrap().dn, before);
}

#[test]
fn test_proof_cycles__fresh_component_does_not_ratchet() {
    let s = shuttle();
    let mut p = proofs(RefreshConfig::default());
    let seeds = vec![s.d, s.c, s.b, s.a];
    assert!(until_fixpoint(&mut p, &s.t.g, &seeds, 16), "the fresh component does not converge");
    let settled: Vec<(u64, u64)> = s.seeds().iter().map(|&n| row(&p, n).unwrap().numbers()).collect();
    for i in 0..3 {
        p.refresh(&s.t.g, &seeds, 300 + i, 0.0);
    }
    for (k, &n) in s.seeds().iter().enumerate() {
        let r = row(&p, n).unwrap();
        assert_eq!(r.numbers(), settled[k]);
        assert!(r.dn < INF);
    }
}

fn chain_fixture(len: usize) -> (T, PosId, ProofTable) {
    let mut t = T::new();
    let parent = t.pos(false, Status::Unknown, None);
    let chain: Vec<PosId> = (0..len).map(|i| t.pos(i % 2 == 0, Status::Unknown, None)).collect();
    t.edge(parent, chain[0], mv("a1a2"));
    let mut table = ProofTable::new();
    table.put(parent, Row { pn: 1, dn: 1, expanded: false, selected: mv("a1a2"), born: 0 });
    table.put(chain[0], Row { pn: 1, dn: 1, expanded: false, selected: mv("b1b2"), born: 0 });
    for i in 0..len - 1 {
        t.edge(chain[i], chain[i + 1], mv("b1b2"));
        table.put(chain[i + 1], Row { pn: 1, dn: 1, expanded: false, selected: mv("b1b2"), born: 0 });
    }
    t.edge(chain[len - 1], parent, mv("b1b2"));
    (t, parent, table)
}

#[test]
fn test_proof_cycles__long_cycles_beyond_the_cap_claim_nothing() {
    let (t, parent, table) = chain_fixture(32 + 2);
    let (mut e, mut o) = (Vec::new(), Vec::new());
    load_children(&t.g, &[parent], &mut e, &mut o);
    assert!(cycling_edges(&t.g, Goal::WhiteWin, &[parent], &[&e], &table, 32).is_empty());
}

#[test]
fn test_proof_cycles__a_cycle_within_the_cap_is_claimed() {
    let (t, parent, table) = chain_fixture(4);
    let (mut e, mut o) = (Vec::new(), Vec::new());
    load_children(&t.g, &[parent], &mut e, &mut o);
    assert_eq!(cycling_edges(&t.g, Goal::WhiteWin, &[parent], &[&e], &table, 32), [(parent.0, mv("a1a2").0)].into_iter().collect());
}

// SaturationVisibilityTests
#[test]
fn test_proof_cycles__open_saturated_nodes_are_counted_apart() {
    let s = shuttle();
    let mut p = proofs(RefreshConfig::default());
    let count = |p: &Proofs| frontier::saturated_open_count(&s.t.g, p.table(1).unwrap(), Saturation::Any);
    assert_eq!(count(&p), 0);
    s.poison(&mut p);
    assert_eq!(count(&p), 4);
    assert_eq!(p.census(&s.t.g).1, 4); // every campaign
    p.table_mut(1).unwrap().put(s.p1, Row::PROVED);
    assert_eq!(count(&p), 4);
}
#[test]
fn test_proof_cycles__the_columns_are_counted_separately() {
    let s = shuttle();
    let mut p = proofs(RefreshConfig::default());
    s.poison(&mut p);
    let c = |p: &Proofs, k| frontier::saturated_open_count(&s.t.g, p.table(1).unwrap(), k);
    assert_eq!((c(&p, Saturation::Dn), c(&p, Saturation::Impossible), c(&p, Saturation::Pn)), (4, 4, 0));
    let rb = row(&p, s.b).unwrap();
    p.table_mut(1).unwrap().put(s.b, Row { pn: INF, dn: 0, ..rb });
    assert_eq!((c(&p, Saturation::Pn), c(&p, Saturation::Impossible)), (1, 3));
}
#[test]
fn test_proof_cycles__a_proved_node_awaiting_its_closure_is_not_an_anomaly() {
    let s = shuttle();
    let mut p = proofs(RefreshConfig::default());
    p.table_mut(1).unwrap().put(s.c, Row { pn: 0, dn: INF, expanded: true, selected: Move::NONE, born: 0 });
    let c = |k| frontier::saturated_open_count(&s.t.g, p.table(1).unwrap(), k);
    assert_eq!((c(Saturation::Dn), c(Saturation::Impossible)), (1, 0));
}
#[test]
fn test_proof_cycles__frontier_stats_carry_the_impossible_count() {
    let s = shuttle();
    let mut p = proofs(RefreshConfig::default());
    s.poison(&mut p);
    let st = frontier::frontier_dn_stats(&s.t.g, p.table(1).unwrap(), Goal::WhiteWin, 2, frontier::PROOF_FRONTIER_SCAN);
    assert_eq!(st.saturated, 4);
    assert_eq!(st.and_nodes, 0);
}

// RecascadeProofCommandTests (mixed in v1: through the command)
#[test]
fn test_proof_cycles__command_drains_the_ratchet_and_reports_the_three_columns() {
    let s = shuttle();
    let mut p = proofs(RefreshConfig::default());
    s.poison(&mut p);
    let rep = p.recascade(&s.t.g, None, 500, 8, 1);
    assert_eq!(rep.before.0, 4);
    assert!(matches!(rep.end, RecascadeEnd::Fixpoint(_) | RecascadeEnd::NothingSaturated(_)), "{:?}", rep.end);
    assert_eq!(rep.after, (0, 0, 0));
}
#[test]
fn test_proof_cycles__command_with_nothing_to_do_says_so() {
    let s = shuttle();
    let mut p = proofs(RefreshConfig::default());
    assert_eq!(p.recascade(&s.t.g, None, 500, 12, 1).end, RecascadeEnd::NothingSaturated(1));
}

// ---------------------------------------------------------------- test_adversarial_explorer.py::ProofFrontierTests

/// `_and_node`: an expanded defender node, refreshed.
fn and_node(t: &mut T, p: &mut Proofs) -> PosId {
    let node = t.pos_fen(Some(AFTER_E4), true, Status::Unknown, None);
    t.expand(node, false);
    p.refresh(&t.g, &[node], 1, 0.0);
    node
}

#[test]
fn test_adversarial_explorer__an_expanded_defender_node_is_an_open_obligation() {
    let mut t = T::new();
    let mut p = proofs(RefreshConfig::default());
    let node = and_node(&mut t, &mut p);
    let rows = frontier::frontier_and_rows(&t.g, p.table(1).unwrap(), Goal::WhiteWin, 20_000);
    assert!(rows.iter().any(|r| r.id == node));
}
#[test]
fn test_adversarial_explorer__a_closed_node_is_a_leaf_of_the_proof_not_frontier() {
    let mut t = T::new();
    let mut p = proofs(RefreshConfig::default());
    let node = and_node(&mut t, &mut p);
    t.set_status(node, Status::WhiteWin);
    assert!(frontier::frontier_rows(&t.g, p.table(1).unwrap(), 20_000).iter().all(|r| r.id != node));
}
#[test]
fn test_adversarial_explorer__a_refuted_node_stops_being_an_obligation() {
    let mut t = T::new();
    let mut p = proofs(RefreshConfig::default());
    let node = and_node(&mut t, &mut p);
    let r = row(&p, node).unwrap();
    p.table_mut(1).unwrap().put(node, Row { pn: INF, ..r });
    assert!(frontier::frontier_rows(&t.g, p.table(1).unwrap(), 20_000).iter().all(|r| r.id != node));
}
#[test]
fn test_adversarial_explorer__an_attacker_node_is_not_an_obligation() {
    let mut t = T::new();
    let (root, _) = t.expanded_root();
    let mut p = proofs(RefreshConfig::default());
    p.refresh(&t.g, &[root], 1, 0.0);
    assert!(frontier::frontier_and_rows(&t.g, p.table(1).unwrap(), Goal::WhiteWin, 20_000).iter().all(|r| r.id != root));
}
#[test]
fn test_adversarial_explorer__the_median_is_a_median() {
    let mut t = T::new();
    let mut p = proofs(RefreshConfig::default());
    let nodes: Vec<PosId> = (0..5).map(|_| and_node(&mut t, &mut p)).collect();
    for (dn, n) in [1, 1, 7, 900, 1_000].into_iter().zip(&nodes) {
        let r = row(&p, *n).unwrap();
        p.table_mut(1).unwrap().put(*n, Row { dn, ..r });
    }
    let st = frontier::frontier_dn_stats(&t.g, p.table(1).unwrap(), Goal::WhiteWin, 2, 20_000);
    assert_eq!((st.and_nodes, st.dn_median, st.thin), (5, 7, 2));
}
#[test]
fn test_adversarial_explorer__an_empty_frontier_reports_zeroes_not_a_gap() {
    // frontier_dn_headline: None without an active campaign.
    let mut p = proofs(RefreshConfig::default());
    p.campaigns[0].active = false;
    assert!(crate::campaign::headline_campaign(&p.campaigns).is_none());
    let t = T::new();
    let st = frontier::frontier_dn_stats(&t.g, &ProofTable::new(), Goal::WhiteWin, 2, 20_000);
    assert_eq!((st.and_nodes, st.dn_median, st.thin, st.saturated), (0, 0, 0, 0));
}
#[test]
fn test_adversarial_explorer__the_scan_is_bounded() {
    let mut t = T::new();
    let mut p = proofs(RefreshConfig::default());
    for _ in 0..4 {
        and_node(&mut t, &mut p);
    }
    assert_eq!(frontier::frontier_rows(&t.g, p.table(1).unwrap(), 2).len(), 2);
}
#[test]
fn frontier_order_is_pn_then_key_and_skips_closed_rows() {
    // test_same_rows_as_the_join (v1-specific in the map: it pins the paging);
    // the property it guards: same rows and order as the JOIN, ties by key.
    let mut t = T::new();
    let ids: Vec<PosId> = (0..15).map(|_| t.pos(true, Status::Unknown, None)).collect();
    let mut table = ProofTable::new();
    for (i, &id) in ids.iter().enumerate() {
        let pn = [0, 1, 1, 2, 3][i % 5];
        let dn = if i % 7 != 0 { 5 } else { INF };
        table.put(id, Row { pn, dn, expanded: true, selected: Move::NONE, born: 0 });
    }
    for &id in ids.iter().skip(1).step_by(3) {
        t.set_status(id, Status::WhiteWin);
    }
    let joined: Vec<PosId> = {
        let mut v: Vec<(u64, u64, PosId)> = ids
            .iter()
            .filter_map(|&id| {
                let r = table.get(id).unwrap();
                (t.g.core(id).status() == Status::Unknown && r.pn < INF && r.dn < INF).then_some((r.pn, id.0, id))
            })
            .collect();
        v.sort();
        v.into_iter().map(|x| x.2).collect()
    };
    for limit in [1, 2, 3, 5, 8, 50] {
        let got: Vec<PosId> = frontier::frontier_rows(&t.g, &table, limit).into_iter().map(|r| r.id).collect();
        assert_eq!(got, joined[..limit.min(joined.len())].to_vec(), "{limit}");
    }
}

// ---------------------------------------------------------------- test_solver_allocation.py::OrClampTests

struct ClampFix {
    t: T,
    c: Campaign,
    root: PosId,
    winner: PosId,
    sibling: PosId,
}
fn clamp_fix() -> ClampFix {
    let mut t = T::new();
    let (root, kids) = t.expanded_root();
    ClampFix { t, c: campaign(), root, winner: kids[0], sibling: kids[1] }
}
impl ClampFix {
    fn clamp(&self, id: PosId) -> Option<(u64, u32)> {
        proved_or_clamp(&self.t.g, id, &[&self.c], true)
    }
}

#[test]
fn test_solver_allocation__without_a_proven_child_nothing_is_clamped() {
    let f = clamp_fix();
    assert_eq!(f.clamp(f.sibling), None);
}
#[test]
fn test_solver_allocation__a_sibling_of_a_proven_child_falls_to_the_minimum() {
    let mut f = clamp_fix();
    f.t.set_status(f.winner, Status::WhiteWin);
    assert_eq!(f.clamp(f.sibling), Some((OR_CLAMP_NODES, OR_CLAMP_MULTIPV)));
}
#[test]
fn test_solver_allocation__the_minimum_is_far_below_the_first_rung() {
    const BUDGET_LADDER_FIRST: u64 = 8_000_000; // ingest.BUDGET_LADDER[0]
    const { assert!(OR_CLAMP_NODES * 4 < BUDGET_LADDER_FIRST) };
}
#[test]
fn test_solver_allocation__a_refuted_child_does_not_clamp_anybody() {
    let mut f = clamp_fix();
    f.t.set_status(f.winner, Status::BlackWin);
    assert_eq!(f.clamp(f.sibling), None);
}
#[test]
fn test_solver_allocation__an_and_node_is_not_touched() {
    let mut f = clamp_fix();
    let answers = f.t.expand(f.winner, false);
    f.t.set_status(answers[0], Status::WhiteWin);
    assert_eq!(f.clamp(answers[1]), None);
}
#[test]
fn test_solver_allocation__a_parent_that_still_needs_it_keeps_the_budget() {
    let mut f = clamp_fix();
    f.t.set_status(f.winner, Status::WhiteWin);
    f.t.edge(f.winner, f.sibling, mv("a7a6"));
    f.t.set_status(f.winner, Status::Unknown);
    assert_eq!(f.clamp(f.sibling), None);
}
#[test]
fn test_solver_allocation__a_closed_parent_does_not_keep_it_alive() {
    let mut f = clamp_fix();
    f.t.set_status(f.winner, Status::WhiteWin);
    let other = f.t.pos(false, Status::Draw, None);
    f.t.edge(f.root, other, mv("h2h3"));
    f.t.edge(other, f.sibling, mv("a7a6"));
    assert_eq!(f.clamp(f.sibling), Some((OR_CLAMP_NODES, OR_CLAMP_MULTIPV)));
}
#[test]
fn test_solver_allocation__another_campaign_that_still_needs_it_keeps_the_budget() {
    let mut f = clamp_fix();
    f.t.set_status(f.winner, Status::WhiteWin);
    let other = Campaign { id: 2, name: "root-black-win".into(), goal: Goal::BlackWin, ..campaign() };
    assert_eq!(proved_or_clamp(&f.t.g, f.sibling, &[&f.c, &other], true), None);
}
#[test]
fn test_solver_allocation__the_switch_gives_the_ladder_back() {
    let mut f = clamp_fix();
    f.t.set_status(f.winner, Status::WhiteWin);
    assert_eq!(proved_or_clamp(&f.t.g, f.sibling, &[&f.c], false), None);
}

// ---------------------------------------------------------------- test_solve_gate.py::GateOnTests / DisagreementQuadrantTests (tree half)

fn tedious_child(t: &mut T, child: PosId, eval: i16) {
    t.set_eval(child, Some(eval));
    t.set_analysis(child, &serde_json::json!([{ "eval_cp": eval, "pv": MANOEUVRE }]));
}

fn gate_on() -> RefreshConfig {
    RefreshConfig { gate: GateConfig { enabled: true }, max_plies: 1, ..RefreshConfig::default() }
}

#[test]
fn test_solve_gate__a_tedious_frontier_leaf_gets_more_expensive() {
    let mut t = T::new();
    let (_root, kids) = t.expanded_root();
    tedious_child(&mut t, kids[0], 1_200);
    let mut on = proofs(gate_on());
    on.refresh(&t.g, &[kids[0]], 1, 0.0);
    let mut off = proofs(RefreshConfig { max_plies: 1, ..RefreshConfig::default() });
    off.refresh(&t.g, &[kids[0]], 1, 0.0);
    assert!(row(&on, kids[0]).unwrap().pn > row(&off, kids[0]).unwrap().pn);
}
#[test]
fn test_solve_gate__turning_it_off_again_restores_the_numbers() {
    let mut t = T::new();
    let (_root, kids) = t.expanded_root();
    tedious_child(&mut t, kids[0], 1_200);
    let mut p = proofs(gate_on());
    p.refresh(&t.g, &[kids[0]], 1, 0.0);
    let gated = row(&p, kids[0]).unwrap().pn;
    p.cfg.gate.enabled = false;
    p.refresh(&t.g, &[kids[0]], 2, 0.0);
    let restored = row(&p, kids[0]).unwrap().pn;
    assert_ne!(gated, restored);
    let leaf = crate::numbers::leaf_numbers(
        crate::numbers::LeafInput { black_to_move: true, status: Status::Unknown, eval_cp: Some(1_200) },
        Goal::WhiteWin,
        None,
        None,
        false,
    );
    assert_eq!(restored, leaf.0.0);
}
#[test]
fn test_solve_gate__the_real_maintenance_pass_logs_the_frontier_leaf() {
    let mut t = T::new();
    let (_root, kids) = t.expanded_root();
    tedious_child(&mut t, kids[0], 1_500);
    let mut p = proofs(gate_on());
    let r = p.refresh(&t.g, &[kids[0]], 1, 0.0);
    let ev: Vec<&ProofEvent> = r.events.iter().filter(|e| matches!(e, ProofEvent::SolveGateDisagree { .. })).collect();
    assert_eq!(ev.len(), 1);
    match ev[0] {
        ProofEvent::SolveGateDisagree { position, eval_cp, factor, .. } => {
            assert_eq!(*position, kids[0]);
            assert_eq!(*eval_cp, Some(1_500));
            assert!(*factor > 1.0);
        }
        _ => unreachable!(),
    }
    // a second pass inside the window logs nothing (steady state)
    assert!(p.refresh(&t.g, &[kids[0]], 2, 10.0).events.is_empty());
}
#[test]
fn test_solve_gate__nothing_is_logged_with_the_gate_off() {
    let mut t = T::new();
    let (_root, kids) = t.expanded_root();
    tedious_child(&mut t, kids[0], 1_500);
    let mut p = proofs(RefreshConfig { max_plies: 1, ..RefreshConfig::default() });
    let r = p.refresh(&t.g, &[kids[0]], 1, 0.0);
    // (max_plies=1 leaves the root unrefreshed: a PROOF_GUARD, as in v1)
    assert!(r.events.iter().all(|e| !matches!(e, ProofEvent::SolveGateDisagree { .. })));
}
#[test]
fn test_solve_gate__the_descent_ranks_the_tedious_child_last() {
    // v1 pins it through `_ranked_children` (PN-6.4, the descent); the same
    // edge-view gate decides the OR primary here: two children with the same
    // winning eval, the bare endgame priced dearer than the rich middlegame.
    let mut t = T::new();
    let root = t.pos_fen(Some(START), false, Status::Unknown, None);
    let bare = t.pos_fen(Some(BARE_BLACK), true, Status::Unknown, Some(1_200));
    let rich = t.pos_fen(Some(AFTER_E4), true, Status::Unknown, Some(1_200));
    t.edge(root, bare, mv("a1a2"));
    t.edge(root, rich, mv("b1b2"));
    let (mut e, mut o) = (Vec::new(), Vec::new());
    load_children(&t.g, &[root], &mut e, &mut o);
    let node = crate::graph::ProofGraph::node(&t.g, root);
    let (on, _) =
        compute_numbers(&t.g, Goal::WhiteWin, GateConfig { enabled: true }, root, node, &e, |_| None, Move::NONE, &Cycling::new());
    assert_eq!(on.selected, mv("b1b2"));
    let (off, _) = compute_numbers(&t.g, Goal::WhiteWin, GateConfig::default(), root, node, &e, |_| None, Move::NONE, &Cycling::new());
    assert_eq!(off.selected, mv("a1a2")); // a tie without the gate: edge order
}

// ---------------------------------------------------------------- PN-4.3 in-place mutation (no v1 test: spec gap)

#[test]
fn a_level_reads_updated_rows_but_not_created_ones() {
    // Level 1 holds X (child of P) and P.  Ordered P before X: P reads X's old
    // row.  Ordered X before P: P reads X's new row if X was updated, but its
    // edge view if X's row was created in this level.
    let build = |x_first: bool, x_has_row: bool| -> (u64, u64) {
        // P: White to move (OR); X: Black to move (AND), eval 1000, one child.
        let mut t = T::new();
        let (p, x) = if x_first {
            let x = t.pos(true, Status::Unknown, Some(1_000));
            (t.pos(false, Status::Unknown, None), x)
        } else {
            let p = t.pos(false, Status::Unknown, None);
            (p, t.pos(true, Status::Unknown, Some(1_000)))
        };
        let leaf = t.pos(false, Status::Unknown, Some(-1_000));
        t.edge(p, x, mv("a1a2"));
        t.edge(x, leaf, mv("b1b2"));
        let mut pr = proofs(RefreshConfig { max_plies: 1, ..RefreshConfig::default() });
        if x_has_row {
            pr.table_mut(1).unwrap().put(x, Row { pn: 77, dn: 77, expanded: true, selected: mv("b1b2"), born: 0 });
        }
        pr.refresh(&t.g, &[p, x], 1, 0.0);
        let r = row(&pr, p).unwrap();
        (r.pn, r.dn)
    };
    // X processed first and updated: P sees X's new numbers, the sum/min of
    // its one child priced from its edge: eval -1000 at an OR node, band
    // (32, 2).
    assert_eq!(build(true, true), (32, 2));
    // X processed first and created: P prices X from its edge (eval 1000 at
    // an AND node: band (2, 16)).
    assert_eq!(build(true, false), (2, 16));
    // P processed first: P reads X's old row (77, 77).
    assert_eq!(build(false, true), (77, 77));
}

// ---------------------------------------------------------------- PN-8 writer hook

#[test]
fn after_batch_replays_the_calls_in_order() {
    let mut t = T::new();
    let (root, kids) = t.expanded_root();
    t.set_status(kids[3], Status::WhiteWin);
    let mut a = proofs(RefreshConfig::default());
    let mut b = proofs(RefreshConfig::default());
    let calls = vec![vec![kids[3], root], vec![kids[1]]];
    a.after_batch(&t.g, &calls, 1, 0.0);
    for c in &calls {
        b.refresh(&t.g, c, 1, 0.0);
    }
    assert!(a.table(1).unwrap().same_as(b.table(1).unwrap()));
    assert_eq!(row(&a, root).unwrap().pn, 0);
}

// ---------------------------------------------------------------- property tests

/// A random growing proof tree: each step lands on an unexpanded open
/// position, creates 1..6 children (some transpositions to existing nodes,
/// some closed), with random evals.  `cyclic` adds back edges.
struct Grow {
    t: T,
    rng: Rng,
    cyclic: bool,
}

impl Grow {
    fn new(seed: u64, cyclic: bool) -> Grow {
        let mut t = T::new();
        t.pos(false, Status::Unknown, Some(0));
        Grow { t, rng: Rng::new(seed), cyclic }
    }
    fn eval(&mut self) -> Option<i16> {
        if self.rng.chance(0.2) { None } else { Some(self.rng.below(4_000) as i16 - 2_000) }
    }
    /// One landing; returns the refresh seeds v1 would use (landed + closed).
    fn land(&mut self) -> Vec<PosId> {
        let n = self.t.g.len();
        let mut tries = 0;
        let p = loop {
            let id = PosId(self.rng.below(n));
            tries += 1;
            if self.t.g.core(id).status() == Status::Unknown && self.t.kids(id).is_empty() {
                break id;
            }
            if tries > 64 {
                break PosId(self.rng.below(n));
            }
        };
        let black = self.t.g.core(p).black_to_move();
        let e = self.eval();
        self.t.set_eval(p, e);
        let mut seeds = vec![p];
        let k = 1 + self.rng.below(6);
        let mut used: Vec<u64> = self.t.kids(p).iter().map(|e| e.child.0).collect();
        for i in 0..k {
            let m = Move::new(i as u8, (i + 20) as u8, 0);
            if self.t.kids(p).iter().any(|e| e.mv == m) {
                continue;
            }
            let child = if self.rng.chance(0.15) && n > 2 {
                // transposition: an existing node with the right side to move,
                // later than the parent (keeps the graph acyclic) unless cyclic
                let c = PosId(self.rng.below(n));
                let ok = self.t.g.core(c).black_to_move() != black && (self.cyclic || c.0 > p.0) && c != p && !used.contains(&c.0);
                if !ok {
                    continue;
                }
                c
            } else {
                let st = if self.rng.chance(0.08) {
                    [Status::WhiteWin, Status::BlackWin, Status::Draw][self.rng.below(3) as usize]
                } else {
                    Status::Unknown
                };
                let ev = self.eval();
                let c = self.t.pos(!black, st, ev);
                if st != Status::Unknown {
                    seeds.push(c);
                }
                c
            };
            used.push(child.0);
            self.t.edge(p, child, m);
        }
        seeds
    }
}

fn numbers_of(t: &ProofTable, ids: impl Iterator<Item = PosId>) -> Vec<(PosId, (u64, u64))> {
    ids.filter_map(|id| t.get(id).map(|r| (id, r.numbers()))).collect()
}

#[test]
fn property_incremental_at_rest_equals_rebuild_on_dags() {
    for seed in 1..=40u64 {
        let mut gr = Grow::new(seed, false);
        let mut p = proofs(RefreshConfig::uncapped());
        for step in 0..120u64 {
            let seeds = gr.land();
            let r = p.refresh(&gr.t.g, &seeds, step, 0.0);
            assert!(r.at_rest());
        }
        let (fresh, rep) = rebuild(&gr.t.g, &campaign(), GateConfig::default(), Scope::All, 0);
        assert_eq!(rep.unconverged, 0);
        let inc = p.table(1).unwrap();
        // Every row the incremental table holds with out-edges agrees.
        let mut buf = Vec::new();
        for id in inc.ids() {
            buf.clear();
            gr.t.g.children(id, &mut buf);
            if buf.is_empty() && gr.t.g.core(id).status() == Status::Unknown {
                continue; // a searched leaf: the rebuild gives it no row
            }
            let (a, b) = (inc.get(id).unwrap(), fresh.get(id));
            assert_eq!(Some(a.numbers()), b.map(|r| r.numbers()), "seed {seed} id {id}");
            assert_eq!(a.expanded, b.unwrap().expanded);
        }
    }
}

#[test]
fn property_refresh_is_idempotent() {
    for seed in 1..=30u64 {
        let mut gr = Grow::new(seed, seed % 2 == 0);
        let mut p = proofs(RefreshConfig::default());
        let mut all = Vec::new();
        for step in 0..60u64 {
            let s = gr.land();
            all.extend(s.iter().copied());
            p.refresh(&gr.t.g, &s, step, 0.0);
        }
        if seed % 2 == 1 {
            // acyclic and uncapped by size: a second refresh of everything changes nothing
            let mut q = p.clone();
            q.cfg = RefreshConfig::uncapped();
            q.refresh(&gr.t.g, &all, 999, 0.0);
            let snap = q.clone();
            assert_eq!(q.refresh(&gr.t.g, &all, 1000, 0.0).written, 0, "seed {seed}");
            assert!(q.table(1).unwrap().same_as(snap.table(1).unwrap()));
        }
    }
}

#[test]
fn property_batch_size_does_not_change_pn_dn_at_rest_on_dags() {
    for seed in 100..=130u64 {
        let mut gr = Grow::new(seed, false);
        let mut batches = Vec::new();
        for _ in 0..80 {
            batches.push(gr.land());
        }
        // one refresh per landing vs one refresh of every seed at the end
        let mut one = proofs(RefreshConfig::uncapped());
        for (i, b) in batches.iter().enumerate() {
            one.refresh(&gr.t.g, b, i as u64, 0.0);
        }
        let mut all = proofs(RefreshConfig::uncapped());
        let seeds: Vec<PosId> = batches.concat();
        all.refresh(&gr.t.g, &seeds, 0, 0.0);
        let ids: Vec<PosId> = one.table(1).unwrap().ids().collect();
        assert_eq!(
            numbers_of(one.table(1).unwrap(), ids.iter().copied()),
            numbers_of(all.table(1).unwrap(), ids.iter().copied()),
            "seed {seed}"
        );
    }
}

#[test]
fn property_key_order_and_id_order_agree_at_rest_on_dags() {
    use crate::graph::KeyOrdered;
    for seed in 200..=220u64 {
        let mut gr = Grow::new(seed, false);
        let mut batches = Vec::new();
        for _ in 0..60 {
            batches.push(gr.land());
        }
        let mut keys: Vec<u128> = (0..gr.t.g.len()).map(|i| (i as u128).wrapping_mul(0x9e37_79b9_7f4a_7c15) % 1_000_003).collect();
        keys.iter_mut().enumerate().for_each(|(i, k)| *k = *k * 1_000_000 + i as u128);
        let ko = KeyOrdered { graph: &gr.t.g, keys: &keys };
        let mut a = proofs(RefreshConfig::uncapped());
        let mut b = proofs(RefreshConfig::uncapped());
        for (i, s) in batches.iter().enumerate() {
            a.refresh(&gr.t.g, s, i as u64, 0.0);
            b.refresh(&ko, s, i as u64, 0.0);
        }
        let ids: Vec<PosId> = a.table(1).unwrap().ids().collect();
        assert_eq!(
            numbers_of(a.table(1).unwrap(), ids.iter().copied()),
            numbers_of(b.table(1).unwrap(), ids.iter().copied()),
            "seed {seed}"
        );
    }
}

#[test]
fn property_cyclic_graphs_terminate_and_stay_in_range() {
    let mut fixpoints = 0;
    for seed in 300..=340u64 {
        let mut gr = Grow::new(seed, true);
        let mut p = proofs(RefreshConfig::default());
        let mut all = Vec::new();
        for step in 0..80u64 {
            let s = gr.land();
            all.extend(s.iter().copied());
            p.refresh(&gr.t.g, &s, step, 0.0);
        }
        for id in p.table(1).unwrap().ids() {
            let r = p.table(1).unwrap().get(id).unwrap();
            assert!(r.pn <= INF && r.dn <= INF);
            if gr.t.g.core(id).status() == Status::Unknown && !r.expanded {
                assert!(r.pn <= PROOF_MAX_LEAF && r.dn <= PROOF_MAX_LEAF && r.pn >= 1 && r.dn >= 1);
            }
        }
        if (0..16).any(|i| p.refresh(&gr.t.g, &all, 1_000 + i, 0.0).written == 0) {
            fixpoints += 1;
        }
        // the rebuild terminates too
        let (_, rep) = rebuild(&gr.t.g, &campaign(), GateConfig::default(), Scope::All, 0);
        assert!(rep.computations > 0);
    }
    // FINDING (not a port bug: `v1diff` reproduces v1 row for row on cyclic
    // graphs): v1's cycle rule (PN-3) is not a fixpoint rule in general.  Its
    // detection reads the current numbers, and pricing a cycling edge at
    // 2^20 changes those numbers, so on 7 of these 41 random cyclic graphs
    // repeated refreshes oscillate with period 2 (one node flips between
    // ~(36, 2) and ~(2^20 + 34, 1)), even uncapped.  The shuttle of
    // test_proof_cycles converges; general cycles need not.  Pinned so that
    // a change of the rule shows up here.
    assert_eq!(fixpoints, 34, "{fixpoints}/41 cyclic graphs reached a fixpoint in 16 passes");
}

#[test]
fn property_table_matches_a_plain_map() {
    let mut rng = Rng::new(7);
    let mut t = ProofTable::new();
    let mut m = std::collections::BTreeMap::new();
    for _ in 0..20_000 {
        let id = PosId(rng.below(3_000));
        match rng.below(5) {
            0 => {
                t.remove(id);
                m.remove(&id.0);
            }
            k => {
                let r = match k {
                    1 => Row::PROVED,
                    2 => Row::REFUTED,
                    _ => Row {
                        pn: if rng.chance(0.2) { 0 } else { rng.below(1 << 30) },
                        dn: if rng.chance(0.2) { INF } else { rng.below(1 << 30) },
                        expanded: rng.chance(0.5),
                        selected: if rng.chance(0.5) { Move::NONE } else { Move(rng.below(4096) as u16 + 1) },
                        born: rng.below(1000),
                    },
                };
                t.put(id, r);
                let implicit = r.same_values(&Row::PROVED) || r.same_values(&Row::REFUTED);
                m.insert(id.0, if implicit { Row { born: 0, ..r } } else { r });
            }
        }
    }
    assert_eq!(t.len(), m.len() as u64);
    for (&k, r) in &m {
        assert_eq!(t.get(PosId(k)), Some(*r));
    }
    assert_eq!(t.ids().map(|i| i.0).collect::<Vec<_>>(), m.keys().copied().collect::<Vec<_>>());
}
