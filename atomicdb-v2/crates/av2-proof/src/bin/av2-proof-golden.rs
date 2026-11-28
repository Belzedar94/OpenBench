//! av2-proof-golden: one-step consistency of production's stored proof rows.
//!
//! ```text
//! av2-proof-golden --subjects subjects.csv --edges edges.csv
//! ```
//!
//! Input (exported read-only from production, see the PR): every position
//! within N plies of the start position with its `ProofNode` row of the
//! default campaign (`subjects.csv`: key, depth, status, eval_cp, stm, pn,
//! dn, expanded_in_proof, selected_child) and every out-edge of those
//! positions with the child's status, eval, side to move and row
//! (`edges.csv`: parent_id, id, move_uci, child_id, status, eval_cp, stm, pn,
//! dn), ordered by edge id.
//!
//! For each subject that has a row, `compute_numbers` (PN-4.1) is run with
//! the stored rows of its children and its stored `selected_child` as the
//! previous primary, gate off (the writer services run without it), and the
//! result is compared with the stored row.  A difference means the stored
//! row is not what v1's own rule gives from its stored children: stale
//! (a cap, a skipped lock, a refresh that never reached it), a cycle edge
//! (not detectable from one ply of rows), or a refresh made with the gate
//! on (web-process cascades).

use av2_model::record::{F_BLACK_TO_MOVE, NO_CP};
use av2_model::{Core, Edge, Fingerprint, GraphRead, GraphWrite, Move, PosId, Status};
use av2_proof::gate::GateConfig;
use av2_proof::graph::ProofGraph;
use av2_proof::numbers::Goal;
use av2_proof::refresh::{ChildEdge, Cycling, compute_numbers};
use av2_proof::table::Row;
use av2_store::mem::MemGraph;
use av2_tools::{Args, die};
use std::collections::HashMap;

struct Pos {
    status: Status,
    eval: Option<i16>,
    black: bool,
    row: Option<(u64, u64)>,
}

fn opt<T: std::str::FromStr>(s: &str) -> Option<T> {
    if s.is_empty() { None } else { s.parse().ok() }
}

struct Graph {
    g: MemGraph,
    id_of: HashMap<String, PosId>,
    info: Vec<Pos>,
}

impl Graph {
    fn add(&mut self, key: &str, p: Pos, fen: Option<&str>) -> PosId {
        if let Some(&id) = self.id_of.get(key) {
            return id;
        }
        let mut c = Core::default();
        c.set_status(p.status, Default::default(), Default::default());
        c.set_flag(F_BLACK_TO_MOVE, p.black);
        c.eval_cp = p.eval.unwrap_or(NO_CP);
        let id = self.g.create(Fingerprint::from_hex(&key[..24]).unwrap(), fen, c).unwrap();
        self.id_of.insert(key.to_string(), id);
        self.info.push(p);
        id
    }
}

fn main() {
    let a = Args::parse();
    let mut gr = Graph { g: MemGraph::new(), id_of: HashMap::new(), info: Vec::new() };
    struct Subject {
        id: PosId,
        depth: u32,
        stored: Option<Row>,
    }
    let mut subjects = Vec::new();
    let mut rdr = csv::Reader::from_path(a.path("subjects")).unwrap_or_else(|e| die(&e.to_string()));
    for r in rdr.records() {
        let r = r.unwrap();
        let p = Pos { status: Status::from_v1(&r[2]).unwrap(), eval: opt(&r[3]), black: &r[4] == "b", row: None };
        let id = gr.add(&r[0], p, None);
        let stored = opt::<u64>(&r[5]).map(|pn| Row {
            pn,
            dn: r[6].parse().unwrap(),
            expanded: &r[7] == "t",
            selected: Move::parse_opt(&r[8]).unwrap_or(Move::NONE),
            born: 0,
        });
        subjects.push(Subject { id, depth: r[1].parse().unwrap(), stored });
    }
    let mut children: HashMap<u64, Vec<(Move, PosId)>> = HashMap::new();
    let mut rdr = csv::Reader::from_path(a.path("edges")).unwrap_or_else(|e| die(&e.to_string()));
    for r in rdr.records() {
        let r = r.unwrap();
        let child_row = opt::<u64>(&r[7]).map(|pn| (pn, r[8].parse::<u64>().unwrap()));
        let fen = r.get(9).filter(|f| !f.is_empty());
        let c = gr.add(&r[3], Pos { status: Status::from_v1(&r[4]).unwrap(), eval: opt(&r[5]), black: &r[6] == "b", row: child_row }, fen);
        if let Some(cr) = child_row {
            gr.info[c.index()].row = Some(cr);
        }
        let parent = gr.id_of[&r[0]];
        children.entry(parent.0).or_default().push((Move::parse(&r[2]).unwrap(), c));
    }
    let Graph { mut g, mut info, .. } = gr;
    for (p, es) in &children {
        let edges: Vec<Edge> = es.iter().map(|&(mv, child)| Edge { child, mv }).collect();
        g.set_children(PosId(*p), &edges).unwrap();
    }
    // Subjects' own rows also serve as child rows of their parents.
    for s in &subjects {
        if let Some(r) = s.stored {
            info[s.id.index()].row = Some((r.pn, r.dn));
        }
    }
    let mut by_depth: HashMap<u32, [u64; 6]> = HashMap::new();
    let mut examples: Vec<String> = Vec::new();
    let mut kinds: std::collections::BTreeMap<String, u64> = Default::default();
    for s in &subjects {
        let e = by_depth.entry(s.depth).or_default();
        e[0] += 1;
        let Some(stored) = s.stored else { continue };
        e[1] += 1;
        let mut es = Vec::new();
        g.children(s.id, &mut es);
        let kids: Vec<ChildEdge> = es.iter().map(|e| ChildEdge { mv: e.mv, child: e.child, node: ProofGraph::node(&g, e.child) }).collect();
        let row_of = |id: PosId| info[id.index()].row.map(|(pn, dn)| Row { pn, dn, expanded: false, selected: Move::NONE, born: 0 });
        let (c, _) = compute_numbers(
            &g,
            Goal::WhiteWin,
            GateConfig::default(),
            s.id,
            ProofGraph::node(&g, s.id),
            &kids,
            row_of,
            stored.selected,
            &Cycling::new(),
        );
        let numbers_ok = (c.pn, c.dn) == (stored.pn, stored.dn);
        let all_ok = numbers_ok && c.expanded == stored.expanded && c.selected == stored.selected;
        if all_ok {
            e[2] += 1;
        } else if numbers_ok {
            e[3] += 1;
        } else {
            e[4] += 1;
            // Would the solve gate (on in the web process) explain it?
            let (cg, _) = compute_numbers(
                &g,
                Goal::WhiteWin,
                GateConfig { enabled: true },
                s.id,
                ProofGraph::node(&g, s.id),
                &kids,
                row_of,
                stored.selected,
                &Cycling::new(),
            );
            let k = (if (cg.pn, cg.dn) == (stored.pn, stored.dn) {
                "gate"
            } else if c.dn != stored.dn {
                "dn"
            } else if stored.pn > c.pn {
                "pn_up"
            } else {
                "pn_down"
            })
            .to_string();
            *kinds.entry(k).or_insert(0u64) += 1;
            if examples.len() < 12 {
                examples.push(format!(
                    "depth {} stored ({}, {}, {:?}) computed ({}, {}, {:?}) children {} with rows {}",
                    s.depth,
                    stored.pn,
                    stored.dn,
                    stored.selected,
                    c.pn,
                    c.dn,
                    c.selected,
                    kids.len(),
                    kids.iter().filter(|k| info[k.child.index()].row.is_some()).count()
                ));
            }
        }
        if kids.is_empty() {
            e[5] += 1;
        }
    }
    let mut depths: Vec<_> = by_depth.into_iter().collect();
    depths.sort();
    for (d, [n, rows, ok, sel_only, num, leaves]) in depths {
        println!(
            "{{\"depth\":{d},\"positions\":{n},\"with_row\":{rows},\"identical\":{ok},\"same_numbers_other_fields_differ\":{sel_only},\"numbers_differ\":{num},\"leaves\":{leaves}}}"
        );
    }
    println!("{{\"numbers_differ_by_kind\":{}}}", serde_json::to_string(&kinds).unwrap());
    for x in examples {
        println!("{x}");
    }
}
