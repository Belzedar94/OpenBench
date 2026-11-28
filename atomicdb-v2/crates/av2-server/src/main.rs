//! av2-server: the writer service with stub rules and the metrics endpoint.
//!
//! ```text
//! av2-server --db DIR [--create] [--listen 127.0.0.1:9187] [--max-batch 256]
//!            [--bench-secs S --clients C [--rate R]] [--fsync-delay-ms D] [--serve] [--rules stub|pipeline]
//! ```
//!
//! Without `--bench-secs` it serves `/metrics` until killed.  With it, `C`
//! client threads submit synthetic landings for `S` seconds (each as fast as
//! it is acknowledged, or `R` per second per client), then it prints the
//! throughput, the batch sizes group commit formed, submit-to-visible
//! quantiles and the per-stage means, and exits (or keeps serving with
//! `--serve`).
//!
//! `--rules pipeline` runs the real batch pipeline (`pipeline.rs`: av2-rules
//! with v1's loop rules, the queue, the proof numbers after the commit) on
//! synthetic prepared landings; `stub` (the default) the stub rules.

use av2_server::pipeline::{PipelineInput, PipelineRules};
use av2_server::stub::{StubInput, StubRules};
use av2_server::{Service, ServiceConfig, writer::STAGES};
use av2_store::{Config, Store};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::time::{Duration, Instant};

fn arg(name: &str) -> Option<String> {
    let v: Vec<String> = std::env::args().collect();
    v.iter().position(|a| a == name).and_then(|i| v.get(i + 1).cloned())
}
fn flag(name: &str) -> bool {
    std::env::args().any(|a| a == name)
}

fn main() {
    let db = std::path::PathBuf::from(arg("--db").expect("--db DIR"));
    let cfg = Config {
        // Simulation of a slower disk (production: 16 ms per fdatasync).
        fsync_delay: Duration::from_secs_f64(arg("--fsync-delay-ms").and_then(|v| v.parse::<f64>().ok()).unwrap_or(0.0) / 1e3),
        ..Config::default()
    };
    let store = if flag("--create") && !db.join("graph").join("ctl").exists() { Store::create(&db, cfg) } else { Store::open(&db, cfg) }
        .unwrap_or_else(|e| {
            eprintln!("error: {e}");
            std::process::exit(2)
        });
    let scfg = ServiceConfig {
        max_batch: arg("--max-batch").and_then(|v| v.parse().ok()).unwrap_or(256),
        metrics_addr: Some(arg("--listen").unwrap_or_else(|| "127.0.0.1:9187".into())),
    };
    if arg("--rules").as_deref() == Some("pipeline") {
        let root = av2_model::PosId(0);
        let svc: Arc<Service<PipelineInput, av2_server::pipeline::PipelineOutput>> =
            Service::start(store, PipelineRules::with_defaults(root), scfg).expect("start");
        // Logical time: a millisecond per input, from a fixed origin.
        run(svc, |seed| PipelineInput::Synthetic { seed, now: 1_800_000_000_000 + (seed & 0xFF_FFFF_FFFF) as i64 }, "pipeline");
    } else {
        let svc: Arc<Service<StubInput, ()>> = Service::start(store, StubRules, scfg).expect("start");
        run(svc, |seed| StubInput::Landing { seed }, "stub");
    }
}

fn run<I: Send + 'static, O: Send + Clone + 'static>(svc: Arc<Service<I, O>>, mk: fn(u64) -> I, rules: &str) {
    eprintln!("av2-server: metrics on http://{}/metrics", svc.metrics_addr().unwrap());
    if let Some(secs) = arg("--bench-secs").and_then(|v| v.parse::<f64>().ok()) {
        let clients: u64 = arg("--clients").and_then(|v| v.parse().ok()).unwrap_or(32);
        let rate: f64 = arg("--rate").and_then(|v| v.parse().ok()).unwrap_or(0.0);
        let stop = Arc::new(AtomicBool::new(false));
        let done = Arc::new(AtomicU64::new(0));
        let t0 = Instant::now();
        let hs: Vec<_> = (0..clients)
            .map(|c| {
                let (svc, stop, done) = (svc.clone(), stop.clone(), done.clone());
                std::thread::spawn(move || {
                    let mut k = 0u64;
                    while !stop.load(Ordering::Relaxed) {
                        let t = Instant::now();
                        if svc.submit_wait(mk((c << 40) | k)).is_ok() {
                            done.fetch_add(1, Ordering::Relaxed);
                        }
                        k += 1;
                        if rate > 0.0 {
                            let gap = Duration::from_secs_f64(1.0 / rate);
                            if let Some(rest) = gap.checked_sub(t.elapsed()) {
                                std::thread::sleep(rest);
                            }
                        }
                    }
                })
            })
            .collect();
        std::thread::sleep(Duration::from_secs_f64(secs));
        stop.store(true, Ordering::Relaxed);
        for h in hs {
            let _ = h.join();
        }
        let el = t0.elapsed().as_secs_f64();
        let m = svc.writer_metrics();
        let stage_means: Vec<String> = STAGES
            .iter()
            .zip(&m.stage)
            .map(|(s, h)| format!("\"{s}\":{:.3}", if h.count() > 0 { h.sum() / h.count() as f64 * 1e3 } else { 0.0 }))
            .collect();
        println!(
            "{{\"stage\":\"service_bench\",\"rules\":\"{rules}\",\"fsync_delay_ms\":{},\"clients\":{clients},\"rate_per_client\":{rate},\"secs\":{el:.1},\"inputs\":{},\"inputs_per_sec\":{:.0},\"batches\":{},\"mean_batch\":{:.1},\"submit_to_visible_ms\":{{\"p50_le\":{},\"p99_le\":{},\"mean\":{:.3}}},\"post_commit_ms_mean\":{:.3},\"stage_mean_ms\":{{{}}}}}",
            arg("--fsync-delay-ms").unwrap_or_else(|| "0".into()),
            done.load(Ordering::Relaxed),
            done.load(Ordering::Relaxed) as f64 / el,
            m.batches.get(),
            m.inputs.get() as f64 / m.batches.get().max(1) as f64,
            m.submit_to_visible.quantile_bound(0.5) * 1e3,
            m.submit_to_visible.quantile_bound(0.99) * 1e3,
            m.submit_to_visible.sum() / m.submit_to_visible.count().max(1) as f64 * 1e3,
            m.post_commit.sum() / m.post_commit.count().max(1) as f64 * 1e3,
            stage_means.join(",")
        );
        std::thread::sleep(Duration::from_millis(600));
        for l in svc.metrics_text().lines() {
            if l.starts_with("av2_archive_bytes_total")
                || l.starts_with("av2_checkpoints_total")
                || l.starts_with("av2_fsync_wait_seconds_sum")
                || l.starts_with("av2_fsync_wait_seconds_count")
            {
                println!("{l}");
            }
        }
        if !flag("--serve") {
            return;
        }
    }
    loop {
        std::thread::sleep(Duration::from_secs(3600));
    }
}
