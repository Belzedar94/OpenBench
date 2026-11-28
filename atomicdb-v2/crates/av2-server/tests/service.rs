//! The writer service end to end: concurrent submitters, group commit,
//! readers see each acknowledged batch, metrics are served, the log holds
//! every fact once.

use av2_facts::{Fact, Opaque, RawJson};
use av2_model::GraphRead;
use av2_server::stub::{StubInput, StubRules};
use av2_server::{Service, ServiceConfig, http};
use av2_store::{Config, Store};
use std::sync::Arc;

#[test]
fn group_commit_service_with_metrics() {
    let d = tempfile::tempdir().unwrap();
    let store = Store::create(d.path(), Config { shard_bits: 4, initial_positions: 4096, ..Config::default() }).unwrap();
    let svc: Arc<Service<StubInput, ()>> =
        Service::start(store, StubRules, ServiceConfig { max_batch: 64, metrics_addr: Some("127.0.0.1:0".into()) }).unwrap();
    let addr = svc.metrics_addr().unwrap();
    let hs: Vec<_> = (0..6u64)
        .map(|c| {
            let svc = svc.clone();
            std::thread::spawn(move || {
                let mut seqs = Vec::new();
                for k in 0..40u64 {
                    let input = if k % 4 == 0 {
                        StubInput::Fact(Box::new(Fact::Opaque(Opaque { kind: "t".into(), data: RawJson(format!("[{c},{k}]")) })))
                    } else {
                        StubInput::Landing { seed: (c << 32) | k }
                    };
                    let ack = svc.submit_wait(input).unwrap();
                    // Visible to readers as soon as it is acknowledged.
                    assert!(svc.reader().applied_seq() >= ack.seq);
                    seqs.push(ack.seq);
                }
                seqs
            })
        })
        .collect();
    let mut all: Vec<u64> = hs.into_iter().flat_map(|h| h.join().unwrap()).collect();
    all.sort();
    assert_eq!(all.len(), 240);
    let m = svc.writer_metrics();
    assert_eq!(m.inputs.get(), 240);
    assert!(m.batches.get() < 240, "group commit merged nothing");
    assert!(svc.reader().read(|v| v.len()) > 180 * 20);
    let text = http::get(addr, "/metrics").unwrap();
    for name in [
        "av2_writer_stage_seconds_bucket{stage=\"fsync\",le=",
        "av2_submit_to_visible_seconds_count 240",
        "av2_inputs_total 240",
        "av2_queue_depth 0",
        "av2_graph_live_bytes{file=\"core.col\"}",
    ] {
        assert!(text.contains(name), "missing {name} in\n{text}");
    }
    assert_eq!(http::get(addr, "/healthz").unwrap(), "ok\n");
    let store = svc.shutdown().expect("sole owner");
    let last = store.last_seq();
    drop(store);
    let s = Store::open(d.path(), Config::default()).unwrap();
    assert_eq!(s.last_seq(), last);
}
