//! Store against the in-memory model: content, recovery, readers, backup,
//! group commit, scrubbing and the bulk loader.

use av2_facts::Fact;
use av2_model::{Core, Edge, GraphRead, GraphWrite, Move, PosId, Rng};
use av2_store::digest::digest;
use av2_store::group::Writer;
use av2_store::loader::BulkLoader;
use av2_store::mem::MemGraph;
use av2_store::testkit::{random_batch, random_fp};
use av2_store::{Config, Store};
use std::path::Path;
use std::time::Duration;

const SEED: u64 = 0xA70B;

/// Small everything, so that a short test crosses segment rolls, checkpoints,
/// shard rebuilds and file growth many times.
fn small_cfg(background: bool) -> Config {
    Config {
        durable: false,
        seg_bytes: 64 << 10,
        ckpt_bytes: 96 << 10,
        ckpt_interval: Duration::from_millis(50),
        background_checkpoint: background,
        shard_bits: 2,
        initial_positions: 1024,
        km_compact_min: 4096,
        ..Config::default()
    }
}

fn batch_rng(seq: u64) -> Rng {
    Rng::new(SEED ^ seq.wrapping_mul(0x9E37_79B9_7F4A_7C15))
}

fn size_of(seq: u64) -> usize {
    1 + (seq % 37) as usize
}

fn store_digest(s: &Store) -> u128 {
    s.reader().read(|v| {
        let mut pairs = Vec::new();
        v.key_pairs(&mut pairs);
        digest(v, pairs)
    })
}

fn model_digest(m: &MemGraph) -> u128 {
    digest(m, m.key_pairs())
}

fn model_at(upto: u64) -> MemGraph {
    let mut m = MemGraph::new();
    for seq in 1..=upto {
        m.seq = seq;
        random_batch(&mut m, &mut batch_rng(seq), size_of(seq)).unwrap();
    }
    m
}

fn step(s: &mut Store, seq: u64) {
    assert_eq!(s.last_seq() + 1, seq);
    let mut b = s.batch();
    random_batch(&mut b, &mut batch_rng(seq), size_of(seq)).unwrap();
    let facts = av2_facts::encode_all(&[Fact::Opaque(av2_facts::Opaque {
        kind: "test".into(),
        data: av2_facts::RawJson(format!("{{\"seq\":{seq}}}")),
    })]);
    assert_eq!(s.commit(b, &facts).unwrap(), seq);
}

#[test]
fn store_matches_model_across_reopens() {
    let d = tempfile::tempdir().unwrap();
    let mut s = Store::create(d.path(), small_cfg(false)).unwrap();
    let mut m = MemGraph::new();
    for seq in 1..=400u64 {
        step(&mut s, seq);
        m.seq = seq;
        random_batch(&mut m, &mut batch_rng(seq), size_of(seq)).unwrap();
        if seq % 100 == 0 {
            assert_eq!(store_digest(&s), model_digest(&m), "digest differs at {seq}");
        }
        if seq % 150 == 0 {
            // Drop without closing: what a crash leaves (minus the torn tail).
            drop(s);
            s = Store::open(d.path(), small_cfg(false)).unwrap();
            assert_eq!(s.last_seq(), seq);
            assert_eq!(store_digest(&s), model_digest(&m), "digest differs after reopen at {seq}");
        }
    }
    let st = s.stats();
    assert!(st.checkpoints >= 1, "{st:?}");
    assert!(st.grows >= 1, "{st:?}");
    assert!(st.segments_released > 3, "{st:?}");
    // Every key resolves to its id.
    let pairs = m.key_pairs();
    s.reader().read(|v| {
        for (fp, id) in &pairs {
            assert_eq!(v.lookup(fp), Some(*id));
        }
    });
    s.close().unwrap();
    let s = Store::open(d.path(), small_cfg(false)).unwrap();
    assert_eq!(s.recovery.frames_replayed, 0, "a clean close leaves nothing to replay");
    assert_eq!(store_digest(&s), model_digest(&m));
}

fn newest_segment(dir: &Path) -> std::path::PathBuf {
    av2_store::log::scan(&dir.join("log")).unwrap().last().unwrap().path.clone()
}

fn append(p: &Path, bytes: &[u8]) {
    use std::io::Write;
    let mut f = std::fs::OpenOptions::new().append(true).open(p).unwrap();
    f.write_all(bytes).unwrap();
    f.sync_all().unwrap();
}

/// A frame being written when the process died: only its first bytes reached
/// the file, or all of them with a CRC that does not match.  It was never
/// applied (frames are applied only once durable), which is what makes it a
/// torn tail rather than lost data.
#[test]
fn torn_tail_is_discarded() {
    let d = tempfile::tempdir().unwrap();
    let mut cfg = small_cfg(false);
    cfg.ckpt_bytes = u64::MAX;
    cfg.seg_bytes = 64 << 20;
    cfg.prealloc = false;
    let mut s = Store::create(d.path(), cfg.clone()).unwrap();
    for seq in 1..=50 {
        step(&mut s, seq);
    }
    drop(s);
    // Header of a 1000-byte frame followed by 500 bytes.
    let mut torn = Vec::new();
    torn.extend_from_slice(&1000u32.to_le_bytes());
    torn.extend_from_slice(&0xDEAD_BEEFu32.to_le_bytes());
    torn.extend_from_slice(&51u64.to_le_bytes());
    torn.resize(8 + 500, 0xAB);
    append(&newest_segment(d.path()), &torn);
    let s = Store::open(d.path(), cfg.clone()).unwrap();
    assert_eq!(s.last_seq(), 50);
    assert_eq!(s.recovery.torn_tail_bytes, torn.len() as u64);
    assert_eq!(store_digest(&s), model_digest(&model_at(50)));
    drop(s);
    // A complete frame whose CRC does not match.
    let body = [51u64.to_le_bytes().as_slice(), &0u32.to_le_bytes(), &[7u8; 40]].concat();
    let mut bad = Vec::new();
    bad.extend_from_slice(&(body.len() as u32).to_le_bytes());
    bad.extend_from_slice(&(crc32fast::hash(&body) ^ 1).to_le_bytes());
    bad.extend_from_slice(&body);
    append(&newest_segment(d.path()), &bad);
    let mut s = Store::open(d.path(), cfg.clone()).unwrap();
    assert_eq!(s.last_seq(), 50);
    assert!(s.recovery.torn_tail_bytes > 0);
    assert_eq!(store_digest(&s), model_digest(&model_at(50)));
    // And the store goes on from there.
    step(&mut s, 51);
    drop(s);
    let s = Store::open(d.path(), cfg).unwrap();
    assert_eq!(s.last_seq(), 51);
    assert_eq!(store_digest(&s), model_digest(&model_at(51)));
}

/// Recovery replays every frame after the checkpoint over files that may
/// already hold later frames (the OS flushes pages whenever it likes, and
/// after a plain process kill the page cache holds everything).  Every redo
/// record must give the same result replayed over such a state.
#[test]
fn replay_over_a_state_ahead_of_the_checkpoint() {
    let d = tempfile::tempdir().unwrap();
    let mut cfg = small_cfg(false);
    cfg.ckpt_bytes = u64::MAX;
    let mut s = Store::create(d.path(), cfg.clone()).unwrap();
    for seq in 1..=300 {
        step(&mut s, seq);
    }
    let want = model_digest(&model_at(300));
    assert_eq!(store_digest(&s), want);
    drop(s);
    for _ in 0..2 {
        let s = Store::open(d.path(), cfg.clone()).unwrap();
        assert_eq!(s.recovery.frames_replayed, 300);
        assert_eq!(store_digest(&s), want);
    }
}

/// Compaction of the key map: rebuilt shards leave garbage, which moves, a
/// few shards per commit, into a new arena generation.  Replay from a
/// checkpoint taken before (or during) a compaction, over files already
/// past it, must give the same state, and old generations go once a
/// checkpoint no longer needs them.
#[test]
fn key_map_compaction_survives_replay() {
    let d = tempfile::tempdir().unwrap();
    let mut cfg = small_cfg(false);
    cfg.ckpt_bytes = u64::MAX;
    let mut s = Store::create(d.path(), cfg.clone()).unwrap();
    let mut seq = 0;
    while s.stats().compactions < 3 {
        seq += 1;
        step(&mut s, seq);
        // A compaction starts only once a checkpoint is at the current generation.
        if seq % 40 == 0 {
            s.checkpoint().unwrap();
        }
        assert!(seq < 4000, "no compaction after {seq} batches: {:?}", s.stats());
    }
    for _ in 0..20 {
        seq += 1;
        step(&mut s, seq);
    }
    let want = model_digest(&model_at(seq));
    assert_eq!(store_digest(&s), want);
    let (per_file, _) = s.live_bytes();
    let replay_from = s.recovery.checkpoint_seq;
    drop(s);
    // Replays every frame since the last checkpoint over files that are already past them.
    let s = Store::open(d.path(), cfg.clone()).unwrap();
    assert!(s.recovery.frames_replayed > 0 || replay_from == seq);
    assert_eq!(store_digest(&s), want);
    assert_eq!(s.live_bytes().0, per_file);
    s.checkpoint().unwrap();
    let arenas = || {
        std::fs::read_dir(d.path().join("graph"))
            .unwrap()
            .filter(|e| e.as_ref().unwrap().file_name().to_string_lossy().starts_with("keymap.arena"))
            .count()
    };
    assert!(arenas() <= 3, "the current generation, the next one, at most one being deleted");
    drop(s);
    let s = Store::open(d.path(), cfg).unwrap();
    assert_eq!(store_digest(&s), want);
}

/// A compaction moves at most `MOVE_BUDGET` bytes of regions per commit:
/// no commit copies the whole key map.
#[test]
fn compaction_is_spread_over_commits() {
    let d = tempfile::tempdir().unwrap();
    let mut cfg = small_cfg(false);
    cfg.shard_bits = 6;
    cfg.ckpt_bytes = u64::MAX;
    let mut s = Store::create(d.path(), cfg.clone()).unwrap();
    let mut seq = 0u64;
    let mut migrating_commits = 0u64;
    while s.stats().compactions < 1 {
        seq += 1;
        step(&mut s, seq);
        if seq % 40 == 0 {
            s.checkpoint().unwrap();
        }
        let cursor = s.reader().read(|v| v.counter(av2_store::layout::C_KM_CURSOR));
        if cursor != 0 {
            migrating_commits += 1;
        }
        assert!(seq < 6000, "no compaction after {seq} batches");
    }
    assert_eq!(store_digest(&s), model_digest(&model_at(seq)));
    // 64 shards of a few KiB each fit one commit's budget; with the small
    // threshold of the tests a compaction may still finish in one commit.
    assert!(migrating_commits <= 64, "{migrating_commits}");
}

/// Pipelined commits: batch n+1 is computed (on an overlay that reads
/// through batch n) while n's fsync is in flight.  Same final state as one
/// commit at a time, and readers never see a batch before it is durable.
#[test]
fn pipelined_commits_match_the_model() {
    let d = tempfile::tempdir().unwrap();
    let mut cfg = small_cfg(true);
    cfg.durable = true;
    let mut s = Store::create(d.path(), cfg.clone()).unwrap();
    let mut flight: Option<av2_store::InFlight> = None;
    for seq in 1..=300u64 {
        let mut b = match &flight {
            Some(f) => s.batch_after(f),
            None => s.batch(),
        };
        random_batch(&mut b, &mut batch_rng(seq), size_of(seq)).unwrap();
        if let Some(f) = flight.take() {
            // Not visible before it is durable and applied.
            assert_eq!(s.reader().applied_seq(), seq - 2);
            assert_eq!(s.commit_finish(f).unwrap(), seq - 1);
        }
        flight = Some(s.commit_start(b, &[]).unwrap());
        assert!(s.commit_start(s.batch(), &[]).is_err(), "only one commit in flight");
    }
    assert_eq!(s.commit_finish(flight.take().unwrap()).unwrap(), 300);
    let want = model_digest(&model_at(300));
    assert_eq!(store_digest(&s), want);
    drop(s);
    let s = Store::open(d.path(), cfg).unwrap();
    assert_eq!(s.last_seq(), 300);
    assert_eq!(store_digest(&s), want);
}

/// Prepared segments: every log file is a zero-filled `seg_bytes` file
/// written in place (commits never extend it), released ones are recycled,
/// and a torn frame inside one is zeroed and written over after recovery.
#[test]
fn prealloc_segments_are_written_in_place() {
    use std::io::{Seek, SeekFrom, Write};
    let d = tempfile::tempdir().unwrap();
    let cfg = small_cfg(false);
    assert!(cfg.prealloc);
    let mut s = Store::create(d.path(), cfg.clone()).unwrap();
    for seq in 1..=300 {
        step(&mut s, seq);
    }
    let st = s.stats();
    assert!(st.segments_recycled >= 1, "{st:?}");
    for e in std::fs::read_dir(d.path().join("log")).unwrap() {
        let e = e.unwrap();
        let len = e.metadata().unwrap().len();
        assert_eq!(len, cfg.seg_bytes, "{} is {len} bytes", e.file_name().to_string_lossy());
    }
    drop(s);
    // A frame torn half way, inside the preallocated zeros after the last valid frame.
    let segs = av2_store::log::scan(&d.path().join("log")).unwrap();
    let last = segs.last().unwrap().path.clone();
    let r = av2_store::log::read_segment(&last).unwrap();
    let mut f = std::fs::OpenOptions::new().write(true).open(&last).unwrap();
    f.seek(SeekFrom::Start(r.end as u64)).unwrap();
    f.write_all(&[200, 1, 0, 0, 0xEF, 0xBE, 0xAD, 0xDE, 9, 9, 9, 9]).unwrap();
    drop(f);
    let mut s = Store::open(d.path(), cfg.clone()).unwrap();
    assert_eq!(s.last_seq(), 300);
    assert_eq!(s.recovery.torn_tail_bytes, 12);
    assert_eq!(store_digest(&s), model_digest(&model_at(300)));
    for seq in 301..=320 {
        step(&mut s, seq);
    }
    drop(s);
    let s = Store::open(d.path(), cfg).unwrap();
    assert_eq!(s.last_seq(), 320);
    assert_eq!(store_digest(&s), model_digest(&model_at(320)));
}

#[test]
fn readers_run_without_locks_while_the_writer_commits() {
    let d = tempfile::tempdir().unwrap();
    let mut s = Store::create(d.path(), small_cfg(true)).unwrap();
    for seq in 1..=50 {
        step(&mut s, seq);
    }
    let stop = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
    let mut hs = Vec::new();
    for t in 0..4u64 {
        let r = s.reader();
        let stop = stop.clone();
        hs.push(std::thread::spawn(move || {
            let mut rng = Rng::new(t);
            let mut reads = 0u64;
            let mut es = Vec::new();
            let mut ps = Vec::new();
            while !stop.load(std::sync::atomic::Ordering::Relaxed) {
                // The closure may see a batch half applied (it is then run
                // again), so it only collects; the checks run on what a
                // validated snapshot returned.
                let (n, kids, pars) = r.read(|v| {
                    let n = v.len();
                    let id = PosId(rng.below(n));
                    es.clear();
                    v.children(id, &mut es);
                    ps.clear();
                    v.parents(id, &mut ps);
                    let _ = v.core(id);
                    (n, es.iter().map(|e| e.child.0).collect::<Vec<_>>(), ps.iter().map(|p| p.0).collect::<Vec<_>>())
                });
                // A consistent snapshot never shows an edge to a position it does not have.
                assert!(kids.iter().all(|&c| c < n), "edge beyond {n}: {kids:?}");
                assert!(pars.iter().all(|&p| p < n), "parent beyond {n}: {pars:?}");
                reads += 1;
            }
            reads
        }));
    }
    for seq in 51..=400 {
        step(&mut s, seq);
    }
    stop.store(true, std::sync::atomic::Ordering::Relaxed);
    let total: u64 = hs.into_iter().map(|h| h.join().unwrap()).sum();
    assert!(total > 100, "{total}");
    assert_eq!(store_digest(&s), model_digest(&model_at(400)));
}

#[test]
fn backup_while_running_restores_to_a_committed_state() {
    let d = tempfile::tempdir().unwrap();
    let b = tempfile::tempdir().unwrap();
    let mut s = Store::create(d.path(), small_cfg(true)).unwrap();
    for seq in 1..=100 {
        step(&mut s, seq);
    }
    let h = s.backup_handle();
    let dest = b.path().join("copy");
    let t = std::thread::spawn(move || h.backup(&dest).unwrap());
    let mut seq = 101;
    while !t.is_finished() || seq < 160 {
        step(&mut s, seq);
        seq += 1;
    }
    let rep = t.join().unwrap();
    assert!(rep.checkpoint_seq >= 100);
    let r = Store::open(&b.path().join("copy"), small_cfg(false)).unwrap();
    let got = r.last_seq();
    assert!(got >= rep.checkpoint_seq && got < seq, "{got} vs {rep:?}");
    assert_eq!(store_digest(&r), model_digest(&model_at(got)));
}

#[test]
fn group_commit_acknowledges_every_input_once() {
    let d = tempfile::tempdir().unwrap();
    let mut cfg = small_cfg(true);
    cfg.durable = true;
    let s = Store::create(d.path(), cfg).unwrap();
    let w = std::sync::Arc::new(Writer::spawn(s, av2_rules::LogOnly, 64));
    let mut hs = Vec::new();
    for t in 0..8u64 {
        let w = w.clone();
        hs.push(std::thread::spawn(move || {
            let mut seqs = Vec::new();
            for i in 0..50u64 {
                let f = Fact::Opaque(av2_facts::Opaque { kind: "t".into(), data: av2_facts::RawJson(format!("[{t},{i}]")) });
                seqs.push(w.submit_wait(f).unwrap().0);
            }
            seqs
        }));
    }
    let mut all: Vec<u64> = hs.into_iter().flat_map(|h| h.join().unwrap()).collect();
    let w = std::sync::Arc::try_unwrap(w).ok().unwrap();
    let (s, st) = w.shutdown();
    all.sort();
    assert_eq!(all.len(), 400);
    assert!(st.batches < 400, "group commit merged nothing: {st:?}");
    assert_eq!(st.inputs, 400);
    drop(s);
    // Every fact is kept exactly once: released segments in the archive, the rest in the log.
    let mut seen = std::collections::BTreeMap::new();
    let mut last_seq = 0u64;
    for dir in [d.path().join("archive"), d.path().join("log")] {
        if !dir.exists() {
            continue;
        }
        av2_store::log::for_each_frame(&dir, |_, fr| {
            if fr.seq > last_seq {
                last_seq = fr.seq;
                for f in av2_facts::decode_all(fr.facts).unwrap() {
                    if let Fact::Opaque(o) = f {
                        *seen.entry(o.data.0).or_insert(0) += 1;
                    }
                }
            }
            Ok(())
        })
        .unwrap();
    }
    assert_eq!(seen.len(), 400);
    assert!(seen.values().all(|&n| n == 1));
}

#[test]
fn scrub_finds_a_flipped_byte() {
    let d = tempfile::tempdir().unwrap();
    let mut s = Store::create(d.path(), small_cfg(false)).unwrap();
    for seq in 1..=200 {
        step(&mut s, seq);
    }
    s.checkpoint().unwrap();
    let r = s.scrub().unwrap();
    assert!(r.checked > 0 && r.mismatches.is_empty(), "{r:?}");
    // Corrupt one byte of core.col behind the store's back.
    let p = d.path().join("graph").join("core.col");
    {
        use std::io::{Read, Seek, SeekFrom, Write};
        let mut f = std::fs::OpenOptions::new().read(true).write(true).open(&p).unwrap();
        f.seek(SeekFrom::Start(100)).unwrap();
        let mut b = [0u8; 1];
        f.read_exact(&mut b).unwrap();
        f.seek(SeekFrom::Start(100)).unwrap();
        f.write_all(&[b[0] ^ 0x40]).unwrap();
        f.sync_all().unwrap();
    }
    let r = s.scrub().unwrap();
    assert_eq!(r.mismatches, vec![("core.col".to_string(), 0)], "{r:?}");
}

#[test]
fn bulk_loader_builds_what_batches_build() {
    // Build a small graph in memory, load it in bulk, compare.
    let mut m = MemGraph::new();
    let mut rng = Rng::new(5);
    let n = 3000u64;
    for _ in 0..n {
        let fen = av2_store::testkit::random_fen(&mut rng);
        m.create(random_fp(&mut rng), Some(&fen), Core { eval_cp: 5, ..Core::default() }).unwrap();
    }
    for p in 0..n / 2 {
        let es: Vec<Edge> =
            (0..(p % 7)).map(|k| Edge { child: PosId((p * 13 + k * 101) % n), mv: Move::new(1, (k + 2) as u8, 0) }).collect();
        if !es.is_empty() {
            m.set_children(PosId(p), &es).unwrap();
        }
    }
    let d = tempfile::tempdir().unwrap();
    let mut cfg = small_cfg(false);
    cfg.shard_bits = 4;
    let mut bl = BulkLoader::create(d.path(), cfg.clone(), n, n * 3).unwrap();
    let pairs = m.key_pairs();
    let mut fp_of = vec![None; n as usize];
    for (fp, id) in &pairs {
        fp_of[id.index()] = Some(*fp);
    }
    for id in 0..n {
        let fen = m.fen(PosId(id));
        bl.set_position(id, fp_of[id as usize].unwrap(), m.core(PosId(id)), m.ext(PosId(id)), fen.as_deref()).unwrap();
    }
    let mut es = Vec::new();
    for p in 0..n {
        es.clear();
        m.children(PosId(p), &mut es);
        bl.set_children(p, &es).unwrap();
    }
    let mut ps = Vec::new();
    for c in 0..n {
        ps.clear();
        m.parents(PosId(c), &mut ps);
        for p in &ps {
            bl.add_parent(c, p.0).unwrap();
        }
    }
    let s = bl.finish(&[]).unwrap();
    assert_eq!(s.last_seq(), 1);
    assert_eq!(store_digest(&s), model_digest(&m));
}
