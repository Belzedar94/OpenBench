//! Differential test of the descents (PN-6.5 `descend`, PN-9.11
//! `descend_value`) against v1's own `atomicdb/proof.py`.
//!
//! The scenarios grow the same random proof trees as the refresh harness
//! (the parent module's generator), and add per step:
//! - `val` ops: the backed-value columns of positions (`backed_eval`,
//!   `backed_move` pointing at an edge, at no edge or NULL, `backed_plies`
//!   including lines longer than 200 plies, `nodes_invested` around the
//!   128M saturation threshold, `backed_nodes`);
//! - `task` / `untask` ops: a PENDING or LEASED `AnalysisTask` on a position
//!   (what `_has_live_task` reads);
//! - queries after the refresh: `descend` or `descend_value` with a counter,
//!   a reservation set, a start (the tree root or any node) and `max_plies`.
//!
//! `v1diff/run_v1_descent.py` answers the queries with v1's code
//! (`v1diff/v1_descent_results.json`, committed); `descents_match_v1`
//! replays them here.
//!
//! Regenerate: `cargo test -p av2-proof write_v1_descent_scenarios --
//! --ignored` (writes `v1diff/descent_scenarios.json`, not committed), then
//! from the repository root `python
//! atomicdb-v2/crates/av2-proof/v1diff/run_v1_descent.py
//! atomicdb-v2/crates/av2-proof/v1diff/descent_scenarios.json
//! atomicdb-v2/crates/av2-proof/v1diff/v1_descent_results.json`.

use super::*;
use crate::descent::{DescentConfig, Walker};
use crate::gate::GateConfig;
use std::collections::HashSet;

const NODE_CHOICES: [u64; 8] = [0, 0, 0, 8_000_000, 32_000_000, 128_000_000, 512_000_000, 2_000_000_000];

impl Gen {
    /// Backed-value columns for a random set of positions.
    fn value_ops(&mut self, ops: &mut Vec<Value>) {
        let n = self.keys.len();
        for id in 0..n {
            if !self.rng.chance(if id + 4 >= n { 0.9 } else { 0.25 }) {
                continue;
            }
            let backed_eval = if self.rng.chance(0.3) { Value::Null } else { json!(self.rng.below(800) as i64 - 400) };
            let kids = &self.kids[id];
            let backed_move = if !kids.is_empty() && self.rng.chance(0.75) {
                json!(kids[self.rng.below(kids.len() as u64) as usize].1)
            } else if self.rng.chance(0.3) {
                json!("h7h8q")
            } else {
                Value::Null
            };
            let backed_plies = if self.rng.chance(0.12) { 150 + self.rng.below(260) } else { self.rng.below(20) };
            let nodes = NODE_CHOICES[self.rng.below(NODE_CHOICES.len() as u64) as usize];
            let backed_nodes = if self.rng.chance(0.5) { 0 } else { NODE_CHOICES[self.rng.below(NODE_CHOICES.len() as u64) as usize] };
            ops.push(json!(["val", self.keys[id], backed_eval, backed_move, backed_plies, nodes, backed_nodes]));
        }
        // Cyclic trees: a back edge that the node's spine follows, so walks
        // bite their tail (the `cycle` arm).
        if self.cyclic && n > 3 && self.rng.chance(0.6) {
            let p = self.rng.below(n as u64) as usize;
            let c = self.rng.below(n as u64) as usize;
            let uci = Move::new(48, 56, 1).to_uci();
            if self.status[p] == Status::Unknown
                && c != p
                && self.black[c] != self.black[p]
                && !self.kids[p].iter().any(|(x, m)| *x == c as u64 || *m == uci)
            {
                ops.push(json!(["edge", self.keys[p], uci, self.keys[c]]));
                self.kids[p].push((c as u64, uci.clone()));
                ops.push(json!(["val", self.keys[p], self.rng.below(800) as i64 - 400, uci, self.rng.below(20), 0, 0]));
            }
        }
        for _ in 0..self.rng.below(3) {
            let id = self.rng.below(n as u64) as usize;
            if self.rng.chance(0.3) {
                ops.push(json!(["untask", self.keys[id]]));
            } else {
                ops.push(json!(["task", self.keys[id], if self.rng.chance(0.5) { "PENDING" } else { "LEASED" }]));
            }
        }
    }

    fn queries(&mut self, max_plies_cap: u64) -> Value {
        let n = self.keys.len() as u64;
        let mut q = Vec::new();
        for i in 0..8 {
            let kind = if i % 2 == 0 { "pn" } else { "value" };
            let start = if self.rng.chance(0.7) { 0 } else { self.rng.below(n) };
            let avoid: Vec<&String> = (0..self.rng.below(4)).map(|_| &self.keys[self.rng.below(n) as usize]).collect();
            let max_plies = if self.rng.chance(0.15) { self.rng.below(4) } else { max_plies_cap };
            q.push(json!([kind, self.rng.below(1 << 32), avoid, self.keys[start as usize], max_plies]));
        }
        Value::Array(q)
    }
}

fn descent_scenario(name: &str, seed: u64, cyclic: bool, steps: usize, gate: bool) -> Value {
    let mut g = Gen { rng: Rng::new(seed), keys: Vec::new(), black: Vec::new(), status: Vec::new(), kids: Vec::new(), cyclic, gate };
    let mut ops = Vec::new();
    g.new_pos(false, Status::Unknown, &mut ops);
    let mut all = vec![json!({"ops": ops, "refresh": [], "q": []})];
    for _ in 0..steps {
        let mut step = g.step();
        let mut extra = Vec::new();
        g.value_ops(&mut extra);
        step["ops"].as_array_mut().unwrap().extend(extra);
        step["q"] = g.queries(96);
        all.push(step);
    }
    json!({"name": name, "max_plies": 64, "max_nodes": 50_000, "gate": gate, "steps": all})
}

fn descent_scenarios() -> Vec<Value> {
    let mut v = Vec::new();
    for s in 0..16u64 {
        v.push(descent_scenario(&format!("descent-dag-{s}"), 7_000 + s, false, 40, false));
    }
    for s in 0..12u64 {
        v.push(descent_scenario(&format!("descent-cyclic-{s}"), 8_000 + s, true, 40, false));
    }
    for s in 0..6u64 {
        v.push(descent_scenario(&format!("descent-gate-{s}"), 9_000 + s, s % 2 == 1, 30, true));
    }
    v
}

#[test]
#[ignore]
fn write_v1_descent_scenarios() {
    let path = format!("{DIR}/descent_scenarios.json");
    std::fs::write(&path, serde_json::to_string(&descent_scenarios()).unwrap()).unwrap();
    eprintln!("wrote {path}");
}

/// Replay one scenario; per step the row digest and the query answers.
fn replay_descents(sc: &Value) -> Vec<Value> {
    let mut g = MemGraph::new();
    let mut id_of: HashMap<String, PosId> = HashMap::new();
    let mut keys: Vec<u128> = Vec::new();
    let mut hex: Vec<String> = Vec::new();
    let mut live: HashSet<PosId> = HashSet::new();
    let campaign = Campaign::default_for(1, PosId(u64::MAX >> 30));
    let gate = GateConfig { enabled: sc["gate"].as_bool().unwrap_or(false) };
    let cfg = RefreshConfig {
        max_plies: sc["max_plies"].as_u64().unwrap() as u32,
        max_nodes: sc["max_nodes"].as_u64().unwrap(),
        gate,
        ..RefreshConfig::default()
    };
    let mut p = Proofs::new(vec![campaign.clone()], cfg);
    let mut out = Vec::new();
    for (si, step) in sc["steps"].as_array().unwrap().iter().enumerate() {
        for op in step["ops"].as_array().unwrap() {
            let op = op.as_array().unwrap();
            let st = |v: &Value| Status::from_v1(v.as_str().unwrap()).unwrap();
            let ev = |v: &Value| v.as_i64().map(|e| e as i16).unwrap_or(NO_CP);
            match op[0].as_str().unwrap() {
                "pos" => {
                    let k = op[1].as_str().unwrap();
                    let mut c = Core::default();
                    c.set_status(st(&op[3]), Default::default(), Default::default());
                    c.set_flag(F_BLACK_TO_MOVE, op[2].as_str() == Some("b"));
                    c.eval_cp = ev(&op[4]);
                    let fp = Fingerprint::from_hex(&k[..24]).unwrap();
                    let id = g.create(fp, op.get(5).and_then(|f| f.as_str()), c).unwrap();
                    id_of.insert(k.to_string(), id);
                    keys.push(key_order(k));
                    hex.push(k.to_string());
                }
                "edge" => {
                    let parent = id_of[op[1].as_str().unwrap()];
                    let child = id_of[op[3].as_str().unwrap()];
                    let mut es = Vec::new();
                    g.children(parent, &mut es);
                    es.push(Edge { child, mv: Move::parse(op[2].as_str().unwrap()).unwrap() });
                    g.set_children(parent, &es).unwrap();
                }
                "set" => {
                    let id = id_of[op[1].as_str().unwrap()];
                    let mut c = g.core(id);
                    c.set_status(st(&op[2]), Default::default(), Default::default());
                    c.eval_cp = ev(&op[3]);
                    g.put_core(id, c).unwrap();
                }
                "ana" => {
                    let id = id_of[op[1].as_str().unwrap()];
                    let blob = g.put_blob(serde_json::to_string(&op[2]).unwrap().as_bytes()).unwrap();
                    let e = av2_model::Ext { analysis: blob, ..g.ext(id).unwrap_or_default() };
                    g.put_ext(id, e).unwrap();
                    let mut c = g.core(id);
                    c.mate_in = op[3].as_u64().map(|m| m as u16).unwrap_or(av2_model::record::NO_MATE);
                    g.put_core(id, c).unwrap();
                }
                "val" => {
                    let id = id_of[op[1].as_str().unwrap()];
                    let mut c = g.core(id);
                    c.backed_eval = ev(&op[2]);
                    c.backed_move = op[3].as_str().map(|m| Move::parse(m).unwrap()).unwrap_or(Move::NONE);
                    c.backed_plies = op[4].as_u64().unwrap() as u16;
                    g.put_core(id, c).unwrap();
                    let e = av2_model::Ext {
                        nodes_invested: op[5].as_u64().unwrap(),
                        backed_nodes: op[6].as_u64().unwrap(),
                        ..g.ext(id).unwrap_or_default()
                    };
                    g.put_ext(id, e).unwrap();
                }
                "task" => {
                    live.insert(id_of[op[1].as_str().unwrap()]);
                }
                "untask" => {
                    live.remove(&id_of[op[1].as_str().unwrap()]);
                }
                other => panic!("{other}"),
            }
        }
        let seeds: Vec<PosId> = step["refresh"].as_array().unwrap().iter().map(|k| id_of[k.as_str().unwrap()]).collect();
        let view = KeyOrdered { graph: &g, keys: &keys };
        p.refresh(&view, &seeds, si as u64, 0.0);
        let t = p.table(1).unwrap();
        let mut rows: Vec<(String, Value)> = t
            .ids()
            .map(|id| {
                let r = t.get(id).unwrap();
                let sel = if r.selected.is_none() { Value::Null } else { json!(r.selected.to_uci()) };
                (hex[id.index()].clone(), json!([hex[id.index()], r.pn, r.dn, r.expanded, sel]))
            })
            .collect();
        rows.sort_by(|a, b| a.0.cmp(&b.0));
        let rows = Value::Array(rows.into_iter().map(|r| r.1).collect());
        let mut answers = Vec::new();
        for q in step["q"].as_array().unwrap() {
            let q = q.as_array().unwrap();
            let counter = q[1].as_u64().unwrap();
            let avoid: HashSet<PosId> = q[2].as_array().unwrap().iter().map(|k| id_of[k.as_str().unwrap()]).collect();
            let start = id_of[q[3].as_str().unwrap()];
            let dcfg = DescentConfig { max_plies: q[4].as_u64().unwrap() as u32, gate, saturated_nodes: 128_000_000 };
            let w = Walker::new(&view, t, &campaign, dcfg);
            let a = if q[0] == "pn" {
                let (pos, plies) = w.descend(counter, &|x| avoid.contains(&x), Some(start));
                json!([pos.map(|x| hex[x.index()].clone()), plies])
            } else {
                match w.descend_value(counter, &|x| avoid.contains(&x), &|x| live.contains(&x), Some(start)) {
                    None => Value::Null,
                    Some(v) => json!([hex[v.pos.index()], v.plies, v.arm.v1(), v.floor_nodes, v.line_plies]),
                }
            };
            answers.push(a);
        }
        out.push(json!({"sha": digest(&rows), "a": answers}));
    }
    out
}

#[test]
fn descents_match_v1() {
    let v1_path = format!("{DIR}/v1_descent_results.json");
    let v1: Vec<Value> = serde_json::from_str(&std::fs::read_to_string(&v1_path).expect("v1 golden file")).unwrap();
    let sc = descent_scenarios();
    assert_eq!(sc.len(), v1.len(), "regenerate the golden file (see the module doc)");
    let (mut queries, mut found) = (0, 0);
    for (s, want) in sc.iter().zip(&v1) {
        assert_eq!(s["name"], want["name"]);
        let name = s["name"].as_str().unwrap();
        let got = replay_descents(s);
        let want_steps = want["steps"].as_array().unwrap();
        assert_eq!(got.len(), want_steps.len());
        for (i, (g, w)) in got.iter().zip(want_steps).enumerate() {
            assert_eq!(g["sha"], w["sha"], "{name} step {i}: proof rows differ");
            let (ga, wa) = (g["a"].as_array().unwrap(), w["a"].as_array().unwrap());
            assert_eq!(ga.len(), wa.len());
            for (j, (x, y)) in ga.iter().zip(wa).enumerate() {
                assert_eq!(x, y, "{name} step {i} query {j}: {}", s["steps"][i]["q"][j]);
                queries += 1;
                found += usize::from(!x.is_null() && !x[0].is_null());
            }
        }
    }
    eprintln!("v1 descent differential: {} scenarios, {queries} queries ({found} with a target)", sc.len());
}
