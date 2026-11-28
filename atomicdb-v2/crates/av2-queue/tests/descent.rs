//! The minting loop over the real descents (`ProofDescent` over av2-proof):
//! the pool-level tests of `test_value_descent.py` (`ProductionRootTests`,
//! `ReservationTests::test_the_pool_never_hands_the_same_position_twice`,
//! `PoolVarietyTests`, `ClampsStillWinTests`, `LongLinePenaltyTests` through
//! the pool).  v1's `_next_tasks_by_proof(n)` is `PnSelector::next_tasks`.

use av2_model::record::{F_BLACK_TO_MOVE, NO_CP};
use av2_model::{Core, Edge, Ext, Fingerprint, GraphRead, GraphWrite, Move, PosId, Status};
use av2_proof::table::Row;
use av2_proof::{Campaign, Proofs, RefreshConfig};
use av2_queue::budget::short_mate_clamp;
use av2_queue::consts::*;
use av2_queue::*;
use av2_store::mem::MemGraph;

#[derive(Default)]
struct P {
    black: bool,
    eval: Option<i16>,
    backed: Option<i16>,
    backed_move: Option<Move>,
    backed_plies: u16,
    mate_in: Option<u16>,
    status: Option<Status>,
}

struct W {
    g: MemGraph,
    moves: u16,
}

impl W {
    fn new() -> W {
        W { g: MemGraph::new(), moves: 0 }
    }
    fn mv(&mut self) -> Move {
        self.moves += 1;
        Move::new((self.moves / 64) as u8, (self.moves % 64) as u8, 0)
    }
    fn pos(&mut self, p: P) -> u64 {
        let mut c = Core::default();
        c.set_status(p.status.unwrap_or(Status::Unknown), Default::default(), Default::default());
        c.set_flag(F_BLACK_TO_MOVE, p.black);
        c.eval_cp = p.eval.unwrap_or(NO_CP);
        c.backed_eval = p.backed.unwrap_or(NO_CP);
        c.backed_move = p.backed_move.unwrap_or(Move::NONE);
        c.backed_plies = p.backed_plies;
        c.mate_in = p.mate_in.unwrap_or(av2_model::record::NO_MATE);
        let fp = Fingerprint::from_hex(&format!("{:024x}", self.g.len() + 1)).unwrap();
        let stm = if p.black { "b" } else { "w" };
        let id = self.g.create(fp, Some(&format!("4k3/8/8/8/8/8/8/4K3 {stm} - - 0 1")), c).unwrap();
        self.g.put_ext(id, Ext::default()).unwrap();
        id.0
    }
    fn edge(&mut self, parent: u64, child: u64, mv: Move) {
        let mut es = Vec::new();
        self.g.children(PosId(parent), &mut es);
        es.push(Edge { child: PosId(child), mv });
        self.g.set_children(PosId(parent), &es).unwrap();
    }
    /// `_spine(name, plies)`.
    fn spine(&mut self, plies: usize) -> Vec<u64> {
        let moves: Vec<Move> = (0..plies).map(|_| self.mv()).collect();
        let nodes: Vec<u64> = (0..=plies)
            .map(|i| {
                let last = i == plies;
                self.pos(P {
                    black: i % 2 == 1,
                    eval: Some(800),
                    backed: (!last).then_some(812),
                    backed_move: (!last).then(|| moves[i]),
                    ..P::default()
                })
            })
            .collect();
        for i in 0..plies {
            self.edge(nodes[i], nodes[i + 1], moves[i]);
        }
        nodes
    }
    /// `_long_spine(name, plies, line)`.
    fn long_spine(&mut self, plies: usize, line: u16) -> Vec<u64> {
        let moves: Vec<Move> = (0..=plies).map(|_| self.mv()).collect();
        let nodes: Vec<u64> = (0..=plies)
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
        nodes
    }
}

/// `_campaign(root)`: the only active campaign, primary-only policy.
fn proofs(root: u64) -> Proofs {
    let c =
        Campaign { policy: serde_json::json!({"primary": 1.0, "backup": 0.0, "explore": 0.0}), ..Campaign::default_for(5, PosId(root)) };
    Proofs::new(vec![c], RefreshConfig::default())
}

fn value_config(on: bool) -> QueueConfig {
    QueueConfig { descent_value: on, ..QueueConfig::default() }
}

/// `ingest._next_tasks_by_proof(n)`.
fn mint(w: &W, p: &Proofs, n: usize, value: bool) -> Vec<MintTask> {
    let q = QueueState::new(value_config(value));
    let env = GraphEnv { graph: &w.g, priority: None };
    let d = ProofDescent::new(&w.g, p, &[]);
    let mut sel = PnSelector { descent: &d };
    sel.next_tasks(&q, &env, n, 1_000).tasks
}

#[test]
fn production_root_tests() {
    let mut w = W::new();
    let (me3, mnf3) = (w.mv(), w.mv());
    let root = w.pos(P { eval: Some(40), backed: Some(812), backed_move: Some(mnf3), ..P::default() });
    let e3 = w.pos(P { black: true, eval: Some(20), ..P::default() });
    let nf3 = w.pos(P { black: true, eval: Some(812), ..P::default() });
    w.edge(root, e3, me3);
    w.edge(root, nf3, mnf3);
    let mut p = proofs(root);
    let t = p.table_mut(5).unwrap();
    let row = |pn, dn| Row { pn, dn, expanded: false, selected: Move::NONE, born: 0 };
    t.put(PosId(root), row(94, 2));
    t.put(PosId(e3), row(119, 1));
    t.put(PosId(nf3), row(475, 1));
    // test_the_minted_task_lands_under_the_backed_move
    let tasks = mint(&w, &p, 1, true);
    assert_eq!(tasks.len(), 1);
    assert_eq!((tasks[0].pos, tasks[0].budget, tasks[0].arm.as_str()), (nf3, VALUE_SPINE_NODES, ARM_DESCEND));
    // test_with_the_switch_off_the_queue_is_the_one_from_yesterday
    let tasks = mint(&w, &p, 1, false);
    assert_eq!((tasks[0].pos, tasks[0].budget, tasks[0].arm.as_str()), (e3, SERVER_PICKED_MIN_NODES, ARM_NONE));
}

/// `ReservationTests::test_the_pool_never_hands_the_same_position_twice`.
#[test]
fn test_the_pool_never_hands_the_same_position_twice() {
    let mut w = W::new();
    let (m0, m1, m2) = (w.mv(), w.mv(), w.mv());
    let root = w.pos(P { eval: Some(800), backed: Some(800), backed_move: Some(m0), ..P::default() });
    let tip = w.pos(P { black: true, eval: Some(800), ..P::default() });
    let alt = w.pos(P { black: true, eval: Some(700), ..P::default() });
    let far = w.pos(P { black: true, eval: Some(-400), ..P::default() });
    w.edge(root, tip, m0);
    w.edge(root, alt, m1);
    w.edge(root, far, m2);
    let p = proofs(root);
    let keys: Vec<u64> = mint(&w, &p, 3, true).into_iter().map(|t| t.pos).collect();
    let set: std::collections::BTreeSet<u64> = keys.iter().copied().collect();
    assert_eq!(keys.len(), set.len());
    assert!(keys.contains(&tip));
}

#[test]
fn test_a_batch_fills_with_distinct_positions_around_the_spine() {
    let mut w = W::new();
    let nodes = w.spine(6);
    for (i, &node) in nodes[..6].iter().enumerate() {
        let rival = w.pos(P { black: i % 2 == 0, eval: Some(750), ..P::default() });
        let m = w.mv();
        w.edge(node, rival, m);
    }
    let p = proofs(nodes[0]);
    let tasks = mint(&w, &p, 6, true);
    let set: std::collections::BTreeSet<u64> = tasks.iter().map(|t| t.pos).collect();
    assert_eq!((tasks.len(), set.len()), (6, 6));
    assert!(tasks.iter().all(|t| t.budget == VALUE_SPINE_NODES));
}

#[test]
fn test_a_far_alternative_is_not_variety_it_is_dispersion() {
    let mut w = W::new();
    let nodes = w.spine(3);
    for (i, &node) in nodes[..3].iter().enumerate() {
        let white_moves = i % 2 == 0;
        let far = w.pos(P { black: white_moves, eval: Some(if white_moves { -400 } else { 2_500 }), ..P::default() });
        let m = w.mv();
        w.edge(node, far, m);
    }
    let p = proofs(nodes[0]);
    let tasks: Vec<u64> = mint(&w, &p, 4, true).into_iter().map(|t| t.pos).collect();
    assert_eq!(tasks, vec![nodes[3]]);
}

#[test]
fn clamps_still_win_tests() {
    // test_a_short_mate_claim_buys_its_verification_not_the_excavation
    let mut w = W::new();
    let m0 = w.mv();
    let root = w.pos(P { eval: Some(9_900), backed: Some(9_900), backed_move: Some(m0), ..P::default() });
    let tip = w.pos(P { black: true, eval: Some(9_900), mate_in: Some(3), ..P::default() });
    w.edge(root, tip, m0);
    let p = proofs(root);
    let tasks = mint(&w, &p, 1, true);
    let env = GraphEnv { graph: &w.g, priority: None };
    let clamp = short_mate_clamp(&env.pos(tip)).expect("a short mate claim");
    assert_eq!(tasks[0].pos, tip);
    assert!(tasks[0].budget < VALUE_SPINE_NODES);
    assert_eq!((tasks[0].budget, tasks[0].multipv), (clamp.0, clamp.1));
    assert_eq!(tasks[0].multipv, MATE_CLAMP_MULTIPV);
    // test_the_proved_or_clamp_still_cheapens_a_settled_sibling
    let mut w = W::new();
    let (m0, m1) = (w.mv(), w.mv());
    let root = w.pos(P { eval: Some(800), backed: Some(812), backed_move: Some(m0), ..P::default() });
    let tip = w.pos(P { black: true, eval: Some(812), ..P::default() });
    let won = w.pos(P { black: true, status: Some(Status::WhiteWin), ..P::default() });
    w.edge(root, tip, m0);
    w.edge(root, won, m1);
    let p = proofs(root);
    let tasks = mint(&w, &p, 1, true);
    assert_eq!((tasks[0].pos, tasks[0].budget), (tip, OR_CLAMP_NODES));
}

#[test]
fn long_line_pool_tests() {
    // test_the_queue_lands_on_the_short_line
    let mut w = W::new();
    let nodes = w.long_spine(av2_proof::descent::DESCENT_MAX_PLIES as usize + 1, 400);
    let alt = w.pos(P { black: true, eval: Some(712), ..P::default() });
    let m = w.mv();
    w.edge(nodes[0], alt, m);
    let p = proofs(nodes[0]);
    assert_eq!(mint(&w, &p, 1, true)[0].pos, alt);
    // test_when_only_long_lines_are_left_the_walk_still_buys_one
    let mut w = W::new();
    let nodes = w.long_spine(4, 400);
    let p = proofs(nodes[0]);
    let tasks = mint(&w, &p, 1, true);
    assert_eq!(tasks.len(), 1);
    assert_eq!(tasks[0].pos, nodes[4]);
}

/// Campaign roots reach the walk: a community campaign's descents start at
/// its root and are marked `campaign` (PN-6.3 through `ProofDescent`).
#[test]
fn community_campaign_roots_reach_the_walk() {
    let mut w = W::new();
    let nodes = w.spine(2);
    let (mc, mt) = (w.mv(), w.mv());
    let camp = w.pos(P { black: true, eval: Some(500), backed: Some(520), backed_move: Some(mt), ..P::default() });
    let ctip = w.pos(P { eval: Some(520), ..P::default() });
    w.edge(nodes[0], camp, mc);
    w.edge(camp, ctip, mt);
    let p = proofs(nodes[0]);
    let community = [av2_proof::descent::CommunityCampaign { id: 3, root: PosId(camp), active: true, votes: 4 }];
    let q = QueueState::new(value_config(true));
    let env = GraphEnv { graph: &w.g, priority: None };
    let d = ProofDescent::new(&w.g, &p, &community);
    assert_eq!(d.campaign_roots(), vec![(camp, 5f64.ln())]);
    let mut sel = PnSelector { descent: &d };
    let tasks = sel.next_tasks(&q, &env, 2, 1_000).tasks;
    let by_arm: std::collections::BTreeMap<&str, u64> = tasks.iter().map(|t| (t.arm.as_str(), t.pos)).collect();
    for t in &tasks {
        if t.arm == ARM_CAMPAIGN {
            assert_eq!(t.pos, ctip);
        } else {
            assert_eq!(t.pos, nodes[2]);
        }
    }
    assert!(!by_arm.is_empty());
}
