//! Steps 13 and 14 of the port (pointer loops, best exit, grounding):
//! v1's portable tests of `test_loop_best_exit.py`,
//! `test_pointer_loop_grounding.py`, `test_backup_echo.py::EchoFamilyTests`
//! and the backup side of `test_explorer_repetition_rows.py`, translated
//! fixture for fixture.
//!
//! v1's fixtures name moves with words (`wb`, `k1`, `N0>N1`...).  They become
//! legal 16-bit moves through one order-preserving table ([`m`]): the names
//! sorted as strings map to moves sorted as UCI strings, so every tie by move
//! name breaks the same way.  Positions are FEN-less named nodes with the
//! side to move of v1's fixture FEN (the backup reads nothing else of it).
//!
//! D1: v1's `BACKED_LOOP_MAX_SECONDS` is a counted budget here
//! (`repetition::LoopCosts::loop_budget`).  v1's "without the rule" (a patched
//! `_loop_at_best_exit` returning None) is the same as a budget of 0: the rule
//! never runs and the loop's other positions are still woken.

use av2_model::record::F_EXPANDED;
use av2_model::{Closure, Edge, GraphRead, GraphWrite, Move, PosId, Proof, Status};
use av2_rules::ascent::{AscentOpts, rows_that_wait};
use av2_rules::backed::{Backed, ChildValue, Row, best_child};
use av2_rules::config::RulesConfig;
use av2_rules::conv::PROVEN_QUALITY;
use av2_rules::effects::Event;
use av2_rules::loops::{LevelChildren, LoopRules, V1Loops};
use av2_rules::pointer_loops::loop_at_best_exit;
use av2_rules::purchase::MemTasks;
use av2_rules::repetition::{LOOP_COSTS, LineCtx, LoopCosts, Meter, SpineCache, draw_cycling_children, resolve_returning_children};
use av2_rules::testkit::{Fixture, mv as uci, play};
use std::collections::{BTreeMap, HashMap, HashSet};
use std::sync::OnceLock;

const ENGINE: u64 = 128_000_000;
const HEAVY: u64 = 768_000_000;
const STALE: i32 = 1208;
const FAR: u16 = 120;

// ---------------------------------------------------------------- move names

const NAMES: &[&str] = &[
    // test_loop_best_exit.py
    "wb", "bw", "wx", "bx", "a1", "a2", "a3", "x1", "x2", "x3", "w1w2", "w2b", "bw1", "o0", "o1", "o2", "o3", "o4", "m0", "m1", "m2", "m3",
    "d1", "d2", "ab", "ba", "ac", "cd", "da", "ax", "cx", "dx", "w1b2", "w2b1", "xy", "yb", "ys", "dw", "k1", "k2", "k3", "k4", "t0", "t1",
    "t2", "t3", "r1", "r2", "l1", "l2", "q1", "q2", "bwin", "uw", "e1e2", "bc", "cl", "ef", "f1", "f2", "N0>N1", "N1>N2", "N1>N1L0",
    "N1>N1L1", "N2>N3", "N3>N0", "N3>N3L0", // test_pointer_loop_grounding.py
    "l1a1", "l2a2", "l3a3", "l4a4", "e1e1", "e2e2", "x1x1", "x2x2", "y1y1", "y2y2", "d1d1", "d2d2", "s1s1", "s2s2", "back", "loop",
    "source",
    // test_backup_echo.py (r0..r33, e0..e33 are added below)
];

fn table() -> &'static (HashMap<String, Move>, HashMap<Move, String>) {
    static T: OnceLock<(HashMap<String, Move>, HashMap<Move, String>)> = OnceLock::new();
    T.get_or_init(|| {
        let mut names: Vec<String> = NAMES.iter().map(|s| s.to_string()).collect();
        for i in 0..40 {
            names.push(format!("r{i}"));
            names.push(format!("e{i}"));
        }
        names.sort();
        names.dedup();
        let mut moves: Vec<Move> = Vec::new();
        for from in 0..64u8 {
            for to in 0..64u8 {
                if from != to {
                    moves.push(Move::new(from, to, 0));
                }
            }
        }
        moves.sort_by_key(|m| m.to_uci());
        let fwd: HashMap<String, Move> = names.iter().cloned().zip(moves.iter().copied()).collect();
        let back: HashMap<Move, String> = fwd.iter().map(|(k, v)| (*v, k.clone())).collect();
        (fwd, back)
    })
}

/// The move a v1 fixture names `name`.
fn m(name: &str) -> Move {
    *table().0.get(name).unwrap_or_else(|| panic!("move name {name} not in NAMES"))
}

/// The fixture name of a stored move (`""` for NULL).
fn name_of(mv: Move) -> String {
    if mv.is_none() {
        return String::new();
    }
    table().1.get(&mv).cloned().unwrap_or_else(|| mv.to_uci())
}

// ---------------------------------------------------------------- fixtures

struct T {
    f: Fixture,
    ids: HashMap<String, PosId>,
}

impl T {
    fn new() -> T {
        T { f: Fixture::new(), ids: HashMap::new() }
    }

    fn id(&self, name: &str) -> PosId {
        self.ids[name]
    }

    fn ids(&self, names: &[&str]) -> Vec<PosId> {
        names.iter().map(|n| self.id(n)).collect()
    }

    /// `_pos(name, stm)`.
    fn pos(&mut self, name: &str, stm: char) -> PosId {
        let id = self.f.named(name, stm == 'b');
        self.ids.insert(name.to_string(), id);
        id
    }

    /// `_leaf(name, stm, value, nodes)`.
    fn leaf(&mut self, name: &str, stm: char, value: i32, nodes: u64) -> PosId {
        let id = self.pos(name, stm);
        self.f.set(id, |c| c.eval_cp = value as i16);
        self.f.set_ext(id, |e| e.nodes_invested = nodes);
        id
    }

    /// `_node(name, stm, move, value, plies)`: searched, every reply on the
    /// table, backed as stored.
    fn node(&mut self, name: &str, stm: char, mv: Option<&str>, value: i32, plies: u16) -> PosId {
        let id = self.pos(name, stm);
        self.f.set(id, |c| {
            c.set_flag(F_EXPANDED, true);
            c.eval_cp = value as i16;
            c.backed_eval = value as i16;
            c.backed_move = mv.map(m).unwrap_or(Move::NONE);
            c.backed_plies = plies;
        });
        self.f.set_ext(id, |e| {
            e.nodes_invested = ENGINE;
            e.backed_nodes = HEAVY;
        });
        id
    }

    fn edge_mv(&mut self, parent: PosId, child: PosId, mv: Move) {
        let mut v = Vec::new();
        self.f.g.children(parent, &mut v);
        v.push(Edge { child, mv });
        self.f.g.set_children(parent, &v).unwrap();
    }

    /// `_edge(parent, child, move)` with a fixture move name.
    fn edge(&mut self, parent: PosId, child: PosId, name: &str) {
        self.edge_mv(parent, child, m(name));
    }

    /// `(backed_eval, backed_move, backed_plies, backed_nodes)`.
    fn state(&self, name: &str) -> (Option<i32>, String, i64, u64) {
        let b = self.f.stored(self.id(name));
        (b.value, name_of(b.mv), b.plies, b.quality)
    }

    fn moves(&self, names: &[&str]) -> Vec<(Option<i32>, String, i64)> {
        names
            .iter()
            .map(|n| {
                let s = self.state(n);
                (s.0, s.1, s.2)
            })
            .collect()
    }

    fn values(&self, names: &[&str]) -> Vec<Option<i32>> {
        names.iter().map(|n| self.state(n).0).collect()
    }

    /// `backup_backed_evals(keys, max_plies=...)` with v1's loop rules (or
    /// the given ones); returns the rows written and the events.
    fn ascent_with(&mut self, keys: &[PosId], max_plies: Option<u64>, loops: &mut dyn LoopRules) -> (u64, Vec<Event>) {
        let opts = AscentOpts { max_plies, ..AscentOpts::default() };
        let (r, rec) = self.f.ascent_loops_with(&RulesConfig::default(), keys, opts, &mut MemTasks::default(), loops);
        (r.changed, rec.events)
    }

    fn ascent(&mut self, keys: &[PosId], max_plies: Option<u64>) -> u64 {
        self.ascent_with(keys, max_plies, &mut V1Loops::default()).0
    }

    /// The backup as it was before 21-Oct-2026 (`_without_the_rule`): the
    /// best-exit rule never runs (a loop budget of 0, D1).
    fn ascent_without_the_rule(&mut self, keys: &[PosId], max_plies: Option<u64>) -> u64 {
        self.ascent_with(keys, max_plies, &mut no_rule()).0
    }

    /// `_loops()`: positions whose chain of pointers comes back to them.
    fn loops(&self) -> HashSet<PosId> {
        let g = &self.f.g;
        let mut looping = HashSet::new();
        for start in 0..g.len() {
            let mut path: Vec<PosId> = Vec::new();
            let mut node = Some(PosId(start));
            while let Some(n) = node {
                if path.contains(&n) {
                    break;
                }
                path.push(n);
                let mv = g.core(n).backed_move;
                node = if mv.is_none() { None } else { self.f.children(n).iter().find(|e| e.mv == mv).map(|e| e.child) };
            }
            if let Some(n) = node {
                let at = path.iter().position(|&x| x == n).unwrap();
                looping.extend(path[at..].iter().copied());
            }
        }
        looping
    }

    fn all(&self) -> Vec<PosId> {
        (0..self.f.g.len()).map(PosId).collect()
    }

    /// `_two_cycle(name, white_out, black_out, on_loop)`.
    fn two_cycle(&mut self, name: &str, white_out: i32, black_out: i32, on_loop: bool) -> (PosId, PosId) {
        let w = self.node(&format!("{name}-W"), 'w', Some(if on_loop { "wb" } else { "wx" }), STALE, FAR);
        let b = self.node(&format!("{name}-B"), 'b', Some(if on_loop { "bw" } else { "bx" }), STALE, FAR);
        self.edge(w, b, "wb");
        self.edge(b, w, "bw");
        let wx = self.leaf(&format!("{name}-WX"), 'b', white_out, ENGINE);
        self.edge(w, wx, "wx");
        let bx = self.leaf(&format!("{name}-BX"), 'w', black_out, ENGINE);
        self.edge(b, bx, "bx");
        (w, b)
    }

    /// `_four_cycle(name, value, outs)`.
    fn four_cycle(&mut self, name: &str, value: i32, outs: [i32; 4]) -> Vec<PosId> {
        let tags = [("W1", 'w'), ("B1", 'b'), ("W2", 'w'), ("B2", 'b')];
        let ring: Vec<PosId> = tags
            .iter()
            .enumerate()
            .map(|(i, (tag, stm))| self.node(&format!("{name}-{tag}"), *stm, Some(&format!("k{}", i + 1)), value, FAR))
            .collect();
        for i in 0..4 {
            self.edge(ring[i], ring[(i + 1) % 4], &format!("k{}", i + 1));
            let o = self.leaf(&format!("{name}-O{}", i + 1), if i % 2 == 0 { 'b' } else { 'w' }, outs[i], ENGINE);
            self.edge(ring[i], o, &format!("o{}", i + 1));
        }
        ring
    }
}

fn no_rule() -> V1Loops {
    V1Loops::with_costs(LoopCosts { loop_budget: 0, ..LOOP_COSTS })
}

fn s(v: i32, mv: &str, plies: i64, q: u64) -> (Option<i32>, String, i64, u64) {
    (Some(v), mv.to_string(), plies, q)
}

fn mp(v: i32, mv: &str, plies: i64) -> (Option<i32>, String, i64) {
    (Some(v), mv.to_string(), plies)
}

fn backed(v: i32, mv: &str, plies: i64, q: u64) -> Backed {
    Backed { value: Some(v), mv: m(mv), plies, quality: q }
}

/// `ingest.line_moves(row, line)` (VIEW-3.1), built from the ported pieces
/// (the read-side function itself is phase 5): `{move: (value, repeats)}`.
fn line_moves(f: &Fixture, row: PosId, line: &[PosId]) -> BTreeMap<String, (Option<i32>, bool)> {
    let mut children: LevelChildren = BTreeMap::new();
    let kids = av2_rules::backed::children_of(&f.g, row, &mut Vec::new());
    children.insert(row, kids);
    let mut cache = SpineCache::default();
    draw_cycling_children(&f.g, &mut children, &mut cache, line, None, None);
    let marked: Vec<Move> = children[&row].iter().filter(|c| c.cycles).map(|c| c.mv).collect();
    if marked.is_empty() {
        return BTreeMap::new();
    }
    let mut unresolved = Vec::new();
    let mut ctx = LineCtx { g: &f.g, cache: &mut cache, clock: Meter::new(LOOP_COSTS.cycle_budget), costs: LOOP_COSTS, verdicts: None };
    resolve_returning_children(&mut ctx, &mut children, line, None, None, Some(&mut unresolved));
    let mut out = BTreeMap::new();
    for c in &children[&row] {
        if !marked.contains(&c.mv) {
            continue;
        }
        if c.cycles {
            if unresolved.contains(&(row, c.mv)) {
                continue;
            }
            out.insert(name_of(c.mv), (Some(0), true));
        } else if c.value.is_some() {
            out.insert(name_of(c.mv), (c.value, false));
        }
    }
    out
}

// ================================================================ test_loop_best_exit.py

/// test_loop_best_exit.py::AboveEveryExitTests::test_a_two_cycle_becomes_its_best_exit
#[test]
fn a_two_cycle_becomes_its_best_exit() {
    let mut t = T::new();
    t.two_cycle("TWO", 1100, 1300, false);
    let keys = t.ids(&["TWO-W", "TWO-B"]);
    t.ascent(&keys, Some(1));
    assert_eq!(vec![t.state("TWO-W"), t.state("TWO-B")], vec![s(1100, "wx", 1, ENGINE), s(1100, "bw", 2, ENGINE)]);
    assert!(t.loops().is_empty());
}

/// test_loop_best_exit.py::AboveEveryExitTests::test_before_the_rule_the_level_wrote_the_loop_at_its_own_number
/// (`_without_the_rule` = a loop budget of 0).
#[test]
fn before_the_rule_the_level_wrote_the_loop_at_its_own_number() {
    let mut t = T::new();
    t.two_cycle("OLD", 1100, 1300, false);
    let keys = t.ids(&["OLD-W", "OLD-B"]);
    t.ascent_without_the_rule(&keys, Some(1));
    assert_eq!(t.moves(&["OLD-W", "OLD-B"]), vec![mp(STALE, "wb", FAR as i64 + 1), mp(STALE, "bw", FAR as i64 + 1)]);
    assert_eq!(t.loops(), keys.into_iter().collect());
}

/// test_loop_best_exit.py::AboveEveryExitTests::test_a_three_cycle_takes_the_best_of_its_exits
#[test]
fn a_three_cycle_takes_the_best_of_its_exits() {
    let mut t = T::new();
    let p1 = t.node("THREE-P1", 'w', Some("x1"), STALE, FAR);
    let p2 = t.node("THREE-P2", 'b', Some("x2"), STALE, FAR);
    let p3 = t.node("THREE-P3", 'w', Some("x3"), STALE, FAR);
    t.edge(p1, p2, "a1");
    t.edge(p2, p3, "a2");
    t.edge(p3, p1, "a3");
    let x1 = t.leaf("THREE-X1", 'b', 1050, ENGINE);
    t.edge(p1, x1, "x1");
    let x2 = t.leaf("THREE-X2", 'w', 1400, ENGINE);
    t.edge(p2, x2, "x2");
    let x3 = t.leaf("THREE-X3", 'b', 1150, ENGINE);
    t.edge(p3, x3, "x3");
    let names = ["THREE-P1", "THREE-P2", "THREE-P3"];
    let keys = t.ids(&names);
    t.ascent(&keys, Some(1));
    assert_eq!(t.moves(&names), vec![mp(1150, "a1", 3), mp(1150, "a2", 2), mp(1150, "x3", 1)]);
    assert!(t.loops().is_empty());
}

/// test_loop_best_exit.py::AboveEveryExitTests::test_the_family_rests_at_the_best_exit
#[test]
fn the_family_rests_at_the_best_exit() {
    let mut t = T::new();
    t.two_cycle("REST", 1100, 1300, true);
    let keys = t.ids(&["REST-W", "REST-B"]);
    t.ascent(&keys, None);
    assert_eq!(t.values(&["REST-W", "REST-B"]), vec![Some(1100), Some(1100)]);
    for _ in 0..3 {
        assert_eq!(t.ascent(&keys, None), 0);
    }
}

/// test_loop_best_exit.py::SideToMoveTests::test_the_mirror_case_is_black_leaving
#[test]
fn the_mirror_case_is_black_leaving() {
    let mut t = T::new();
    let (w, b) = t.two_cycle("MIRROR", -1300, -1100, false);
    for id in [w, b] {
        t.f.set(id, |c| {
            c.backed_eval = -STALE as i16;
            c.eval_cp = -STALE as i16;
        });
    }
    t.ascent(&[w, b], Some(1));
    assert_eq!(t.moves(&["MIRROR-W", "MIRROR-B"]), vec![mp(-1100, "wb", 2), mp(-1100, "bx", 1)]);
}

/// test_loop_best_exit.py::SideToMoveTests::test_sides_need_not_alternate_round_the_loop
#[test]
fn sides_need_not_alternate_round_the_loop() {
    let mut t = T::new();
    let w1 = t.node("ODD-W1", 'w', Some("o1"), STALE, FAR);
    let w2 = t.node("ODD-W2", 'w', Some("o2"), STALE, FAR);
    let b = t.node("ODD-B", 'b', Some("o3"), STALE, FAR);
    t.edge(w1, w2, "w1w2");
    t.edge(w2, b, "w2b");
    t.edge(b, w1, "bw1");
    let o1 = t.leaf("ODD-O1", 'w', 1000, ENGINE);
    t.edge(w1, o1, "o1");
    let o2 = t.leaf("ODD-O2", 'b', 1150, ENGINE);
    t.edge(w2, o2, "o2");
    let o3 = t.leaf("ODD-O3", 'w', 1400, ENGINE);
    t.edge(b, o3, "o3");
    t.ascent(&[w1, w2, b], Some(1));
    assert_eq!(t.moves(&["ODD-W1", "ODD-W2", "ODD-B"]), vec![mp(1150, "w1w2", 2), mp(1150, "o2", 1), mp(1150, "bw1", 3)]);
}

/// test_loop_best_exit.py::NoWayOutTests::test_a_loop_nobody_wants_to_leave_is_a_draw
/// (`repetition_moves` through the ported pieces, see [`line_moves`]).
#[test]
fn a_loop_nobody_wants_to_leave_is_a_draw() {
    let mut t = T::new();
    let (w, b) = t.two_cycle("DRAW", -150, 1500, false);
    let (_, events) = t.ascent_with(&[w, b], Some(1), &mut V1Loops::default());
    assert_eq!(vec![t.state("DRAW-W"), t.state("DRAW-B")], vec![s(0, "wb", 1, 0), s(0, "bw", 1, 0)]);
    let rep: BTreeMap<String, Option<i32>> = line_moves(&t.f, w, &[]).into_iter().map(|(k, (v, _))| (k, v)).collect();
    assert_eq!(rep, BTreeMap::from([("wb".to_string(), Some(0))]));
    assert!(events.iter().any(|e| matches!(e, Event::CycleDisambiguation { .. })));
}

/// test_loop_best_exit.py::NoWayOutTests::test_a_loop_with_no_move_out_at_all_is_a_draw
#[test]
fn a_loop_with_no_move_out_at_all_is_a_draw() {
    let mut t = T::new();
    let ring: Vec<PosId> =
        ['w', 'b', 'w', 'b'].iter().enumerate().map(|(n, &stm)| t.node(&format!("SHUT-{n}"), stm, None, STALE, FAR)).collect();
    for i in 0..4 {
        t.edge(ring[i], ring[(i + 1) % 4], &format!("m{i}"));
    }
    let mut cache = SpineCache::default();
    for (i, &k) in ring.iter().enumerate() {
        cache.wrote(k, m(&format!("m{i}")));
    }
    let mut meter = Meter::new(5 * LOOP_COSTS.loop_budget);
    let worth = loop_at_best_exit(&t.f.g, &ring, &BTreeMap::new(), &BTreeMap::new(), &mut cache, &mut meter, &LOOP_COSTS, &mut Vec::new())
        .expect("the rule understands the loop");
    let got: Vec<Backed> = ring.iter().map(|k| worth[k]).collect();
    let want: Vec<Backed> = (0..4).map(|i| backed(0, &format!("m{i}"), 1, 0)).collect();
    assert_eq!(got, want);
}

fn sink(t: &mut T, name: &str) {
    let (w, _b) = t.two_cycle(name, 1100, 1300, false);
    let d1 = t.node(&format!("{name}-D1"), 'b', Some("d1"), STALE, FAR);
    let d2 = t.node(&format!("{name}-D2"), 'w', Some("d2"), STALE, FAR);
    t.edge(d1, w, "d1");
    t.edge(d2, d1, "d2");
}

/// test_loop_best_exit.py::DrainTests::test_the_rule_writes_the_loop_and_nothing_that_drains_into_it
#[test]
fn the_rule_writes_the_loop_and_nothing_that_drains_into_it() {
    let mut t = T::new();
    sink(&mut t, "SINK1");
    let keys = t.ids(&["SINK1-W", "SINK1-B"]);
    t.ascent(&keys, Some(1));
    assert_eq!(t.values(&["SINK1-W", "SINK1-B"]), vec![Some(1100), Some(1100)]);
    assert_eq!(t.values(&["SINK1-D1", "SINK1-D2"]), vec![Some(STALE), Some(STALE)]);
}

/// test_loop_best_exit.py::DrainTests::test_what_drains_into_the_loop_follows_through_the_backup
#[test]
fn what_drains_into_the_loop_follows_through_the_backup() {
    let mut t = T::new();
    sink(&mut t, "SINK2");
    let keys = t.ids(&["SINK2-W", "SINK2-B"]);
    t.ascent(&keys, None);
    assert_eq!(t.moves(&["SINK2-D1", "SINK2-D2"]), vec![mp(1100, "d1", 2), mp(1100, "d2", 3)]);
}

/// test_loop_best_exit.py::OverlappingLoopTests::test_a_loop_sharing_a_position_with_another_is_valued_through_it
#[test]
fn a_loop_sharing_a_position_with_another_is_valued_through_it() {
    let mut t = T::new();
    let a = t.node("EIGHT-A", 'w', Some("ab"), STALE, 4);
    let b = t.node("EIGHT-B", 'b', Some("ba"), STALE, 4);
    let c = t.node("EIGHT-C", 'b', Some("cd"), STALE, 4);
    let d = t.node("EIGHT-D", 'w', Some("da"), STALE, 4);
    t.edge(a, b, "ab");
    t.edge(b, a, "ba");
    t.edge(a, c, "ac");
    t.edge(c, d, "cd");
    t.edge(d, a, "da");
    let ax = t.leaf("EIGHT-AX", 'b', 1050, ENGINE);
    t.edge(a, ax, "ax");
    let bx = t.leaf("EIGHT-BX", 'w', 1400, ENGINE);
    t.edge(b, bx, "bx");
    let cx = t.leaf("EIGHT-CX", 'w', 1300, ENGINE);
    t.edge(c, cx, "cx");
    let dx = t.leaf("EIGHT-DX", 'b', 1150, ENGINE);
    t.edge(d, dx, "dx");
    let names = ["EIGHT-A", "EIGHT-B", "EIGHT-C", "EIGHT-D"];
    let keys = t.ids(&names);
    for _ in 0..3 {
        t.ascent(&keys, None);
    }
    assert_eq!(t.ascent(&keys, None), 0);
    assert_eq!(t.moves(&names), vec![mp(1150, "ac", 3), mp(1150, "ba", 4), mp(1150, "cd", 2), mp(1150, "dx", 1)]);
    assert!(t.loops().is_empty());
}

/// test_loop_best_exit.py::OverlappingLoopTests::test_two_loops_that_feed_each_other_rest_at_the_best_exit_of_both
#[test]
fn two_loops_that_feed_each_other_rest_at_the_best_exit_of_both() {
    let mut t = T::new();
    let (w1, b1) = t.two_cycle("PAIR1", 1100, 1300, true);
    let (w2, b2) = t.two_cycle("PAIR2", 1150, 1350, true);
    t.edge(w1, b2, "w1b2");
    t.edge(w2, b1, "w2b1");
    let names = ["PAIR1-W", "PAIR1-B", "PAIR2-W", "PAIR2-B"];
    let keys = t.ids(&names);
    t.ascent(&keys, Some(1));
    assert_eq!(t.values(&names), vec![Some(1150); 4]);
    for _ in 0..3 {
        t.ascent(&keys, None);
    }
    assert_eq!(t.ascent(&keys, None), 0);
    assert_eq!(t.values(&names), vec![Some(1150); 4]);
    assert!(t.loops().is_empty());
}

/// test_loop_best_exit.py::OverlappingLoopTests::test_a_way_back_is_no_way_out_and_its_own_exit_comes_through
#[test]
fn a_way_back_is_no_way_out_and_its_own_exit_comes_through() {
    let mut t = T::new();
    let w = t.node("ECHO-W", 'w', Some("wb"), STALE, FAR);
    let b = t.node("ECHO-B", 'b', Some("bw"), STALE, FAR);
    let x = t.node("ECHO-X", 'b', Some("xy"), STALE, FAR);
    let y = t.node("ECHO-Y", 'w', Some("yb"), STALE, FAR);
    t.edge(w, b, "wb");
    t.edge(b, w, "bw");
    t.edge(w, x, "wx");
    t.edge(x, y, "xy");
    t.edge(y, b, "yb");
    let sl = t.leaf("ECHO-S", 'b', 1100, ENGINE);
    t.edge(y, sl, "ys");
    let bx = t.leaf("ECHO-BX", 'w', 1400, ENGINE);
    t.edge(b, bx, "bx");
    let mut cache = SpineCache::default();
    cache.wrote(w, m("wb"));
    cache.wrote(b, m("bw"));
    let mut meter = Meter::new(5 * LOOP_COSTS.loop_budget);
    let worth =
        loop_at_best_exit(&t.f.g, &[w, b], &BTreeMap::new(), &BTreeMap::new(), &mut cache, &mut meter, &LOOP_COSTS, &mut Vec::new())
            .unwrap();
    assert_eq!(worth[&w], backed(0, "wb", 1, 0));
    assert_eq!(worth[&b], backed(0, "bw", 1, 0));
    let names = ["ECHO-W", "ECHO-B", "ECHO-X", "ECHO-Y"];
    let keys = t.ids(&names);
    for _ in 0..3 {
        t.ascent(&keys, None);
    }
    assert_eq!(t.ascent(&keys, None), 0);
    assert_eq!(t.moves(&names), vec![mp(1100, "wx", 3), mp(1100, "bw", 4), mp(1100, "xy", 2), mp(1100, "ys", 1)]);
    assert!(t.loops().is_empty());
}

/// test_loop_best_exit.py::PliesTests::test_the_distance_is_counted_from_the_exit_and_does_not_grow
/// (`recascade_backed --keys-file --until-rest` is a command: here the
/// ascent over the same keys repeated until it writes nothing).
#[test]
fn the_distance_is_counted_from_the_exit_and_does_not_grow() {
    let mut t = T::new();
    let (w, _) = t.two_cycle("FAR", 1100, 1300, true);
    let d = t.node("FAR-D", 'b', Some("dw"), STALE, FAR);
    t.edge(d, w, "dw");
    let names = ["FAR-W", "FAR-B", "FAR-D"];
    let keys = t.ids(&names);
    for _ in 0..3 {
        let mut passes = 0;
        while t.ascent(&keys, None) > 0 {
            passes += 1;
            assert!(passes < 20, "no rest");
        }
        assert_eq!(t.moves(&names), vec![mp(1100, "wx", 1), mp(1100, "bw", 2), mp(1100, "dw", 2)]);
    }
}

/// test_loop_best_exit.py::PliesTests::test_a_loop_kept_at_its_value_counts_from_the_exit
#[test]
fn a_loop_kept_at_its_value_counts_from_the_exit() {
    let mut t = T::new();
    t.four_cycle("KEEP", 1199, [1150, 1400, 1199, 1450]);
    let b1 = t.id("KEEP-B1");
    t.ascent(&[b1], Some(1));
    assert_eq!(t.state("KEEP-B1"), s(1199, "k2", 2, ENGINE));
}

/// test_loop_best_exit.py::UntouchedTests::test_positions_off_a_loop_back_up_as_before
/// (`_the_rule_must_not_run`: the same ascent with a loop budget of 0 must
/// write the same rows).
#[test]
fn positions_off_a_loop_back_up_as_before() {
    let build = || {
        let mut t = T::new();
        let root = t.pos("DAG-R", 'w');
        t.f.set(root, |c| {
            c.set_flag(F_EXPANDED, true);
            c.eval_cp = 40;
        });
        t.f.set_ext(root, |e| e.nodes_invested = ENGINE);
        let left = t.pos("DAG-L", 'b');
        t.f.set(left, |c| {
            c.set_flag(F_EXPANDED, true);
            c.eval_cp = 35;
        });
        t.f.set_ext(left, |e| e.nodes_invested = ENGINE);
        let right = t.leaf("DAG-RR", 'b', 60, ENGINE);
        let analysis =
            format!("[{{\"move\": \"{}\", \"eval_cp\": 60}}, {{\"move\": \"{}\", \"eval_cp\": 20}}]", m("q1").to_uci(), m("q2").to_uci());
        t.f.set_analysis(right, &analysis);
        let meet = t.leaf("DAG-M", 'w', 55, ENGINE);
        t.edge(root, left, "r1");
        t.edge(root, right, "r2");
        t.edge(left, meet, "l1");
        let win = t.pos("DAG-WIN", 'w');
        t.f.close(win, Status::WhiteWin, Closure::Minimax, Proof::None);
        t.edge(left, win, "l2");
        t.edge(right, meet, "q1");
        let q2 = t.leaf("DAG-Q2", 'w', 20, ENGINE);
        t.edge(right, q2, "q2");
        t
    };
    let mut t = build();
    let all = t.all();
    t.ascent(&all, None);
    let names = ["DAG-L", "DAG-RR", "DAG-R"];
    assert_eq!(
        names.iter().map(|n| t.state(n)).collect::<Vec<_>>(),
        vec![s(55, "l1", 1, ENGINE), s(20, "q2", 1, ENGINE), s(55, "r1", 2, ENGINE)]
    );
    let mut u = build();
    u.ascent_without_the_rule(&all, None);
    assert_eq!(names.iter().map(|n| u.state(n)).collect::<Vec<_>>(), names.iter().map(|n| t.state(n)).collect::<Vec<_>>());
}

/// test_loop_best_exit.py::UntouchedTests::test_a_loop_opened_for_the_same_value_is_left_to_part_2
/// (`_the_rule_must_not_run`: the same with a loop budget of 0).
#[test]
fn a_loop_opened_for_the_same_value_is_left_to_part_2() {
    for budget_zero in [false, true] {
        let mut t = T::new();
        let ring: Vec<PosId> = ['w', 'b', 'w', 'b']
            .iter()
            .enumerate()
            .map(|(n, &stm)| t.node(&format!("TIE-{n}"), stm, Some(&format!("t{n}")), 1199, FAR))
            .collect();
        for i in 0..4 {
            t.edge(ring[i], ring[(i + 1) % 4], &format!("t{i}"));
            let o = t.leaf(&format!("TIE-O{i}"), if i % 2 == 0 { 'b' } else { 'w' }, 1199, 32_000_000);
            t.edge(ring[i], o, &format!("o{i}"));
        }
        for _ in 0..3 {
            if budget_zero {
                t.ascent_without_the_rule(&ring, None);
            } else {
                t.ascent(&ring, None);
            }
        }
        assert!(t.loops().is_empty(), "budget zero: {budget_zero}");
        assert_eq!(t.values(&["TIE-0", "TIE-1", "TIE-2", "TIE-3"]), vec![Some(1199); 4]);
    }
}

/// test_loop_best_exit.py::UntouchedTests::test_exact_closures_are_never_touched
#[test]
fn exact_closures_are_never_touched() {
    let mut t = T::new();
    let (w, b) = t.two_cycle("EXACT", 1100, 1300, false);
    let won = t.pos("EXACT-WIN", 'w');
    t.f.set(won, |c| {
        c.set_status(Status::WhiteWin, Closure::MatePv, Proof::AndOr);
        c.best_move = m("e1e2");
        c.mate_in = 7;
        c.backed_eval = 10_000;
        c.backed_move = m("e1e2");
    });
    t.f.set_ext(won, |e| e.backed_nodes = PROVEN_QUALITY);
    t.edge(b, won, "bwin");
    let above = t.pos("EXACT-UP", 'b');
    t.f.set(above, |c| {
        c.set_status(Status::BlackWin, Closure::Minimax, Proof::None);
        c.best_move = m("uw");
        c.backed_eval = -10_000;
        c.backed_move = m("uw");
    });
    t.f.set_ext(above, |e| e.backed_nodes = PROVEN_QUALITY);
    t.edge(above, w, "uw");
    let snap = |t: &T| {
        [won, above]
            .iter()
            .map(|&k| {
                let c = t.f.core(k);
                (
                    c.status(),
                    c.closure(),
                    c.proof(),
                    c.best_move,
                    c.mate_in,
                    c.backed_eval,
                    c.backed_move,
                    c.backed_plies,
                    t.f.ext(k).backed_nodes,
                )
            })
            .collect::<Vec<_>>()
    };
    let before = snap(&t);
    t.ascent(&[w, b, above], Some(1));
    assert_eq!(t.moves(&["EXACT-W", "EXACT-B"]), vec![mp(1100, "wx", 1), mp(1100, "bw", 2)]);
    assert_eq!(snap(&t), before);
}

/// test_loop_best_exit.py::BoundTests::test_no_time_left_leaves_the_loop
/// (`BACKED_LOOP_MAX_SECONDS = 0` is a loop budget of 0 units, D1).
#[test]
fn no_time_left_leaves_the_loop() {
    let mut t = T::new();
    let (w, b) = t.two_cycle("CLOCK", 1100, 1300, false);
    t.ascent_without_the_rule(&[w, b], Some(1));
    assert_eq!(t.values(&["CLOCK-W", "CLOCK-B"]), vec![Some(STALE), Some(STALE)]);
}

/// test_loop_best_exit.py::BoundTests::test_a_deadline_already_past_reads_nothing
/// (v1-specific in the map for its query count; the rule half: a meter
/// already past its limit returns None before reading anything).
#[test]
fn a_deadline_already_past_reads_nothing() {
    let mut t = T::new();
    let (w, b) = t.two_cycle("LATE", 1100, 1300, true);
    let mut cache = SpineCache::default();
    cache.wrote(w, m("wb"));
    cache.wrote(b, m("bw"));
    let mut meter = Meter { used: 1, limit: 0 };
    let worth =
        loop_at_best_exit(&t.f.g, &[w, b], &BTreeMap::new(), &BTreeMap::new(), &mut cache, &mut meter, &LOOP_COSTS, &mut Vec::new());
    assert!(worth.is_none());
    assert_eq!(meter.used, 1, "nothing charged: nothing read");
    assert!(cache.steps.is_empty());
}

/// test_loop_best_exit.py::BoundTests::test_only_rows_of_the_level_are_written
/// (v1's spy on `_loop_at_best_exit`'s `by_key` is not expressible; the
/// values it pins are).
#[test]
fn only_rows_of_the_level_are_written() {
    let mut t = T::new();
    t.four_cycle("LEVEL", 1199, [1150, 1400, 1199, 1450]);
    let b1 = t.id("LEVEL-B1");
    t.ascent(&[b1], Some(1));
    let far = FAR as i64;
    assert_eq!(t.moves(&["LEVEL-W1", "LEVEL-W2", "LEVEL-B2"]), vec![mp(1199, "k1", far), mp(1199, "k3", far), mp(1199, "k4", far)]);
    t.ascent(&[b1], None);
    assert_eq!(
        t.moves(&["LEVEL-W1", "LEVEL-B1", "LEVEL-W2", "LEVEL-B2"]),
        vec![mp(1199, "k1", 3), mp(1199, "k2", 2), mp(1199, "o3", 1), mp(1199, "k4", 4)]
    );
    assert!(t.loops().is_empty());
}

fn wheel(t: &mut T) -> Vec<PosId> {
    let stm = [("N0", 'w'), ("N1", 'b'), ("N2", 'w'), ("N3", 'b')];
    let kids: [(&str, &[&str]); 4] = [("N0", &["N1"]), ("N1", &["N2", "N1L0", "N1L1"]), ("N2", &["N3"]), ("N3", &["N0", "N3L0"])];
    for (leaf, value) in [("N1L0", -150), ("N1L1", 50), ("N3L0", -150)] {
        t.leaf(&format!("X-{leaf}"), 'w', value, ENGINE);
    }
    let stored: [(&str, i32, &str, u16); 4] =
        [("N0", 900, "N1", 4), ("N1", 1208, "N1L0", 4), ("N2", -1208, "N3", 4), ("N3", 0, "N3L0", FAR)];
    for (node, value, mv, plies) in stored {
        let s = stm.iter().find(|x| x.0 == node).unwrap().1;
        let id = t.pos(&format!("X-{node}"), s);
        let name = format!("{node}>{mv}");
        t.f.set(id, |c| {
            c.set_flag(F_EXPANDED, true);
            c.backed_eval = value as i16;
            c.backed_move = m(&name);
            c.backed_plies = plies;
        });
        t.f.set_ext(id, |e| {
            e.nodes_invested = ENGINE;
            e.backed_nodes = HEAVY;
        });
    }
    for (node, children) in kids {
        for child in children {
            let (p, c) = (t.id(&format!("X-{node}")), t.id(&format!("X-{child}")));
            t.edge(p, c, &format!("{node}>{child}"));
        }
    }
    stm.iter().map(|(n, _)| t.id(&format!("X-{n}"))).collect()
}

/// test_loop_best_exit.py::WaitingTests::test_waiting_alone_brings_the_miniature_to_rest
#[test]
fn waiting_alone_brings_the_miniature_to_rest() {
    let mut t = T::new();
    let keys = wheel(&mut t);
    t.ascent_without_the_rule(&keys, None);
    assert_eq!(t.ascent_without_the_rule(&keys, None), 0);
    assert_eq!(
        t.moves(&["X-N0", "X-N1", "X-N2", "X-N3"]),
        vec![mp(-150, "N0>N1", 2), mp(-150, "N1>N1L0", 1), mp(-150, "N2>N3", 2), mp(-150, "N3>N3L0", 1)]
    );
    assert!(t.loops().is_empty());
}

/// test_loop_best_exit.py::WaitingTests::test_the_miniature_comes_to_rest_at_its_value
#[test]
fn the_miniature_comes_to_rest_at_its_value() {
    let mut t = T::new();
    let keys = wheel(&mut t);
    t.ascent(&keys, None);
    assert_eq!(t.ascent(&keys, None), 0);
    assert_eq!(t.values(&["X-N0", "X-N1", "X-N2", "X-N3"]), vec![Some(-150); 4]);
    assert!(t.loops().is_empty());
}

fn wrow(id: u64) -> Row {
    Row { id: PosId(id), core: av2_model::Core::default(), nodes_invested: ENGINE }
}

fn wkid(mv: &str, key: u64, returns: bool) -> ChildValue {
    let mut c = ChildValue::uninformed(m(mv), PosId(key));
    c.value = Some(0);
    c.quality = ENGINE;
    c.returns = returns;
    c.cycles = returns;
    c
}

/// test_loop_best_exit.py::WaitingTests::test_a_chain_alternates_from_the_bottom
#[test]
fn a_chain_alternates_from_the_bottom() {
    let (a, b, c, l) = (0u64, 1u64, 2u64, 3u64);
    let rows = [wrow(a), wrow(b), wrow(c)];
    let stored: HashMap<PosId, Backed> =
        [(a, "ab"), (b, "bc"), (c, "cl")].iter().map(|&(k, mv)| (PosId(k), backed(10, mv, 1, ENGINE))).collect();
    let mut children: LevelChildren = BTreeMap::new();
    children.insert(PosId(a), vec![wkid("ab", b, false)]);
    children.insert(PosId(b), vec![wkid("bc", c, false)]);
    children.insert(PosId(c), vec![wkid("cl", l, false)]);
    let decided: BTreeMap<PosId, Backed> =
        [(a, "ab"), (b, "bc"), (c, "cl")].iter().map(|&(k, mv)| (PosId(k), backed(20, mv, 1, ENGINE))).collect();
    let wait = rows_that_wait(&rows, &children, &decided, &stored, &HashSet::new());
    assert_eq!(wait.into_iter().collect::<Vec<_>>(), vec![PosId(b)]);
    let ruled: HashSet<PosId> = [PosId(b)].into_iter().collect();
    let wait = rows_that_wait(&rows, &children, &decided, &stored, &ruled);
    assert_eq!(wait.into_iter().collect::<Vec<_>>(), vec![PosId(a)]);
}

/// test_loop_best_exit.py::WaitingTests::test_what_does_not_wait
#[test]
fn what_does_not_wait() {
    // A B C D E F; X G H outside.
    let (a, b, c, d, e, f, x, g, h) = (0u64, 1, 2, 3, 4, 5, 6, 7, 8);
    let rows: Vec<Row> = [a, b, c, d, e, f].iter().map(|&k| wrow(k)).collect();
    let stored: HashMap<PosId, Backed> = [(a, "ab"), (b, "ba"), (c, "cd"), (d, "dx"), (e, "ef"), (f, "f1")]
        .iter()
        .map(|&(k, mv)| (PosId(k), backed(10, mv, 1, ENGINE)))
        .collect();
    let mut children: LevelChildren = BTreeMap::new();
    children.insert(PosId(a), vec![wkid("ab", b, false)]);
    children.insert(PosId(b), vec![wkid("ba", a, false)]);
    children.insert(PosId(c), vec![wkid("cd", d, true)]);
    children.insert(PosId(d), vec![wkid("dx", x, false)]);
    children.insert(PosId(e), vec![wkid("ef", f, false)]);
    children.insert(PosId(f), vec![wkid("f1", g, false), wkid("f2", h, false)]);
    let mut decided: BTreeMap<PosId, Backed> = BTreeMap::new();
    for (k, mv) in [(a, "ab"), (b, "ba"), (c, "cd"), (d, "dx"), (e, "ef")] {
        decided.insert(PosId(k), backed(20, mv, 1, ENGINE));
    }
    decided.insert(PosId(f), backed(10, "f2", 1, ENGINE));
    assert!(rows_that_wait(&rows, &children, &decided, &stored, &HashSet::new()).is_empty());
}

// ================================================================ test_pointer_loop_grounding.py

const LOOP_NODES: u64 = 768_000_000;
const NODES: u64 = 128_000_000;
const LIGHT: u64 = 32_000_000;
const VALUE: i32 = 1208;

fn basin(t: &mut T, name: &str) -> Vec<PosId> {
    let node = |t: &mut T, tag: &str, stm: char, mv: &str, nodes: u64| {
        let id = t.pos(&format!("{name}-{tag}"), stm);
        let mvm = m(mv);
        t.f.set(id, |c| {
            c.set_flag(F_EXPANDED, true);
            c.eval_cp = VALUE as i16;
            c.backed_eval = VALUE as i16;
            c.backed_move = mvm;
            c.backed_plies = 4;
        });
        t.f.set_ext(id, |e| {
            e.nodes_invested = nodes;
            e.backed_nodes = nodes;
        });
        id
    };
    let w1 = node(t, "W1", 'w', "l1a1", LOOP_NODES);
    let b1 = node(t, "B1", 'b', "l2a2", LOOP_NODES);
    let w2 = node(t, "W2", 'w', "l3a3", LOOP_NODES);
    let b2 = node(t, "B2", 'b', "l4a4", LOOP_NODES);
    t.edge(w1, b1, "l1a1");
    t.edge(b1, w2, "l2a2");
    t.edge(w2, b2, "l3a3");
    t.edge(b2, w1, "l4a4");
    let e1 = t.leaf(&format!("{name}-E1"), 'w', VALUE + 200, NODES);
    t.edge(b1, e1, "e1e1");
    let e2 = t.leaf(&format!("{name}-E2"), 'w', VALUE + 200, NODES);
    t.edge(b2, e2, "e2e2");
    for (index, (white, black)) in [(w1, b1), (w2, b2)].into_iter().enumerate() {
        let i = index + 1;
        let x = node(t, &format!("X{i}"), 'b', &format!("y{i}y{i}"), NODES);
        let y = node(t, &format!("Y{i}"), 'w', &format!("d{i}d{i}"), NODES);
        let source = t.leaf(&format!("{name}-S{i}"), 'b', VALUE, LIGHT);
        t.edge(white, x, &format!("x{i}x{i}"));
        t.edge(x, y, &format!("y{i}y{i}"));
        t.edge(y, black, &format!("d{i}d{i}"));
        t.edge(y, source, &format!("s{i}s{i}"));
    }
    vec![w1, b1, w2, b2]
}

/// test_pointer_loop_grounding.py::SelfProtectingLoopTests::test_the_loop_opens_once_the_positions_around_it_drain_to_a_source
#[test]
fn the_loop_opens_once_the_positions_around_it_drain_to_a_source() {
    let mut t = T::new();
    let ring = basin(&mut t, "BASIN");
    let all = t.all();
    for _ in 0..3 {
        t.ascent(&all, None);
    }
    assert_eq!(t.ascent(&all, None), 0);
    let loops = t.loops();
    assert!(ring.iter().all(|k| !loops.contains(k)));
    let mut values: Vec<Option<i32>> = ring.iter().map(|&k| t.f.stored(k).value).collect();
    values.sort();
    values.dedup();
    assert_eq!(values, vec![Some(VALUE)]);
}

/// test_pointer_loop_grounding.py::SelfProtectingLoopTests::test_a_drain_prefers_the_source_to_the_loop_at_the_same_value
#[test]
fn a_drain_prefers_the_source_to_the_loop_at_the_same_value() {
    let mut t = T::new();
    basin(&mut t, "DRAIN");
    let keys = t.ids(&["DRAIN-Y1", "DRAIN-Y2"]);
    t.ascent(&keys, None);
    for tag in ["Y1", "Y2"] {
        let st = t.state(&format!("DRAIN-{tag}"));
        let n = &tag[1..];
        assert_eq!((st.0, st.1), (Some(VALUE), format!("s{n}s{n}")), "{tag}");
    }
}

fn gchild(mv: &str, value: i32, quality: u64, returns: bool, circular: bool) -> ChildValue {
    let mut c = ChildValue::uninformed(m(mv), PosId(0));
    c.value = Some(value);
    c.quality = quality;
    c.plies = 1;
    c.returns = returns;
    c.circular = circular;
    c
}

/// test_pointer_loop_grounding.py::GroundingTests::test_a_source_beats_a_loop_elsewhere_beats_a_return
#[test]
fn a_source_beats_a_loop_elsewhere_beats_a_return() {
    for stm_white in [true, false] {
        let kids = vec![
            gchild("back", VALUE, 6 * LOOP_NODES, true, false),
            gchild("loop", VALUE, LOOP_NODES, false, true),
            gchild("source", VALUE, LIGHT, false, false),
        ];
        assert_eq!(best_child(&kids, &[0, 1, 2], stm_white, Move::NONE), 2);
        assert_eq!(best_child(&kids, &[0, 1], stm_white, Move::NONE), 1);
    }
}

/// test_pointer_loop_grounding.py::GroundingTests::test_a_better_value_still_wins_whatever_its_chain
#[test]
fn a_better_value_still_wins_whatever_its_chain() {
    let kids = vec![gchild("loop", VALUE + 1, LOOP_NODES, false, true), gchild("source", VALUE, LIGHT, false, false)];
    assert_eq!(best_child(&kids, &[0, 1], true, Move::NONE), 0);
}

// ================================================================ test_backup_echo.py::EchoFamilyTests

/// `BACKED_CYCLE_MAX_PLIES + 2`: too long for every walk.
const RING: usize = 34;

fn echo_ring(t: &mut T, name: &str) -> Vec<PosId> {
    let ring: Vec<PosId> = (0..RING)
        .map(|i| {
            let white = i % 2 == 0;
            let id = t.pos(&format!("{name}-R{i}"), if white { 'w' } else { 'b' });
            let mvm = m(&format!("r{i}"));
            t.f.set(id, |c| {
                c.set_flag(F_EXPANDED, true);
                c.eval_cp = if white { 1180 } else { 1190 };
                c.backed_eval = 1208;
                c.backed_move = mvm;
                c.backed_plies = 3000 + i as u16;
            });
            t.f.set_ext(id, |e| {
                e.nodes_invested = ENGINE;
                e.backed_nodes = 2_000_000_000;
            });
            id
        })
        .collect();
    for i in 0..RING {
        t.edge(ring[i], ring[(i + 1) % RING], &format!("r{i}"));
        let white = i % 2 == 0;
        let e = t.leaf(&format!("{name}-E{i}"), if white { 'b' } else { 'w' }, if white { 1198 } else { 1250 }, ENGINE);
        t.edge(ring[i], e, &format!("e{i}"));
    }
    ring
}

/// test_backup_echo.py::EchoFamilyTests::test_the_echo_dies_and_the_ring_takes_its_best_exit
#[test]
fn the_echo_dies_and_the_ring_takes_its_best_exit() {
    let mut t = T::new();
    let ring = echo_ring(&mut t, "ON");
    let mut changed = Vec::new();
    for _ in 0..6 {
        changed.push(t.ascent(&ring, None));
        if *changed.last().unwrap() == 0 {
            break;
        }
    }
    assert_eq!(*changed.last().unwrap(), 0, "{changed:?}");
    for &k in &ring {
        let b = t.f.stored(k);
        assert_eq!(b.value, Some(1198));
        assert!(b.plies < 64);
    }
}

// ================================================================ test_explorer_repetition_rows.py (backup side)

const X_FEN: &str = "r1b1kb1r/pp5p/2np2p1/5qB1/1P1PP3/5Q2/P1P2PPP/R3K2R w KQkq - 0 1";

struct Sicilian {
    f: Fixture,
    x: PosId,
    y: PosId,
    p: PosId,
    c: PosId,
}

/// `SicilianLoopFixture.setUp`: X -g5h6-> Y -f8e7-> P -h6g5-> C -e7f8-> X,
/// the ways out, and up to four ascents (with v1's loop rules).
fn sicilian() -> Sicilian {
    let mut f = Fixture::new();
    let y_fen = play(X_FEN, "g5h6");
    let p_fen = play(&y_fen, "f8e7");
    let c_fen = play(&p_fen, "h6g5");
    assert_eq!(play(&c_fen, "e7f8"), X_FEN);
    let ids: Vec<PosId> = [X_FEN, &y_fen, &p_fen, &c_fen].iter().map(|fen| f.pos(fen)).collect();
    for &id in &ids {
        f.set(id, |c| c.set_flag(F_EXPANDED, true));
    }
    let (x, y, p, c) = (ids[0], ids[1], ids[2], ids[3]);
    f.edge(x, "g5h6", y);
    f.edge(y, "f8e7", p);
    f.edge(p, "h6g5", c);
    f.edge(c, "e7f8", x);
    for (parent, mv, value) in [(p, "e4f5", 1141), (x, "e1g1", 29), (c, "e8g8", 1160), (y, "f8g7", 1500)] {
        let child = f.child(parent, mv);
        f.set(child, |core| core.eval_cp = value);
        f.set_ext(child, |e| e.nodes_invested = ENGINE);
    }
    let keys = [x, y, p, c];
    for _ in 0..4 {
        let (r, _, _) = f.ascent_loops(&keys);
        if r.changed == 0 {
            break;
        }
    }
    Sicilian { f, x, y, p, c }
}

/// test_explorer_repetition_rows.py::BackupPinTests::test_p_stands_on_e4f5
#[test]
fn p_stands_on_e4f5() {
    let s = sicilian();
    let b = s.f.stored(s.p);
    assert_eq!((b.value, b.mv), (Some(1141), uci("e4f5")));
}

/// test_explorer_repetition_rows.py::BackupPinTests::test_h6g5_on_the_line_is_worth_its_way_out
/// (`line_moves` / `repetition_moves` through the ported pieces).
#[test]
fn h6g5_on_the_line_is_worth_its_way_out() {
    let s = sicilian();
    let got = line_moves(&s.f, s.p, &[]);
    assert_eq!(got, BTreeMap::from([("h6g5".to_string(), (Some(29), false))]));
}

/// test_explorer_repetition_rows.py::BackupPinTests::test_the_stored_values_are_round_the_loop
#[test]
fn the_stored_values_are_round_the_loop() {
    let s = sicilian();
    let vals: Vec<Option<i32>> = [s.c, s.x, s.y].iter().map(|&k| s.f.stored(k).value).collect();
    assert_eq!(vals, vec![Some(1141); 3]);
    assert_eq!(s.f.stored(s.c).mv, uci("e7f8"));
}

/// test_explorer_repetition_rows.py::ReturningRowTests::test_the_gate_on_a_published_0_is_gone
/// (`views._repetition_moves` is `line_moves` without a route).
#[test]
fn the_gate_on_a_published_0_is_gone() {
    let s = sicilian();
    assert_ne!(s.f.stored(s.p).value, Some(0));
    assert_eq!(line_moves(&s.f, s.p, &[]), BTreeMap::from([("h6g5".to_string(), (Some(29), false))]));
}
