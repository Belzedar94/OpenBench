//! Steps 11 and 12 of the port (repetition marking, returning lines, the
//! settling look, cycle disambiguation), through `loops::V1Loops`: v1's
//! portable tests of `test_backprop_repetition.py`, `test_repetition_progress.py`,
//! `test_cycle_disambiguation.py`, `test_cycle_deadline.py` (counted, D1) and
//! the repetition classes of `test_backed_eval.py`, fixture for fixture.
//!
//! v1's fixtures give every position the same FEN and tell them apart by key:
//! here they are FEN-less named positions with the same side to move.  v1's
//! move names that are not moves in v2's encoding are mapped, keeping the
//! order among siblings: `a1a1` -> `a1a2`, `x1x1` -> `h1h2`, `y1y1` -> `h1h3`
//! (each the only move of its position), and `TieBreakTests`' `back` < `loop`
//! < `out` -> `a1a2` < `b1b2` < `c1c2`.

use av2_model::{Closure, Core, GraphRead, Move, PosId, Proof, Status};
use av2_rules::ascent::AscentOpts;
use av2_rules::backed::{Backed, ChildValue, Row, Showcase, backed_for, best_child, children_of};
use av2_rules::config::RulesConfig;
use av2_rules::conv::best_known_eval;
use av2_rules::effects::{Event, Recorder};
use av2_rules::loops::{LevelChildren, LoopRules, LoopVerdict, V1Loops};
use av2_rules::purchase::{MemTasks, PurchaseKind};
use av2_rules::repetition::{
    BACKED_CYCLE_MAX_NODES, BACKED_CYCLE_MAX_PLIES, LOOP_COSTS, LineCtx, Meter, SpineCache, draw_cycling_children,
    resolve_returning_children, value_on_line,
};
use av2_rules::testkit::{Fixture, mv};
use std::collections::BTreeMap;

const ENGINE: u64 = 128_000_000;
const SHALLOW: u64 = 32_000_000;

fn m(s: &str) -> &str {
    match s {
        "a1a1" => "a1a2",
        "x1x1" => "h1h2",
        "y1y1" => "h1h3",
        other => other,
    }
}

/// The fields of v1's `_pos(name, stm, **kw)`.
#[derive(Default, Clone, Copy)]
struct P {
    expanded: bool,
    eval: Option<i16>,
    nodes: u64,
    backed: Option<i16>,
    bmove: &'static str,
    plies: u16,
    bnodes: u64,
    status: Option<(Status, Closure)>,
}

fn pos(f: &mut Fixture, name: &str, stm: char, p: P) -> PosId {
    let id = f.named(name, stm == 'b');
    f.set(id, |c| {
        c.set_flag(av2_model::record::F_EXPANDED, p.expanded);
        c.eval_cp = p.eval.unwrap_or(av2_model::record::NO_CP);
        c.backed_eval = p.backed.unwrap_or(av2_model::record::NO_CP);
        c.backed_move = Move::parse_opt(m(p.bmove)).unwrap();
        c.backed_plies = p.plies;
        if let Some((s, cl)) = p.status {
            c.set_status(s, cl, Proof::None);
        }
    });
    if p.nodes != 0 || p.bnodes != 0 {
        f.set_ext(id, |e| {
            e.nodes_invested = p.nodes;
            e.backed_nodes = p.bnodes;
        });
    }
    id
}

fn edge(f: &mut Fixture, parent: PosId, child: PosId, uci: &str) {
    f.edge(parent, m(uci), child);
}

fn leaf(f: &mut Fixture, name: &str, stm: char, value: i16) -> PosId {
    pos(f, name, stm, P { eval: Some(value), nodes: ENGINE, ..P::default() })
}

fn state(f: &Fixture, ids: &[PosId]) -> Vec<(Option<i32>, String)> {
    ids.iter()
        .map(|&id| {
            let c = f.core(id);
            (c.backed().map(i32::from), c.backed_move.to_uci())
        })
        .collect()
}

fn values(f: &Fixture, ids: &[PosId]) -> Vec<Option<i32>> {
    state(f, ids).into_iter().map(|(v, _)| v).collect()
}

fn guarded(rec: &Recorder) -> bool {
    rec.events.iter().any(|e| matches!(e, Event::BackedGuard { .. }))
}

fn ascent_plies(f: &mut Fixture, seeds: &[PosId], max_plies: u64) -> Recorder {
    let mut tasks = MemTasks::default();
    let mut loops = V1Loops::default();
    let opts = AscentOpts { max_plies: Some(max_plies), ..AscentOpts::default() };
    f.ascent_loops_with(&RulesConfig::default(), seeds, opts, &mut tasks, &mut loops).1
}

fn all(f: &Fixture) -> Vec<PosId> {
    (0..f.g.len()).map(PosId).collect()
}

fn task_budget(tasks: &MemTasks, child: PosId) -> Option<u64> {
    tasks.tasks.iter().find(|((p, _), _)| *p == child).map(|(_, (b, _))| *b)
}

fn disambiguated(rec: &Recorder) -> bool {
    rec.events.iter().any(|e| matches!(e, Event::CycleDisambiguation { .. }))
}

// ------------------------------------------------------------ test_backprop_repetition.py

/// `ReturningLineTests._loop`: P2(w) -f5f6-> A(b) -g6g5-> B(w) -f6f5-> C(b) -g5g6-> P2.
fn returning_loop(f: &mut Fixture, name: &str, leave_p2: i16, leave_b: i16) -> [PosId; 4] {
    let loop_value = 1246;
    let link = |f: &mut Fixture, tag: &str, stm: char, mv: &'static str| {
        pos(
            f,
            &format!("{name}-{tag}"),
            stm,
            P { expanded: true, backed: Some(loop_value), bmove: mv, plies: 4, bnodes: ENGINE, ..P::default() },
        )
    };
    let p2 = link(f, "P2", 'w', "f5f6");
    let a = link(f, "A", 'b', "g6g5");
    let b = link(f, "B", 'w', "f6f5");
    let c = link(f, "C", 'b', "g5g6");
    edge(f, p2, a, "f5f6");
    edge(f, a, b, "g6g5");
    edge(f, b, c, "f6f5");
    edge(f, c, p2, "g5g6");
    let l = leaf(f, &format!("{name}-leaveP2"), 'b', leave_p2);
    edge(f, p2, l, "f5g5");
    let l = leaf(f, &format!("{name}-leaveB"), 'b', leave_b);
    edge(f, b, l, "g2g3");
    let l = leaf(f, &format!("{name}-A-other"), 'w', 1235);
    edge(f, a, l, "g6h5");
    let l = leaf(f, &format!("{name}-C-other"), 'w', 1300);
    edge(f, c, l, "g5h6");
    [p2, a, b, c]
}

/// test_backprop_repetition.py::ReturningLineTests::test_the_winning_side_keeps_the_move_that_can_leave_the_loop
#[test]
fn the_winning_side_keeps_the_move_that_can_leave_the_loop() {
    let mut f = Fixture::new();
    let [_, _, b, _] = returning_loop(&mut f, "WIN", 1163, 916);
    ascent_plies(&mut f, &[b], 1);
    assert_eq!(state(&f, &[b]), vec![(Some(1163), "f6f5".into())]);
}

/// test_backprop_repetition.py::ReturningLineTests::test_the_losing_side_is_not_handed_a_draw_the_other_can_refuse
#[test]
fn the_losing_side_is_not_handed_a_draw_the_other_can_refuse() {
    let mut f = Fixture::new();
    let [_, _, _, c] = returning_loop(&mut f, "LOSE", 1163, 916);
    ascent_plies(&mut f, &[c], 1);
    assert_eq!(state(&f, &[c]), vec![(Some(1163), "g5g6".into())]);
}

/// test_backprop_repetition.py::ReturningLineTests::test_a_loop_nobody_can_leave_is_still_a_draw
/// (the backup half; `repetition_moves` is the explorer's, VIEW-3.1, phase 5).
#[test]
fn a_returning_loop_nobody_can_leave_is_still_a_draw() {
    let mut f = Fixture::new();
    let [_, _, b, _] = returning_loop(&mut f, "DRAW", -200, -300);
    ascent_plies(&mut f, &[b], 1);
    let s = f.stored(b);
    assert_eq!((s.value, s.mv.to_uci().as_str(), s.quality), (Some(0), "f6f5", 0));
}

/// test_backprop_repetition.py::ReturningLineTests::test_one_touch_settles_the_whole_loop
#[test]
fn one_touch_settles_the_whole_loop() {
    for (name, pick) in [("LAP-B", 2usize), ("LAP-C", 3), ("LAP-P2", 0)] {
        let mut f = Fixture::new();
        let nodes = returning_loop(&mut f, name, 1163, 916);
        f.ascent_loops(&[nodes[pick]]);
        assert_eq!(values(&f, &nodes), vec![Some(1163); 4], "{name}");
        assert_eq!(state(&f, &[nodes[0]]), vec![(Some(1163), "f5g5".into())], "{name}");
    }
}

/// `V1Loops` with `BACKED_CYCLE_MAX_NODES = 0`: the top-level valuation gives
/// each returning child a re-read budget of 0 (v1 patches the constant).
struct NoRereads(V1Loops);

impl LoopRules for NoRereads {
    fn begin_ascent(&mut self) {
        self.0.begin_ascent()
    }
    fn wrote(&mut self, id: PosId, mv: Move) {
        self.0.wrote(id, mv)
    }
    fn mark_returning(&mut self, g: &dyn GraphRead, children: &mut LevelChildren) -> (Vec<PosId>, Vec<(PosId, Move)>) {
        self.0.mark_returning(g, children)
    }
    fn value_returning(&mut self, g: &dyn GraphRead, children: &mut LevelChildren, open_rows: &[PosId]) {
        let costs = self.0.costs;
        let mut ctx = LineCtx { g, cache: &mut self.0.cache, clock: Meter::new(costs.cycle_budget), costs, verdicts: None };
        let mut zero = 0i64;
        resolve_returning_children(&mut ctx, children, &[], Some(&mut zero), Some(open_rows), None);
    }
    fn open_pointer_loops(
        &mut self,
        g: &dyn GraphRead,
        rows: &[Row],
        children: &mut LevelChildren,
        decided: &mut BTreeMap<PosId, Backed>,
        showcases: &BTreeMap<PosId, Showcase>,
        cycle_picks: &mut Vec<av2_rules::backed::Discrepancy>,
    ) -> LoopVerdict {
        self.0.open_pointer_loops(g, rows, children, decided, showcases, cycle_picks)
    }
}

/// test_backprop_repetition.py::ReturningLineTests::test_out_of_budget_the_walk_keeps_its_old_verdict
/// (`BACKED_CYCLE_MAX_NODES = 0`, as v1 patches it).
#[test]
fn out_of_budget_the_walk_keeps_its_old_verdict() {
    let mut f = Fixture::new();
    let [_, _, b, _] = returning_loop(&mut f, "BUDGET", 1163, 916);
    let mut loops = NoRereads(V1Loops::default());
    let opts = AscentOpts { max_plies: Some(1), ..AscentOpts::default() };
    f.ascent_loops_with(&RulesConfig::default(), &[b], opts, &mut MemTasks::default(), &mut loops);
    assert_eq!(state(&f, &[b]), vec![(Some(916), "g2g3".into())]);
}

const HEAVY: u64 = 2_640_000_000;

/// `NestedLoopTests._nest`.
fn nest(f: &mut Fixture, name: &str, deep_exit: usize) -> [PosId; 6] {
    let node = |f: &mut Fixture, tag: &str, stm: char, value: i16, mv: &'static str, eval: i16, nodes: u64| {
        pos(
            f,
            &format!("{name}-{tag}"),
            stm,
            P { expanded: true, backed: Some(value), bmove: mv, plies: 3, bnodes: ENGINE, eval: Some(eval), nodes, ..P::default() },
        )
    };
    let p2 = node(f, "P2", 'w', 1163, "f5g5", 1265, HEAVY);
    let q = node(f, "Q", 'w', 1163, "f5h5", 1092, ENGINE);
    let c = node(f, "C", 'b', 1163, "g5g6", 1079, ENGINE);
    let b = node(f, "B", 'w', 1163, "f6f5", 1083, ENGINE);
    let a = node(f, "A", 'b', 916, "g6g5", 962, ENGINE);
    let e = node(f, "E", 'b', 916, "h6g5", 1185, ENGINE);
    edge(f, p2, a, "f5f6");
    edge(f, a, b, "g6g5");
    edge(f, b, c, "f6f5");
    edge(f, c, p2, "g5g6");
    edge(f, c, q, "g5h6");
    edge(f, q, e, "f5f6");
    edge(f, e, b, "h6g5");
    let l = leaf(f, &format!("{name}-leaveP2"), 'b', 1163);
    edge(f, p2, l, "f5g5");
    let (mut upper, mut colour) = (q, 'w');
    for ply in 0..deep_exit {
        colour = if colour == 'w' { 'b' } else { 'w' };
        let lower = pos(f, &format!("{name}-deep{ply}"), colour, P { expanded: true, ..P::default() });
        edge(f, upper, lower, if upper == q { "f5h5" } else { "a1a1" });
        upper = lower;
    }
    let l = leaf(f, &format!("{name}-leaveQ"), 'b', 1163);
    edge(f, upper, l, if upper == q { "f5h5" } else { "a1a1" });
    let l = leaf(f, &format!("{name}-leaveB"), 'b', 916);
    edge(f, b, l, "g2g3");
    let l = leaf(f, &format!("{name}-A-other"), 'w', 1235);
    edge(f, a, l, "g6h5");
    let l = leaf(f, &format!("{name}-E-other"), 'w', 1300);
    edge(f, e, l, "h6h7");
    [p2, q, c, b, a, e]
}

/// test_backprop_repetition.py::NestedLoopTests::test_an_ascent_through_a_nest_of_loops_comes_to_rest
#[test]
fn an_ascent_through_a_nest_of_loops_comes_to_rest() {
    let mut f = Fixture::new();
    let nodes = nest(&mut f, "NEST", 0);
    let (_, rec, _) = f.ascent_loops(&[nodes[4], nodes[5]]);
    assert!(!guarded(&rec));
    assert_eq!(values(&f, &nodes), vec![Some(1163); 6]);
    assert_eq!(state(&f, &[nodes[3]]), vec![(Some(1163), "f6f5".into())]);
    let (_, rec, _) = f.ascent_loops(&nodes);
    assert!(!guarded(&rec));
    for _ in 0..3 {
        let (r, rec, _) = f.ascent_loops(&nodes);
        assert_eq!(r.changed, 0);
        assert!(!guarded(&rec));
    }
    assert_eq!(values(&f, &nodes), vec![Some(1163); 6]);
}

/// test_backprop_repetition.py::NestedLoopTests::test_the_rest_does_not_depend_on_how_ties_are_broken
#[test]
fn the_rest_does_not_depend_on_how_ties_are_broken() {
    let mut f = Fixture::new();
    let nodes = nest(&mut f, "DEEP", 8);
    let everybody = all(&f);
    let (_, r1, _) = f.ascent_loops(&everybody);
    let (_, r2, _) = f.ascent_loops(&everybody);
    assert!(!guarded(&r1) && !guarded(&r2));
    for _ in 0..3 {
        let (r, rec, _) = f.ascent_loops(&everybody);
        assert_eq!(r.changed, 0);
        assert!(!guarded(&rec));
    }
    assert_eq!(values(&f, &nodes), vec![Some(1163); 6]);
}

/// test_backprop_repetition.py::MoverDrawTests::test_the_side_that_wants_the_draw_makes_the_other_leave_the_loop
#[test]
fn the_side_that_wants_the_draw_makes_the_other_leave_the_loop() {
    let mut f = Fixture::new();
    let name = "OPAB";
    let link = |f: &mut Fixture, tag: &str, stm: char, mv: &'static str| {
        pos(f, &format!("{name}-{tag}"), stm, P { expanded: true, backed: Some(86), bmove: mv, plies: 2, bnodes: ENGINE, ..P::default() })
    };
    let x0 = link(&mut f, "X0", 'w', "a3b4");
    let x1 = link(&mut f, "X1", 'b', "b6d8");
    let x2 = link(&mut f, "X2", 'w', "b4a3");
    let x3 = pos(&mut f, "OPAB-X3", 'b', P { eval: Some(86), nodes: ENGINE, backed: Some(86), bnodes: ENGINE, ..P::default() });
    edge(&mut f, x0, x1, "a3b4");
    edge(&mut f, x1, x2, "b6d8");
    edge(&mut f, x2, x3, "b4a3");
    edge(&mut f, x3, x0, "d8b6");
    let o = pos(&mut f, "OPAB-a6b5", 'w', P { eval: Some(86), ..P::default() });
    edge(&mut f, x3, o, "a6b5");
    let l = leaf(&mut f, "OPAB-f1g2", 'b', 68);
    edge(&mut f, x0, l, "f1g2");
    let l = leaf(&mut f, "OPAB-b5b6", 'b', 22);
    edge(&mut f, x2, l, "b5b6");
    let nodes = [x0, x1, x2, x3];

    f.ascent_loops(&[x3]);

    assert_eq!(values(&f, &nodes), vec![Some(68); 4]);
    assert_eq!(state(&f, &[x0]), vec![(Some(68), "f1g2".into())]);
    assert_eq!(state(&f, &[x3]), vec![(Some(68), "d8b6".into())]);
    assert_eq!(f.ascent_loops(&nodes).0.changed, 0);
}

/// `OpenRingOrderTests._ring`: A(w) -e6c5-> C(b) -x1x1-> D(w) -y1y1-> A.
fn open_ring(f: &mut Fixture, name: &str) -> [PosId; 3] {
    let a = pos(f, &format!("{name}-A"), 'w', P::default());
    let c = pos(
        f,
        &format!("{name}-C"),
        'b',
        P { eval: Some(898), nodes: ENGINE, backed: Some(903), bmove: "x1x1", plies: 2, bnodes: ENGINE, ..P::default() },
    );
    let d = pos(f, &format!("{name}-D"), 'w', P { backed: Some(903), bmove: "y1y1", plies: 1, ..P::default() });
    edge(f, a, c, "e6c5");
    edge(f, c, d, "x1x1");
    edge(f, d, a, "y1y1");
    let l = pos(f, &format!("{name}-L"), 'b', P { status: Some((Status::BlackWin, Closure::Minimax)), ..P::default() });
    edge(f, a, l, "a2a3");
    [a, c, d]
}

/// test_backprop_repetition.py::OpenRingOrderTests::test_entering_from_the_top_or_from_inside_publishes_the_same
#[test]
fn entering_from_the_top_or_from_inside_publishes_the_same() {
    let mut f = Fixture::new();
    let top = open_ring(&mut f, "TOP");
    let inside = open_ring(&mut f, "INSIDE");
    f.ascent_loops(&[top[0]]);
    f.ascent_loops(&[inside[1]]);
    let published = |f: &Fixture, ids: &[PosId]| ids.iter().map(|&i| best_known_eval(&f.core(i))).collect::<Vec<_>>();
    assert_eq!(published(&f, &inside), vec![Some(898); 3]);
    assert_eq!(published(&f, &top), vec![Some(898); 3]);
    assert_eq!(f.core(top[0]).backed_move, mv("e6c5"));
    assert_eq!(f.core(inside[0]).backed_move, mv("e6c5"));
}

// ------------------------------------------------------------ test_repetition_progress.py

const LOOP: [&str; 4] = ["d6f6", "f8d8", "f6d6", "d8f8"];
const OWN: [(i16, u64); 4] = [(1118, ENGINE), (1105, ENGINE), (1124, ENGINE), (1188, 6 * ENGINE)];

/// `_loop(white_out, black_out, stored)`: the four positions of
/// `909727fb`'s loop (White, Black, White, Black) with one way out each.
fn reported_loop(f: &mut Fixture, tag: &str, white_out: i16, black_out: (i16, i16), stored: i16) -> [PosId; 4] {
    let stm = ['w', 'b', 'w', 'b'];
    let ring: Vec<PosId> = (0..4)
        .map(|i| {
            pos(
                f,
                &format!("{tag}-R{i}"),
                stm[i],
                P {
                    expanded: true,
                    eval: Some(OWN[i].0),
                    nodes: OWN[i].1,
                    backed: Some(stored),
                    bmove: LOOP[i],
                    plies: 4,
                    bnodes: ENGINE,
                    ..P::default()
                },
            )
        })
        .collect();
    for i in 0..4 {
        edge(f, ring[i], ring[(i + 1) % 4], LOOP[i]);
    }
    let ways_out =
        [("d6h6", white_out, SHALLOW), ("f8e8", black_out.0, ENGINE), ("f6f7", white_out, SHALLOW), ("d8e8", black_out.1, ENGINE)];
    for (i, (uci, value, nodes)) in ways_out.into_iter().enumerate() {
        let opp = if stm[i] == 'w' { 'b' } else { 'w' };
        let l = pos(f, &format!("{tag}-out{i}"), opp, P { eval: Some(value), nodes, ..P::default() });
        edge(f, ring[i], l, uci);
    }
    [ring[0], ring[1], ring[2], ring[3]]
}

/// `_line(start)`: the positions the published line goes through.
fn line(f: &Fixture, start: PosId, plies: usize) -> Vec<PosId> {
    let mut keys = vec![start];
    let mut node = start;
    for _ in 0..plies {
        let bm = f.core(node).backed_move;
        if bm.is_none() {
            break;
        }
        let Some(e) = f.children(node).into_iter().find(|e| e.mv == bm) else { break };
        node = e.child;
        keys.push(node);
    }
    keys
}

fn no_repeat(keys: &[PosId]) -> bool {
    let mut s = keys.to_vec();
    s.sort();
    s.dedup();
    s.len() == keys.len()
}

/// test_repetition_progress.py::ReportedLoopTests::test_the_loop_opens_at_the_way_out
#[test]
fn the_loop_opens_at_the_way_out() {
    let mut f = Fixture::new();
    let ring = reported_loop(&mut f, "R", 1199, (1365, 1444), 1200);
    f.ascent_loops(&[ring[0]]);
    assert_eq!(values(&f, &ring), vec![Some(1199); 4]);
    let p = state(&f, &ring);
    assert_eq!(p[0], (Some(1199), "d6h6".into()));
    assert_eq!((p[1].clone(), p[3].clone()), ((Some(1199), "f8d8".into()), (Some(1199), "d8f8".into())));
}

/// test_repetition_progress.py::ReportedLoopTests::test_the_published_line_never_repeats
#[test]
fn the_published_line_never_repeats() {
    let mut f = Fixture::new();
    let ring = reported_loop(&mut f, "R", 1199, (1365, 1444), 1200);
    f.ascent_loops(&[ring[0]]);
    for &n in &ring {
        let keys = line(&f, n, 12);
        assert!(no_repeat(&keys), "{keys:?}");
        assert!(!ring.contains(keys.last().unwrap()));
    }
}

/// test_repetition_progress.py::ReportedLoopTests::test_wherever_the_ascent_starts_the_loop_comes_to_rest_open
#[test]
fn wherever_the_ascent_starts_the_loop_comes_to_rest_open() {
    for start in 0..4 {
        let mut f = Fixture::new();
        let ring = reported_loop(&mut f, "R", 1199, (1365, 1444), 1200);
        f.ascent_loops(&[ring[start]]);
        f.ascent_loops(&ring);
        assert_eq!(values(&f, &ring), vec![Some(1199); 4], "{start}");
        for &n in &ring {
            let keys = line(&f, n, 12);
            assert!(no_repeat(&keys), "{start} {keys:?}");
        }
        assert_eq!(f.ascent_loops(&ring).0.changed, 0, "{start}");
    }
}

const MATE_LOOP: [&str; 4] = ["b8c8", "c7b7", "c8b8", "b7c7"];
const MATE_OUT: [&str; 4] = ["b8d8", "c7d7", "c8c7", "b7c6"];

/// test_repetition_progress.py::LoopRecomputedAtOnceTests::test_the_loop_opens_at_one_position_and_rests
#[test]
fn the_loop_opens_at_one_position_and_rests() {
    let mut f = Fixture::new();
    let stm = ['w', 'b', 'w', 'b'];
    let ring: Vec<PosId> = (0..4)
        .map(|i| {
            pos(
                &mut f,
                &format!("M-R{i}"),
                stm[i],
                P {
                    expanded: true,
                    eval: Some(9500),
                    nodes: ENGINE,
                    backed: Some(9500),
                    bmove: MATE_LOOP[i],
                    plies: 4,
                    bnodes: ENGINE,
                    ..P::default()
                },
            )
        })
        .collect();
    for i in 0..4 {
        edge(&mut f, ring[i], ring[(i + 1) % 4], MATE_LOOP[i]);
        let opp = if stm[i] == 'w' { 'b' } else { 'w' };
        let l = pos(&mut f, &format!("M-out{i}"), opp, P { eval: Some(9500), ..P::default() });
        edge(&mut f, ring[i], l, MATE_OUT[i]);
    }
    for _ in 0..3 {
        f.ascent_loops(&ring);
    }
    assert_eq!(f.ascent_loops(&ring).0.changed, 0);
    let published = state(&f, &ring);
    assert_eq!(published.iter().map(|p| p.0).collect::<Vec<_>>(), vec![Some(9500); 4]);
    assert_eq!(published.iter().zip(MATE_LOOP).filter(|(p, l)| p.1 == *l).count(), 3);
    for &n in &ring {
        let keys = line(&f, n, 12);
        assert!(no_repeat(&keys), "{keys:?}");
    }
}

/// test_repetition_progress.py::DrawStillDrawsTests::test_a_loop_nobody_can_leave_is_still_a_draw
/// (the backup half; `repetition_moves` is VIEW-3.1, phase 5).
#[test]
fn a_reported_loop_nobody_can_leave_is_still_a_draw() {
    let mut f = Fixture::new();
    let ring = reported_loop(&mut f, "R", -150, (1365, 1444), 1200);
    f.ascent_loops(&[ring[0]]);
    assert_eq!(values(&f, &ring), vec![Some(0); 4]);
    assert_eq!(state(&f, &ring)[0], (Some(0), "d6f6".into()));
}

fn tie_child(name: &str, value: i32, quality: u64, returns: bool) -> ChildValue {
    let mut c = ChildValue::uninformed(mv(name), PosId(0));
    c.value = Some(value);
    c.quality = quality;
    c.plies = 1;
    c.returns = returns;
    c
}

/// test_repetition_progress.py::TieBreakTests::test_a_returning_move_loses_a_tie_whatever_its_weight
#[test]
fn a_returning_move_loses_a_tie_whatever_its_weight() {
    for stm_white in [true, false] {
        let lp = tie_child("b1b2", 1199, 6 * ENGINE, true);
        let out = tie_child("c1c2", 1199, SHALLOW, false);
        assert_eq!(best_child(&[lp, out], &[0, 1], stm_white, Move::NONE), 1);
        assert_eq!(best_child(&[out, lp], &[0, 1], stm_white, Move::NONE), 0);
    }
}

/// test_repetition_progress.py::TieBreakTests::test_a_returning_move_still_wins_when_it_is_strictly_better
#[test]
fn a_returning_move_still_wins_when_it_is_strictly_better() {
    let lp = tie_child("b1b2", 1163, ENGINE, true);
    let out = tie_child("c1c2", 916, 6 * ENGINE, false);
    assert_eq!(best_child(&[lp, out], &[0, 1], true, Move::NONE), 0);
    let draw = tie_child("a1a2", 0, 0, true);
    let loss = tie_child("c1c2", 1444, ENGINE, false);
    assert_eq!(best_child(&[loss, draw], &[0, 1], false, Move::NONE), 1);
}

/// test_repetition_progress.py::TieBreakTests::test_the_negamax_stands_on_the_way_out
#[test]
fn the_negamax_stands_on_the_way_out() {
    let mut core = Core::default();
    core.set_flag(av2_model::record::F_EXPANDED, true);
    core.eval_cp = 1118;
    core.best_move = mv("d6h6");
    let row = Row { id: PosId(0), core, nodes_invested: ENGINE };
    let mut kids = [tie_child("d6f6", 1199, ENGINE, true), tie_child("d6h6", 1199, SHALLOW, false)];
    let b = backed_for(&row, &mut kids, None, &Showcase::default(), None);
    assert_eq!((b.value, b.mv, b.quality), (Some(1199), mv("d6h6"), SHALLOW));
}

// ------------------------------------------------------------ test_cycle_disambiguation.py

/// `_wolfram_shape`: A(w) -e6c5-> C(b) -x1x1-> D(w) -y1y1-> A, A -a2a3-> L (lost).
fn wolfram_shape(f: &mut Fixture, name: &str) -> (PosId, PosId) {
    let a = pos(f, &format!("{name}-A"), 'w', P { eval: Some(827), nodes: ENGINE, expanded: true, ..P::default() });
    let c = pos(
        f,
        &format!("{name}-C"),
        'b',
        P { eval: Some(898), nodes: ENGINE, backed: Some(903), bmove: "x1x1", plies: 2, bnodes: ENGINE, expanded: true, ..P::default() },
    );
    let d = pos(f, &format!("{name}-D"), 'w', P { backed: Some(903), bmove: "y1y1", plies: 1, expanded: true, ..P::default() });
    let lost = pos(f, &format!("{name}-L"), 'b', P { status: Some((Status::BlackWin, Closure::Minimax)), ..P::default() });
    edge(f, a, c, "e6c5");
    edge(f, c, d, "x1x1");
    edge(f, d, a, "y1y1");
    edge(f, a, lost, "a2a3");
    (a, c)
}

/// test_cycle_disambiguation.py::CycleDisambiguationTests::test_a_chosen_cycle_zero_buys_the_child_a_deeper_pass
#[test]
fn a_chosen_cycle_zero_buys_the_child_a_deeper_pass() {
    let mut f = Fixture::new();
    let (a, c) = wolfram_shape(&mut f, "CD");
    let (_, rec, tasks) = f.ascent_loops(&[a]);
    let s = f.stored(a);
    assert_eq!(s.value, Some(0));
    assert_eq!(s.mv, mv("e6c5"));
    assert!(task_budget(&tasks, c).unwrap() > ENGINE);
    assert!(tasks.log.iter().any(|p| p.child == c && p.kind == PurchaseKind::CycleDisambiguation));
    assert!(disambiguated(&rec));
}

/// test_cycle_disambiguation.py::CycleDisambiguationTests::test_a_stale_pin_retries_the_purchase_on_every_pass
#[test]
fn a_stale_pin_retries_the_purchase_on_every_pass() {
    let mut f = Fixture::new();
    let (a, c) = wolfram_shape(&mut f, "SP");
    f.ascent_loops(&[a]);
    // A drained queue: a fresh task model.
    let (r, _, tasks) = f.ascent_loops(&[a]);
    assert_eq!(r.changed, 0);
    assert!(task_budget(&tasks, c).is_some());
}

/// test_cycle_disambiguation.py::CycleDisambiguationTests::test_an_unchosen_cycle_buys_nothing
#[test]
fn an_unchosen_cycle_buys_nothing() {
    let mut f = Fixture::new();
    let (a, c) = wolfram_shape(&mut f, "NC");
    let w = pos(&mut f, "NC-W", 'b', P { eval: Some(500), nodes: ENGINE, ..P::default() });
    edge(&mut f, a, w, "d2d4");
    let (_, _, tasks) = f.ascent_loops(&[a]);
    let s = f.stored(a);
    assert_eq!((s.value, s.mv), (Some(500), mv("d2d4")));
    assert!(task_budget(&tasks, c).is_none());
}

// ------------------------------------------------------------ test_backed_eval.py::BackedRepetitionTests

/// test_backed_eval.py::BackedRepetitionTests::test_a_two_move_loop_cannot_justify_its_own_value
#[test]
fn a_two_move_loop_cannot_justify_its_own_value() {
    let mut f = Fixture::new();
    let a = pos(&mut f, "LOOP-A", 'w', P { expanded: true, backed: Some(900), bmove: "g1f3", plies: 4, bnodes: 1_000, ..P::default() });
    let b = pos(&mut f, "LOOP-B", 'b', P { expanded: true, backed: Some(900), bmove: "g8f6", plies: 3, bnodes: 1_000, ..P::default() });
    edge(&mut f, a, b, "g1f3");
    edge(&mut f, b, a, "g8f6");
    f.ascent_loops(&[a]);
    assert_eq!(f.stored(a).value, Some(0));
    assert_eq!(f.stored(a).mv, mv("g1f3"));
    assert_eq!(f.stored(b).value, Some(0));
    assert_eq!(f.stored(a).plies, 1);
    assert_eq!(f.ascent_loops(&[a]).0.changed, 0);
}

/// test_backed_eval.py::BackedRepetitionTests::test_a_real_alternative_beats_the_loop_that_outranked_it
#[test]
fn a_real_alternative_beats_the_loop_that_outranked_it() {
    let mut f = Fixture::new();
    let x = pos(&mut f, "ALT-X", 'w', P { expanded: true, ..P::default() });
    let lp = pos(&mut f, "ALT-LOOP", 'b', P { expanded: true, backed: Some(930), bmove: "e8d7", plies: 3, bnodes: 5_000, ..P::default() });
    let real = pos(&mut f, "ALT-REAL", 'b', P { eval: Some(914), nodes: 9_000, ..P::default() });
    edge(&mut f, x, lp, "d1d2");
    edge(&mut f, x, real, "d7e8");
    edge(&mut f, lp, x, "e8d7");
    f.ascent_loops(&[x]);
    assert_eq!((f.stored(x).value, f.stored(x).mv), (Some(914), mv("d7e8")));
}

/// test_backed_eval.py::BackedRepetitionTests::test_an_outside_refutation_finally_reaches_a_poisoned_loop
#[test]
fn an_outside_refutation_finally_reaches_a_poisoned_loop() {
    let mut f = Fixture::new();
    let x = pos(&mut f, "POISON-X", 'b', P { expanded: true, ..P::default() });
    let lp =
        pos(&mut f, "POISON-LOOP", 'w', P { expanded: true, backed: Some(-400), bmove: "b1c3", plies: 2, bnodes: 8_000, ..P::default() });
    let r = pos(&mut f, "POISON-REF", 'w', P { eval: Some(-200), nodes: ENGINE, ..P::default() });
    edge(&mut f, x, lp, "b8c6");
    edge(&mut f, x, r, "e7e5");
    edge(&mut f, lp, x, "b1c3");
    f.ascent_loops(&[x]);
    assert_eq!((f.stored(x).value, f.stored(x).mv), (Some(-200), mv("e7e5")));
}

/// test_backed_eval.py::BackedRepetitionTests::test_a_transposition_that_does_not_come_back_keeps_its_value
/// (also in `backed_ascent.rs` without loop rules; here with them).
#[test]
fn a_transposition_that_does_not_come_back_keeps_its_value() {
    let mut f = Fixture::new();
    let a = pos(&mut f, "DIA-A", 'w', P { expanded: true, ..P::default() });
    let b = pos(&mut f, "DIA-B", 'b', P { expanded: true, ..P::default() });
    let c = pos(&mut f, "DIA-C", 'b', P { expanded: true, ..P::default() });
    let d = pos(&mut f, "DIA-D", 'w', P { eval: Some(-260), nodes: 1_000, ..P::default() });
    edge(&mut f, a, b, "g1f3");
    edge(&mut f, a, c, "b1c3");
    edge(&mut f, b, d, "b1c3");
    edge(&mut f, c, d, "g1f3");
    f.ascent_loops(&[b, c]);
    for n in [b, c, a] {
        assert_eq!(f.stored(n).value, Some(-260));
    }
    assert!([mv("g1f3"), mv("b1c3")].contains(&f.stored(a).mv));
}

/// test_backed_eval.py::BackedRepetitionTests::test_a_cycle_beyond_the_guard_is_not_claimed
#[test]
fn a_cycle_beyond_the_guard_is_not_claimed() {
    let mut f = Fixture::new();
    let length = BACKED_CYCLE_MAX_PLIES + 4;
    let top = pos(&mut f, "RING-TOP", 'b', P { expanded: true, ..P::default() });
    let chain: Vec<PosId> = (0..length)
        .map(|i| {
            pos(
                &mut f,
                &format!("RING-{i}"),
                if i % 2 == 0 { 'w' } else { 'b' },
                P { eval: Some(i as i16), backed: Some(640), bmove: "a1a1", plies: (length - i) as u16, bnodes: 1_000, ..P::default() },
            )
        })
        .collect();
    edge(&mut f, top, chain[0], "a1a1");
    for w in chain.windows(2) {
        edge(&mut f, w[0], w[1], "a1a1");
    }
    edge(&mut f, *chain.last().unwrap(), top, "a1a1");
    let mut children: LevelChildren = BTreeMap::new();
    children.insert(top, children_of(&f.g, top, &mut Vec::new()));
    draw_cycling_children(&f.g, &mut children, &mut SpineCache::default(), &[], None, None);
    assert_eq!(children[&top].iter().map(|c| c.value).collect::<Vec<_>>(), vec![Some(640)]);
}

// ------------------------------------------------------------ test_backed_eval.py::RepetitionZeroContractTests

/// `_shape(name, claim, ring_expanded, **row)`: A -e6c5-> C -x1x1-> D -y1y1-> A.
fn zero_shape(f: &mut Fixture, name: &str, ring_expanded: bool, row: P) -> (PosId, PosId) {
    let claim = 903;
    let a = pos(f, &format!("{name}-A"), 'w', row);
    let c = pos(
        f,
        &format!("{name}-C"),
        'b',
        P {
            eval: Some(claim - 5),
            nodes: ENGINE,
            backed: Some(claim),
            bmove: "x1x1",
            plies: 2,
            bnodes: ENGINE,
            expanded: ring_expanded,
            ..P::default()
        },
    );
    let d = pos(f, &format!("{name}-D"), 'w', P { backed: Some(claim), bmove: "y1y1", plies: 1, expanded: ring_expanded, ..P::default() });
    edge(f, a, c, "e6c5");
    edge(f, c, d, "x1x1");
    edge(f, d, a, "y1y1");
    (a, c)
}

fn losing_sibling(f: &mut Fixture, parent: PosId, name: &str) {
    let l = pos(f, name, 'b', P { status: Some((Status::BlackWin, Closure::Minimax)), ..P::default() });
    edge(f, parent, l, "a2a3");
}

/// test_backed_eval.py::RepetitionZeroContractTests::test_a_repetition_does_not_decide_a_node_with_open_moves
#[test]
fn a_repetition_does_not_decide_a_node_with_open_moves() {
    let mut f = Fixture::new();
    let (a, c) = zero_shape(&mut f, "GRAY", true, P::default());
    losing_sibling(&mut f, a, "GRAY-L");
    let (_, rec, tasks) = f.ascent_loops(&[a]);
    assert_eq!(f.stored(a).value, None);
    assert!(f.stored(a).mv.is_none());
    assert!(task_budget(&tasks, c).unwrap() > ENGINE);
    assert!(disambiguated(&rec));
}

/// test_backed_eval.py::RepetitionZeroContractTests::test_a_repetition_does_not_push_a_node_off_its_own_measure
#[test]
fn a_repetition_does_not_push_a_node_off_its_own_measure() {
    let mut f = Fixture::new();
    let (a, c) = zero_shape(&mut f, "ANCHOR", true, P { eval: Some(-300), nodes: ENGINE, ..P::default() });
    losing_sibling(&mut f, a, "ANCHOR-L");
    let (_, _, tasks) = f.ascent_loops(&[a]);
    assert_eq!(f.stored(a).value, Some(-300));
    assert!(f.stored(a).mv.is_none());
    assert!(task_budget(&tasks, c).is_some());
}

/// test_backed_eval.py::RepetitionZeroContractTests::test_a_real_alternative_better_than_the_draw_wins_the_backup
#[test]
fn a_real_alternative_better_than_the_draw_wins_the_backup() {
    let mut f = Fixture::new();
    let (a, c) = zero_shape(&mut f, "BETTER", true, P { expanded: true, ..P::default() });
    losing_sibling(&mut f, a, "BETTER-L");
    let r = pos(&mut f, "BETTER-R", 'b', P { eval: Some(250), nodes: ENGINE, ..P::default() });
    edge(&mut f, a, r, "d2d4");
    let (_, _, tasks) = f.ascent_loops(&[a]);
    assert_eq!((f.stored(a).value, f.stored(a).mv), (Some(250), mv("d2d4")));
    assert!(task_budget(&tasks, c).is_none());
}

/// test_backed_eval.py::RepetitionZeroContractTests::test_with_every_answer_on_the_table_the_draw_is_the_verdict
#[test]
fn with_every_answer_on_the_table_the_draw_is_the_verdict() {
    let mut f = Fixture::new();
    let (a, c) = zero_shape(&mut f, "FULL", true, P { expanded: true, ..P::default() });
    losing_sibling(&mut f, a, "FULL-L");
    let (_, _, tasks) = f.ascent_loops(&[a]);
    let s = f.stored(a);
    assert_eq!((s.value, s.mv, s.quality), (Some(0), mv("e6c5"), 0));
    assert!(task_budget(&tasks, c).is_some());
    assert_eq!(f.stored(c).value, Some(0));
}

/// test_backed_eval.py::RepetitionZeroContractTests::test_a_partial_ring_stops_lending_the_number_it_invented
#[test]
fn a_partial_ring_stops_lending_the_number_it_invented() {
    let mut f = Fixture::new();
    let (a, c) = zero_shape(&mut f, "OPEN", false, P::default());
    losing_sibling(&mut f, a, "OPEN-L");
    f.ascent_loops(&[c]);
    assert_eq!(f.stored(c).value, Some(898));
    assert!(f.stored(c).mv.is_none());
    assert_eq!((f.stored(a).value, f.stored(a).mv), (Some(898), mv("e6c5")));
}

/// test_backed_eval.py::RepetitionZeroContractTests::test_proven_draws_win_the_tie_against_a_walked_one
#[test]
fn proven_draws_win_the_tie_against_a_walked_one() {
    let mut f = Fixture::new();
    let (a, _c) = zero_shape(&mut f, "TIE", true, P { expanded: true, ..P::default() });
    let p = pos(&mut f, "TIE-PROVEN", 'b', P { status: Some((Status::Draw, Closure::Terminal)), ..P::default() });
    edge(&mut f, a, p, "b2b3");
    f.ascent_loops(&[a]);
    assert_eq!((f.stored(a).value, f.stored(a).mv), (Some(0), mv("b2b3")));
}

// ------------------------------------------------------------ test_cycle_deadline.py (D1: counted)

/// test_cycle_deadline.py::CycleDeadlineTests::test_a_walk_past_its_deadline_stops_before_any_read
/// (D1: the clock is a `Meter` already past its limit; no unit of the
/// re-read budget is spent and nothing is read, so a position that does not
/// exist is no error).
#[test]
fn a_walk_past_its_deadline_stops_before_any_read() {
    let f = Fixture::new();
    let mut cache = SpineCache::default();
    let mut ctx = LineCtx { g: &f.g, cache: &mut cache, clock: Meter { used: 1, limit: 0 }, costs: LOOP_COSTS, verdicts: None };
    let mut budget = BACKED_CYCLE_MAX_NODES;
    assert_eq!(value_on_line(&mut ctx, PosId(999), &[], &mut budget), None);
    assert_eq!(budget, BACKED_CYCLE_MAX_NODES);
    assert_eq!(ctx.clock.used, 1);
}

/// test_cycle_deadline.py::CycleDeadlineTests::test_a_top_level_call_opens_a_fresh_deadline
/// (D1: each top-level valuation, `V1Loops::value_returning`, starts a fresh
/// `Meter` of `cycle_budget` units: a level after an exhausted one still values).
#[test]
fn a_top_level_call_opens_a_fresh_meter() {
    // Two ascents of the WIN shape with the same V1Loops: the second is not
    // starved by what the first spent.
    let mut f = Fixture::new();
    let [_, _, b, _] = returning_loop(&mut f, "FRESH", 1163, 916);
    let mut loops = V1Loops::default();
    let opts = AscentOpts { max_plies: Some(1), ..AscentOpts::default() };
    f.ascent_loops_with(&RulesConfig::default(), &[b], opts, &mut MemTasks::default(), &mut loops);
    assert_eq!(state(&f, &[b]), vec![(Some(1163), "f6f5".into())]);
    let mut g2 = Fixture::new();
    let [_, _, b2, _] = returning_loop(&mut g2, "FRESH", 1163, 916);
    g2.ascent_loops_with(&RulesConfig::default(), &[b2], opts, &mut MemTasks::default(), &mut loops);
    assert_eq!(state(&g2, &[b2]), vec![(Some(1163), "f6f5".into())]);
}

/// test_cycle_deadline.py::CycleDeadlineTests::test_a_nested_call_keeps_the_running_deadline
/// (D1: a nested call, with a re-read budget, shares the caller's meter and
/// does not reset it).
#[test]
fn a_nested_call_keeps_the_running_meter() {
    let f = Fixture::new();
    let mut cache = SpineCache::default();
    let mut ctx = LineCtx { g: &f.g, cache: &mut cache, clock: Meter { used: 123, limit: 1_000 }, costs: LOOP_COSTS, verdicts: None };
    let mut by_parent: LevelChildren = BTreeMap::new();
    let mut one = 1i64;
    resolve_returning_children(&mut ctx, &mut by_parent, &[], Some(&mut one), None, None);
    assert_eq!((ctx.clock.used, ctx.clock.limit), (123, 1_000));
}

/// test_cycle_deadline.py (the cap's purpose): with no cycle budget at all
/// (`cycle_budget = 0`: v1 with `BACKED_CYCLE_MAX_SECONDS` spent at once), a
/// valuation reads one position and the nested ones stop: the shape where the
/// first re-read alone settles nothing keeps the walk's verdict.
#[test]
fn with_no_cycle_budget_the_walk_keeps_its_verdict() {
    let mut f = Fixture::new();
    let [_, _, b, _] = returning_loop(&mut f, "NOTIME", 1163, 916);
    let costs = av2_rules::repetition::LoopCosts { cycle_budget: 0, ..LOOP_COSTS };
    let mut loops = V1Loops::with_costs(costs);
    let opts = AscentOpts { max_plies: Some(1), ..AscentOpts::default() };
    f.ascent_loops_with(&RulesConfig::default(), &[b], opts, &mut MemTasks::default(), &mut loops);
    // B's returning child C is re-read (the clock is read before the first
    // charge), its own returning child P2 is not: C is valued on what it
    // stores below, and B ends on its way out.
    assert_eq!(state(&f, &[b]), vec![(Some(916), "g2g3".into())]);
}
