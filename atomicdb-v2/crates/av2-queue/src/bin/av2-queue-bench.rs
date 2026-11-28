//! av2-queue-bench: lease latency and selector speed on a real slice.
//!
//! ```text
//! cargo run --release -p av2-queue --features bench --bin av2-queue-bench -- STORE_DIR [--queue N,N,...] [--leases N]
//! ```
//!
//! `STORE_DIR` is a store built by `av2-load` from the production slice.
//! Prints one JSON line per measurement.

use av2_model::{GraphRead, PosId, Rng, Status};
use av2_queue::consts::*;
use av2_queue::selector::*;
use av2_queue::*;
use std::collections::{BTreeSet, HashMap};
use std::time::Instant;

fn pct(v: &mut [f64], p: f64) -> f64 {
    v.sort_by(|a, b| a.total_cmp(b));
    if v.is_empty() {
        return 0.0;
    }
    v[((v.len() - 1) as f64 * p).round() as usize]
}

/// A stand-in for the df-pn / value descent of `av2-proof` (rules-pn): walk
/// from the root along the child that is best for the side to move
/// (`backed ?? eval`, unknown children first, ties by edge order) until an
/// open position that is not expanded, skipping positions already picked.
/// Same read pattern (depth x branching core reads) as a real descent.
struct GreedyDescent<'a, G: GraphRead + ?Sized> {
    g: &'a G,
    root: PosId,
    max_plies: usize,
}

impl<G: GraphRead + ?Sized> Descent for GreedyDescent<'_, G> {
    fn active_campaigns(&self) -> Vec<u64> {
        vec![1]
    }
    fn campaign_roots(&self) -> Vec<(u64, f64)> {
        Vec::new()
    }
    fn descend(&self, _c: u64, counter: u64, avoid: &BTreeSet<u64>, _start: Option<u64>) -> Option<u64> {
        let mut at = self.root;
        let mut kids = Vec::new();
        for ply in 0..self.max_plies {
            let core = self.g.core(at);
            if core.status() != Status::Unknown {
                return None;
            }
            kids.clear();
            self.g.children(at, &mut kids);
            // Not expanded, or expanded beyond the slice: a leaf.
            if !core.expanded() || kids.is_empty() {
                return (!avoid.contains(&at.0)).then_some(at.0);
            }
            let white = !core.black_to_move();
            let mut best: Option<(i32, usize, PosId)> = None;
            for (i, e) in kids.iter().enumerate() {
                let c = self.g.core(e.child);
                if c.status() != Status::Unknown || avoid.contains(&e.child.0) && !c.expanded() {
                    continue;
                }
                // Rotate the tie-break with the counter so descents spread.
                let v = c.backed().or(c.eval()).map(i32::from).unwrap_or(0);
                let score = if white { v } else { -v };
                let rank = (i + counter as usize + ply) % kids.len();
                if best.map(|(s, r, _)| (score, std::cmp::Reverse(rank)) > (s, std::cmp::Reverse(r))).unwrap_or(true) {
                    best = Some((score, rank, e.child));
                }
            }
            match best {
                Some((_, _, c)) => at = c,
                None => return None,
            }
        }
        None
    }
    fn descend_value(
        &self,
        c: u64,
        counter: u64,
        avoid: &BTreeSet<u64>,
        start: Option<u64>,
        _live: &dyn Fn(u64) -> bool,
    ) -> Option<ValueTarget> {
        self.descend(c, counter, avoid, start).map(|pos| ValueTarget { pos, arm: av2_queue::budget::ValueArm::Spine, floor_nodes: None })
    }
    fn proved_or_clamp(&self, _pos: u64, _c: &[u64]) -> Option<(u64, u32)> {
        None
    }
}

fn arg(args: &[String], name: &str) -> Option<String> {
    args.iter().position(|a| a == name).and_then(|i| args.get(i + 1).cloned())
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let dir = std::path::PathBuf::from(args.get(1).expect("usage: av2-queue-bench STORE_DIR"));
    let sizes: Vec<usize> =
        arg(&args, "--queue").unwrap_or_else(|| "1000,10000,50000".into()).split(',').map(|s| s.parse().unwrap()).collect();
    let leases: usize = arg(&args, "--leases").map(|s| s.parse().unwrap()).unwrap_or(2000);
    let cfg = av2_store::Config { background_checkpoint: false, ..Default::default() };
    let store = av2_store::Store::open(&dir, cfg).expect("open store");
    let reader = store.reader();
    reader.read(|view| {
        let n = view.len();
        let root_key = arg(&args, "--root-key").unwrap_or_else(|| "b1791d7fc9ae3d38966568c2".into());
        let root = av2_model::Fingerprint::from_hex(&root_key[..24]).ok().and_then(|fp| view.lookup(&fp)).unwrap_or(PosId(0));
        let open: Vec<u64> = (0..n).filter(|&i| view.core(PosId(i)).status() == Status::Unknown).collect();
        let mut kids = Vec::new();
        view.children(root, &mut kids);
        let rc = view.core(root);
        println!(
            "{{\"stage\":\"slice\",\"positions\":{n},\"open\":{},\"root_children\":{},\"root_expanded\":{},\"root_status\":\"{}\"}}",
            open.len(),
            kids.len(),
            rc.expanded(),
            rc.status().v1()
        );

        // ---------------- selector: regret pass (PRI-2.1 + PRI-1.5, in memory, D5)
        let _warm = regret_from_root(view, root);
        let t = Instant::now();
        let regret = regret_from_root(view, root);
        let dijkstra = t.elapsed().as_secs_f64();
        let reached = regret.iter().filter(|r| r.is_finite()).count();
        let t = Instant::now();
        let sel = RegretSelector::compute(view, root, &Votes::new(), &BTreeSet::new());
        let pass = t.elapsed().as_secs_f64();
        println!(
            "{{\"stage\":\"regret_pass\",\"dijkstra_s\":{dijkstra:.3},\"full_pass_s\":{pass:.3},\"reached\":{reached},\"ranked\":{}}}",
            sel.ranked.len()
        );

        // ---------------- stored v1 priorities (the exact AUTO/FILL order)
        let mut stored: HashMap<u64, f64> = HashMap::new();
        let (mut dead, mut nonzero) = (0usize, 0usize);
        if let Some(csv) = arg(&args, "--pos-csv") {
            let text = std::fs::read_to_string(&csv).expect("read --pos-csv");
            let mut lines = text.lines();
            let header: Vec<&str> = lines.next().unwrap().split(',').collect();
            let (ki, pi) = (header.iter().position(|h| *h == "key").unwrap(), header.iter().position(|h| *h == "priority").unwrap());
            for line in lines {
                let cols: Vec<&str> = line.split(',').collect();
                let p: f64 = cols[pi].parse().unwrap_or(0.0);
                if p == 0.0 {
                    continue;
                }
                let Ok(fp) = av2_model::Fingerprint::from_hex(&cols[ki][..24]) else { continue };
                if let Some(id) = view.lookup(&fp) {
                    if p <= DEAD / 2.0 {
                        dead += 1;
                    } else {
                        nonzero += 1;
                    }
                    stored.insert(id.0, p);
                }
            }
            println!("{{\"stage\":\"stored_priority\",\"non_zero_live\":{nonzero},\"tombstones\":{dead},\"positions\":{n}}}");
        }

        // ---------------- selector: descents and minting plans (PRI-4.4)
        // Without --pos-csv: the loader's v1prio.col.
        let file = V1Priority::open(&dir).expect("v1prio.col");
        if stored.is_empty() {
            println!("{{\"stage\":\"stored_priority_file\",\"non_zero\":{}}}", file.non_zero());
        }
        let prio: &dyn StoredPriority = if stored.is_empty() { &file } else { &stored };
        let env = GraphEnv { graph: view, priority: Some(prio) };
        let descent = GreedyDescent { g: view, root, max_plies: 64 };
        let avoid = BTreeSet::new();
        let t = Instant::now();
        let mut found = 0;
        let rounds = 20_000u64;
        for c in 0..rounds {
            if descent.descend(1, c, &avoid, None).is_some() {
                found += 1;
            }
        }
        let secs = t.elapsed().as_secs_f64();
        println!(
            "{{\"stage\":\"descents\",\"descents\":{rounds},\"found\":{found},\"per_s\":{:.0},\"us_each\":{:.2}}}",
            rounds as f64 / secs,
            secs * 1e6 / rounds as f64
        );
        let q = QueueState::new(QueueConfig::production_known());
        let mut pn = PnSelector { descent: &descent };
        let t = Instant::now();
        let plans = 2000;
        let mut tasks = 0;
        for i in 0..plans {
            let plan = pn.next_tasks(&q, &env, TASK_REFILL_COUNT, T0 + i);
            tasks += plan.tasks.len();
        }
        let secs = t.elapsed().as_secs_f64();
        println!(
            "{{\"stage\":\"refill_plans\",\"plans\":{plans},\"n\":{TASK_REFILL_COUNT},\"tasks\":{tasks},\"per_s\":{:.0},\"us_each\":{:.1}}}",
            plans as f64 / secs,
            secs * 1e6 / plans as f64
        );
        let mut regret_sel = sel.clone();
        let t = Instant::now();
        for i in 0..plans {
            regret_sel.next_tasks(&q, &env, TASK_REFILL_COUNT, T0 + i);
        }
        let secs = t.elapsed().as_secs_f64();
        println!("{{\"stage\":\"regret_refill_plans\",\"plans\":{plans},\"us_each\":{:.1}}}", secs * 1e6 / plans as f64);

        // ---------------- lease latency at several queue sizes
        for &size in &sizes {
            let mut rng = Rng::new(size as u64);
            let mut q = QueueState::new(QueueConfig::production_known());
            let accounts: Vec<String> = (0..50).map(|i| if i == 0 { String::new() } else { format!("acct{i}") }).collect();
            let mut next = 1u64;
            let mut used = BTreeSet::new();
            let mut pick_pos = |rng: &mut Rng| loop {
                let p = open[rng.below(open.len() as u64) as usize];
                if used.insert(p) {
                    return p;
                }
            };
            // Production shape: a pool of 64 AUTO, some FILL, `size` USER requests
            // with a skewed account distribution, a few backed and background rows.
            for _ in 0..64 {
                let p = pick_pos(&mut rng);
                q.import_task(Task::new(next, p, env.visits(p), B128, T0));
                next += 1;
            }
            for _ in 0..size {
                let p = pick_pos(&mut rng);
                let mut t = Task::new(next, p, env.visits(p), [B128, B128, B128, B512, B2][rng.below(5) as usize], T0 + next as i64);
                t.source = Source::User;
                let a = (rng.below(50) * rng.below(50) / 50) as usize; // skewed
                t.requested_by = accounts[a].clone();
                if rng.chance(0.05) {
                    t.also = vec![accounts[(a + 1) % 50].clone()].into_iter().filter(|x| !x.is_empty() && *x != t.requested_by).collect();
                }
                t.background = rng.chance(0.2);
                q.import_task(t);
                next += 1;
            }
            for k in 0..100 {
                q.import_ping(&format!("w{k}#0"), "worker", Ping { last_seen: T0, threads: 8, ..Default::default() });
            }
            let mut now = T0 + 10 * MINUTE;
            let mut lat = Vec::with_capacity(leases);
            let mut fact_bytes = 0usize;
            let mut held: Vec<(u64, String, String)> = Vec::new();
            for i in 0..leases {
                now += 50;
                let machine = format!("w{}#0", i % 100);
                // Keep ~100 slots busy: complete the oldest lease of this slot first.
                if let Some(pos) = held.iter().position(|h| h.1 == machine) {
                    let (id, m, tok) = held.remove(pos);
                    let r = SubmitReq { now, account: "worker".into(), machine: m, task: id, token: tok, nodes: B128, elapsed: 60.0, lines: "[]".into(), ..Default::default() };
                    q.apply(&Input::Submit(r), &env, &mut NoRefill);
                }
                let token = format!("{:043}", i);
                let r = LeaseReq {
                    now,
                    account: "worker".into(),
                    machine: machine.clone(),
                    telemetry: Telemetry { threads: 8, ..Default::default() },
                    build: LEASE_TOKEN_BUILD,
                    session: format!("s{i}"),
                    token: token.clone(),
                    max_budget: None,
                    tb: true,
                };
                let input = Input::Lease(r);
                let t = Instant::now();
                let applied = q.apply(&input, &env, &mut NoRefill);
                lat.push(t.elapsed().as_secs_f64() * 1e6);
                fact_bytes += av2_facts::encode_all(&[av2_queue::fact::record(&input, &applied)]).len();
                if let Output::Lease(LeaseOut::Task { grant }) = applied.output {
                    held.push((grant.id, machine, token));
                }
            }
            let mean = lat.iter().sum::<f64>() / lat.len() as f64;
            let (p50, p99, max) = (pct(&mut lat, 0.5), pct(&mut lat, 0.99), pct(&mut lat, 1.0));
            println!(
                "{{\"stage\":\"lease\",\"user_requests\":{size},\"leases\":{leases},\"mean_us\":{mean:.1},\"p50_us\":{p50:.1},\"p99_us\":{p99:.1},\"max_us\":{max:.1},\"fact_bytes_per_lease\":{}}}",
                fact_bytes / leases
            );
            // Snapshot and replay at this size.
            let t = Instant::now();
            let snap = q.snapshot();
            let snap_s = t.elapsed().as_secs_f64();
            let t = Instant::now();
            let back = QueueState::restore(&snap).unwrap();
            let restore_s = t.elapsed().as_secs_f64();
            assert_eq!(back.retained_count(), q.retained_count());
            println!(
                "{{\"stage\":\"snapshot\",\"tasks\":{},\"bytes\":{},\"write_ms\":{:.1},\"restore_ms\":{:.1}}}",
                q.retained_count(),
                snap.len(),
                snap_s * 1e3,
                restore_s * 1e3
            );
            // queue_ahead (the estimator a request answers with).
            let probe: Vec<u64> = q.tasks().filter(|t| t.state == TState::Pending && t.source == Source::User).map(|t| t.id).take(20).collect();
            let t = Instant::now();
            for id in &probe {
                q.queue_ahead(*id, now, &env);
            }
            println!("{{\"stage\":\"queue_ahead\",\"user_requests\":{size},\"us_each\":{:.1}}}", t.elapsed().as_secs_f64() * 1e6 / probe.len().max(1) as f64);
        }
    });
}

const B128: u64 = 128_000_000;
const B512: u64 = 512_000_000;
const B2: u64 = 2_000_000_000;
const T0: Ms = 1_800_000_000_000;
