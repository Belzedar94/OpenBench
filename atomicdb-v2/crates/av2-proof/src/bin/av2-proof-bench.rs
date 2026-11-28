//! av2-proof-bench: proof numbers and priority on a loaded v2 store.
//!
//! ```text
//! av2-proof-bench --src STORE --work DIR [--b1 2000] [--b32 200] [--seed 7] [--json out.jsonl] [--id-order]
//! ```
//!
//! `--src` (a store built by `av2-load` from the production slice) is copied
//! to `--work` with an online backup; everything runs on the copy.
//!
//! 1. `rebuild`: proof numbers of the default campaign from scratch over the
//!    tree below the root (the startup cost of D5, and the cost per commit of
//!    the lazy alternative: recompute on read, cached per epoch).
//! 2. `priority`: the global regret walk and the formula over every open
//!    position (PRI-2.1, PRI-3.3).
//! 3. `writer_b1` / `writer_b32`: the writer-maintained alternative.  Each
//!    batch lands 1 or 32 searches on unexpanded open positions (20-38
//!    children each, as `av2-perf`), commits with fsync, then runs the PN-8
//!    hook (`Proofs::after_batch`, one refresh call per landing).  The refresh
//!    time per batch is what the writer pays.
//! 4. `consistency`: the maintained table against a rebuild of the final
//!    graph (where they differ and why).
//! 5. `memory`: the table's bytes, per row, and the extrapolation to
//!    production's 39.4M rows on 206M positions.
//!
//! Level order: v1's key order (`KeyOrdered`) unless `--id-order`.

use av2_model::record::F_EXPANDED;
use av2_model::{Edge, GraphRead, GraphWrite, Move, PosId, Rng, Status};
use av2_proof::campaign::Campaign;
use av2_proof::gate::GateConfig;
use av2_proof::graph::{KeyOrdered, fingerprint_order};
use av2_proof::maintain::Proofs;
use av2_proof::rebuild::{Scope, rebuild};
use av2_proof::refresh::RefreshConfig;
use av2_proof::{frontier, priority};
use av2_store::testkit::{random_fen, random_fp};
use av2_store::{Batch, Config, Store};
use av2_tools::{Args, Lat, die, emit};
use std::time::Instant;

fn cfg() -> Config {
    Config { ckpt_bytes: 256 << 20, ..Config::default() }
}

/// One landing (shape of `av2-perf`): expand an unexpanded open position.
fn landing(b: &mut Batch, rng: &mut Rng, keys: &mut Vec<u128>) -> PosId {
    let n = b.len();
    let mut es = Vec::new();
    let p = loop {
        let id = PosId(rng.below(n));
        es.clear();
        b.children(id, &mut es);
        if es.is_empty() && b.core(id).status() == Status::Unknown {
            break id;
        }
    };
    let k = 20 + rng.below(19) as usize;
    let mut block = Vec::with_capacity(k);
    let black = !b.core(p).black_to_move();
    for i in 0..k {
        let fen = random_fen(rng);
        let fp = random_fp(rng);
        let mut core = av2_model::Core { eval_cp: (rng.below(1200) as i16) - 600, ..Default::default() };
        core.set_flag(av2_model::record::F_BLACK_TO_MOVE, black);
        if rng.chance(0.02) {
            core.set_status(
                [Status::WhiteWin, Status::BlackWin, Status::Draw][rng.below(3) as usize],
                Default::default(),
                Default::default(),
            );
        }
        let c = b.create(fp, Some(&fen), core).unwrap();
        debug_assert_eq!(c.index(), keys.len());
        keys.push(fingerprint_order(&fp));
        block.push(Edge { child: c, mv: Move::new((i / 8) as u8, (i % 8 + 16) as u8, 0) });
    }
    b.set_children(p, &block).unwrap();
    let mut core = b.core(p);
    core.set_flag(F_EXPANDED, true);
    core.eval_cp = (rng.below(1200) as i16) - 600;
    b.put_core(p, core).unwrap();
    p
}

fn main() {
    let a = Args::parse();
    let src = a.path("src");
    let work = a.path("work");
    let out = a.get("json").map(std::path::PathBuf::from);
    let b1 = a.n("b1", 2000);
    let b32 = a.n("b32", 200);
    let id_order = a.flag("id-order");
    let mut rng = Rng::new(a.n("seed", 7));

    if work.exists() {
        std::fs::remove_dir_all(&work).unwrap_or_else(|e| die(&e.to_string()));
    }
    {
        let s0 = Store::open(&src, cfg()).unwrap_or_else(|e| die(&e.to_string()));
        s0.backup(&work).unwrap_or_else(|e| die(&e.to_string()));
        s0.close().unwrap();
    }
    let mut s = Store::open(&work, cfg()).unwrap_or_else(|e| die(&e.to_string()));
    let reader = s.reader();
    let mut keys: Vec<u128> = reader.read(|v| {
        let mut pairs = Vec::new();
        v.key_pairs(&mut pairs);
        let mut k = vec![0u128; v.len() as usize];
        for (fp, id) in pairs {
            k[id.index()] = fingerprint_order(&fp);
        }
        k
    });
    if id_order {
        keys = (0..keys.len() as u128).collect();
    }
    // The start position is not id 0 in the slice: it has an incoming edge
    // (1.Nf3 Nf6 2.Ng1 Ng8 transposes back to it), so it is looked up.
    let root_fp = av2_model::Fingerprint::from_hex(&a.s("root", "b1791d7fc9ae3d38966568c2")).unwrap_or_else(|e| die(&e.to_string()));
    let root = reader.read(|v| v.lookup(&root_fp)).unwrap_or_else(|| die("root not in the store"));
    let campaign = Campaign::default_for(1, root);
    let gate = GateConfig::default();
    let positions = keys.len();

    // 1. rebuild
    let t = Instant::now();
    let (table, rep) = reader.read(|v| rebuild(&KeyOrdered { graph: v, keys: &keys }, &campaign, gate, Scope::Below(root), 0));
    let rebuild_s = t.elapsed().as_secs_f64();
    let t = Instant::now();
    let (_all, rep_all) = reader.read(|v| rebuild(&KeyOrdered { graph: v, keys: &keys }, &campaign, gate, Scope::All, 0));
    let rebuild_all_s = t.elapsed().as_secs_f64();
    let root_row = table.get(root).unwrap();
    emit(
        &out,
        &format!(
            "{{\"stage\":\"rebuild\",\"positions\":{positions},\"rows\":{},\"explicit_rows\":{},\"computations\":{},\"cyclic_nodes\":{},\"unconverged\":{},\"secs\":{rebuild_s:.3},\"secs_scope_all\":{rebuild_all_s:.3},\"rows_scope_all\":{},\"root_pn\":{},\"root_dn\":{},\"root_selected\":\"{}\",\"table_bytes\":{}}}",
            rep.rows,
            table.explicit_rows(),
            rep.computations,
            rep.cyclic_nodes,
            rep.unconverged,
            rep_all.rows,
            root_row.pn,
            root_row.dn,
            root_row.selected.to_uci(),
            table.heap_bytes()
        ),
    );

    // 2. priority
    let t = Instant::now();
    let regret = reader.read(|v| priority::regret_from_root(v, root));
    let regret_s = t.elapsed().as_secs_f64();
    let t = Instant::now();
    let scored = reader.read(|v| priority::priorities(v, root, &priority::Votes::new(), |_| false));
    let score_s = t.elapsed().as_secs_f64();
    let top = priority::top_k(scored.iter().copied(), priority::SELECTOR_HORIZON);
    let reached = regret.iter().filter(|r| r.is_finite()).count();
    emit(
        &out,
        &format!(
            "{{\"stage\":\"priority\",\"reached\":{reached},\"scored_open\":{},\"secs_regret\":{regret_s:.3},\"secs_score\":{score_s:.3},\"top1\":{:.3},\"regret_bytes\":{}}}",
            scored.len(),
            top.first().map(|x| x.1).unwrap_or(0.0),
            regret.len() * 8
        ),
    );
    drop(regret);
    drop(scored);

    // 3. writer-maintained
    let mut proofs = Proofs::new(vec![campaign.clone()], RefreshConfig::default());
    proofs.set_table(1, table);
    let mut seq = 1u64;
    for (stage, batches, per) in [("writer_b1", b1, 1usize), ("writer_b32", b32, 32usize)] {
        let mut lat = Lat::new();
        let mut commit = Lat::new();
        let (mut processed, mut written, mut plies, mut guards, mut max_processed) = (0u64, 0u64, 0u64, 0u64, 0u64);
        let t_all = Instant::now();
        for _ in 0..batches {
            let mut b = s.batch();
            let mut calls = Vec::with_capacity(per);
            for _ in 0..per {
                calls.push(vec![landing(&mut b, &mut rng, &mut keys)]);
            }
            let tc = Instant::now();
            seq = s.commit(b, &[]).unwrap();
            commit.add(tc.elapsed().as_secs_f64() * 1e6);
            let tr = Instant::now();
            let r = reader.read(|v| proofs.after_batch(&KeyOrdered { graph: v, keys: &keys }, &calls, seq, 0.0));
            lat.add(tr.elapsed().as_secs_f64() * 1e6);
            processed += r.processed;
            written += r.written;
            plies += r.plies as u64;
            guards += r.guard.is_some() as u64;
            max_processed = max_processed.max(r.processed);
        }
        let secs = t_all.elapsed().as_secs_f64();
        emit(
            &out,
            &format!(
                "{{\"stage\":\"{stage}\",\"batches\":{batches},\"landings_per_batch\":{per},\"refresh_us\":{},\"commit_us\":{},\"processed_per_batch\":{:.1},\"max_processed\":{max_processed},\"written_per_batch\":{:.1},\"levels_per_call\":{:.2},\"guards\":{guards},\"secs\":{secs:.2}}}",
                lat.json(),
                commit.json(),
                processed as f64 / batches as f64,
                written as f64 / batches as f64,
                plies as f64 / (batches as f64 * per as f64),
            ),
        );
    }

    // 4. consistency: maintained table vs a rebuild of the final graph
    let t = Instant::now();
    let (fresh, frep) = reader.read(|v| rebuild(&KeyOrdered { graph: v, keys: &keys }, &campaign, gate, Scope::Below(root), seq));
    let rebuild2_s = t.elapsed().as_secs_f64();
    let maintained = proofs.table(1).unwrap();
    let (mut both, mut same_numbers, mut same_all, mut only_m, mut only_f) = (0u64, 0u64, 0u64, 0u64, 0u64);
    for id in maintained.ids() {
        match (maintained.get(id), fresh.get(id)) {
            (Some(m), Some(f)) => {
                both += 1;
                if m.numbers() == f.numbers() {
                    same_numbers += 1;
                    if m.same_values(&f) {
                        same_all += 1;
                    }
                }
            }
            (Some(_), None) => only_m += 1,
            _ => {}
        }
    }
    for id in fresh.ids() {
        if !maintained.has(id) {
            only_f += 1;
        }
    }
    let mroot = maintained.get(root).unwrap();
    let froot = fresh.get(root).unwrap();
    emit(
        &out,
        &format!(
            "{{\"stage\":\"consistency\",\"rows_both\":{both},\"same_pn_dn\":{same_numbers},\"same_all_fields\":{same_all},\"only_maintained\":{only_m},\"only_rebuild\":{only_f},\"root_maintained\":[{},{},\"{}\"],\"root_rebuild\":[{},{},\"{}\"],\"rebuild_secs\":{rebuild2_s:.3},\"rebuild_rows\":{}}}",
            mroot.pn,
            mroot.dn,
            mroot.selected.to_uci(),
            froot.pn,
            froot.dn,
            froot.selected.to_uci(),
            frep.rows
        ),
    );

    // 5. memory and frontier reads
    let t = Instant::now();
    let fr = reader.read(|v| frontier::frontier_rows(&KeyOrdered { graph: v, keys: &keys }, maintained, frontier::PROOF_FRONTIER_SCAN));
    let frontier_ms = t.elapsed().as_secs_f64() * 1e3;
    let t = Instant::now();
    let stats = reader.read(|v| {
        frontier::frontier_dn_stats(&KeyOrdered { graph: v, keys: &keys }, maintained, campaign.goal, 2, frontier::PROOF_FRONTIER_SCAN)
    });
    let stats_ms = t.elapsed().as_secs_f64() * 1e3;
    let n_now = reader.read(|v| v.len());
    let explicit = maintained.explicit_rows() as f64;
    let rows = maintained.len() as f64;
    let bytes = maintained.heap_bytes() as f64;
    let kind_bytes = n_now as f64 / 4.0;
    let per_explicit = (bytes - kind_bytes).max(0.0) / explicit.max(1.0);
    // production: 206.6M positions, 39.4M rows, ~80% proved rows (pn = 0).
    let prod_rows = 39.4e6;
    let prod_explicit = prod_rows * (explicit / rows.max(1.0));
    let prod_bytes = 206.6e6 / 4.0 + prod_explicit * per_explicit;
    emit(
        &out,
        &format!(
            "{{\"stage\":\"memory\",\"positions\":{n_now},\"rows\":{rows},\"explicit_rows\":{explicit},\"table_bytes\":{bytes},\"bytes_per_row\":{:.1},\"bytes_per_explicit_row\":{per_explicit:.1},\"prod_estimate_mb\":{:.0},\"frontier_rows\":{},\"frontier_ms\":{frontier_ms:.2},\"dn_stats_ms\":{stats_ms:.2},\"and_nodes\":{},\"dn_median\":{}}}",
            bytes / rows.max(1.0),
            prod_bytes / 1e6,
            fr.len(),
            stats.and_nodes,
            stats.dn_median
        ),
    );
    s.close().unwrap();
}
