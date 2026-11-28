//! Steps 6 to 8 of the port through the backed ascent: v1's portable tests of
//! groups BKD-1 to BKD-9 and BKD-12 that drive `backup_backed_evals`,
//! `_backed_for`, `_best_child` or `stale_backed_rows` directly, translated
//! fixture for fixture.  Each test names its v1 original.  Tests that need
//! the loop rules (repetition walks), a landing, a view or a command are not
//! here (see `docs/rules-port-status.md`).

use av2_model::{Closure, GraphRead, Move, PosId, Proof, Status};
use av2_rules::ascent::{AscentOpts, stale_backed_rows};
use av2_rules::backed::{Backed, ChildValue, best_child};
use av2_rules::config::RulesConfig;
use av2_rules::conv::{PROVEN_QUALITY, best_known_eval};
use av2_rules::effects::{Event, GuardReason};
use av2_rules::loops::NoLoops;
use av2_rules::purchase::MemTasks;
use av2_rules::testkit::{Fixture, START_FEN};
use std::collections::HashMap;

// ------------------------------------------------------------ fixtures

/// v1's synthetic positions (`_pos(name, stm, **kw)`: only the side to move
/// and the backup's fields matter) and edges (`_edge(parent, child, uci)`).
struct T {
    f: Fixture,
    auto: HashMap<PosId, usize>,
}

/// A v1 move name that is not UCI (`a1a1`, `m1m1`, ...) as a legal 16-bit
/// move keeping the order among siblings.
fn name_move(s: &str) -> String {
    match s {
        "a1a1" => "a1a2".into(),
        "h1h1" => "h1h2".into(),
        "m1m1" => "e1e2".into(),
        "m2m2" => "e1e3".into(),
        "m3m3" => "e1e4".into(),
        other => other.into(),
    }
}

impl T {
    fn new() -> T {
        T { f: Fixture::new(), auto: HashMap::new() }
    }

    fn pos(&mut self, name: &str, stm: char) -> PosId {
        self.f.named(name, stm == 'b')
    }

    /// `_edge(parent, child)` with v1's default name `a1a{1 + count}`, read
    /// one square up (`a1a1` is not a move): the order among the default
    /// names, and against named moves, is kept.
    fn edge(&mut self, parent: PosId, child: PosId, uci: Option<&str>) {
        let count = self.f.children(parent).len();
        let name = match uci {
            Some(u) => name_move(u),
            None => format!("a1a{}", count + 2),
        };
        *self.auto.entry(parent).or_insert(0) += 1;
        self.f.edge(parent, &name, child);
    }

    fn expanded(&mut self, id: PosId) {
        self.f.set(id, |c| c.set_flag(av2_model::record::F_EXPANDED, true));
    }

    fn eval(&mut self, id: PosId, cp: i16, nodes: u64) {
        self.f.set(id, |c| c.eval_cp = cp);
        self.f.set_ext(id, |e| e.nodes_invested = nodes);
    }

    fn backed(&mut self, id: PosId, value: i16, mv: &str, plies: u16, nodes: u64) {
        let m = Move::parse_opt(mv).unwrap();
        self.f.set(id, |c| {
            c.backed_eval = value;
            c.backed_move = m;
            c.backed_plies = plies;
        });
        self.f.set_ext(id, |e| e.backed_nodes = nodes);
    }

    fn status(&mut self, id: PosId, s: Status, c: Closure) {
        self.f.close(id, s, c, Proof::None);
    }

    fn stored(&self, id: PosId) -> Backed {
        self.f.stored(id)
    }

    fn value(&self, id: PosId) -> Option<i32> {
        self.stored(id).value
    }

    fn mv(&self, id: PosId) -> String {
        self.stored(id).mv.to_uci()
    }

    fn ascent(&mut self, seeds: &[PosId]) -> (av2_rules::ascent::AscentReport, av2_rules::effects::Recorder) {
        let (r, rec, _) = self.f.ascent(seeds);
        (r, rec)
    }

    fn ascent_opts(&mut self, seeds: &[PosId], opts: AscentOpts) -> (av2_rules::ascent::AscentReport, av2_rules::effects::Recorder) {
        self.f.ascent_with(&RulesConfig::default(), seeds, opts, &mut MemTasks::default())
    }

    fn parents(&self, id: PosId) -> Vec<PosId> {
        let mut v = Vec::new();
        self.f.g.parents(id, &mut v);
        v
    }

    fn all(&self) -> Vec<PosId> {
        (0..self.f.g.len()).map(PosId).collect()
    }
}

fn guards(rec: &av2_rules::effects::Recorder) -> Vec<(GuardReason, u32, u64, Vec<PosId>)> {
    rec.events
        .iter()
        .filter_map(|e| match e {
            Event::BackedGuard { reason, generation, left, keys, .. } => Some((*reason, *generation, *left, keys.clone())),
            _ => None,
        })
        .collect()
}

// ------------------------------------------------------------ test_backed_eval.py::BackedChainTests

/// test_backed_eval.py::BackedChainTests::test_three_level_chain_propagates_upward
#[test]
fn three_level_chain_propagates_upward() {
    let mut t = T::new();
    let p = t.pos("P", 'w');
    let c = t.pos("C", 'b');
    let g = t.pos("G", 'w');
    let h = t.pos("H", 'b');
    for x in [p, c, g, h] {
        t.expanded(x);
    }
    let leaf = t.pos("L", 'w');
    t.eval(leaf, 300, 1_000);
    t.edge(p, c, Some("d2d4"));
    t.edge(c, g, None);
    t.edge(g, h, None);
    t.edge(h, leaf, None);
    t.ascent(&[h]);
    for (node, plies) in [(h, 1), (g, 2), (c, 3), (p, 4)] {
        assert_eq!(t.value(node), Some(300));
        assert_eq!(t.stored(node).plies, plies);
    }
    assert_eq!(t.mv(p), "d2d4");
}

/// test_backed_eval.py::BackedChainTests::test_partial_loss_without_anchor_backs_nothing
#[test]
fn partial_loss_without_anchor_backs_nothing() {
    let mut t = T::new();
    let padre = t.pos("PLA", 'b');
    t.expanded(padre);
    let perdida = t.pos("PLA-LOSS", 'w');
    t.status(perdida, Status::WhiteWin, Closure::None);
    t.backed(perdida, 10_000, "", 0, 0);
    t.edge(padre, perdida, Some("e8d7"));
    for i in 0..3 {
        let h = t.pos(&format!("PLA-H{i}"), 'w');
        t.edge(padre, h, None);
    }
    t.ascent(&[padre]);
    assert_eq!(t.value(padre), None);
    assert_eq!(t.stored(padre).mv, Move::NONE);
}

/// test_backed_eval.py::BackedChainTests::test_partial_win_for_mover_still_backs_up
#[test]
fn partial_win_for_mover_still_backs_up() {
    let mut t = T::new();
    let padre = t.pos("PWI", 'w');
    t.expanded(padre);
    let ganada = t.pos("PWI-WIN", 'b');
    t.status(ganada, Status::WhiteWin, Closure::None);
    t.backed(ganada, 10_000, "", 0, 0);
    t.edge(padre, ganada, Some("d1h5"));
    for i in 0..3 {
        let h = t.pos(&format!("PWI-H{i}"), 'b');
        t.edge(padre, h, None);
    }
    t.ascent(&[padre]);
    assert_eq!(t.value(padre), Some(10_000));
    assert_eq!(t.mv(padre), "d1h5");
}

/// test_backed_eval.py::BackedChainTests::test_transposition_updates_every_parent
#[test]
fn transposition_updates_every_parent() {
    let mut t = T::new();
    let a = t.pos("A", 'w');
    let b = t.pos("B", 'w');
    let shared = t.pos("S", 'b');
    for x in [a, b, shared] {
        t.expanded(x);
    }
    let leaf = t.pos("SL", 'w');
    t.eval(leaf, -450, 1_000);
    t.edge(a, shared, Some("g1f3"));
    t.edge(b, shared, Some("b1c3"));
    t.edge(shared, leaf, None);
    t.ascent(&[shared]);
    for node in [a, b, shared] {
        assert_eq!(t.value(node), Some(-450));
    }
}

/// test_backed_eval.py::BackedChainTests::test_transposition_cycle_terminates
#[test]
fn transposition_cycle_terminates() {
    let mut t = T::new();
    let a = t.pos("CA", 'w');
    let b = t.pos("CB", 'b');
    t.expanded(a);
    t.expanded(b);
    let leaf = t.pos("CL", 'w');
    t.eval(leaf, 120, 1_000);
    t.edge(a, b, None);
    t.edge(b, a, None);
    t.edge(b, leaf, None);
    let (r, _) = t.ascent(&[b]);
    assert!(r.changed > 0);
    assert!(t.value(b).is_some());
}

/// test_backed_eval.py::BackedChainTests::test_ply_guard_is_recorded
#[test]
fn ply_guard_is_recorded() {
    let mut t = T::new();
    let chain: Vec<PosId> = (0..8)
        .map(|i| {
            let id = t.pos(&format!("D{i}"), if i % 2 == 0 { 'w' } else { 'b' });
            t.expanded(id);
            id
        })
        .collect();
    let leaf = t.pos("DL", 'w');
    t.eval(leaf, 700, 1_000);
    for w in chain.windows(2) {
        t.edge(w[0], w[1], None);
    }
    t.edge(chain[7], leaf, None);
    let (_, rec) = t.ascent_opts(&[chain[7]], AscentOpts { max_plies: Some(3), ..AscentOpts::default() });
    assert_eq!(t.value(chain[0]), None);
    assert!(!guards(&rec).is_empty());
}

// ------------------------------------------------------------ BackedGuardTests

/// test_backed_eval.py::BackedGuardTests::test_or_node_never_drops_below_its_own_eval
#[test]
fn or_node_never_drops_below_its_own_eval() {
    let mut t = T::new();
    let p = t.pos("ORP", 'w');
    t.expanded(p);
    t.eval(p, 200, 1_000);
    let bad = t.pos("ORBAD", 'b');
    t.eval(bad, -500, 8_000);
    let blind = t.pos("ORBLIND", 'b');
    t.edge(p, bad, None);
    t.edge(p, blind, None);
    t.ascent(&[p]);
    assert_eq!(t.value(p), Some(200));
    assert_eq!(t.stored(p).mv, Move::NONE);
}

/// test_backed_eval.py::BackedGuardTests::test_or_node_rises_with_a_deep_child
#[test]
fn or_node_rises_with_a_deep_child() {
    let mut t = T::new();
    let p = t.pos("ORP2", 'w');
    t.expanded(p);
    t.eval(p, 200, 1_000);
    let good = t.pos("ORGOOD", 'b');
    t.eval(good, 760, 8_000);
    let blind = t.pos("ORBLIND2", 'b');
    t.edge(p, good, Some("d2d4"));
    t.edge(p, blind, None);
    t.ascent(&[p]);
    assert_eq!(t.value(p), Some(760));
    assert_eq!(t.mv(p), "d2d4");
}

/// test_backed_eval.py::BackedGuardTests::test_and_node_ignores_an_optimistic_partial_min
#[test]
fn and_node_ignores_an_optimistic_partial_min() {
    let mut t = T::new();
    let p = t.pos("ANDP", 'b');
    t.expanded(p);
    t.eval(p, -200, 1_000);
    let gw = t.pos("ANDW", 'w');
    t.eval(gw, 500, 8_000);
    let blind = t.pos("ANDBLIND", 'w');
    t.edge(p, gw, None);
    t.edge(p, blind, None);
    t.ascent(&[p]);
    assert_eq!(t.value(p), Some(-200));
}

/// test_backed_eval.py::BackedGuardTests::test_and_node_accepts_a_proven_better_defence
#[test]
fn and_node_accepts_a_proven_better_defence() {
    let mut t = T::new();
    let p = t.pos("ANDP2", 'b');
    t.expanded(p);
    t.eval(p, -200, 1_000);
    let better = t.pos("ANDD", 'w');
    t.eval(better, -820, 8_000);
    let blind = t.pos("ANDBLIND2", 'w');
    t.edge(p, better, Some("e7e5"));
    t.edge(p, blind, None);
    t.ascent(&[p]);
    assert_eq!(t.value(p), Some(-820));
    assert_eq!(t.mv(p), "e7e5");
}

/// test_backed_eval.py::BackedGuardTests::test_complete_coverage_replaces_the_point_eval_either_way
#[test]
fn complete_coverage_replaces_the_point_eval_either_way() {
    let mut t = T::new();
    let p = t.pos("FULL", 'w');
    t.expanded(p);
    t.eval(p, 400, 1_000);
    for (name, value) in [("FA", -50), ("FB", -900)] {
        let c = t.pos(name, 'b');
        t.eval(c, value, 10);
        t.edge(p, c, None);
    }
    t.ascent(&[p]);
    assert_eq!(t.value(p), Some(-50));
}

/// test_backed_eval.py::BackedGuardTests::test_solved_children_enter_with_their_truth_value
#[test]
fn solved_children_enter_with_their_truth_value() {
    let mut t = T::new();
    let p = t.pos("TP", 'w');
    t.expanded(p);
    t.eval(p, -300, 1_000);
    let draw = t.pos("TDRAW", 'b');
    t.status(draw, Status::Draw, Closure::Terminal);
    t.edge(p, draw, Some("a2a3"));
    let bad = t.pos("TBAD", 'b');
    t.eval(bad, -800, 1_000);
    t.edge(p, bad, None);
    t.ascent(&[p]);
    assert_eq!(t.value(p), Some(0));
    assert_eq!(t.mv(p), "a2a3");
}

/// test_backed_eval.py::BackedGuardTests::test_backed_never_contradicts_a_proven_status
#[test]
fn backed_never_contradicts_a_proven_status() {
    let mut t = T::new();
    let won = t.pos("WON", 'w');
    t.status(won, Status::WhiteWin, Closure::MatePv);
    t.f.set(won, |c| c.best_move = Move::parse("h5f7").unwrap());
    t.expanded(won);
    let kid = t.pos("WONKID", 'b');
    t.eval(kid, -4_000, 1_000);
    t.edge(won, kid, None);
    t.ascent(&[won]);
    assert_eq!(t.value(won), Some(10_000));
    assert_eq!(t.stored(won).quality, PROVEN_QUALITY);
}

// ------------------------------------------------------------ BackedPropagationCostTests

/// test_backed_eval.py::BackedPropagationCostTests::test_sub_epsilon_drift_still_reaches_whoever_stands_on_it
#[test]
fn sub_epsilon_drift_still_reaches_whoever_stands_on_it() {
    let mut t = T::new();
    let grand = t.pos("EG", 'w');
    let parent = t.pos("EP", 'b');
    t.expanded(grand);
    t.expanded(parent);
    let leaf = t.pos("EL", 'w');
    t.eval(leaf, -300, 1_000);
    t.edge(grand, parent, None);
    t.edge(parent, leaf, None);
    t.ascent(&[parent]);
    assert_eq!(t.value(grand), Some(-300));
    t.f.set(leaf, |c| c.eval_cp = -304);
    t.ascent(&[parent]);
    assert_eq!(t.value(grand), Some(-304));
    t.f.set(leaf, |c| c.eval_cp = -420);
    t.ascent(&[parent]);
    assert_eq!(t.value(grand), Some(-420));
}

/// test_backed_eval.py::BackedPropagationCostTests::test_noise_under_a_move_nobody_stands_on_still_does_not_climb
/// (the query count becomes: the second ascent writes the one row and stops
/// at its level).
#[test]
fn noise_under_a_move_nobody_stands_on_still_does_not_climb() {
    let mut t = T::new();
    let parent = t.pos("NP", 'b');
    t.expanded(parent);
    let chosen = t.pos("NC", 'w');
    t.eval(chosen, 1265, 2_000);
    let idle = t.pos("NI", 'w');
    t.expanded(idle);
    let leaf = t.pos("NL", 'b');
    t.eval(leaf, 2_000, 2_000);
    t.edge(parent, chosen, Some("b8d7"));
    t.edge(parent, idle, Some("a7a6"));
    t.edge(idle, leaf, Some("d2d4"));
    t.ascent(&[idle]);
    assert_eq!(t.mv(parent), "b8d7");
    t.f.set(leaf, |c| c.eval_cp = 2_004);
    let (r, _) = t.ascent(&[idle]);
    assert_eq!((r.levels, r.changed), (1, 1));
    assert_eq!(t.value(parent), Some(1265));
    assert_eq!(t.mv(parent), "b8d7");
}

/// The same shape with the noise cut switched off (owner decision D2, not a
/// v1 test): the drift wakes every parent, and the value is the same.
#[test]
fn without_the_noise_cut_the_drift_climbs_and_changes_nothing() {
    let mut t = T::new();
    let parent = t.pos("NP", 'b');
    t.expanded(parent);
    let chosen = t.pos("NC", 'w');
    t.eval(chosen, 1265, 2_000);
    let idle = t.pos("NI", 'w');
    t.expanded(idle);
    let leaf = t.pos("NL", 'b');
    t.eval(leaf, 2_000, 2_000);
    t.edge(parent, chosen, Some("b8d7"));
    t.edge(parent, idle, Some("a7a6"));
    t.edge(idle, leaf, Some("d2d4"));
    let cfg = RulesConfig { noise_cut: false, ..RulesConfig::default() };
    t.f.ascent_with(&cfg, &[idle], AscentOpts::default(), &mut MemTasks::default());
    t.f.set(leaf, |c| c.eval_cp = 2_004);
    let (r, _) = t.f.ascent_with(&cfg, &[idle], AscentOpts::default(), &mut MemTasks::default());
    assert_eq!(r.levels, 2);
    assert_eq!((t.value(parent), t.mv(parent).as_str()), (Some(1265), "b8d7"));
}

// ------------------------------------------------------------ BackedArgmaxDriftTests

fn drift_shape(t: &mut T, p: &str) -> (PosId, PosId, PosId, PosId) {
    let grand = t.pos(&format!("{p}G"), 'w');
    let parent = t.pos(&format!("{p}P"), 'b');
    let chosen = t.pos(&format!("{p}B8D7"), 'w');
    for x in [grand, parent, chosen] {
        t.expanded(x);
    }
    let rival = t.pos(&format!("{p}C6C5"), 'w');
    t.eval(rival, 1268, 2_000);
    let leaf = t.pos(&format!("{p}L"), 'b');
    t.eval(leaf, 1265, 2_000);
    t.edge(grand, parent, Some("d1g4"));
    t.edge(parent, chosen, Some("b8d7"));
    t.edge(parent, rival, Some("c6c5"));
    t.edge(chosen, leaf, Some("d2d4"));
    (grand, parent, chosen, leaf)
}

/// test_backed_eval.py::BackedArgmaxDriftTests::test_a_drift_that_flips_the_parents_edge_is_never_silenced
#[test]
fn a_drift_that_flips_the_parents_edge_is_never_silenced() {
    let mut t = T::new();
    let (grand, parent, chosen, leaf) = drift_shape(&mut t, "D");
    t.ascent(&[chosen]);
    assert_eq!((t.value(parent), t.mv(parent).as_str()), (Some(1265), "b8d7"));
    assert_eq!(t.value(grand), Some(1265));
    t.f.set(leaf, |c| c.eval_cp = 1269);
    t.ascent(&[chosen]);
    assert_eq!(t.value(chosen), Some(1269));
    assert_eq!((t.value(parent), t.mv(parent).as_str()), (Some(1268), "c6c5"));
}

/// test_backed_eval.py::BackedArgmaxDriftTests::test_the_correction_climbs_to_the_ancestors_that_showed_it
#[test]
fn the_correction_climbs_to_the_ancestors_that_showed_it() {
    let mut t = T::new();
    let (grand, _parent, chosen, leaf) = drift_shape(&mut t, "C");
    t.ascent(&[chosen]);
    t.f.set(leaf, |c| c.eval_cp = 1269);
    t.ascent(&[chosen]);
    assert_eq!((t.value(grand), t.mv(grand).as_str()), (Some(1268), "d1g4"));
}

// ------------------------------------------------------------ BackedIngestTests (the direct ones)

/// test_backed_eval.py::BackedIngestTests::test_a_walked_mate_line_stops_at_the_losers_partial_node
#[test]
fn a_walked_mate_line_stops_at_the_losers_partial_node() {
    let mut f = Fixture::new();
    let root = f.pos(START_FEN);
    f.expand_opening(root);
    f.set(root, |c| c.eval_cp = 120);
    f.set_ext(root, |e| e.nodes_invested = 128_000_000);
    // The first edge by move name, and its first legal move (any one does).
    let walked = f.edge_child(root, "a2a3");
    let deep = f.child(walked, "a7a6");
    f.close(deep, Status::WhiteWin, Closure::Terminal, Proof::None);
    f.ascent(&[deep, walked, root]);
    assert_eq!(f.stored(walked).value, None);
    assert_eq!(f.stored(walked).mv, Move::NONE);
    assert_eq!(f.stored(root).value, None);
    assert_eq!(best_known_eval(&f.core(root)), Some(120));
}

/// test_backed_eval.py::BackedIngestTests::test_partially_expanded_lineage_still_backs_up
#[test]
fn partially_expanded_lineage_still_backs_up() {
    let mut f = Fixture::new();
    let root = f.pos(START_FEN);
    let middle = f.child(root, "a2a3");
    let deep = f.child(middle, "a7a6");
    f.set(deep, |c| c.eval_cp = -880);
    f.set_ext(deep, |e| e.nodes_invested = 10_000_000);
    assert!(!f.core(middle).expanded());
    f.cascade(&[deep]);
    assert_eq!(f.core(middle).eval(), None);
    f.ascent(&[middle]);
    assert_eq!(f.stored(middle).value, Some(-880));
    assert_eq!(f.stored(root).value, Some(-880));
}

// ------------------------------------------------------------ BackedRelicTests

/// test_backed_eval.py::BackedRelicTests::test_current_rules_would_not_regenerate_the_relic
#[test]
fn current_rules_would_not_regenerate_the_relic() {
    let mut t = T::new();
    let anchor = t.pos("RELIC-A", 'b');
    t.eval(anchor, 671, 0);
    t.backed(anchor, 9994, "c8g4", 6, 0);
    let spine = t.pos("RELIC-S", 'w');
    t.edge(anchor, spine, Some("c8g4"));
    let leaf = t.pos("RELIC-L", 'b');
    t.eval(leaf, 9994, 128_000_000);
    t.edge(spine, leaf, Some("c2c3"));
    t.ascent(&[spine]);
    assert_eq!(t.value(anchor), Some(671));
    assert_eq!(t.stored(anchor).mv, Move::NONE);
}

// ------------------------------------------------------------ BackedRepetitionTests (the loop-free one)

/// test_backed_eval.py::BackedRepetitionTests::test_a_transposition_that_does_not_come_back_keeps_its_value
/// (no loop rule is involved: the diamond has no cycle, and the loop-free
/// ascent must already give every node the value).
#[test]
fn a_transposition_that_does_not_come_back_keeps_its_value() {
    let mut t = T::new();
    let a = t.pos("DIA-A", 'w');
    let b = t.pos("DIA-B", 'b');
    let c = t.pos("DIA-C", 'b');
    for x in [a, b, c] {
        t.expanded(x);
    }
    let d = t.pos("DIA-D", 'w');
    t.eval(d, -260, 1_000);
    t.edge(a, b, Some("g1f3"));
    t.edge(a, c, Some("b1c3"));
    t.edge(b, d, Some("b1c3"));
    t.edge(c, d, Some("g1f3"));
    t.ascent(&[b, c]);
    for node in [b, c, a] {
        assert_eq!(t.value(node), Some(-260));
    }
    assert!(["g1f3", "b1c3"].contains(&t.mv(a).as_str()));
}

// ------------------------------------------------------------ test_backprop_wakes.py::LateWakeTests

fn chain(t: &mut T, name: &str, top: PosId, bottom: PosId, plies: usize) {
    let mut upper = top;
    let mut black = t.f.core(top).black_to_move();
    for i in 0..plies - 1 {
        black = !black;
        let lower = t.pos(&format!("{name}-{i}"), if black { 'b' } else { 'w' });
        t.expanded(lower);
        t.edge(upper, lower, Some("a1a1"));
        upper = lower;
    }
    t.edge(upper, bottom, Some("a1a1"));
}

fn leaf(t: &mut T, name: &str, stm: char, value: i16) -> PosId {
    let id = t.pos(name, stm);
    t.eval(id, value, 1_000);
    id
}

/// `_three_roads`: P(white) reaches S by 2, 4 and 6 plies; the two short
/// roads have a cheaper exit (150).
fn three_roads(t: &mut T, name: &str) -> (PosId, PosId, PosId) {
    let p = t.pos(&format!("{name}-P"), 'w');
    t.expanded(p);
    let s = leaf(t, &format!("{name}-S"), 'w', 100);
    let c1 = t.pos(&format!("{name}-c1"), 'b');
    t.expanded(c1);
    t.edge(p, c1, Some("m1m1"));
    t.edge(c1, s, Some("a1a1"));
    let cap1 = leaf(t, &format!("{name}-cap1"), 'w', 150);
    t.edge(c1, cap1, Some("h1h1"));
    let c2 = t.pos(&format!("{name}-c2"), 'b');
    let x2 = t.pos(&format!("{name}-x2"), 'w');
    let y2 = t.pos(&format!("{name}-y2"), 'b');
    for x in [c2, x2, y2] {
        t.expanded(x);
    }
    t.edge(p, c2, Some("m2m2"));
    t.edge(c2, x2, Some("a1a1"));
    t.edge(x2, y2, Some("a1a1"));
    t.edge(y2, s, Some("a1a1"));
    let cap2 = leaf(t, &format!("{name}-cap2"), 'w', 150);
    t.edge(y2, cap2, Some("h1h1"));
    let c3 = t.pos(&format!("{name}-c3"), 'b');
    t.expanded(c3);
    t.edge(p, c3, Some("m3m3"));
    chain(t, &format!("{name}-long"), c3, s, 5);
    (p, s, c3)
}

/// `_analyse`: the analysed position and its parents seed the ascent.
fn analyse(t: &mut T, cfg: &RulesConfig, pos: PosId, value: i16) -> (av2_rules::ascent::AscentReport, av2_rules::effects::Recorder) {
    t.f.set(pos, |c| c.eval_cp = value);
    let mut seeds = vec![pos];
    seeds.extend(t.parents(pos));
    t.f.ascent_with(cfg, &seeds, AscentOpts::default(), &mut MemTasks::default())
}

/// test_backprop_wakes.py::LateWakeTests::test_the_last_wave_of_an_ascent_still_reaches_the_node
#[test]
fn the_last_wave_of_an_ascent_still_reaches_the_node() {
    let mut t = T::new();
    let cfg = RulesConfig::default();
    let (p, s, c3) = three_roads(&mut t, "LATE");
    analyse(&mut t, &cfg, s, 100);
    assert_eq!(t.value(p), Some(100));
    analyse(&mut t, &cfg, s, 200);
    assert_eq!(t.value(c3), Some(200));
    assert_eq!((t.value(p), t.stored(p).mv), (Some(200), Move::parse(&name_move("m3m3")).unwrap()));
}

/// test_backprop_wakes.py::LateWakeTests::test_an_ascent_leaves_nothing_for_the_next_one_to_fix
#[test]
fn an_ascent_leaves_nothing_for_the_next_one_to_fix() {
    let mut t = T::new();
    let cfg = RulesConfig::default();
    let (_p, s, _c3) = three_roads(&mut t, "FIXPOINT");
    analyse(&mut t, &cfg, s, 100);
    analyse(&mut t, &cfg, s, 200);
    let every = t.all();
    assert_eq!(t.ascent(&every).0.changed, 0);
}

/// test_backprop_wakes.py::LateWakeTests::test_a_wake_is_never_dropped_in_silence
#[test]
fn a_wake_is_never_dropped_in_silence() {
    let mut t = T::new();
    let (p, s, _c3) = three_roads(&mut t, "LOUD");
    analyse(&mut t, &RulesConfig::default(), s, 100);
    let tight = RulesConfig { backed_max_revisits: 2, ..RulesConfig::default() };
    let (_, rec) = analyse(&mut t, &tight, s, 200);
    let last = guards(&rec).pop().expect("BACKED_GUARD");
    assert_eq!(last.0, GuardReason::RevisitCap);
    assert!(last.3.contains(&p));
}

// ------------------------------------------------------------ test_stale_backed_sweep.py

/// `_line(name, plies, value)`: `plies` positions down to a leaf searched at
/// `value`, every one standing on its one move.
fn line(t: &mut T, name: &str, plies: usize, value: i16) -> (Vec<PosId>, PosId) {
    let mut nodes = Vec::new();
    let mut black = false;
    for i in 0..plies {
        let id = t.pos(&format!("{name}-{i}"), if black { 'b' } else { 'w' });
        t.expanded(id);
        nodes.push(id);
        black = !black;
    }
    let lf = t.pos(&format!("{name}-leaf"), if black { 'b' } else { 'w' });
    t.eval(lf, value, 1_000_000);
    for i in 0..plies {
        let lower = if i + 1 < plies { nodes[i + 1] } else { lf };
        t.edge(nodes[i], lower, Some("a1a2"));
    }
    t.ascent(&[lf, *nodes.last().unwrap()]);
    (nodes, lf)
}

fn analyse_line(
    t: &mut T,
    lf: PosId,
    parent: PosId,
    value: i16,
    opts: AscentOpts,
) -> (av2_rules::ascent::AscentReport, av2_rules::effects::Recorder) {
    t.f.set(lf, |c| c.eval_cp = value);
    t.ascent_opts(&[lf, parent], opts)
}

/// `_lost_update(name)`: the shape of 5fc31111 (19-Oct).
fn lost_update(t: &mut T, name: &str) -> PosId {
    let parent = t.pos(&format!("{name}-P"), 'b');
    t.expanded(parent);
    t.eval(parent, 1177, 128_000_000);
    t.backed(parent, 1216, "b7b5", 1, 0);
    let b5 = t.pos(&format!("{name}-b7b5"), 'w');
    t.eval(b5, 1139, 128_000_000);
    let b6 = t.pos(&format!("{name}-b7b6"), 'w');
    t.eval(b6, 1246, 128_000_000);
    t.edge(parent, b5, Some("b7b5"));
    t.edge(parent, b6, Some("b7b6"));
    parent
}

const CUT: AscentOpts = AscentOpts { max_plies: Some(3), open_only: false, generation: 0 };

/// test_stale_backed_sweep.py::CutAscentTests::test_a_cut_ascent_leaves_its_frontier_on_record
#[test]
fn a_cut_ascent_leaves_its_frontier_on_record() {
    let mut t = T::new();
    let (nodes, lf) = line(&mut t, "CUT", 8, 100);
    let (_, rec) = analyse_line(&mut t, lf, nodes[7], 250, CUT);
    let g = guards(&rec);
    assert_eq!(g.len(), 1);
    let (reason, generation, left, keys) = g[0].clone();
    assert_eq!(reason, GuardReason::PlyGuard);
    assert_eq!(generation, 0);
    assert_eq!(keys, vec![nodes[4]]);
    assert_eq!(left, 1);
    assert_eq!(t.value(nodes[5]), Some(250));
    assert_eq!(t.value(nodes[4]), Some(100));
    let stale = stale_backed_rows(&t.f.g, &[nodes[4]], &mut NoLoops);
    assert!(stale.iter().any(|(k, _, _)| *k == nodes[4]));
}

/// test_stale_backed_sweep.py::IngestCarryTests::test_the_frontiers_of_a_block_are_collected
/// (`carried_cuts` is the report's `carry`).
#[test]
fn the_frontiers_of_a_block_are_collected() {
    let mut t = T::new();
    let (nodes, lf) = line(&mut t, "COLLECT", 8, 100);
    let (r, _) = analyse_line(&mut t, lf, nodes[7], 250, CUT);
    assert_eq!(r.carry, vec![nodes[4]]);
}

/// test_stale_backed_sweep.py::IngestCarryTests::test_an_ascent_outside_an_ingest_collects_nothing
/// (v2 has no collecting context: the cut is only in the ascent's own report
/// and event, and nothing climbs past it).
#[test]
fn an_ascent_outside_an_ingest_collects_nothing() {
    let mut t = T::new();
    let (nodes, lf) = line(&mut t, "OUTSIDE", 8, 100);
    let (r, rec) = analyse_line(&mut t, lf, nodes[7], 250, CUT);
    assert_eq!(r.guard, Some(GuardReason::PlyGuard));
    assert_eq!(guards(&rec).len(), 1);
    assert_eq!(t.value(nodes[0]), Some(100));
}

/// test_stale_backed_sweep.py::LostUpdateTests::test_the_check_finds_what_one_level_of_the_backup_would_rewrite
#[test]
fn the_check_finds_what_one_level_of_the_backup_would_rewrite() {
    let mut t = T::new();
    let parent = lost_update(&mut t, "CHECK");
    let stale = stale_backed_rows(&t.f.g, &[parent], &mut NoLoops);
    let b7b5 = Move::parse("b7b5").unwrap();
    assert_eq!(
        stale,
        vec![(
            parent,
            Backed { value: Some(1216), mv: b7b5, plies: 1, quality: 0 },
            Backed { value: Some(1139), mv: b7b5, plies: 1, quality: 128_000_000 }
        )]
    );
    assert_eq!(t.stored(parent), Backed { value: Some(1216), mv: b7b5, plies: 1, quality: 0 });
}

/// test_stale_backed_sweep.py::AtRestTests::test_no_stale_rows_once_the_backup_is_at_rest
#[test]
fn no_stale_rows_once_the_backup_is_at_rest() {
    let mut t = T::new();
    let parent = lost_update(&mut t, "REST");
    let (nodes, _lf) = line(&mut t, "REST-LINE", 5, 40);
    t.edge(nodes[4], parent, Some("g1g2"));
    let everything = t.all();
    assert!(!stale_backed_rows(&t.f.g, &everything, &mut NoLoops).is_empty());
    let mut rounds = 0;
    while t.ascent(&everything).0.changed > 0 {
        rounds += 1;
        assert!(rounds < 100);
    }
    assert!(stale_backed_rows(&t.f.g, &everything, &mut NoLoops).is_empty());
}

// ------------------------------------------------------------ test_tb_distance.py::BackupTests

const ENGINE: u64 = 128_000_000;

fn tb_family(t: &mut T, stm: char, values: &[(&str, i16)]) -> PosId {
    let node = t.pos(&format!("TBD-{stm}"), stm);
    t.expanded(node);
    t.eval(node, values[0].1, ENGINE);
    let first = Move::parse(values[0].0).unwrap();
    t.f.set(node, |c| c.best_move = first);
    let other = if stm == 'w' { 'b' } else { 'w' };
    for &(mv, value) in values {
        let child = t.pos(&format!("TBD-{stm}-{mv}"), other);
        t.eval(child, value, ENGINE);
        t.edge(node, child, Some(mv));
    }
    node
}

/// test_tb_distance.py::BackupTests::test_the_winner_takes_the_shorter_way_in
#[test]
fn the_winner_takes_the_shorter_way_in() {
    let mut t = T::new();
    let node = tb_family(&mut t, 'w', &[("a1a2", 9_470), ("a1b1", 9_481)]);
    t.ascent(&[node]);
    assert_eq!((t.value(node), t.mv(node).as_str()), (Some(9_481), "a1b1"));
}

/// test_tb_distance.py::BackupTests::test_the_loser_takes_the_longer_way_in
#[test]
fn the_loser_takes_the_longer_way_in() {
    let mut t = T::new();
    let node = tb_family(&mut t, 'b', &[("a1a2", 9_481), ("a1b1", 9_470)]);
    t.ascent(&[node]);
    assert_eq!((t.value(node), t.mv(node).as_str()), (Some(9_470), "a1b1"));
}

/// test_tb_distance.py::BackupTests::test_best_child_orders_by_distance
#[test]
fn best_child_orders_by_distance() {
    let cv = |m: &str, v: i32| {
        let mut c = ChildValue::uninformed(Move::parse(m).unwrap(), PosId(0));
        c.value = Some(v);
        c.quality = ENGINE;
        c.plies = 1;
        c
    };
    let (near, far, big) = (cv("a1b1", 9_481), cv("a1a2", 9_470), cv("a1b2", 9_240));
    let kids = [far, big, near];
    assert_eq!(best_child(&kids, &[0, 1, 2], true, Move::NONE), 2);
    let neg: Vec<ChildValue> = kids.iter().map(|c| cv(&c.mv.to_uci(), -c.value.unwrap())).collect();
    assert_eq!(neg[best_child(&neg, &[0, 1, 2], false, Move::NONE)].mv.to_uci(), "a1b1");
}
