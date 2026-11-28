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
use std::sync::{Arc, Condvar, Mutex, mpsc};
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
    /// Compact the key map once its garbage reaches its live size and at
    /// least this many bytes.
    pub km_compact_min: u64,
    /// zstd level of `archive/`: kept for the day a C zstd is linked; the
    /// pure-Rust encoder used now has one level (about zstd's 1).
    pub archive_zstd_level: i32,
    /// Simulation only: sleep this long after every fsync (to measure the
    /// writer against a slow disk, e.g. production's 16 ms md RAID1).
    pub fsync_delay: Duration,
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
            km_compact_min: 64 << 20,
            archive_zstd_level: 3,
            fsync_delay: Duration::ZERO,
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

/// Where the time of the last commit went.
#[derive(Clone, Copy, Debug, Default)]
pub struct CommitTimings {
    /// The overlay turned into redo records.
    pub finalize: Duration,
    /// Frame header and CRC, segment roll, `write`.
    pub write: Duration,
    /// Growing graph files before the apply (rare; `set_len`, remap).
    pub grow: Duration,
    pub fsync: Duration,
    /// How long `commit_finish` waited for the fsync (less than `fsync`
    /// when the writer computed the next batch meanwhile).
    pub fsync_wait: Duration,
    /// Redo records applied to the maps (between the epoch bumps).
    pub apply: Duration,
    pub frame_bytes: u64,
    pub ops: u64,
}

#[derive(Clone, Debug, Default)]
pub struct Stats {
    pub commits: u64,
    pub log_bytes: u64,
    pub finalize_micros: u64,
    pub write_micros: u64,
    pub fsync_micros: u64,
    pub apply_micros: u64,
    pub checkpoints: u64,
    pub checkpoint_micros_total: u64,
    pub checkpoint_micros_max: u64,
    pub manifest_chunks_hashed: u64,
    pub grows: u64,
    pub segments_released: u64,
    pub compactions: u64,
    /// Log segments the pool zero-filled: new files, recycled ones, and
    /// rolls that had to wait for one.
    pub segments_prepared: u64,
    pub segments_recycled: u64,
    pub segment_waits: u64,
    /// Facts written to `archive/`, before and after compression.
    pub archive_raw_bytes: u64,
    pub archive_bytes: u64,
}

pub(crate) struct CkptCtl {
    requested: bool,
    stop: bool,
    error: Option<String>,
    done_seq: u64,
    done_km_gen: u64,
    generation: u64,
    count: u64,
}

pub(crate) struct Shared {
    pub(crate) maps: ArcSwap<Maps>,
    epoch: AtomicU64,
    applied: AtomicU64,
    /// Generation of the key-map arena currently mapped.
    km_gen: AtomicU64,
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
    /// Spare log segments (with `prealloc`).
    pool: Mutex<Option<log::SegPool>>,
}

/// A commit whose frame is written and whose fsync is in flight.
pub struct InFlight {
    seq: u64,
    batch: Arc<Batch>,
    fin: Finalized,
    done: mpsc::Receiver<std::io::Result<Duration>>,
    result: Option<std::io::Result<Duration>>,
    finalize: Duration,
    grow: Duration,
    write: Duration,
}

impl InFlight {
    pub fn seq(&self) -> u64 {
        self.seq
    }

    /// True once the frame's fsync has completed (it is then durable and
    /// `commit_finish` will not wait).  Never blocks.
    pub fn is_durable(&mut self) -> bool {
        if self.result.is_none()
            && let Ok(r) = self.done.try_recv()
        {
            self.result = Some(r);
        }
        self.result.is_some()
    }
}

/// Called by the sync thread once a frame is durable (see `commit_start_notify`).
pub type Notify = Box<dyn FnOnce() + Send>;

type SyncJob = (log::PendingSync, mpsc::SyncSender<std::io::Result<Duration>>, Option<Notify>);

fn sync_thread(rx: mpsc::Receiver<SyncJob>, delay: Duration) {
    while let Ok((p, reply, notify)) = rx.recv() {
        let t = Instant::now();
        let r = p.run();
        if !delay.is_zero() {
            // Simulation only (`Config::fsync_delay`): a slower disk.
            std::thread::sleep(delay);
        }
        let _ = reply.send(r.map(|_| t.elapsed()));
        if let Some(n) = notify {
            n();
        }
    }
}

pub struct Store {
    syncer: Option<mpsc::Sender<SyncJob>>,
    sync_handle: Option<JoinHandle<()>>,
    pool_handle: Option<JoinHandle<()>>,
    in_flight: bool,
    sh: Arc<Shared>,
    log: LogWriter,
    seq: u64,
    thread: Option<JoinHandle<()>>,
    pub recovery: RecoveryReport,
    last: CommitTimings,
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
            km_gen: AtomicU64::new(ckpt.km_gen),
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
                done_km_gen: ckpt.km_gen,
                generation: ckpt.generation,
                count: 0,
            }),
            cv: Condvar::new(),
            ckpt_lock: Mutex::new(()),
            manifest: Mutex::new(manifest),
            stats: Mutex::new(Stats::default()),
            bg: std::sync::atomic::AtomicBool::new(false),
            pool: Mutex::new(None),
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
        let mut maps = self.maps.load_full();
        // Chunks this frame touches, marked before any byte changes (the
        // checkpoint's manifest relies on it).
        let mut marks: Vec<(u8, u64, usize)> = Vec::new();
        for (f, o, b) in ops.clone() {
            match f {
                F_KM_REBUILD => {
                    let (_, region, slots) = crate::batch::decode_rebuild(b).unwrap_or_default();
                    marks.push((F_KM_ARENA, region, region_bytes(slots) as usize));
                    marks.push((F_KM_DIR, o * DIR_ENTRY as u64, DIR_ENTRY));
                }
                F_KM_INSERT => {
                    let block = crate::batch::decode_insert(b).map(|x| x.1).unwrap_or(0);
                    marks.push((F_KM_ARENA, o, SLOT));
                    marks.push((F_KM_ARENA, block, 64));
                }
                F_KM_COMPACT => {
                    // The whole new arena, the directory and the control block.
                    marks.push((F_KM_ARENA, 0, maps.f(F_KM_ARENA).len() as usize));
                    marks.push((F_KM_DIR, 0, maps.f(F_KM_DIR).len() as usize));
                    marks.push((F_CTL, 0, CTL_SIZE as usize));
                }
                _ => marks.push((f, o, b.len())),
            }
        }
        self.mark(seq, marks.into_iter());
        let shard_bits = maps.ctl(C_SHARD_BITS) as u32;
        let e = self.epoch.fetch_add(1, Ordering::AcqRel);
        debug_assert!(e.is_multiple_of(2));
        let mut bad = None;
        for (f, off, b) in ops {
            match f {
                F_KM_REBUILD => {
                    let Some((old, region, slots)) = crate::batch::decode_rebuild(b) else {
                        bad = Some("malformed key map rebuild record".into());
                        break;
                    };
                    match crate::batch::rebuild_region(&maps, shard_bits, &old, region, slots) {
                        Ok((nsh, r)) if region + r.len() as u64 <= maps.f(F_KM_ARENA).len() => {
                            maps.f(F_KM_ARENA).put(region, &r);
                            maps.f(F_KM_DIR).put(off * DIR_ENTRY as u64, &nsh.encode());
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
                }
                F_KM_INSERT => {
                    let a = maps.f(F_KM_ARENA);
                    match crate::batch::decode_insert(b) {
                        Some((slot, block)) if off + SLOT as u64 <= a.len() && block + 64 <= a.len() => {
                            a.put(off, slot);
                            let fp = av2_model::Fingerprint(slot[..12].try_into().unwrap());
                            let mut blk: [u8; 64] = a.get(block, 64).unwrap().try_into().unwrap();
                            for bit in crate::view::filter_bits(&fp) {
                                blk[bit as usize / 8] |= 1 << (bit % 8);
                            }
                            a.put(block, &blk);
                        }
                        _ => {
                            bad = Some(format!("malformed or out-of-bounds key map insert at {off}"));
                            break;
                        }
                    }
                }
                F_KM_COMPACT => match self.compact(&maps, off, b) {
                    Ok(m) => maps = m,
                    Err(e) => {
                        bad = Some(e.to_string());
                        break;
                    }
                },
                _ => {
                    if (f as usize) >= NFILES || off + b.len() as u64 > maps.m[f as usize].len() {
                        bad = Some(format!("redo record outside file {f}: {off}+{}", b.len()));
                        break;
                    }
                    maps.m[f as usize].put(off, b);
                }
            }
        }
        self.applied.store(seq, Ordering::Release);
        self.epoch.fetch_add(1, Ordering::AcqRel);
        match bad {
            Some(s) => Err(StoreError::Corrupt(s)),
            None => Ok(()),
        }
    }

    /// Apply a compaction record: copy every live region named by it from the
    /// current arena generation into a new one, packed in shard order, then
    /// point the directory and the counters at it and publish the new maps.
    fn compact(&self, maps: &Arc<Maps>, new_gen: u64, b: &[u8]) -> Result<Arc<Maps>> {
        let (old_gen, live) =
            crate::batch::decode_compact(b).ok_or_else(|| StoreError::Corrupt("malformed key map compaction record".into()))?;
        let cur = self.km_gen.load(Ordering::Acquire);
        if old_gen != cur || new_gen != old_gen + 1 {
            return Err(StoreError::Corrupt(format!("compaction from generation {old_gen} to {new_gen} while generation {cur} is mapped")));
        }
        let total: u64 = live.iter().map(|(_, sh)| region_bytes(sh.slots)).sum();
        let path = graph_dir(&self.dir).join(arena_name(new_gen));
        let new = crate::mapped::Mapped::open(&path, total.max(GROW_ALIGN))?;
        let old = maps.f(F_KM_ARENA);
        let dir = maps.f(F_KM_DIR);
        let mut at = 0u64;
        for (s, sh) in &live {
            let n = region_bytes(sh.slots);
            let src = old
                .get(sh.region, n as usize)
                .ok_or_else(|| StoreError::Corrupt(format!("key map shard {s} outside generation {old_gen}")))?;
            new.put(at, src);
            dir.put(s * DIR_ENTRY as u64, &crate::view::Shard { region: at, ..*sh }.encode());
            at += n;
        }
        let ctl = maps.f(F_CTL);
        ctl.put(C_KM_TAIL as u64, &total.to_le_bytes());
        ctl.put(C_KM_LIVE as u64, &total.to_le_bytes());
        ctl.put(C_KM_GEN as u64, &new_gen.to_le_bytes());
        let next = Arc::new(maps.with(F_KM_ARENA, new));
        self.maps.store(next.clone());
        self.km_gen.store(new_gen, Ordering::Release);
        self.stats.lock().unwrap().compactions += 1;
        Ok(next)
    }

    /// Delete the arena files whose generation `drop` selects (best effort: a
    /// file still mapped by a reader is retried at the next checkpoint).
    fn delete_arenas(&self, drop: impl Fn(u64) -> bool) {
        if let Ok(rd) = std::fs::read_dir(graph_dir(&self.dir)) {
            for e in rd.flatten() {
                if let Some(g) = arena_generation(&e.file_name().to_string_lossy())
                    && drop(g)
                {
                    let _ = std::fs::remove_file(e.path());
                }
            }
        }
    }

    /// One checkpoint: flush the maps, write the manifest, record the
    /// sequence number, release the segments it covers.  Safe to run while
    /// the writer commits.
    fn checkpoint(&self) -> Result<u64> {
        self.checkpoint_gen().map(|(s, _)| s)
    }

    /// A checkpoint; returns its sequence number and key-map generation.
    fn checkpoint_gen(&self) -> Result<(u64, u64)> {
        let _one = self.ckpt_lock.lock().unwrap();
        let t0 = Instant::now();
        // Sequence number, key-map generation and maps, read together while
        // no batch is being applied (the epoch is even and does not move).
        let (s, km_gen, maps) = loop {
            let e = self.epoch.load(Ordering::Acquire);
            if !e.is_multiple_of(2) {
                std::hint::spin_loop();
                continue;
            }
            let s = self.applied.load(Ordering::Acquire);
            let g = self.km_gen.load(Ordering::Acquire);
            let m = self.maps.load_full();
            if self.epoch.load(Ordering::Acquire) == e {
                break (s, g, m);
            }
        };
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
        checkpoint::write(&graph_dir(&self.dir).join("ckpt"), Ckpt { generation, seq: s, km_gen })?;
        let released = self.release_segments(s)?;
        // Older generations are needed by no recovery from now on (a backup in
        // progress may still copy one: not while the log is pinned).
        if self.pin.lock().unwrap().is_none() {
            self.delete_arenas(|g| g < km_gen);
        }
        let us = t0.elapsed().as_micros() as u64;
        {
            let mut c = self.ctl.lock().unwrap();
            c.generation = generation;
            c.count = count;
            c.done_seq = s;
            c.done_km_gen = km_gen;
        }
        let mut st = self.stats.lock().unwrap();
        st.checkpoints += 1;
        st.checkpoint_micros_total += us;
        st.checkpoint_micros_max = st.checkpoint_micros_max.max(us);
        st.segments_released += released;
        Ok((s, km_gen))
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
                let (raw, packed) = archive_facts(&g.path, &d, self.cfg.archive_zstd_level)?;
                let mut st = self.stats.lock().unwrap();
                st.archive_raw_bytes += raw;
                st.archive_bytes += packed;
            }
            match self.pool.lock().unwrap().as_ref() {
                Some(p) => p.recycle(&g.path)?,
                None => std::fs::remove_file(&g.path)?,
            }
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
        self.checkpoint_covering_now_gen().map(|(s, _)| s)
    }

    fn checkpoint_covering_now_gen(&self) -> Result<(u64, u64)> {
        if !self.bg.load(Ordering::Acquire) {
            self.since_ckpt.store(0, Ordering::Relaxed);
            return self.checkpoint_gen();
        }
        let target = self.applied.load(Ordering::Acquire);
        let mut c = self.ctl.lock().unwrap();
        loop {
            if let Some(e) = &c.error {
                return Err(StoreError::Checkpoint(e.clone()));
            }
            if c.done_seq >= target && c.done_seq > 0 || target == 0 && c.count > 0 {
                return Ok((c.done_seq, c.done_km_gen));
            }
            if c.stop {
                drop(c);
                return self.checkpoint_gen();
            }
            c.requested = true;
            self.cv.notify_all();
            c = self.cv.wait_timeout(c, Duration::from_millis(100)).unwrap().0;
        }
    }
}

/// Rewrite segment `seg` into `dir` with the facts of every frame and none
/// of its redo records, compressed with zstd (`<name>.seg.zst`; atomic:
/// temporary file, fsync, rename).
fn archive_facts(seg: &Path, dir: &Path, level: i32) -> Result<(u64, u64)> {
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
    // Pure-Rust zstd (ruzstd, about zstd level 1): the store links no C,
    // so a static musl binary cross-compiles from anywhere.
    let _ = level;
    let packed = ruzstd::encoding::compress_to_vec(&out[..], ruzstd::encoding::CompressionLevel::Fastest);
    let name = format!("{}.zst", seg.file_name().unwrap().to_string_lossy());
    let tmp = dir.join(format!("{name}.tmp"));
    {
        let mut f = std::fs::File::create(&tmp)?;
        f.write_all(&packed)?;
        f.sync_all()?;
    }
    std::fs::rename(&tmp, dir.join(name))?;
    log::sync_dir(dir)?;
    Ok((out.len() as u64, packed.len() as u64))
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
        let maps = Maps::open(&g, &initial_lens(&cfg), 0)?;
        init_ctl(&maps, cfg.shard_bits);
        for m in maps.m.iter() {
            m.flush()?;
        }
        checkpoint::write(&g.join("ckpt"), Ckpt { generation: 1, seq: 0, km_gen: 0 })?;
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
        let ckpt = checkpoint::read(&g.join("ckpt"))?.ok_or_else(|| StoreError::Corrupt("no valid checkpoint record".into()))?;
        if !g.join(arena_name(ckpt.km_gen)).exists() {
            return Err(StoreError::Corrupt(format!("key map arena generation {} is missing", ckpt.km_gen)));
        }
        let maps = Maps::open(&g, &[0; NFILES], ckpt.km_gen)?;
        if maps.ctl(C_MAGIC) != MAGIC {
            return Err(StoreError::Corrupt(format!("{} is not an AtomicDB v2 graph", g.display())));
        }
        if maps.ctl(C_FORMAT) != FORMAT_VERSION {
            return Err(StoreError::Corrupt(format!("graph format {} (this build reads {})", maps.ctl(C_FORMAT), FORMAT_VERSION)));
        }
        let manifest = Manifest::read(&g.join("manifest"))?.unwrap_or_else(Manifest::empty);
        let mut cfg = cfg;
        cfg.shard_bits = maps.ctl(C_SHARD_BITS) as u32;
        let segs = log::scan(&ld)?;
        let sh = Arc::new(Shared::new(dir, cfg.clone(), maps, ckpt.seq, Vec::new(), ckpt, manifest));
        let mut rep = RecoveryReport { checkpoint_seq: ckpt.seq, last_seq: ckpt.seq, ..Default::default() };
        let mut last = ckpt.seq;
        let mut kept: Vec<Seg> = Vec::new();
        let mut resume: Option<(Seg, u64)> = None;
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
                    let k = F_KM_ARENA as usize;
                    if f == F_KM_REBUILD {
                        let (_, region, slots) = crate::batch::decode_rebuild(b)
                            .ok_or_else(|| StoreError::Corrupt("malformed key map rebuild record".into()))?;
                        need[k] = need[k].max(region + region_bytes(slots));
                    } else if f == F_KM_INSERT {
                        let (_, block) =
                            crate::batch::decode_insert(b).ok_or_else(|| StoreError::Corrupt("malformed key map insert record".into()))?;
                        need[k] = need[k].max((off + SLOT as u64).max(block + 64));
                    } else if f == F_KM_COMPACT {
                        // The compaction creates its own file.
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
            // A torn frame is overwritten with zeros (durably) before anything
            // follows it: only the end of the log may hold one.
            if r.torn {
                rep.torn_tail_bytes += log::zero_tail(&s.path, &r)?;
            }
            // Writing resumes in the last segment, after its valid frames, if
            // its name fits the next sequence number.
            let fits = !r.frames.is_empty() || s.first == last + 1;
            if is_last && fits {
                resume = Some((s.clone(), r.end as u64));
                kept.push(s.clone());
            } else if r.frames.is_empty() {
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
        // Generations newer than the one replay ended at belong to compactions
        // that never became durable; older than the checkpoint's are dead.
        let (lo, hi) = (ckpt.km_gen, sh.km_gen.load(Ordering::Acquire));
        sh.delete_arenas(|g| g < lo || g > hi);
        let (pool, pool_handle) = match cfg.prealloc {
            true => {
                let (p, h) = log::SegPool::start(&ld, cfg.seg_bytes)?;
                (Some(p), Some(h))
            }
            false => (None, None),
        };
        let log = match resume {
            Some((seg, pos)) => LogWriter::resume(&ld, &seg.path, seg.first, pos, cfg.seg_bytes, pool.clone())?,
            None => {
                let l = LogWriter::create(&ld, last + 1, cfg.seg_bytes, pool.clone())?;
                kept.push(Seg { first: last + 1, path: l.path() });
                l
            }
        };
        *sh.segs.lock().unwrap() = kept;
        *sh.pool.lock().unwrap() = pool.clone();
        rep.micros = t0.elapsed().as_micros() as u64;
        let thread = if cfg.background_checkpoint {
            let s2 = sh.clone();
            sh.bg.store(true, Ordering::Release);
            Some(std::thread::Builder::new().name("av2-checkpoint".into()).spawn(move || checkpoint_thread(s2))?)
        } else {
            None
        };
        let (stx, srx) = mpsc::channel::<SyncJob>();
        let delay = cfg.fsync_delay;
        let sync_handle = Some(std::thread::Builder::new().name("av2-fsync".into()).spawn(move || sync_thread(srx, delay))?);
        Ok(Store {
            syncer: Some(stx),
            sync_handle,
            pool_handle,
            in_flight: false,
            sh,
            log,
            seq: last,
            thread,
            recovery: rep,
            last: CommitTimings::default(),
        })
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
        Batch::new(self.sh.maps.load_full(), self.sh.cfg.km_compact_min)
    }

    pub fn reader(&self) -> Reader {
        Reader { sh: self.sh.clone() }
    }

    /// Commit `batch` with the encoded `facts` as the next frame.  Returns its
    /// sequence number once the frame is durable and applied.
    pub fn commit(&mut self, batch: Batch, facts: &[u8]) -> Result<u64> {
        let f = self.commit_start(batch, facts)?;
        self.commit_finish(f)
    }

    /// First half of a commit: turn `batch` into a frame, write it, and start
    /// its fsync on the store's sync thread.  While it is in flight the
    /// writer can compute the next batch on `batch_after` (which reads
    /// through this one); `commit_finish` waits for the fsync and applies.
    /// One commit may be in flight at a time.
    pub fn commit_start(&mut self, batch: Batch, facts: &[u8]) -> Result<InFlight> {
        self.commit_start_notify(batch, facts, None)
    }

    /// `commit_start`, and call `notify` (on the sync thread) as soon as the
    /// frame is durable, so that a writer waiting on its own channel can wake.
    pub fn commit_start_notify(&mut self, mut batch: Batch, facts: &[u8], notify: Option<Notify>) -> Result<InFlight> {
        if let Some(e) = self.sh.ctl.lock().unwrap().error.clone() {
            return Err(StoreError::Checkpoint(e));
        }
        if self.in_flight {
            return Err(StoreError::Corrupt("a commit is already in flight".into()));
        }
        // Nothing is in flight, so the batch this one was built on (if any) is
        // applied: drop it and finalize against the current maps (that batch
        // may have published new ones, e.g. a compacted key map).
        batch.detach();
        batch.maps = self.sh.maps.load_full();
        let seq = self.seq + 1;
        let t0 = Instant::now();
        let fin: Finalized = batch.finalize(seq, facts)?;
        let t1 = Instant::now();
        self.sh.ensure(&fin.need)?;
        let grow = t1.elapsed();
        let (app, sync) = self.log.write_frame(seq, &fin.body)?;
        if let Some(seg) = app.rolled {
            self.sh.segs.lock().unwrap().push(seg);
        }
        let (tx, done) = mpsc::sync_channel(1);
        if self.sh.cfg.durable {
            self.syncer
                .as_ref()
                .expect("sync thread")
                .send((sync, tx, notify))
                .map_err(|_| StoreError::Corrupt("sync thread stopped".into()))?;
        } else {
            let _ = tx.send(Ok(Duration::ZERO));
            if let Some(n) = notify {
                n();
            }
        }
        self.in_flight = true;
        Ok(InFlight { seq, batch: Arc::new(batch), fin, done, result: None, finalize: t1 - t0, grow, write: app.write })
    }

    /// An overlay for the next batch while `f` is in flight.
    pub fn batch_after(&self, f: &InFlight) -> Batch {
        Batch::new_after(f.batch.clone(), self.sh.maps.load_full(), self.sh.cfg.km_compact_min)
    }

    /// Second half of a commit: wait for the frame's fsync, apply it, and
    /// return its sequence number.
    pub fn commit_finish(&mut self, f: InFlight) -> Result<u64> {
        let InFlight { seq, fin, done, result, finalize, grow, write, .. } = f;
        self.in_flight = false;
        let t_wait = Instant::now();
        let fsync_own = match result {
            Some(r) => r?,
            None => done.recv().map_err(|_| StoreError::Corrupt("sync thread stopped".into()))??,
        };
        let waited = t_wait.elapsed();
        let t2 = Instant::now();
        let body = &fin.body;
        self.sh.apply(seq, fin.ops.iter().map(|o| (o.file, o.off, &body[o.at..o.at + o.len])))?;
        let t3 = Instant::now();
        self.seq = seq;
        let frame = (fin.body.len() + log::FRAME_HEADER) as u64;
        self.last = CommitTimings {
            finalize,
            write,
            grow,
            fsync: fsync_own,
            fsync_wait: waited,
            apply: t3 - t2,
            frame_bytes: frame,
            ops: fin.ops.len() as u64,
        };
        {
            let mut st = self.sh.stats.lock().unwrap();
            st.commits += 1;
            st.log_bytes += frame;
            st.finalize_micros += finalize.as_micros() as u64;
            st.write_micros += write.as_micros() as u64;
            st.fsync_micros += fsync_own.as_micros() as u64;
            st.apply_micros += (t3 - t2).as_micros() as u64;
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

    /// Timings of the last commit.
    pub fn last_commit(&self) -> CommitTimings {
        self.last
    }

    pub fn stats(&self) -> Stats {
        let mut st = self.sh.stats.lock().unwrap().clone();
        if let Some(p) = self.sh.pool.lock().unwrap().as_ref() {
            let ps = p.stats();
            st.segments_prepared = ps.created;
            st.segments_recycled = ps.recycled;
            st.segment_waits = ps.waited;
        }
        st
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
        self.syncer = None;
        if let Some(h) = self.sync_handle.take() {
            let _ = h.join();
        }
        if let Some(p) = self.sh.pool.lock().unwrap().take() {
            p.stop();
        }
        if let Some(h) = self.pool_handle.take() {
            let _ = h.join();
        }
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
    let (s, gen_s) = match sh.checkpoint_covering_now_gen() {
        Ok(x) => x,
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
            let name = maps.m[f].path.file_name().unwrap().to_owned();
            rep.graph_bytes += copy_prefix(&maps.m[f].path, &gd.join(name), len)?;
        }
        // A compaction after the checkpoint: replay needs the older arena too
        // (it is frozen, and not deleted while the log is pinned).
        let cur_gen = sh.km_gen.load(Ordering::Acquire);
        if gen_s != cur_gen {
            let p = graph_dir(&sh.dir).join(arena_name(gen_s));
            let len = std::fs::metadata(&p)?.len();
            rep.graph_bytes += copy_prefix(&p, &gd.join(arena_name(gen_s)), len)?;
        }
        checkpoint::write(&gd.join("ckpt"), Ckpt { generation: 1, seq: s, km_gen: gen_s })?;
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
