//! Differential test against v1's own `atomicdb/proof.py`.
//!
//! `write_v1_scenarios` (ignored) generates random scenarios: growing proof
//! trees (acyclic and cyclic, with transpositions, closures, eval changes),
//! each step a set of graph changes followed by `refresh_proof_numbers(seeds)`,
//! some with v1's caps lowered so that `PROOF_GUARD` paths run.
//! `v1diff/run_v1.py` replays them with v1's code on SQLite and records every
//! `ProofNode` row and guard event after every step (`v1diff/v1_results.json`,
//! committed).  `matches_v1_golden` replays the scenarios here and requires
//! the same rows, the same `written` count and the same guard events at every
//! step.
//!
//! The scenarios are generated from fixed seeds (`av2_model::Rng`), so only
//! v1's answers are committed: per step the `written` count, the guard events,
//! the row count and a sha256 prefix of the canonical rows JSON.
//!
//! Regenerate: `cargo test -p av2-proof write_v1_scenarios -- --ignored`
//! (writes `v1diff/scenarios.json`, not committed), then from the repository
//! root `python atomicdb-v2/crates/av2-proof/v1diff/run_v1.py
//! atomicdb-v2/crates/av2-proof/v1diff/scenarios.json
//! atomicdb-v2/crates/av2-proof/v1diff/v1_results.json` (add `--full` to keep
//! every row, which this test then compares row by row).

use crate::campaign::Campaign;
use crate::graph::KeyOrdered;
use crate::maintain::Proofs;
use crate::refresh::{ProofEvent, RefreshConfig};
use av2_model::record::{F_BLACK_TO_MOVE, NO_CP};
use av2_model::{Core, Edge, Fingerprint, GraphRead, GraphWrite, Move, PosId, Rng, Status};
use av2_store::mem::MemGraph;
use serde_json::{Value, json};
use std::collections::HashMap;

const DIR: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/v1diff");

/// The same trees, with the descents (PN-6.5, PN-9.11) queried after every step.
mod descent;

fn status_v1(s: Status) -> &'static str {
    s.v1()
}

/// Board fields of different material (32, 24, 16, 12, 8, 5 men) for the
/// solve-gate scenarios.
const BOARDS: [&str; 6] = [
    "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR",
    "r1bqk2r/pppp1ppp/2n2n2/4p3/4P3/2N2N2/PPPP1PPP/R1BQK2R",
    "4k3/pppp1ppp/8/8/8/8/PPPP1PPP/4K3",
    "4k3/pp4pp/8/8/8/8/PP4PP/3RK2R",
    "4k3/p6p/8/8/8/8/P6P/3RK2R",
    "8/8/8/4k3/8/8/4K3/4R3",
];
const PV_MOVES: [&str; 14] =
    ["e2e4", "d7d5", "e4d5", "d8d5", "g1f3", "g8f6", "f3g1", "f6g8", "d1d2", "e8e7", "a2a4", "h7h5", "e7e8q", "b1c3"];

/// Scenario generator: a random proof tree that grows by landings.
struct Gen {
    rng: Rng,
    keys: Vec<String>,
    black: Vec<bool>,
    status: Vec<Status>,
    kids: Vec<Vec<(u64, String)>>,
    cyclic: bool,
    gate: bool,
}

impl Gen {
    /// A `last_analysis` list: some lines, maybe a prior-pass line first, maybe a mate.
    fn lines(&mut self) -> Value {
        let mut v = Vec::new();
        if self.rng.chance(0.2) {
            v.push(json!({"pv": ["g1f3", "g8f6"], "prior_pass": true}));
        }
        for _ in 0..1 + self.rng.below(6) {
            let len = self.rng.below(40) as usize;
            let pv: Vec<&str> = (0..len).map(|_| PV_MOVES[self.rng.below(PV_MOVES.len() as u64) as usize]).collect();
            let mut l = json!({"eval_cp": self.rng.below(2_000) as i64 - 1_000, "pv": pv});
            if self.rng.chance(0.1) {
                l["mate"] = json!(self.rng.below(20) as i64 - 10);
            }
            v.push(l);
        }
        Value::Array(v)
    }
    fn key(&mut self) -> String {
        let mut s = String::with_capacity(64);
        for _ in 0..4 {
            s.push_str(&format!("{:016x}", self.rng.next_u64()));
        }
        s
    }
    fn eval(&mut self) -> Value {
        if self.rng.chance(0.2) { Value::Null } else { json!(self.rng.below(4_000) as i64 - 2_000) }
    }
    fn new_pos(&mut self, black: bool, status: Status, ops: &mut Vec<Value>) -> u64 {
        let key = self.key();
        let ev = self.eval();
        if self.gate {
            let board = BOARDS[self.rng.below(BOARDS.len() as u64) as usize];
            let fen = format!("{board} {} - - 0 1", if black { "b" } else { "w" });
            ops.push(json!(["pos", key, if black { "b" } else { "w" }, status_v1(status), ev, fen]));
            if self.rng.chance(0.6) {
                let lines = self.lines();
                let mate = if self.rng.chance(0.1) { json!(self.rng.below(30) + 1) } else { Value::Null };
                ops.push(json!(["ana", key, lines, mate]));
            }
        } else {
            ops.push(json!(["pos", key, if black { "b" } else { "w" }, status_v1(status), ev]));
        }
        self.keys.push(key);
        self.black.push(black);
        self.status.push(status);
        self.kids.push(Vec::new());
        (self.keys.len() - 1) as u64
    }
    fn step(&mut self) -> Value {
        let mut ops = Vec::new();
        let n = self.keys.len() as u64;
        let mut seeds: Vec<String> = Vec::new();
        if self.rng.chance(0.15) && n > 3 {
            // A verdict or an eval change on an existing node.
            let id = self.rng.below(n) as usize;
            let st = if self.rng.chance(0.5) {
                [Status::WhiteWin, Status::BlackWin, Status::Draw][self.rng.below(3) as usize]
            } else {
                Status::Unknown
            };
            self.status[id] = st;
            let ev = self.eval();
            ops.push(json!(["set", self.keys[id], status_v1(st), ev]));
            seeds.push(self.keys[id].clone());
        } else {
            let mut tries = 0;
            let p = loop {
                let id = self.rng.below(n) as usize;
                tries += 1;
                if (self.status[id] == Status::Unknown && self.kids[id].is_empty()) || tries > 64 {
                    break id;
                }
            };
            let ev = self.eval();
            ops.push(json!(["set", self.keys[p], status_v1(self.status[p]), ev]));
            if self.gate && self.rng.chance(0.7) {
                let lines = self.lines();
                ops.push(json!(["ana", self.keys[p], lines, Value::Null]));
            }
            seeds.push(self.keys[p].clone());
            let k = 1 + self.rng.below(6);
            for i in 0..k {
                let uci = Move::new(i as u8, i as u8 + 20, 0).to_uci();
                if self.kids[p].iter().any(|(_, m)| *m == uci) {
                    continue;
                }
                let child = if self.rng.chance(0.15) && n > 2 {
                    let c = self.rng.below(n);
                    let ok = self.black[c as usize] != self.black[p]
                        && (self.cyclic || c > p as u64)
                        && c != p as u64
                        && !self.kids[p].iter().any(|(x, _)| *x == c);
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
                    let c = self.new_pos(!self.black[p], st, &mut ops);
                    if st != Status::Unknown {
                        seeds.push(self.keys[c as usize].clone());
                    }
                    c
                };
                ops.push(json!(["edge", self.keys[p], uci, self.keys[child as usize]]));
                self.kids[p].push((child, uci));
            }
        }
        json!({"ops": ops, "refresh": seeds})
    }
}

fn scenario(name: &str, seed: u64, cyclic: bool, steps: usize, max_plies: u32, max_nodes: u64) -> Value {
    let gate = name.starts_with("gate");
    let mut g = Gen { rng: Rng::new(seed), keys: Vec::new(), black: Vec::new(), status: Vec::new(), kids: Vec::new(), cyclic, gate };
    let mut ops = Vec::new();
    g.new_pos(false, Status::Unknown, &mut ops);
    let mut all = vec![json!({"ops": ops, "refresh": []})];
    for _ in 0..steps {
        all.push(g.step());
    }
    json!({"name": name, "max_plies": max_plies, "max_nodes": max_nodes, "gate": gate, "steps": all})
}

fn scenarios() -> Vec<Value> {
    let mut v = Vec::new();
    for s in 0..24u64 {
        v.push(scenario(&format!("dag-{s}"), 1_000 + s, false, 60, 64, 50_000));
    }
    for s in 0..24u64 {
        v.push(scenario(&format!("cyclic-{s}"), 2_000 + s, true, 60, 64, 50_000));
    }
    for s in 0..8u64 {
        v.push(scenario(&format!("capped-nodes-{s}"), 3_000 + s, s % 2 == 1, 50, 64, 7));
        v.push(scenario(&format!("capped-plies-{s}"), 4_000 + s, s % 2 == 1, 50, 3, 50_000));
    }
    for s in 0..12u64 {
        v.push(scenario(&format!("gate-{s}"), 5_000 + s, s % 3 == 2, 50, 64, 50_000));
    }
    v
}

#[test]
#[ignore]
fn write_v1_scenarios() {
    let path = format!("{DIR}/scenarios.json");
    std::fs::write(&path, serde_json::to_string(&scenarios()).unwrap()).unwrap();
    eprintln!("wrote {path}");
}

fn key_order(hex: &str) -> u128 {
    u128::from_str_radix(&hex[..32], 16).unwrap()
}

/// Replay one scenario on av2-proof; return per step `(written, guards, rows)`
/// in v1's JSON shape.
fn replay(sc: &Value) -> Vec<Value> {
    let mut g = MemGraph::new();
    let mut id_of: HashMap<String, PosId> = HashMap::new();
    let mut keys: Vec<u128> = Vec::new();
    let mut hex: Vec<String> = Vec::new();
    let campaign = Campaign::default_for(1, PosId(u64::MAX >> 30));
    let cfg = RefreshConfig {
        max_plies: sc["max_plies"].as_u64().unwrap() as u32,
        max_nodes: sc["max_nodes"].as_u64().unwrap(),
        gate: crate::gate::GateConfig { enabled: sc["gate"].as_bool().unwrap_or(false) },
        ..RefreshConfig::default()
    };
    let mut p = Proofs::new(vec![campaign], cfg);
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
                other => panic!("{other}"),
            }
        }
        let seeds: Vec<PosId> = step["refresh"].as_array().unwrap().iter().map(|k| id_of[k.as_str().unwrap()]).collect();
        let view = KeyOrdered { graph: &g, keys: &keys };
        let rep = p.refresh(&view, &seeds, si as u64, 0.0);
        let guards: Vec<Value> = rep
            .events
            .iter()
            .filter_map(|e| match e {
                ProofEvent::Guard { campaign, reason, processed, plies, seed_count } => Some(json!({
                    "campaign": campaign, "reason": reason.v1(), "processed": processed, "plies": plies, "seed_count": seed_count
                })),
                _ => None,
            })
            .collect();
        let disagree: Vec<Value> = rep
            .events
            .iter()
            .filter_map(|e| match e {
                ProofEvent::SolveGateDisagree { position, eval_cp, annoyance, factor } => {
                    Some(json!({"key": hex[position.index()], "eval_cp": eval_cp, "annoyance": annoyance, "factor": factor}))
                }
                _ => None,
            })
            .collect();
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
        out.push(json!({"written": rep.written, "guards": guards, "disagree": disagree, "rows": rows.into_iter().map(|r| r.1).collect::<Vec<_>>()}));
    }
    out
}

fn digest(rows: &Value) -> String {
    use sha2::{Digest, Sha256};
    let h = Sha256::digest(serde_json::to_string(rows).unwrap().as_bytes());
    h.iter().take(8).map(|b| format!("{b:02x}")).collect()
}

#[test]
fn matches_v1_golden() {
    let v1_path = format!("{DIR}/v1_results.json");
    let v1: Vec<Value> = serde_json::from_str(&std::fs::read_to_string(&v1_path).expect("v1 golden file")).unwrap();
    let sc = scenarios();
    assert_eq!(sc.len(), v1.len(), "regenerate the golden file (see the module doc)");
    let mut steps = 0;
    let mut rows = 0;
    for (s, want) in sc.iter().zip(&v1) {
        assert_eq!(s["name"], want["name"]);
        let got = replay(s);
        let want_steps = want["steps"].as_array().unwrap();
        assert_eq!(got.len(), want_steps.len());
        for (i, (g, w)) in got.iter().zip(want_steps).enumerate() {
            let name = s["name"].as_str().unwrap();
            assert_eq!(g["written"], w["written"], "{name} step {i}: written");
            assert_eq!(g["guards"], w["guards"], "{name} step {i}: guards");
            assert_eq!(g["disagree"], w["disagree"], "{name} step {i}: SOLVE_GATE_DISAGREE events");
            let n = g["rows"].as_array().unwrap().len();
            if let Some(wr) = w.get("rows")
                && g["rows"] != *wr
            {
                let (ga, wa) = (g["rows"].as_array().unwrap(), wr.as_array().unwrap());
                let first = ga.iter().zip(wa).find(|(a, b)| a != b);
                panic!("{name} step {i}: rows differ ({} vs {} rows), first: {first:?}", ga.len(), wa.len());
            }
            assert_eq!(n as u64, w["n"].as_u64().unwrap(), "{name} step {i}: row count");
            assert_eq!(digest(&g["rows"]), w["sha"].as_str().unwrap(), "{name} step {i}: rows differ (rerun run_v1.py with --full)");
            steps += 1;
            rows += n;
        }
    }
    eprintln!("v1 differential: {} scenarios, {steps} steps, {rows} rows compared", sc.len());
}
