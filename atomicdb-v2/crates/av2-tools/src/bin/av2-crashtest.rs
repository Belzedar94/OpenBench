//! av2-crashtest: kill -9 the writer at random moments and check that no
//! acknowledged batch is lost and that the recovered state is exactly the
//! state of an independent in-memory model after the same batches.
//!
//! ```text
//! av2-crashtest --dir DIR [--trials 1000] [--seed 1] [--out crash.jsonl]
//!               [--min-ms 20] [--max-ms 400] [--early 0.2] [--reset-every 100]
//! ```
//!
//! The child process (`--child`) opens the store, commits synthetic batches
//! (`av2_store::testkit::random_batch`, a pure function of the graph and the
//! batch's sequence number) and prints `ACK <seq>` after each durable commit.
//! The parent kills it (`TerminateProcess` on Windows, `SIGKILL` elsewhere)
//! either at a random moment after its first acknowledgement, or (`--early`
//! of the trials) a few milliseconds after start, which lands in recovery.
//! It then reopens the store itself, times the recovery and compares.
//!
//! `--verify --dir DIR --seed S [--min-seq N]` opens a store left by a child
//! (for example a disk image replayed to a flush point by the power-cut
//! script, `scripts/powercut/`), recovers it, and checks that it holds at
//! least batch N and exactly the model's state at the batch it recovered.
//!
//! Small segments, frequent checkpoints, few key-map shards and a small
//! initial capacity make kills land in segment rolls, checkpoints, shard
//! rebuilds and file growth.

use av2_facts::{Fact, Opaque, RawJson};
use av2_model::Rng;
use av2_store::digest::digest;
use av2_store::mem::MemGraph;
use av2_store::testkit::random_batch;
use av2_store::{Config, Store};
use av2_tools::{Args, Lat, die, emit};
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant};

fn cfg(background: bool) -> Config {
    Config {
        durable: true,
        seg_bytes: 256 << 10,
        prealloc: true,
        ckpt_bytes: 512 << 10,
        ckpt_interval: Duration::from_millis(200),
        background_checkpoint: background,
        manifest_every: 3,
        shard_bits: 3,
        initial_positions: 2048,
        ..Config::default()
    }
}

fn batch_rng(seed: u64, seq: u64) -> Rng {
    Rng::new(seed ^ seq.wrapping_mul(0x9E37_79B9_7F4A_7C15))
}

fn size_of(seq: u64) -> usize {
    1 + (seq * 7 % 41) as usize
}

fn facts_of(seq: u64) -> Vec<u8> {
    av2_facts::encode_all(&[Fact::Opaque(Opaque { kind: "crashtest".into(), data: RawJson(format!("{{\"seq\":{seq}}}")) })])
}

fn child(a: &Args) {
    let dir = a.path("dir");
    let seed = a.n("seed", 1);
    let mut s = if dir.join("graph").join("ctl").exists() {
        Store::open(&dir, cfg(true)).unwrap_or_else(|e| die(&format!("open: {e}")))
    } else {
        Store::create(&dir, cfg(true)).unwrap_or_else(|e| die(&format!("create: {e}")))
    };
    let stdout = std::io::stdout();
    let mut o = stdout.lock();
    writeln!(o, "OPEN {}", s.last_seq()).unwrap();
    o.flush().unwrap();
    loop {
        let seq = s.last_seq() + 1;
        let mut b = s.batch();
        random_batch(&mut b, &mut batch_rng(seed, seq), size_of(seq)).unwrap_or_else(|e| die(&format!("batch {seq}: {e}")));
        let got = s.commit(b, &facts_of(seq)).unwrap_or_else(|e| die(&format!("commit {seq}: {e}")));
        assert_eq!(got, seq);
        writeln!(o, "ACK {seq}").unwrap();
        o.flush().unwrap();
    }
}

fn store_digest(s: &Store) -> u128 {
    s.reader().read(|v| {
        let mut pairs = Vec::new();
        v.key_pairs(&mut pairs);
        digest(v, pairs)
    })
}

fn wipe(dir: &Path) {
    if dir.exists() {
        std::fs::remove_dir_all(dir).unwrap_or_else(|e| die(&format!("remove {}: {e}", dir.display())));
    }
}

fn verify(a: &Args) {
    let dir = a.path("dir");
    let seed = a.n("seed", 1);
    let min_seq = a.n("min-seq", 0);
    let t = Instant::now();
    let s = Store::open(&dir, cfg(false)).unwrap_or_else(|e| die(&format!("recovery failed: {e}")));
    let ms = t.elapsed().as_secs_f64() * 1e3;
    let got = s.last_seq();
    let mut model = MemGraph::new();
    for seq in 1..=got {
        model.seq = seq;
        random_batch(&mut model, &mut batch_rng(seed, seq), size_of(seq)).unwrap();
    }
    let identical = store_digest(&s) == digest(&model, model.key_pairs());
    let ok = identical && got >= min_seq;
    println!(
        "{{\"stage\":\"verify\",\"recovered\":{got},\"min_seq\":{min_seq},\"identical\":{identical},\"frames_replayed\":{},\"torn_tail_bytes\":{},\"recovery_ms\":{ms:.2},\"ok\":{ok}}}",
        s.recovery.frames_replayed, s.recovery.torn_tail_bytes
    );
    if !ok {
        std::process::exit(1);
    }
}

fn main() {
    let a = Args::parse();
    if a.flag("child") {
        child(&a);
        return;
    }
    if a.flag("verify") {
        verify(&a);
        return;
    }
    let dir = a.path("dir");
    let trials = a.n("trials", 100);
    let seed = a.n("seed", 1);
    let (min_ms, max_ms) = (a.n("min-ms", 20), a.n("max-ms", 400));
    let early = a.f("early", 0.2);
    let reset_every = a.n("reset-every", 100).max(1);
    let out: Option<PathBuf> = a.get("out").map(PathBuf::from);
    let exe = std::env::current_exe().unwrap();
    let mut rng = Rng::new(seed ^ 0x00C8_A511);
    wipe(&dir);
    let mut model = MemGraph::new();
    let mut ref_seq = 0u64;
    let (mut ok, mut lost, mut differ, mut early_kills) = (0u64, 0u64, 0u64, 0u64);
    let mut recov = Lat::new();
    let mut replayed = Lat::new();
    let t_all = Instant::now();
    for trial in 0..trials {
        if trial > 0 && trial % reset_every == 0 {
            wipe(&dir);
            model = MemGraph::new();
            ref_seq = 0;
        }
        let mut ch = Command::new(&exe)
            .args(["--child", "--dir"])
            .arg(&dir)
            .args(["--seed", &seed.to_string()])
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .spawn()
            .unwrap_or_else(|e| die(&format!("spawn: {e}")));
        let acked = Arc::new(AtomicU64::new(0));
        let opened = Arc::new(AtomicU64::new(u64::MAX));
        let (a2, o2) = (acked.clone(), opened.clone());
        let so = ch.stdout.take().unwrap();
        let th = std::thread::spawn(move || {
            for l in BufReader::new(so).lines().map_while(Result::ok) {
                if let Some(v) = l.strip_prefix("ACK ") {
                    a2.store(v.trim().parse().unwrap(), Ordering::SeqCst);
                } else if let Some(v) = l.strip_prefix("OPEN ") {
                    o2.store(v.trim().parse().unwrap(), Ordering::SeqCst);
                }
            }
        });
        let is_early = rng.chance(early);
        let t0 = Instant::now();
        if is_early {
            early_kills += 1;
            std::thread::sleep(Duration::from_millis(rng.below(15)));
        } else {
            loop {
                let o = opened.load(Ordering::SeqCst);
                if o != u64::MAX && acked.load(Ordering::SeqCst) > o {
                    break;
                }
                if t0.elapsed() > Duration::from_secs(120) {
                    die("child never acknowledged a batch");
                }
                if let Ok(Some(st)) = ch.try_wait() {
                    die(&format!("child exited early: {st}"));
                }
                std::thread::sleep(Duration::from_millis(1));
            }
            std::thread::sleep(Duration::from_millis(min_ms + rng.below(max_ms - min_ms + 1)));
        }
        let _ = ch.kill();
        let _ = ch.wait();
        let _ = th.join();
        let last_ack = acked.load(Ordering::SeqCst);
        let open_seq = opened.load(Ordering::SeqCst);
        if !dir.join("graph").join("ckpt").exists() {
            // Killed before the store was even created: nothing to check.
            emit(&out, &format!("{{\"stage\":\"crash_trial\",\"trial\":{trial},\"note\":\"killed before create\"}}"));
            wipe(&dir);
            ok += 1;
            continue;
        }
        let t = Instant::now();
        let s = match Store::open(&dir, cfg(false)) {
            Ok(s) => s,
            Err(e) => die(&format!("trial {trial}: recovery failed: {e}")),
        };
        let recovery_ms = t.elapsed().as_secs_f64() * 1e3;
        let recovered = s.last_seq();
        recov.add(recovery_ms);
        replayed.add(s.recovery.frames_replayed as f64);
        // Every acknowledged batch survives; at most the one in flight is added.
        let floor = if last_ack > 0 {
            last_ack
        } else if open_seq != u64::MAX {
            open_seq
        } else {
            0
        };
        let durable_ok = recovered >= floor.max(ref_seq) && recovered <= floor.max(ref_seq) + 1;
        let mut identical = false;
        if recovered >= ref_seq {
            while ref_seq < recovered {
                ref_seq += 1;
                model.seq = ref_seq;
                random_batch(&mut model, &mut batch_rng(seed, ref_seq), size_of(ref_seq)).unwrap();
            }
            identical = store_digest(&s) == digest(&model, model.key_pairs());
        }
        if !durable_ok {
            lost += 1;
        }
        if !identical {
            differ += 1;
        }
        if durable_ok && identical {
            ok += 1;
        }
        emit(
            &out,
            &format!(
                "{{\"stage\":\"crash_trial\",\"trial\":{trial},\"early\":{is_early},\"opened_at\":{},\"last_ack\":{last_ack},\"recovered\":{recovered},\"frames_replayed\":{},\"torn_tail_bytes\":{},\"recovery_ms\":{recovery_ms:.2},\"durable_ok\":{durable_ok},\"identical\":{identical},\"positions\":{}}}",
                if open_seq == u64::MAX { -1 } else { open_seq as i64 },
                s.recovery.frames_replayed,
                s.recovery.torn_tail_bytes,
                av2_model::GraphRead::len(&model)
            ),
        );
        drop(s);
        if !durable_ok || !identical {
            eprintln!("trial {trial}: FAILED (durable_ok={durable_ok}, identical={identical}); keeping {}", dir.display());
            break;
        }
    }
    let summary = format!(
        "{{\"stage\":\"crash_summary\",\"trials\":{trials},\"ok\":{ok},\"lost_acknowledged\":{lost},\"state_differs\":{differ},\"early_kills\":{early_kills},\"recovery_ms\":{},\"frames_replayed\":{},\"secs\":{:.1}}}",
        recov.json(),
        replayed.json(),
        t_all.elapsed().as_secs_f64()
    );
    emit(&out, &summary);
    if ok != trials {
        std::process::exit(1);
    }
    wipe(&dir);
}
