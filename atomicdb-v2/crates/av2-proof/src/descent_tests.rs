//! Ports of the descent tests: `test_proof_manager.py::DescentTests`,
//! `test_selector_leak.py::ProofDescentIsImmuneTests`,
//! `test_solver_allocation.py::CampaignShareTests` and the graph-walk tests
//! of `test_value_descent.py` (the pool-level ones live in `av2-queue`).
//! v1's tests write `m0`-style moves; real moves stand in for them here.

use crate::campaign::Campaign;
use crate::descent::*;
use crate::gate::GateConfig;
use crate::numbers::Goal;
use crate::table::{ProofTable, Row};
use av2_model::record::{F_BLACK_TO_MOVE, NO_CP};
use av2_model::{Core, Edge, Ext, Fingerprint, GraphRead, GraphWrite, Move, PosId, Status};
use av2_store::mem::MemGraph;
use serde_json::json;
use std::collections::HashSet;

const SPINE: u64 = 128_000_000;

#[derive(Default)]
struct P {
    black: bool,
    eval: Option<i16>,
    backed: Option<i16>,
    backed_move: Option<Move>,
    backed_plies: u16,
    nodes: u64,
    status: Option<Status>,
}

struct T {
    g: MemGraph,
    moves: u16,
    table: ProofTable,
    live: HashSet<PosId>,
}

impl T {
    fn new() -> T {
        T { g: MemGraph::new(), moves: 0, table: ProofTable::new(), live: HashSet::new() }
    }
    fn pos(&mut self, p: P) -> PosId {
        let mut c = Core::default();
        c.set_status(p.status.unwrap_or(Status::Unknown), Default::default(), Default::default());
        c.set_flag(F_BLACK_TO_MOVE, p.black);
        c.eval_cp = p.eval.unwrap_or(NO_CP);
        c.backed_eval = p.backed.unwrap_or(NO_CP);
        c.backed_move = p.backed_move.unwrap_or(Move::NONE);
        c.backed_plies = p.backed_plies;
        let fp = Fingerprint::from_hex(&format!("{:024x}", self.g.len() + 1)).unwrap();
        let stm = if p.black { "b" } else { "w" };
        let id = self.g.create(fp, Some(&format!("4k3/8/8/8/8/8/8/4K3 {stm} - - 0 1")), c).unwrap();
        if p.nodes > 0 {
            self.g.put_ext(id, Ext { nodes_invested: p.nodes, ..Ext::default() }).unwrap();
        }
        id
    }
    /// A fresh, distinct move.
    fn mv(&mut self) -> Move {
        self.moves += 1;
        Move::new((self.moves / 64) as u8, (self.moves % 64) as u8, 0)
    }
    fn edge(&mut self, parent: PosId, child: PosId, mv: Move) {
        let mut es = Vec::new();
        self.g.children(parent, &mut es);
        es.push(Edge { child, mv });
        self.g.set_children(parent, &es).unwrap();
    }
    fn set_nodes(&mut self, id: PosId, nodes: u64) {
        let e = Ext { nodes_invested: nodes, ..self.g.ext(id).unwrap_or_default() };
        self.g.put_ext(id, e).unwrap();
    }
    fn set_status(&mut self, id: PosId, s: Status) {
        let mut c = self.g.core(id);
        c.set_status(s, Default::default(), Default::default());
        self.g.put_core(id, c).unwrap();
    }
    /// `_spine(name, plies, tip_fields)`: nodes chained by `backed_move`, the
    /// last one without backing.
    #[allow(clippy::needless_range_loop)]
    fn spine(&mut self, plies: usize, tip_nodes: u64) -> Vec<PosId> {
        let mut moves = Vec::new();
        for _ in 0..plies {
            moves.push(self.mv());
        }
        let mut nodes = Vec::new();
        for i in 0..=plies {
            let last = i == plies;
            nodes.push(self.pos(P {
                black: i % 2 == 1,
                eval: Some(800),
                backed: (!last).then_some(812),
                backed_move: (!last).then(|| moves[i]),
                nodes: if last { tip_nodes } else { 0 },
                ..P::default()
            }));
        }
        for i in 0..plies {
            self.edge(nodes[i], nodes[i + 1], moves[i]);
        }
        nodes
    }
    /// `_long_spine(name, plies, line)`.
    fn long_spine(&mut self, plies: usize, line: u16) -> (Vec<PosId>, Vec<Move>) {
        let moves: Vec<Move> = (0..=plies).map(|_| self.mv()).collect();
        let nodes: Vec<PosId> = (0..=plies)
            .map(|i| {
                self.pos(P {
                    black: i % 2 == 1,
                    eval: Some(800),
                    backed: Some(812),
                    backed_move: Some(moves[i]),
                    backed_plies: line - i as u16,
                    ..P::default()
                })
            })
            .collect();
        for i in 0..plies {
            self.edge(nodes[i], nodes[i + 1], moves[i]);
        }
        (nodes, moves)
    }
    fn walker<'a>(&'a self, c: &'a Campaign, max_plies: u32) -> Walker<'a, MemGraph> {
        Walker::new(&self.g, &self.table, c, DescentConfig { max_plies, gate: GateConfig { enabled: false }, saturated_nodes: SPINE })
    }
    fn value(&self, c: &Campaign, avoid: &[PosId], start: Option<PosId>, max_plies: u32) -> Option<ValueTarget> {
        let avoid: HashSet<PosId> = avoid.iter().copied().collect();
        self.walker(c, max_plies).descend_value(0, &|x| avoid.contains(&x), &|x| self.live.contains(&x), start)
    }
    fn pn(&self, c: &Campaign, counter: u64, avoid: &[PosId], start: Option<PosId>, max_plies: u32) -> (Option<PosId>, u32) {
        let avoid: HashSet<PosId> = avoid.iter().copied().collect();
        self.walker(c, max_plies).descend(counter, &|x| avoid.contains(&x), start)
    }
}

fn campaign(root: PosId, policy: serde_json::Value) -> Campaign {
    Campaign { policy, ..Campaign::default_for(2, root) }
}
fn primary_only() -> serde_json::Value {
    json!({"primary": 1.0, "backup": 0.0, "explore": 0.0})
}

// ------------------------------------------------------------ draws

#[test]
fn test_allocation_is_deterministic_not_random() {
    let p = (0.8, 0.15, 0.05);
    let a: Vec<Bucket> = (0..200).map(|c| bucket(c, p)).collect();
    let b: Vec<Bucket> = (0..200).map(|c| bucket(c, p)).collect();
    assert_eq!(a, b);
}

#[test]
fn test_soft_repertoire_spends_roughly_eighty_fifteen_five() {
    let p = normalized(&Campaign::default_for(1, PosId(0)));
    let mut n = [0usize; 3];
    for c in 0..2000 {
        n[bucket(c, p) as usize] += 1;
    }
    let f = |i: usize| n[i] as f64 / 2000.0;
    assert!((f(0) - 0.8).abs() < 0.03 && (f(1) - 0.15).abs() < 0.03 && (f(2) - 0.05).abs() < 0.02, "{n:?}");
}

fn normalized(c: &Campaign) -> (f64, f64, f64) {
    crate::campaign::normalized_policy(c)
}

/// `test_solver_allocation.py::DeterministicDrawTests`: domains are separated.
#[test]
fn draws_are_domain_separated() {
    assert_eq!(point(1, "campaign:"), point(1, "campaign:"));
    assert_ne!(point(1, "campaign:"), point(1, ""));
    assert!((0..1000).all(|c| (0.0..1.0).contains(&point(c, ""))));
}

fn share(roots: &[(PosId, f64)]) -> f64 {
    (0..4000).filter(|&c| campaign_start(c, roots).is_some()).count() as f64 / 4000.0
}

#[test]
fn campaign_share_tests() {
    let mut t = T::new();
    let a = t.pos(P::default());
    let b = t.pos(P::default());
    let c = t.pos(P::default());
    let closed = t.pos(P { status: Some(Status::WhiteWin), ..P::default() });
    let cc = |id: u64, root: PosId, active: bool, votes: i64| CommunityCampaign { id, root, active, votes };
    // test_an_active_campaign_takes_the_capped_share
    let roots = campaign_roots(&t.g, &[cc(1, a, true, 5)]);
    assert!((share(&roots) - CAMPAIGN_DESCENT_SHARE).abs() < 0.02);
    // test_an_active_campaign_without_votes_is_not_inert
    let roots = campaign_roots(&t.g, &[cc(1, a, true, 0)]);
    assert!((roots[0].1 - 2f64.ln()).abs() < 1e-12);
    assert!((share(&roots) - CAMPAIGN_DESCENT_SHARE).abs() < 0.02);
    // test_the_cap_holds_with_many_campaigns
    let roots = campaign_roots(&t.g, &[cc(1, a, true, 900), cc(2, b, true, 700), cc(3, c, true, 800)]);
    assert!((share(&roots) - CAMPAIGN_DESCENT_SHARE).abs() < 0.02);
    // test_two_campaigns_split_the_cap_by_log_votes
    let roots = campaign_roots(&t.g, &[cc(2, b, true, 1), cc(1, a, true, 20)]);
    assert_eq!(roots[0].0, a, "by campaign id");
    let n_a = (0..4000).filter(|&k| campaign_start(k, &roots) == Some(a)).count() as f64 / 4000.0;
    let want = CAMPAIGN_DESCENT_SHARE * 21f64.ln() / (21f64.ln() + 2f64.ln());
    assert!((n_a - want).abs() < 0.02, "{n_a} vs {want}");
    // test_a_closed_campaign_root_is_not_a_starting_point
    assert!(campaign_roots(&t.g, &[cc(1, closed, true, 9)]).is_empty());
    // test_without_active_campaigns_every_descent_starts_at_the_root
    let roots = campaign_roots(&t.g, &[cc(1, a, false, 9)]);
    assert!(roots.is_empty());
    assert!((0..4000).all(|k| campaign_start(k, &roots).is_none()));
}

// ------------------------------------------------------------ df-pn descent

#[test]
fn test_a_closed_root_yields_nothing() {
    let mut t = T::new();
    let root = t.pos(P { status: Some(Status::WhiteWin), ..P::default() });
    let c = campaign(root, primary_only());
    assert_eq!(t.pn(&c, 0, &[], None, DESCENT_MAX_PLIES).0, None);
}

#[test]
fn test_descent_reaches_an_unexpanded_frontier_node() {
    let mut t = T::new();
    let root = t.pos(P::default());
    let kids: Vec<PosId> = (0..3).map(|_| t.pos(P { black: true, ..P::default() })).collect();
    for &k in &kids {
        let m = t.mv();
        t.edge(root, k, m);
    }
    let c = campaign(root, primary_only());
    let (found, plies) = t.pn(&c, 0, &[], None, DESCENT_MAX_PLIES);
    assert!(kids.contains(&found.unwrap()));
    assert_eq!(plies, 1);
}

#[test]
fn test_a_reserved_node_is_not_handed_out_twice() {
    let mut t = T::new();
    let root = t.pos(P::default());
    for _ in 0..3 {
        let k = t.pos(P { black: true, ..P::default() });
        let m = t.mv();
        t.edge(root, k, m);
    }
    let c = campaign(root, primary_only());
    let first = t.pn(&c, 0, &[], None, DESCENT_MAX_PLIES).0.unwrap();
    let second = t.pn(&c, 0, &[first], None, DESCENT_MAX_PLIES).0.unwrap();
    assert_ne!(first, second);
}

#[test]
fn test_or_descent_follows_the_smallest_pn() {
    let mut t = T::new();
    let root = t.pos(P::default());
    let mut kids = Vec::new();
    for e in [-400, 3000, -400] {
        let k = t.pos(P { black: true, eval: Some(e), ..P::default() });
        let m = t.mv();
        t.edge(root, k, m);
        kids.push(k);
    }
    let c = campaign(root, primary_only());
    assert_eq!(t.pn(&c, 0, &[], None, DESCENT_MAX_PLIES).0, Some(kids[1]));
}

#[test]
fn test_descent_never_loops_through_a_transposition() {
    // Nf3 Nf6 Ng1 Ng8: back to the start.
    let mut t = T::new();
    let start = t.pos(P::default());
    let a = t.pos(P { black: true, ..P::default() });
    let b = t.pos(P::default());
    let c3 = t.pos(P { black: true, ..P::default() });
    for (p, q) in [(start, a), (a, b), (b, c3), (c3, start)] {
        let m = t.mv();
        t.edge(p, q, m);
    }
    let c = campaign(start, primary_only());
    let (found, plies) = t.pn(&c, 0, &[], None, 200);
    assert!(found.is_some());
    assert!(plies < 200);
}

/// `test_selector_leak.py::ProofDescentIsImmuneTests::test_a_closed_child_is_never_descended_into`.
#[test]
fn test_a_closed_child_is_never_descended_into() {
    let mut t = T::new();
    let root = t.pos(P::default());
    let won = t.pos(P { black: true, status: Some(Status::WhiteWin), ..P::default() });
    let open = t.pos(P { black: true, ..P::default() });
    for k in [won, open] {
        let m = t.mv();
        t.edge(root, k, m);
    }
    let c = Campaign::default_for(1, root);
    for counter in 0..20 {
        assert_ne!(t.pn(&c, counter, &[], None, DESCENT_MAX_PLIES).0, Some(won));
    }
}

/// `test_solver_allocation.py::CampaignShareTests::test_a_descent_that_starts_at_a_campaign_stays_under_it`.
#[test]
fn test_a_descent_that_starts_at_a_campaign_stays_under_it() {
    let mut t = T::new();
    let root = t.pos(P::default());
    let camp = t.pos(P { black: true, ..P::default() });
    let m = t.mv();
    t.edge(root, camp, m);
    let c = Campaign::default_for(1, root);
    assert_eq!(t.pn(&c, 0, &[], Some(camp), DESCENT_MAX_PLIES).0, Some(camp));
}

/// `test_proof_manager.py::SelectedChildHysteresisTests::test_the_descent_follows_the_sticky_primary`
/// and `::test_the_backup_bucket_still_gets_an_alternative`.
#[test]
fn test_the_descent_follows_the_sticky_primary() {
    let mut t = T::new();
    let root = t.pos(P::default());
    let best = t.pos(P { black: true, ..P::default() });
    let held = t.pos(P { black: true, ..P::default() });
    let (mb, mh) = (t.mv(), t.mv());
    t.edge(root, best, mb);
    t.edge(root, held, mh);
    t.table.put(root, Row { pn: 5, dn: 9, expanded: true, selected: mh, born: 0 });
    t.table.put(best, Row { pn: 5, dn: 1, expanded: false, selected: Move::NONE, born: 0 });
    t.table.put(held, Row { pn: 8, dn: 1, expanded: false, selected: Move::NONE, born: 0 });
    let c = campaign(root, primary_only());
    assert_eq!(t.pn(&c, 0, &[], None, DESCENT_MAX_PLIES).0, Some(held));
    let c = campaign(root, json!({"primary": 0.0, "backup": 1.0, "explore": 0.0}));
    assert_eq!(t.pn(&c, 0, &[], None, DESCENT_MAX_PLIES).0, Some(best));
}

/// `test_value_descent.py::ProductionRootTests` (the two walk tests).
#[test]
fn production_root_tests() {
    let mut t = T::new();
    let (me3, mnf3) = (t.mv(), t.mv());
    let root = t.pos(P { eval: Some(40), backed: Some(812), backed_move: Some(mnf3), ..P::default() });
    let e3 = t.pos(P { black: true, eval: Some(20), ..P::default() });
    let nf3 = t.pos(P { black: true, eval: Some(812), ..P::default() });
    t.edge(root, e3, me3);
    t.edge(root, nf3, mnf3);
    let row = |pn, dn| Row { pn, dn, expanded: false, selected: Move::NONE, born: 0 };
    t.table.put(root, row(94, 2));
    t.table.put(e3, row(119, 1));
    t.table.put(nf3, row(475, 1));
    let c = campaign(root, primary_only());
    // test_the_proof_descent_still_goes_for_the_cheap_line
    assert_eq!(t.pn(&c, 0, &[], None, DESCENT_MAX_PLIES).0, Some(e3));
    // test_the_value_descent_goes_for_the_backed_move
    let v = t.value(&c, &[], None, DESCENT_MAX_PLIES).unwrap();
    assert_eq!((v.pos, v.arm), (nf3, ValueArm::Spine));
}

// ------------------------------------------------------------ value walker

#[test]
fn spine_walk_tests() {
    let mut t = T::new();
    let nodes = t.spine(4, 0);
    let c = campaign(nodes[0], primary_only());
    // test_the_walk_ends_on_the_leaf_without_backing
    let v = t.value(&c, &[], None, DESCENT_MAX_PLIES).unwrap();
    assert_eq!((v.pos, v.plies, v.arm, v.floor_nodes), (nodes[4], 4, ValueArm::Spine, None));
    // test_a_short_line_is_untouched
    assert_eq!(v.line_plies, 4);
    // test_a_closed_root_yields_nothing
    t.set_status(nodes[0], Status::WhiteWin);
    assert_eq!(t.value(&c, &[], None, DESCENT_MAX_PLIES), None);
}

#[test]
fn saturated_tip_tests() {
    // test_the_target_climbs_to_the_node_with_unjudged_replies
    let mut t = T::new();
    let nodes = t.spine(2, SPINE);
    let spare = t.pos(P::default());
    let m = t.mv();
    t.edge(nodes[1], spare, m);
    let c = campaign(nodes[0], primary_only());
    let v = t.value(&c, &[], None, DESCENT_MAX_PLIES).unwrap();
    assert_eq!((v.pos, v.arm, v.floor_nodes), (nodes[1], ValueArm::Bottleneck, None));
    // test_a_spine_without_bottlenecks_buys_the_next_rung
    let mut t = T::new();
    let nodes = t.spine(2, SPINE);
    let c = campaign(nodes[0], primary_only());
    let v = t.value(&c, &[], None, DESCENT_MAX_PLIES).unwrap();
    assert_eq!((v.pos, v.arm, v.floor_nodes), (nodes[2], ValueArm::Rung, Some(SPINE)));
}

#[test]
fn test_the_pre_cycle_node_buys_a_deeper_pass() {
    let mut t = T::new();
    let (m0, m1) = (t.mv(), t.mv());
    let root = t.pos(P { eval: Some(800), backed: Some(812), backed_move: Some(m0), nodes: 8_000_000, ..P::default() });
    let mid = t.pos(P { black: true, eval: Some(800), backed: Some(812), backed_move: Some(m1), ..P::default() });
    t.edge(root, mid, m0);
    t.edge(mid, root, m1);
    let c = campaign(root, primary_only());
    let v = t.value(&c, &[], None, DESCENT_MAX_PLIES).unwrap();
    assert_eq!((v.pos, v.arm, v.floor_nodes), (mid, ValueArm::Cycle, Some(8_000_001)));
}

/// `ReservationTests` fixture: root → tip (800), alt (700, competitive), far (-400).
fn reservation() -> (T, PosId, PosId, PosId, PosId) {
    let mut t = T::new();
    let (m0, m1, m2) = (t.mv(), t.mv(), t.mv());
    let root = t.pos(P { eval: Some(800), backed: Some(800), backed_move: Some(m0), ..P::default() });
    let tip = t.pos(P { black: true, eval: Some(800), ..P::default() });
    let alt = t.pos(P { black: true, eval: Some(700), ..P::default() });
    let far = t.pos(P { black: true, eval: Some(-400), ..P::default() });
    t.edge(root, tip, m0);
    t.edge(root, alt, m1);
    t.edge(root, far, m2);
    (t, root, tip, alt, far)
}

#[test]
fn reservation_tests() {
    let (mut t, root, tip, alt, far) = reservation();
    let c = campaign(root, primary_only());
    // test_the_batch_reservation_moves_the_walk_too
    assert_eq!(t.value(&c, &[tip], None, DESCENT_MAX_PLIES).unwrap().pos, alt);
    // test_a_walk_with_nothing_left_to_offer_comes_back_empty
    assert_eq!(t.value(&c, &[root, tip, alt], None, DESCENT_MAX_PLIES), None);
    // test_a_live_task_on_the_tip_diverts_to_the_alternative
    t.live.insert(tip);
    assert_eq!(t.value(&c, &[], None, DESCENT_MAX_PLIES).unwrap().pos, alt);
    t.live.clear();
    // test_the_backup_bucket_spends_on_the_alternative
    let backup = campaign(root, json!({"primary": 0.0, "backup": 1.0, "explore": 0.0}));
    let v = t.value(&backup, &[], None, DESCENT_MAX_PLIES).unwrap();
    assert_eq!((v.pos, v.arm), (alt, ValueArm::Backup));
    // test_the_explore_bucket_never_buys_the_worst_child
    let explore = campaign(root, json!({"primary": 0.0, "backup": 0.0, "explore": 1.0}));
    let v = t.value(&explore, &[], None, DESCENT_MAX_PLIES).unwrap();
    assert_ne!(v.pos, far);
    assert_eq!(v.pos, tip);
    // test_the_explore_bucket_buys_the_best_seeded_virgin
    let virgin = t.pos(P { eval: Some(-50), ..P::default() });
    let better = t.pos(P { eval: Some(-900), ..P::default() });
    let (m1, m2) = (t.mv(), t.mv());
    t.edge(tip, virgin, m1);
    t.edge(tip, better, m2);
    let v = t.value(&explore, &[], None, DESCENT_MAX_PLIES).unwrap();
    assert_eq!((v.pos, v.arm), (better, ValueArm::Explore));
}

#[test]
fn campaign_root_tests() {
    let mut t = T::new();
    let [g0, g1, g9, c0, c1, c2] = [t.mv(), t.mv(), t.mv(), t.mv(), t.mv(), t.mv()];
    let root = t.pos(P { eval: Some(40), backed: Some(812), backed_move: Some(g0), ..P::default() });
    let gmid = t.pos(P { black: true, eval: Some(812), backed: Some(812), backed_move: Some(g1), ..P::default() });
    let gtip = t.pos(P { eval: Some(812), ..P::default() });
    let gspare = t.pos(P { black: true, ..P::default() });
    t.edge(root, gmid, g0);
    t.edge(gmid, gtip, g1);
    t.edge(root, gspare, g9);
    let camp = t.pos(P { black: true, eval: Some(500), backed: Some(520), backed_move: Some(c1), ..P::default() });
    let cmid = t.pos(P { eval: Some(520), backed: Some(520), backed_move: Some(c2), ..P::default() });
    let ctip = t.pos(P { black: true, eval: Some(520), ..P::default() });
    t.edge(root, camp, c0);
    t.edge(camp, cmid, c1);
    t.edge(cmid, ctip, c2);
    let c = campaign(root, primary_only());
    // test_the_walk_follows_the_spine_of_that_root
    let v = t.value(&c, &[], Some(camp), DESCENT_MAX_PLIES).unwrap();
    assert_eq!((v.pos, v.arm, v.plies), (ctip, ValueArm::Spine, 2));
    // test_without_a_start_the_same_campaign_walks_the_global_spine
    assert_eq!(t.value(&c, &[], None, DESCENT_MAX_PLIES).unwrap().pos, gtip);
    // test_a_campaign_spine_without_bottlenecks_deepens_its_own_tip
    t.set_nodes(ctip, SPINE);
    let v = t.value(&c, &[], Some(camp), DESCENT_MAX_PLIES).unwrap();
    assert_eq!((v.pos, v.arm), (ctip, ValueArm::Rung));
    // test_a_saturated_tip_climbs_inside_the_campaign_subtree
    let spare = t.pos(P { black: true, ..P::default() });
    let c9 = t.mv();
    t.edge(cmid, spare, c9);
    let v = t.value(&c, &[], Some(camp), DESCENT_MAX_PLIES).unwrap();
    assert_eq!((v.pos, v.arm), (cmid, ValueArm::Bottleneck));
    assert!(![root, gmid, gtip].contains(&v.pos));
}

// ------------------------------------------------------------ long lines

#[test]
fn long_line_weight_tests() {
    // test_no_penalty_up_to_the_threshold
    for plies in [0, 33, LONG_LINE_PLIES] {
        assert_eq!(long_line_weight(plies), 1.0);
    }
    // test_the_weight_halves_every_half_life
    assert!((long_line_weight(LONG_LINE_PLIES + LONG_LINE_HALF_LIFE_PLIES) - 0.5).abs() < 1e-12);
    assert!((long_line_weight(400) - 0.5f64.powf(200.0 / 40.0)).abs() < 1e-15);
    assert!(long_line_weight(400) < 0.05);
    // test_it_never_reaches_zero
    assert!(long_line_weight(5_000) > 0.0);
}

/// `_with_alternative`: a 4-ply long spine plus a competitive 1-ply reply.
fn with_alternative(line: u16) -> (T, Vec<PosId>, PosId) {
    let mut t = T::new();
    let (nodes, _) = t.long_spine(4, line);
    let alt = t.pos(P { black: true, eval: Some(712), ..P::default() });
    let m = t.mv();
    t.edge(nodes[0], alt, m);
    (t, nodes, alt)
}

#[test]
fn long_line_penalty_tests() {
    // test_the_line_length_counts_what_the_cut_hides
    let mut t = T::new();
    let (nodes, _) = t.long_spine(4, 400);
    let c = campaign(nodes[0], primary_only());
    let v = t.value(&c, &[], None, 2).unwrap();
    assert_eq!((v.pos, v.plies, v.line_plies), (nodes[2], 2, 400));
    // test_when_only_long_lines_are_left_the_walk_still_buys_one
    assert_eq!(v.pos, nodes[2]);
    // test_a_very_long_line_yields_to_a_short_alternative
    let (t, nodes, alt) = with_alternative(400);
    let c = campaign(nodes[0], primary_only());
    let v = t.value(&c, &[], None, 2).unwrap();
    assert_eq!((v.pos, v.line_plies), (alt, 1));
    // test_a_mildly_long_line_still_beats_the_next_plan
    let (t, nodes, _alt) = with_alternative(220);
    let c = campaign(nodes[0], primary_only());
    assert_eq!(t.value(&c, &[], None, 2).unwrap().pos, nodes[2]);
    // test_values_are_not_touched: the walker only reads (`&self` over a
    // shared graph); nothing to compare.
}

/// The gate only reorders: with the gate on, the walks still find a target
/// (the differential test checks the exact answers with the gate on).
#[test]
fn the_gate_is_a_parameter() {
    let (t, root, tip, _alt, _far) = reservation();
    let c = campaign(root, primary_only());
    let w = Walker::new(&t.g, &t.table, &c, DescentConfig { gate: GateConfig { enabled: true }, ..DescentConfig::default() });
    assert_eq!(w.descend_value(0, &|_| false, &|_| false, None).unwrap().pos, tip);
    assert_eq!(c.goal, Goal::WhiteWin);
}
