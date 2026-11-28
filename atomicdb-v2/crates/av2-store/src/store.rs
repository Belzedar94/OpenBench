//! The store: create, open (recover), commit, read, checkpoint, scrub, backup.

use crate::batch::{Batch, Finalized};
use crate::checkpoint::{self, Ckpt, Manifest};
use crate::layout::*;
use crate::log::{self, LogWriter, Seg};
use crate::mapped::Maps;
use crate::view::View;
use crate::{Result, StoreError};
use arc_swap::ArcSwap;
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Condvar, Mutex};
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

#[derive(Clone, Debug)]
pub struct Config {
    /// fsync every frame (false only for tests and bulk tools).
    pub durable: bool,
    /// Size at which a log segment is closed and a new one started.
    pub seg_bytes: u64,
    /// Preallocate segments (see `log.rs`).
    pub prealloc: bool,
    /// Log bytes since the last checkpoint that trigger the next one.
    pub ckpt_bytes: u64,
    /// Also checkpoint at least this often while there are new frames.
    pub ckpt_interval: Duration,
    /// Run checkpoints on their own thread (production) or inline in the
    /// committing thread (tests that want determinism).
    pub background_checkpoint: bool,
    /// Compute the page-checksum manifest every N checkpoints (0 = never).
    pub manifest_every: u64,
    /// What happens to a log segment once a checkpoint covers it.  The facts
    /// are the system of record, so by default they are kept: the segment is
    /// rewritten without its redo records into `<store>/archive/` (same frame
    /// format, readable by `log::for_each_frame` and `av2-replay`).
    pub archive: Archive,
    /// Key-map shards = 2^shard_bits (fixed at creation).
    pub shard_bits: u32,
    /// Capacity reserved at creation, in positions (files grow on demand).
    pub initial_positions: u64,
}

impl Default for Config {
    fn default() -> Self {
        Config {
            durable: true,
            seg_bytes: 64 << 20,
            prealloc: true,
            ckpt_bytes: 256 << 20,
            ckpt_interval: Duration::from_secs(300),
            background_checkpoint: true,
            manifest_every: 1,
            archive: Archive::Facts,
            shard_bits: 12,
            initial_positions: 1 << 20,
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Archive {
    /// Keep the facts of released segments in `<store>/archive/`.
    Facts,
    /// Keep the facts in this directory.
    FactsIn(PathBuf),
    /// Delete released segments (tests and throw-away stores only).
    Delete,
}

#[derive(Clone, Debug, Default)]
pub struct RecoveryReport {
    pub checkpoint_seq: u64,
    pub last_seq: u64,
    pub frames_replayed: u64,
    pub bytes_replayed: u64,
    pub torn_tail_bytes: u64,
    pub micros: u64,
}

#[derive(Clone, Debug, Default)]
pub struct Stats {
    pub commits: u64,
    pub log_bytes: u64,
    pub fsync_micros: u64,
    pub apply_micros: u64,
    pub checkpoints: u64,
    pub checkpoint_micros_total: u64,
    pub checkpoint_micros_max: u64,
    pub manifest_chunks_hashed: u64,
    pub grows: u64,
    pub segments_released: u64,
}

pub(crate) struct CkptCtl {
    requested: bool,
    stop: bool,
    error: Option<String>,
    done_seq: u64,
    generation: u64,
    count: u64,
}

pub(crate) struct Shared {
    pub(crate) maps: ArcSwap<Maps>,
    epoch: AtomicU64,
    applied: AtomicU64,
    dir: PathBuf,
    cfg: Config,
    chunk_mod: Mutex<Vec<Vec<u64>>>,
    segs: Mutex<Vec<Seg>>,
    pin: Mutex<Option<u64>>,
    since_ckpt: AtomicU64,
    ctl: Mutex<CkptCtl>,
    cv: Condvar,
    ckpt_lock: Mutex<()>,
    manifest: Mutex<Manifest>,
    stats: Mutex<Stats>,
    /// True while the checkpoint thread runs.
    bg: std::sync::atomic::AtomicBool,
}

pub struct Store {
    sh: Arc<Shared>,
    log: LogWriter,
    seq: u64,
    thread: Option<JoinHandle<()>>,
    pub recovery: RecoveryReport,
}

/// A lock-free reader of the committed graph.  Cheap to clone; usable from
/// any thread while the writer runs.
#[derive(Clone)]
pub struct Reader {
    sh: Arc<Shared>,
}

fn graph_dir(dir: &Path) -> PathBuf {
    dir.join("graph")
}
fn log_dir(dir: &Path) -> PathBuf {
    dir.join("log")
}

fn initial_lens(cfg: &Config) -> [u64; NFILES] {
    let n = cfg.initial_positions.max(1024);
    let shards = 1u64 << cfg.shard_bits;
    let mut l = [0u64; NFILES];
    l[F_CTL as usize] = CTL_SIZE;
    l[F_CORE as usize] = n * stride(F_CORE) as u64;
    l[F_EXT as usize] = n / 2 * stride(F_EXT) as u64;
    l[F_OUT as usize] = n * OUT_SIZE as u64;
    l[F_PARENT as usize] = n * PARENT_SIZE as u64;
    l[F_FEN as usize] = n * stride(F_FEN) as u64;
    l[F_OUT_ARENA as usize] = n * 12;
    l[F_INX_ARENA as usize] = n / 8 * INX_CELL as u64 + 4096;
    l[F_BLOB_ARENA as usize] = n * 32;
    l[F_KM_DIR as usize] = shards * DIR_ENTRY as u64;
    l[F_KM_ARENA as usize] = n * 48 + shards * region_bytes(MIN_SLOTS);
    l
}

/// Live bytes of each file according to the counters in `ctl`.
pub(crate) fn live_lens(v: &View) -> [u64; NFILES] {
    let n = v.counter(C_POSITIONS);
    let mut l = [0u64; NFILES];
    l[F_CTL as usize] = CTL_SIZE;
    l[F_CORE as usize] = n * stride(F_CORE) as u64;
    l[F_EXT as usize] = v.counter(C_EXTS) * stride(F_EXT) as u64;
    l[F_OUT as usize] = n * OUT_SIZE as u64;
    l[F_PARENT as usize] = n * PARENT_SIZE as u64;
    l[F_FEN as usize] = n * stride(F_FEN) as u64;
    l[F_OUT_ARENA as usize] = v.counter(C_OUT_TAIL);
    l[F_INX_ARENA as usize] = v.counter(C_INX_TAIL) * INX_CELL as u64;
    l[F_BLOB_ARENA as usize] = v.counter(C_BLOB_TAIL);
    l[F_KM_DIR as usize] = (1u64 << v.shard_bits) * DIR_ENTRY as u64;
    l[F_KM_ARENA as usize] = v.counter(C_KM_TAIL);
    l
}

impl Shared {
    fn new(dir: &Path, cfg: Config, maps: Maps, applied: u64, segs: Vec<Seg>, ckpt: Ckpt, manifest: Manifest) -> Shared {
        Shared {
            maps: ArcSwap::from_pointee(maps),
            epoch: AtomicU64::new(0),
            applied: AtomicU64::new(applied),
            dir: dir.to_path_buf(),
            cfg,
            chunk_mod: Mutex::new(vec![Vec::new(); NFILES]),
            segs: Mutex::new(segs),
            pin: Mutex::new(None),
            since_ckpt: AtomicU64::new(0),
            ctl: Mutex::new(CkptCtl {
                requested: false,
                stop: false,
                error: None,
                done_seq: ckpt.seq,
                generation: ckpt.generation,
                count: 0,
            }),
            cv: Condvar::new(),
            ckpt_lock: Mutex::new(()),
            manifest: Mutex::new(manifest),
            stats: Mutex::new(Stats::default()),
            bg: std::sync::atomic::AtomicBool::new(false),
        }
    }

    fn mark(&self, seq: u64, ops: impl Iterator<Item = (u8, u64, usize)>) {
        let mut m = self.chunk_mod.lock().unwrap();
        for (f, off, len) in ops {
            if len == 0 {
                continue;
            }
            let v = &mut m[f as usize];
            let (a, b) = (off / CHUNK, (off + len as u64 - 1) / CHUNK);
            if v.len() <= b as usize {
                v.resize(b as usize + 1, 0);
            }
            for c in a..=b {
                v[c as usize] = seq;
            }
        }
    }

    /// Grow files so that each reaches `need`; publishes the new maps.
    fn ensure(&self, need: &[u64; NFILES]) -> Result<()> {
        let mut maps = self.maps.load_full();
        let mut grew = false;
        for f in 0..NFILES {
            if need[f] > maps.m[f].len() {
                maps = Arc::new(maps.grown(f as u8, need[f])?);
                grew = true;
                self.stats.lock().unwrap().grows += 1;
            }
        }
        if grew {
            self.maps.store(maps);
        }
        Ok(())
    }

    /// Apply redo records to the maps between two epoch bumps.
    fn apply<'a>(&self, seq: u64, ops: impl Iterator<Item = (u8, u64, &'a [u8])> + Clone) -> Result<()> {
        let maps = self.maps.load();
        // Chunks this frame touches, marked before any byte changes (the
        // checkpoint's manifest relies on it).
        self.mark(
            seq,
            ops.clone().map(|(f, o, b)| {
                if f == F_KM_REBUILD {
                    let (_, region, slots) = crate::batch::decode_rebuild(b).unwrap_or_default();
                    (F_KM_ARENA, region, region_bytes(slots) as usize)
                } else {
                    (f, o, b.len())
                }
            }),
        );
        let shard_bits = maps.ctl(C_SHARD_BITS) as u32;
        let e = self.epoch.fetch_add(1, Ordering::AcqRel);
        debug_assert!(e.is_multiple_of(2));
        let mut bad = None;
        for (f, off, b) in ops {
            if f == F_KM_REBUILD {
                let Some((old, region, slots)) = crate::batch::decode_rebuild(b) else {
                    bad = Some("malformed key map rebuild record".into());
                    break;
                };
                match crate::batch::rebuild_region(&maps, shard_bits, &old, region, slots) {
                    Ok((nsh, r)) if region + r.len() as u64 <= maps.f(F_KM_ARENA).len() => {
                        maps.f(F_KM_ARENA).put(region, &r);
                        maps.f(F_KM_DIR).put(off * DIR_ENTRY as u64, &nsh.encode());
                        self.mark(seq, std::iter::once((F_KM_DIR, off * DIR_ENTRY as u64, DIR_ENTRY)));
                    }
                    Ok(_) => {
                        bad = Some(format!("key map region {region}+{} outside the arena", region_bytes(slots)));
                        break;
                    }
                    Err(e) => {
                        bad = Some(e.to_string());
                        break;
                    }
                }
                continue;
            }
            if (f as usize) >= NFILES || off + b.len() as u64 > maps.m[f as usize].len() {
                bad = Some(format!("redo record outside file {f}: {off}+{}", b.len()));
                break;
            }
            maps.m[f as usize].put(off, b);
        }
        self.epoch.fetch_add(1, Ordering::AcqRel);
        self.applied.store(seq, Ordering::Release);
        match bad {
            Some(s) => Err(StoreError::Corrupt(s)),
            None => Ok(()),
        }
    }

    /// One checkpoint: flush the maps, write the manifest, record the
    /// sequence number, release the segments it covers.  Safe to run while
    /// the writer commits.
    fn checkpoint(&self) -> Result<u64> {
        let _one = self.ckpt_lock.lock().unwrap();
        let t0 = Instant::now();
        // Read the sequence number first, then the maps: the maps published
        // before frame `s` was applied include every region it wrote.
        let s = self.applied.load(Ordering::Acquire);
        let maps = self.maps.load_full();
        for m in maps.m.iter() {
            m.flush()?;
        }
        let (generation, count) = {
            let c = self.ctl.lock().unwrap();
            (c.generation + 1, c.count + 1)
        };
        if self.cfg.manifest_every > 0 && count % self.cfg.manifest_every == 0 {
            let hashed = self.update_manifest(&maps, s)?;
            self.stats.lock().unwrap().manifest_chunks_hashed += hashed;
        }
        checkpoint::write(&graph_dir(&self.dir).join("ckpt"), Ckpt { generation, seq: s })?;
        let released = self.release_segments(s)?;
        let us = t0.elapsed().as_micros() as u64;
        {
            let mut c = self.ctl.lock().unwrap();
            c.generation = generation;
            c.count = count;
            c.done_seq = s;
        }
        let mut st = self.stats.lock().unwrap();
        st.checkpoints += 1;
        st.checkpoint_micros_total += us;
        st.checkpoint_micros_max = st.checkpoint_micros_max.max(us);
        st.segments_released += released;
        Ok(s)
    }

    fn update_manifest(&self, maps: &Maps, s: u64) -> Result<u64> {
        let prev = self.manifest.lock().unwrap().clone();
        let mods = self.chunk_mod.lock().unwrap().clone();
        let v = View::new(maps);
        let live = live_lens(&v);
        let mut next = Manifest { seq: s, files: vec![Vec::new(); NFILES] };
        let mut hashed = 0u64;
        for f in 0..NFILES {
            let n = checkpoint::chunks(live[f]);
            for c in 0..n {
                let start = c * CHUNK;
                let len = (live[f] - start).min(CHUNK) as u32;
                let m = mods[f].get(c as usize).copied().unwrap_or(0);
                let entry = if m > s {
                    (0, 0)
                } else if let Some(&(h, l)) = prev.files.get(f).and_then(|p| p.get(c as usize))
                    && l == len
                    && m <= prev.seq
                {
                    (h, l)
                } else {
                    let Some(bytes) = maps.m[f].get(start, len as usize) else { continue };
                    let h = xxhash_rust::xxh3::xxh3_64(bytes);
                    hashed += 1;
                    let m2 = self.chunk_mod.lock().unwrap()[f].get(c as usize).copied().unwrap_or(0);
                    if m2 > s { (0, 0) } else { (h, len) }
                };
                next.files[f].push(entry);
            }
        }
        next.write(&graph_dir(&self.dir).join("manifest"))?;
        *self.manifest.lock().unwrap() = next;
        Ok(hashed)
    }

    fn release_segments(&self, s: u64) -> Result<u64> {
        let pin = *self.pin.lock().unwrap();
        let mut segs = self.segs.lock().unwrap();
        let mut keep_from = 0usize;
        while keep_from + 1 < segs.len() {
            let next_first = segs[keep_from + 1].first;
            let covered = next_first <= s + 1;
            let unpinned = pin.is_none_or(|p| next_first <= p);
            if !(covered && unpinned) {
                break;
            }
            keep_from += 1;
        }
        let gone: Vec<Seg> = segs.drain(..keep_from).collect();
        drop(segs);
        for g in &gone {
            let dir = match &self.cfg.archive {
                Archive::Facts => Some(self.dir.join("archive")),
                Archive::FactsIn(d) => Some(d.clone()),
                Archive::Delete => None,
            };
            if let Some(d) = dir {
                archive_facts(&g.path, &d)?;
            }
            std::fs::remove_file(&g.path)?;
        }
        Ok(gone.len() as u64)
    }
}

impl Shared {
    /// A checkpoint covering at least everything applied now.  With a
    /// checkpoint thread, ask it and wait (competing for `ckpt_lock` against a
    /// thread that checkpoints back to back can starve the caller); without
    /// one, run it here.
    fn checkpoint_covering_now(&self) -> Result<u64> {
        if !self.bg.load(Ordering::Acquire) {
            self.since_ckpt.store(0, Ordering::Relaxed);
            return self.checkpoint();
        }
        let target = self.applied.load(Ordering::Acquire);
        let mut c = self.ctl.lock().unwrap();
        loop {
            if let Some(e) = &c.error {
                return Err(StoreError::Checkpoint(e.clone()));
            }
            if c.done_seq >= target && c.done_seq > 0 || target == 0 && c.count > 0 {
                return Ok(c.done_seq);
            }
            if c.stop {
                drop(c);
                return self.checkpoint();
            }
            c.requested = true;
            self.cv.notify_all();
            c = self.cv.wait_timeout(c, Duration::from_millis(100)).unwrap().0;
        }
    }
}

/// Rewrite segment `seg` into `dir` with the facts of every frame and none
/// of its redo records (atomic: temporary file, fsync, rename).
fn archive_facts(seg: &Path, dir: &Path) -> Result<()> {
    std::fs::create_dir_all(dir)?;
    let r = log::read_segment(seg)?;
    let mut out = Vec::with_capacity(r.end / 4);
    for &(at, len) in &r.frames {
        let fr =
            log::parse_body(&r.data[at..at + len]).ok_or_else(|| StoreError::Corrupt(format!("bad frame body in {}", seg.display())))?;
        let mut body = Vec::with_capacity(12 + fr.facts.len());
        body.extend_from_slice(&fr.seq.to_le_bytes());
        body.extend_from_slice(&(fr.facts.len() as u32).to_le_bytes());
        body.extend_from_slice(fr.facts);
        out.extend_from_slice(&(body.len() as u32).to_le_bytes());
        out.extend_from_slice(&crc32fast::hash(&body).to_le_bytes());
        out.extend_from_slice(&body);
    }
    let name = seg.file_name().unwrap();
    let tmp = dir.join(format!("{}.tmp", name.to_string_lossy()));
    {
        let mut f = std::fs::File::create(&tmp)?;
        f.write_all(&out)?;
        f.sync_all()?;
    }
    std::fs::rename(&tmp, dir.join(name))?;
    log::sync_dir(dir)?;
    Ok(())
}

fn checkpoint_thread(sh: Arc<Shared>) {
    let interval = sh.cfg.ckpt_interval;
    loop {
        {
            let mut c = sh.ctl.lock().unwrap();
            let deadline = Instant::now() + interval;
            while !c.requested && !c.stop {
                let now = Instant::now();
                if now >= deadline {
                    break;
                }
                c = sh.cv.wait_timeout(c, deadline - now).unwrap().0;
            }
            if c.stop {
                return;
            }
            c.requested = false;
            if sh.applied.load(Ordering::Acquire) == c.done_seq && c.count > 0 {
                continue;
            }
        }
        sh.since_ckpt.store(0, Ordering::Relaxed);
        if let Err(e) = sh.checkpoint() {
            sh.ctl.lock().unwrap().error = Some(e.to_string());
        }
        sh.cv.notify_all();
    }
}

impl Store {
    /// Create an empty store in `dir` (which must not hold one).
    pub fn create(dir: &Path, cfg: Config) -> Result<Store> {
        let g = graph_dir(dir);
        if g.join("ctl").exists() {
            return Err(StoreError::Corrupt(format!("{} already holds a store", dir.display())));
        }
        std::fs::create_dir_all(&g)?;
        std::fs::create_dir_all(log_dir(dir))?;
        let maps = Maps::open(&g, &initial_lens(&cfg))?;
        init_ctl(&maps, cfg.shard_bits);
        for m in maps.m.iter() {
            m.flush()?;
        }
        checkpoint::write(&g.join("ckpt"), Ckpt { generation: 1, seq: 0 })?;
        log::sync_dir(&g)?;
        log::sync_dir(dir)?;
        drop(maps);
        Store::open(dir, cfg)
    }

    /// Open the store in `dir`, replaying the log after the last checkpoint.
    pub fn open(dir: &Path, cfg: Config) -> Result<Store> {
        let t0 = Instant::now();
        let g = graph_dir(dir);
        let ld = log_dir(dir);
        std::fs::create_dir_all(&ld)?;
        let maps = Maps::open(&g, &[0; NFILES])?;
        if maps.ctl(C_MAGIC) != MAGIC {
            return Err(StoreError::Corrupt(format!("{} is not an AtomicDB v2 graph", g.display())));
        }
        if maps.ctl(C_FORMAT) != FORMAT_VERSION {
            return Err(StoreError::Corrupt(format!("graph format {} (this build reads {})", maps.ctl(C_FORMAT), FORMAT_VERSION)));
        }
        let ckpt = checkpoint::read(&g.join("ckpt"))?.ok_or_else(|| StoreError::Corrupt("no valid checkpoint record".into()))?;
        let manifest = Manifest::read(&g.join("manifest"))?.unwrap_or_else(Manifest::empty);
        let mut cfg = cfg;
        cfg.shard_bits = maps.ctl(C_SHARD_BITS) as u32;
        let segs = log::scan(&ld)?;
        let sh = Arc::new(Shared::new(dir, cfg.clone(), maps, ckpt.seq, Vec::new(), ckpt, manifest));
        let mut rep = RecoveryReport { checkpoint_seq: ckpt.seq, last_seq: ckpt.seq, ..Default::default() };
        let mut last = ckpt.seq;
        let mut kept: Vec<Seg> = Vec::new();
        for (i, s) in segs.iter().enumerate() {
            let r = log::read_segment(&s.path)?;
            let is_last = i + 1 == segs.len();
            if r.torn && !is_last {
                let later: usize = segs[i + 1..].iter().map(|t| log::read_segment(&t.path).map(|r| r.frames.len()).unwrap_or(1)).sum();
                if later > 0 {
                    return Err(StoreError::Corrupt(format!("bad frame in the middle of the log: {} at byte {}", s.path.display(), r.end)));
                }
            }
            for &(at, len) in &r.frames {
                let fr = log::parse_body(&r.data[at..at + len])
                    .ok_or_else(|| StoreError::Corrupt(format!("bad frame body in {}", s.path.display())))?;
                if fr.seq <= last {
                    continue;
                }
                if fr.seq != last + 1 {
                    return Err(StoreError::Corrupt(format!("log jumps from {last} to {} in {}", fr.seq, s.path.display())));
                }
                let mut need = [0u64; NFILES];
                let mut recs = Vec::new();
                for rec in log::redo_records(fr.redo) {
                    let (f, off, b) = rec?;
                    if f == F_KM_REBUILD {
                        let (_, region, slots) = crate::batch::decode_rebuild(b)
                            .ok_or_else(|| StoreError::Corrupt("malformed key map rebuild record".into()))?;
                        let k = F_KM_ARENA as usize;
                        need[k] = need[k].max(region + region_bytes(slots));
                    } else if f as usize >= NFILES {
                        return Err(StoreError::Corrupt(format!("redo record for file {f}")));
                    } else {
                        need[f as usize] = need[f as usize].max(off + b.len() as u64);
                    }
                    recs.push((f, off, b));
                }
                sh.ensure(&need)?;
                sh.apply(fr.seq, recs.iter().copied())?;
                last = fr.seq;
                rep.frames_replayed += 1;
                rep.bytes_replayed += len as u64;
            }
            if r.torn || r.end < r.data.len() {
                // Cut the segment where the valid frames end: a torn tail or the
                // unused preallocation.  Writing resumes in a new segment.
                rep.torn_tail_bytes += if r.torn { (r.data.len() - r.end) as u64 } else { 0 };
                let f = std::fs::OpenOptions::new().write(true).open(&s.path)?;
                f.set_len(r.end as u64)?;
                // A torn tail must be gone for good before new segments follow
                // (only the last segment may end badly); unused zeros may stay.
                if r.torn {
                    f.sync_all()?;
                }
            }
            if r.frames.is_empty() && r.end == 0 {
                std::fs::remove_file(&s.path)?;
            } else {
                kept.push(s.clone());
            }
        }
        if let Some(first) = kept.first()
            && first.first > ckpt.seq + 1
        {
            return Err(StoreError::Corrupt(format!("log starts at {} but the checkpoint is at {}", first.first, ckpt.seq)));
        }
        rep.last_seq = last;
        let log = LogWriter::create(&ld, last + 1, cfg.seg_bytes, cfg.prealloc)?;
        kept.push(Seg { first: last + 1, path: log.path() });
        *sh.segs.lock().unwrap() = kept;
        rep.micros = t0.elapsed().as_micros() as u64;
        let thread = if cfg.background_checkpoint {
            let s2 = sh.clone();
            sh.bg.store(true, Ordering::Release);
            Some(std::thread::Builder::new().name("av2-checkpoint".into()).spawn(move || checkpoint_thread(s2))?)
        } else {
            None
        };
        Ok(Store { sh, log, seq: last, thread, recovery: rep })
    }

    pub fn dir(&self) -> &Path {
        &self.sh.dir
    }

    pub fn config(&self) -> &Config {
        &self.sh.cfg
    }

    /// Sequence number of the last committed batch.
    pub fn last_seq(&self) -> u64 {
        self.seq
    }

    /// A new overlay on the committed state.
    pub fn batch(&self) -> Batch {
        Batch::new(self.sh.maps.load_full())
    }

    pub fn reader(&self) -> Reader {
        Reader { sh: self.sh.clone() }
    }

    /// Commit `batch` with the encoded `facts` as the next frame.  Returns its
    /// sequence number once the frame is durable and applied.
    pub fn commit(&mut self, batch: Batch, facts: &[u8]) -> Result<u64> {
        if let Some(e) = self.sh.ctl.lock().unwrap().error.clone() {
            return Err(StoreError::Checkpoint(e));
        }
        let seq = self.seq + 1;
        let fin: Finalized = batch.finalize(seq, facts)?;
        self.sh.ensure(&fin.need)?;
        let t0 = Instant::now();
        if let Some(seg) = self.log.append(seq, &fin.body, self.sh.cfg.durable)? {
            self.sh.segs.lock().unwrap().push(seg);
        }
        let t1 = Instant::now();
        let body = &fin.body;
        self.sh.apply(seq, fin.ops.iter().map(|o| (o.file, o.off, &body[o.at..o.at + o.len])))?;
        let t2 = Instant::now();
        self.seq = seq;
        let frame = (fin.body.len() + log::FRAME_HEADER) as u64;
        {
            let mut st = self.sh.stats.lock().unwrap();
            st.commits += 1;
            st.log_bytes += frame;
            st.fsync_micros += (t1 - t0).as_micros() as u64;
            st.apply_micros += (t2 - t1).as_micros() as u64;
        }
        let since = self.sh.since_ckpt.fetch_add(frame, Ordering::Relaxed) + frame;
        if since >= self.sh.cfg.ckpt_bytes {
            if self.thread.is_some() {
                let mut c = self.sh.ctl.lock().unwrap();
                if !c.requested {
                    c.requested = true;
                    self.sh.cv.notify_all();
                }
            } else {
                self.sh.since_ckpt.store(0, Ordering::Relaxed);
                self.sh.checkpoint()?;
            }
        }
        Ok(seq)
    }

    /// Checkpoint now, in the calling thread (waits for a running one).
    pub fn checkpoint(&self) -> Result<u64> {
        self.sh.checkpoint_covering_now()
    }

    /// Clean shutdown: checkpoint, stop the checkpoint thread.
    pub fn close(mut self) -> Result<()> {
        self.stop_thread();
        self.sh.checkpoint()?;
        self.log.sync()?;
        Ok(())
    }

    fn stop_thread(&mut self) {
        if let Some(t) = self.thread.take() {
            self.sh.bg.store(false, Ordering::Release);
            self.sh.ctl.lock().unwrap().stop = true;
            self.sh.cv.notify_all();
            let _ = t.join();
        }
    }

    pub fn stats(&self) -> Stats {
        self.sh.stats.lock().unwrap().clone()
    }

    /// Bytes in use per file, and their total (what a backup copies).
    pub fn live_bytes(&self) -> ([u64; NFILES], u64) {
        let maps = self.sh.maps.load();
        let l = live_lens(&View::new(&maps));
        (l, l.iter().sum())
    }

    /// Bytes of the log segments currently kept.
    pub fn log_bytes(&self) -> u64 {
        self.sh.segs.lock().unwrap().iter().filter_map(|s| std::fs::metadata(&s.path).ok()).map(|m| m.len()).sum()
    }

    /// Verify the page checksums of every chunk not written since the last
    /// manifest.  Returns `(chunks checked, chunks mismatching)`.
    pub fn scrub(&self) -> Result<ScrubReport> {
        scrub(&self.sh)
    }

    /// Copy a consistent, recoverable image of the store to `dest` while the
    /// writer keeps running: checkpoint, copy `graph/`, copy the log from the
    /// checkpoint on.  Opening `dest` replays that log, as crash recovery does.
    pub fn backup(&self, dest: &Path) -> Result<BackupReport> {
        backup(&self.sh, dest)
    }

    /// The backup entry point for another thread (the writer keeps the `Store`).
    pub fn backup_handle(&self) -> BackupHandle {
        BackupHandle { sh: self.sh.clone() }
    }
}

impl Drop for Store {
    fn drop(&mut self) {
        self.stop_thread();
    }
}

fn init_ctl(maps: &Maps, shard_bits: u32) {
    let c = maps.f(F_CTL);
    let mut b = [0u8; 72];
    let mut put = |off: usize, v: u64| b[off..off + 8].copy_from_slice(&v.to_le_bytes());
    put(C_MAGIC, MAGIC);
    put(C_FORMAT, FORMAT_VERSION);
    put(C_SHARD_BITS, shard_bits as u64);
    put(C_POSITIONS, 0);
    put(C_EXTS, 0);
    put(C_OUT_TAIL, 0);
    put(C_INX_TAIL, 1);
    put(C_BLOB_TAIL, 8);
    put(C_KM_TAIL, 0);
    c.put(0, &b);
}

pub(crate) fn init_ctl_for_loader(maps: &Maps, shard_bits: u32) {
    init_ctl(maps, shard_bits)
}

#[derive(Clone, Debug, Default)]
pub struct ScrubReport {
    pub manifest_seq: u64,
    pub checked: u64,
    pub skipped: u64,
    pub mismatches: Vec<(String, u64)>,
}

fn scrub(sh: &Shared) -> Result<ScrubReport> {
    let man = sh.manifest.lock().unwrap().clone();
    let maps = sh.maps.load_full();
    let mut rep = ScrubReport { manifest_seq: man.seq, ..Default::default() };
    for (f, chunks) in man.files.iter().enumerate() {
        for (c, &(h, l)) in chunks.iter().enumerate() {
            let m = sh.chunk_mod.lock().unwrap()[f].get(c).copied().unwrap_or(0);
            if l == 0 || m > man.seq {
                rep.skipped += 1;
                continue;
            }
            let Some(b) = maps.m[f].get(c as u64 * CHUNK, l as usize) else {
                rep.mismatches.push((NAMES[f].to_string(), c as u64));
                continue;
            };
            let got = xxhash_rust::xxh3::xxh3_64(b);
            let m2 = sh.chunk_mod.lock().unwrap()[f].get(c).copied().unwrap_or(0);
            if m2 > man.seq {
                rep.skipped += 1;
                continue;
            }
            rep.checked += 1;
            if got != h {
                rep.mismatches.push((NAMES[f].to_string(), c as u64));
            }
        }
    }
    Ok(rep)
}

#[derive(Clone, Debug, Default)]
pub struct BackupReport {
    pub checkpoint_seq: u64,
    pub graph_bytes: u64,
    pub log_bytes: u64,
    pub segments: u64,
    pub micros: u64,
}

#[derive(Clone)]
pub struct BackupHandle {
    sh: Arc<Shared>,
}

impl BackupHandle {
    pub fn backup(&self, dest: &Path) -> Result<BackupReport> {
        backup(&self.sh, dest)
    }
}

fn copy_prefix(src: &Path, dst: &Path, len: u64) -> Result<u64> {
    let mut i = std::fs::File::open(src)?;
    let mut o = std::fs::File::create(dst)?;
    let mut left = len;
    let mut buf = vec![0u8; 4 << 20];
    while left > 0 {
        let n = (left as usize).min(buf.len());
        i.read_exact(&mut buf[..n])?;
        o.write_all(&buf[..n])?;
        left -= n as u64;
    }
    o.sync_all()?;
    Ok(len)
}

fn backup(sh: &Shared, dest: &Path) -> Result<BackupReport> {
    let t0 = Instant::now();
    // Pin the log before the checkpoint: a later checkpoint of the background
    // thread must not release the frames this copy will need (s >= now).
    {
        let now = sh.applied.load(Ordering::Acquire);
        let mut p = sh.pin.lock().unwrap();
        *p = Some(p.map_or(now + 1, |q| q.min(now + 1)));
    }
    let s = match sh.checkpoint_covering_now() {
        Ok(s) => s,
        Err(e) => {
            *sh.pin.lock().unwrap() = None;
            return Err(e);
        }
    };
    let res = (|| -> Result<BackupReport> {
        let gd = graph_dir(dest);
        let ld = log_dir(dest);
        std::fs::create_dir_all(&gd)?;
        std::fs::create_dir_all(&ld)?;
        let mut rep = BackupReport { checkpoint_seq: s, ..Default::default() };
        // Graph files: the live prefix at copy time (what frames after `s`
        // add beyond it is replayed from the log).
        let maps = sh.maps.load_full();
        let live = live_lens(&View::new(&maps));
        for f in 0..NFILES {
            let len = live[f].min(maps.m[f].len());
            rep.graph_bytes += copy_prefix(&maps.m[f].path, &gd.join(NAMES[f]), len)?;
        }
        checkpoint::write(&gd.join("ckpt"), Ckpt { generation: 1, seq: s })?;
        // Log: every segment that may hold frames after `s`, oldest first; the
        // segment being written last (a torn tail there is cut on open).
        let segs = sh.segs.lock().unwrap().clone();
        for (i, sg) in segs.iter().enumerate() {
            let next_first = segs.get(i + 1).map(|n| n.first);
            if next_first.is_some_and(|n| n <= s + 1) {
                continue;
            }
            let len = std::fs::metadata(&sg.path)?.len();
            rep.log_bytes += copy_prefix(&sg.path, &ld.join(sg.path.file_name().unwrap()), len)?;
            rep.segments += 1;
        }
        log::sync_dir(&gd)?;
        log::sync_dir(&ld)?;
        rep.micros = t0.elapsed().as_micros() as u64;
        Ok(rep)
    })();
    *sh.pin.lock().unwrap() = None;
    res
}

impl Reader {
    /// Run `f` on a consistent snapshot of the committed graph.  `f` may run
    /// more than once (if a batch was applied meanwhile) and must not have
    /// side effects other than building its result.
    pub fn read<R>(&self, mut f: impl FnMut(&View<'_>) -> R) -> R {
        loop {
            let e = loop {
                let e = self.sh.epoch.load(Ordering::Acquire);
                if e.is_multiple_of(2) {
                    break e;
                }
                std::hint::spin_loop();
            };
            let maps = self.sh.maps.load();
            let r = f(&View::new(&maps));
            std::sync::atomic::fence(Ordering::Acquire);
            if self.sh.epoch.load(Ordering::Acquire) == e {
                return r;
            }
        }
    }

    /// Sequence number of the last applied batch.
    pub fn applied_seq(&self) -> u64 {
        self.sh.applied.load(Ordering::Acquire)
    }
}
