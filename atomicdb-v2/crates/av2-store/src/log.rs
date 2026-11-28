//! The redo log: segment files of CRC-protected frames.
//!
//! `log/<first seq, 16 digits>.seg` holds consecutive frames:
//!
//! ```text
//! frame = [len u32][crc32 u32][body: len bytes]
//! body  = [seq u64][facts_len u32][facts][redo record]*
//! redo  = [file u8][offset u64][len u32][bytes]
//! ```
//!
//! A frame is written with one `write` and made durable with `fdatasync`
//! before any of its redo records is applied to the maps.
//!
//! **Segments are written in place, never extended.**  With `prealloc`, every
//! segment is a file of `seg_bytes` whose blocks were all written (with
//! zeros) before the writer gets it, so a commit's `fdatasync` flushes data
//! only: no size change, no block allocation, no unwritten-extent
//! conversion, nothing for the filesystem journal.  (On the production box
//! that is 6.5 ms per fdatasync instead of 13.8 ms; `fallocate` alone would
//! leave unwritten extents whose conversion is journaled.)  A background
//! thread (`SegPool`) keeps a zeroed spare ready, recycling released
//! segments when it can, so rolling over is a rename.  A zero frame header
//! marks the end of the written part.
//!
//! On recovery the last segment is kept: a torn tail (an incomplete frame,
//! or one that fails its CRC) is overwritten with zeros and made durable,
//! and writing resumes after the last valid frame.

use crate::StoreError;
use std::fs::{File, OpenOptions};
use std::io::{self, Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};

pub const FRAME_HEADER: usize = 8;
/// Largest frame body the log writes (a torn write lies within one frame
/// after the last valid one, so recovery reads only that far).
pub const MAX_FRAME: usize = 16 << 20;
/// Spare segments the pool keeps ready.
const SPARES: usize = 1;
/// Released segments kept for recycling beyond the ready ones.
const MAX_DIRTY: usize = 2;

#[derive(Clone, Debug)]
pub struct Seg {
    pub first: u64,
    pub path: PathBuf,
}

pub fn seg_name(first: u64) -> String {
    format!("{first:016}.seg")
}

/// Segments of `dir`, sorted by first sequence number.
pub fn scan(dir: &Path) -> io::Result<Vec<Seg>> {
    let mut v = Vec::new();
    for e in std::fs::read_dir(dir)? {
        let e = e?;
        let name = e.file_name().to_string_lossy().to_string();
        // `archive/` holds the same segments compressed (`.seg.zst`).
        if let Some(stem) = name.strip_suffix(".seg").or_else(|| name.strip_suffix(".seg.zst"))
            && let Ok(first) = stem.parse::<u64>()
        {
            v.push(Seg { first, path: e.path() });
        }
    }
    v.sort_by_key(|s| s.first);
    Ok(v)
}

/// Make a directory entry change durable (Unix); a no-op on Windows, where
/// NTFS journals metadata and `std` cannot open a directory for syncing.
pub fn sync_dir(dir: &Path) -> io::Result<()> {
    #[cfg(unix)]
    {
        File::open(dir)?.sync_all()?;
    }
    let _ = dir;
    Ok(())
}

/// What one append did and how long its parts took.
pub struct Appended {
    pub rolled: Option<Seg>,
    /// Building the frame, rolling the segment if needed, `write`.
    pub write: std::time::Duration,
    /// `fdatasync` (or `fsync` when the size changed).
    pub sync: std::time::Duration,
    /// The file had to grow for this frame (its fsync covers metadata).
    pub grew: bool,
}

/// The fsync a written frame still needs.
pub struct PendingSync {
    file: File,
    full: bool,
}

impl PendingSync {
    pub fn run(&self) -> io::Result<()> {
        if self.full { self.file.sync_all() } else { self.file.sync_data() }
    }
}

/// Zeroed, fully written spare segments, prepared by a background thread.
#[derive(Clone)]
pub struct SegPool {
    inner: std::sync::Arc<PoolInner>,
}

struct PoolInner {
    dir: PathBuf,
    seg_bytes: u64,
    st: std::sync::Mutex<PoolState>,
    cv: std::sync::Condvar,
}

#[derive(Default)]
struct PoolState {
    ready: Vec<PathBuf>,
    dirty: Vec<PathBuf>,
    next: u64,
    stop: bool,
    error: Option<String>,
    /// Segments zero-filled by the pool: new files and recycled ones.
    created: u64,
    recycled: u64,
    /// Rolls that found no spare and zero-filled a segment themselves.
    waited: u64,
}

/// Counters of the segment pool.
#[derive(Clone, Copy, Debug, Default)]
pub struct PoolStats {
    pub created: u64,
    pub recycled: u64,
    pub waited: u64,
}

fn spare_id(name: &str) -> Option<u64> {
    name.strip_prefix("spare-").and_then(|r| r.strip_suffix(".seg")).and_then(|r| r.parse().ok())
}

/// Write zeros over the first `len` bytes of `path` (creating it), so every
/// block is allocated and written, and make that durable.
fn zero_fill(path: &Path, len: u64) -> io::Result<()> {
    let mut f = OpenOptions::new().read(true).write(true).create(true).truncate(false).open(path)?;
    f.set_len(len)?;
    let zeros = vec![0u8; 1 << 20];
    f.seek(SeekFrom::Start(0))?;
    let mut left = len;
    while left > 0 {
        let n = left.min(zeros.len() as u64) as usize;
        f.write_all(&zeros[..n])?;
        left -= n as u64;
    }
    f.sync_all()
}

impl SegPool {
    /// Start the pool for `dir`; spare files left by an earlier run are
    /// zeroed again before use.
    pub fn start(dir: &Path, seg_bytes: u64) -> io::Result<(SegPool, std::thread::JoinHandle<()>)> {
        let mut st = PoolState::default();
        for e in std::fs::read_dir(dir)? {
            let e = e?;
            if let Some(id) = spare_id(&e.file_name().to_string_lossy()) {
                st.next = st.next.max(id + 1);
                st.dirty.push(e.path());
            }
        }
        let inner = std::sync::Arc::new(PoolInner {
            dir: dir.to_path_buf(),
            seg_bytes,
            st: std::sync::Mutex::new(st),
            cv: std::sync::Condvar::new(),
        });
        let i2 = inner.clone();
        let h = std::thread::Builder::new().name("av2-segpool".into()).spawn(move || pool_thread(i2))?;
        Ok((SegPool { inner }, h))
    }

    pub fn stop(&self) {
        self.inner.st.lock().unwrap().stop = true;
        self.inner.cv.notify_all();
    }

    pub fn stats(&self) -> PoolStats {
        let s = self.inner.st.lock().unwrap();
        PoolStats { created: s.created, recycled: s.recycled, waited: s.waited }
    }

    /// A zeroed segment, renamed to `to`.  Waits for the pool if it has
    /// none ready (counted in `waited`).
    fn take_as(&self, to: &Path) -> io::Result<()> {
        let mut s = self.inner.st.lock().unwrap();
        if s.ready.is_empty() {
            s.waited += 1;
            self.inner.cv.notify_all();
            while s.ready.is_empty() {
                if let Some(e) = &s.error {
                    return Err(io::Error::other(e.clone()));
                }
                if s.stop {
                    drop(s);
                    // The pool is gone: prepare one here.
                    return zero_fill(to, self.inner.seg_bytes);
                }
                s = self.inner.cv.wait(s).unwrap();
            }
        }
        let p = s.ready.pop().unwrap();
        self.inner.cv.notify_all();
        drop(s);
        std::fs::rename(&p, to)?;
        sync_dir(&self.inner.dir)
    }

    /// Give a released segment back for reuse (deleted if the pool has enough).
    pub fn recycle(&self, path: &Path) -> io::Result<()> {
        let mut s = self.inner.st.lock().unwrap();
        let full = s.dirty.len() + s.ready.len() >= SPARES + MAX_DIRTY;
        let wrong_size = std::fs::metadata(path).map(|m| m.len() != self.inner.seg_bytes).unwrap_or(true);
        if full || wrong_size || s.stop {
            drop(s);
            return std::fs::remove_file(path);
        }
        let to = self.inner.dir.join(format!("spare-{}.seg", s.next));
        s.next += 1;
        std::fs::rename(path, &to)?;
        s.dirty.push(to);
        self.inner.cv.notify_all();
        Ok(())
    }
}

fn pool_thread(p: std::sync::Arc<PoolInner>) {
    loop {
        let (path, recycled) = {
            let mut s = p.st.lock().unwrap();
            while !s.stop && s.ready.len() >= SPARES {
                s = p.cv.wait(s).unwrap();
            }
            if s.stop {
                return;
            }
            match s.dirty.pop() {
                Some(d) => (d, true),
                None => {
                    let n = s.next;
                    s.next += 1;
                    (p.dir.join(format!("spare-{n}.seg")), false)
                }
            }
        };
        let r = zero_fill(&path, p.seg_bytes).and_then(|_| sync_dir(&p.dir));
        let mut s = p.st.lock().unwrap();
        match r {
            Ok(()) => {
                if recycled {
                    s.recycled += 1;
                } else {
                    s.created += 1;
                }
                s.ready.push(path);
            }
            Err(e) => {
                s.error = Some(format!("preparing {}: {e}", path.display()));
            }
        }
        p.cv.notify_all();
        if s.error.is_some() {
            return;
        }
    }
}

pub struct LogWriter {
    dir: PathBuf,
    file: File,
    pub cur_first: u64,
    pos: u64,
    cap: u64,
    seg_bytes: u64,
    pool: Option<SegPool>,
    buf: Vec<u8>,
}

impl LogWriter {
    /// Start a new segment whose first frame will be `first`: a zeroed
    /// spare from `pool`, or (without a pool) a plain file that grows.
    pub fn create(dir: &Path, first: u64, seg_bytes: u64, pool: Option<SegPool>) -> io::Result<LogWriter> {
        let path = dir.join(seg_name(first));
        let (file, cap) = match &pool {
            Some(p) => {
                p.take_as(&path)?;
                let f = OpenOptions::new().read(true).write(true).open(&path)?;
                let cap = f.metadata()?.len();
                (f, cap)
            }
            None => {
                let f = OpenOptions::new().read(true).write(true).create(true).truncate(true).open(&path)?;
                sync_dir(dir)?;
                (f, 0)
            }
        };
        Ok(LogWriter { dir: dir.to_path_buf(), file, cur_first: first, pos: 0, cap, seg_bytes, pool, buf: Vec::new() })
    }

    /// Continue writing segment `path` (named for `first`) at byte `pos`.
    pub fn resume(dir: &Path, path: &Path, first: u64, pos: u64, seg_bytes: u64, pool: Option<SegPool>) -> io::Result<LogWriter> {
        let file = OpenOptions::new().read(true).write(true).open(path)?;
        let cap = file.metadata()?.len();
        Ok(LogWriter { dir: dir.to_path_buf(), file, cur_first: first, pos, cap, seg_bytes, pool, buf: Vec::new() })
    }

    pub fn path(&self) -> PathBuf {
        self.dir.join(seg_name(self.cur_first))
    }

    /// Append one frame and make it durable.  `rolled` is the new segment
    /// when it rolled over.
    pub fn append(&mut self, seq: u64, body: &[u8], durable: bool) -> io::Result<Appended> {
        let (mut app, sync) = self.write_frame(seq, body)?;
        if durable {
            let t = std::time::Instant::now();
            sync.run()?;
            app.sync = t.elapsed();
        }
        Ok(app)
    }

    /// Write one frame without waiting for it to be durable; the returned
    /// `PendingSync` does that (from any thread).
    pub fn write_frame(&mut self, seq: u64, body: &[u8]) -> io::Result<(Appended, PendingSync)> {
        let t0 = std::time::Instant::now();
        if body.len() > MAX_FRAME {
            return Err(io::Error::other(format!("frame of {} bytes, more than the {MAX_FRAME} a frame may hold", body.len())));
        }
        let len = (FRAME_HEADER + body.len()) as u64;
        let mut rolled = None;
        let limit = if self.pool.is_some() { self.cap.max(self.seg_bytes) } else { self.seg_bytes };
        if self.pos > 0 && self.pos + len > limit {
            self.file.sync_all()?;
            *self = LogWriter::create(&self.dir, seq, self.seg_bytes, self.pool.take())?;
            rolled = Some(Seg { first: seq, path: self.path() });
        }
        self.buf.clear();
        self.buf.extend_from_slice(&(body.len() as u32).to_le_bytes());
        self.buf.extend_from_slice(&crc32fast::hash(body).to_le_bytes());
        self.buf.extend_from_slice(body);
        // Inside a prepared segment the file never grows; it grows for a
        // frame larger than a segment, and always without a pool (plain
        // appends: tests and tools).
        let mut grew = false;
        if self.pos + len > self.cap {
            if self.pool.is_some() {
                self.cap = (self.pos + len).next_multiple_of(1 << 20);
                self.file.set_len(self.cap)?;
            } else {
                self.cap = self.pos + len;
            }
            grew = true;
        }
        self.file.seek(SeekFrom::Start(self.pos))?;
        self.file.write_all(&self.buf)?;
        self.pos += len;
        let pending = PendingSync { file: self.file.try_clone()?, full: grew };
        Ok((Appended { rolled, write: t0.elapsed(), sync: std::time::Duration::ZERO, grew }, pending))
    }

    pub fn sync(&mut self) -> io::Result<()> {
        self.file.sync_all()
    }
}

/// One frame read back from the log.
pub struct Frame<'a> {
    pub seq: u64,
    pub facts: &'a [u8],
    pub redo: &'a [u8],
}

pub fn parse_body(body: &[u8]) -> Option<Frame<'_>> {
    if body.len() < 12 {
        return None;
    }
    let seq = u64::from_le_bytes(body[..8].try_into().unwrap());
    let flen = u32::from_le_bytes(body[8..12].try_into().unwrap()) as usize;
    if 12 + flen > body.len() {
        return None;
    }
    Some(Frame { seq, facts: &body[12..12 + flen], redo: &body[12 + flen..] })
}

/// Iterate the redo records of a frame: `(file, offset, bytes)`.  A record
/// is `[file u8][offset varint][length varint][bytes]`.
pub fn redo_records(redo: &[u8]) -> impl Iterator<Item = Result<(u8, u64, &[u8]), StoreError>> {
    let mut p = 0usize;
    std::iter::from_fn(move || {
        if p >= redo.len() {
            return None;
        }
        let file = redo[p];
        let mut q = p + 1;
        let parsed =
            crate::batch::get_varint(redo, &mut q).and_then(|off| crate::batch::get_varint(redo, &mut q).map(|l| (off, l as usize)));
        match parsed {
            Some((off, l)) if q + l <= redo.len() => {
                let r = (file, off, &redo[q..q + l]);
                p = q + l;
                Some(Ok(r))
            }
            _ => {
                p = redo.len();
                Some(Err(StoreError::Corrupt("redo record cut short".into())))
            }
        }
    })
}

/// Result of reading one segment.
pub struct SegRead {
    pub data: Vec<u8>,
    /// `(offset of body, body length)` of every valid frame, in order.
    pub frames: Vec<(usize, usize)>,
    /// Byte offset just past the last valid frame.
    pub end: usize,
    /// True when something other than zeros followed the last valid frame
    /// (a torn or corrupt frame).
    pub torn: bool,
}

pub fn read_segment(path: &Path) -> io::Result<SegRead> {
    let mut f = File::open(path)?;
    let mut data = Vec::new();
    if path.extension().is_some_and(|e| e == "zst") {
        f.read_to_end(&mut data)?;
        let mut dec = ruzstd::decoding::StreamingDecoder::new(&data[..]).map_err(io::Error::other)?;
        let mut out = Vec::new();
        dec.read_to_end(&mut out)?;
        data = out;
        return Ok(parse_segment(data, usize::MAX));
    }
    // Read only what is needed: the frames, then a window of one maximal
    // frame after them (a torn write lies within it).  A prepared segment is
    // mostly zeros and need not be read in full at every open.
    let flen = f.metadata()?.len() as usize;
    let mut fill = |data: &mut Vec<u8>, upto: usize| -> io::Result<()> {
        let upto = upto.min(flen);
        if data.len() < upto {
            let want = (upto - data.len()).max(4 << 20).min(flen - data.len());
            let at = data.len();
            data.resize(at + want, 0);
            f.read_exact(&mut data[at..])?;
        }
        Ok(())
    };
    let mut pos = 0usize;
    loop {
        fill(&mut data, pos + FRAME_HEADER)?;
        if data.len() < pos + FRAME_HEADER {
            break;
        }
        let len = u32::from_le_bytes(data[pos..pos + 4].try_into().unwrap()) as usize;
        let crc = u32::from_le_bytes(data[pos + 4..pos + 8].try_into().unwrap());
        if len == 0 && crc == 0 {
            break;
        }
        fill(&mut data, pos + FRAME_HEADER + len.min(MAX_FRAME))?;
        if !(12..=MAX_FRAME).contains(&len)
            || data.len() < pos + FRAME_HEADER + len
            || crc32fast::hash(&data[pos + 8..pos + 8 + len]) != crc
        {
            break;
        }
        pos += FRAME_HEADER + len;
    }
    fill(&mut data, pos + FRAME_HEADER + MAX_FRAME)?;
    Ok(parse_segment(data, pos + FRAME_HEADER + MAX_FRAME))
}

fn parse_segment(data: Vec<u8>, window: usize) -> SegRead {
    let mut frames = Vec::new();
    let mut pos = 0usize;
    let mut torn = false;
    while pos + FRAME_HEADER <= data.len() {
        let len = u32::from_le_bytes(data[pos..pos + 4].try_into().unwrap()) as usize;
        let crc = u32::from_le_bytes(data[pos + 4..pos + 8].try_into().unwrap());
        if len == 0 && crc == 0 {
            // End of the written part (or preallocated zeros).
            break;
        }
        if len < 12 || pos + FRAME_HEADER + len > data.len() || crc32fast::hash(&data[pos + 8..pos + 8 + len]) != crc {
            torn = true;
            break;
        }
        frames.push((pos + FRAME_HEADER, len));
        pos += FRAME_HEADER + len;
    }
    let tail = &data[pos..data.len().min(window.max(pos))];
    if !torn && tail.iter().any(|&b| b != 0) {
        torn = true;
    }
    SegRead { data, frames, end: pos, torn }
}

/// Overwrite with zeros whatever follows the last valid frame of a segment
/// (a torn frame), up to its last non-zero byte, and make it durable, so
/// that writing can resume after the valid frames.
pub fn zero_tail(path: &Path, r: &SegRead) -> io::Result<u64> {
    let Some(last) = r.data[r.end..].iter().rposition(|&b| b != 0) else { return Ok(0) };
    let n = last as u64 + 1;
    let mut f = OpenOptions::new().write(true).open(path)?;
    f.seek(SeekFrom::Start(r.end as u64))?;
    let zeros = vec![0u8; (n as usize).min(1 << 20)];
    let mut left = n;
    while left > 0 {
        let k = left.min(zeros.len() as u64) as usize;
        f.write_all(&zeros[..k])?;
        left -= k as u64;
    }
    f.sync_all()?;
    Ok(n)
}

/// Walk every valid frame of the log in `dir` (for tools).  Stops at a torn
/// tail of the last segment; a bad frame anywhere else is an error.
pub fn for_each_frame(dir: &Path, mut f: impl FnMut(&Seg, Frame<'_>) -> Result<(), StoreError>) -> Result<u64, StoreError> {
    let segs = scan(dir)?;
    let mut n = 0u64;
    for (i, s) in segs.iter().enumerate() {
        let r = read_segment(&s.path)?;
        if r.torn && i + 1 < segs.len() && segs[i + 1..].iter().any(|t| std::fs::metadata(&t.path).map(|m| m.len() > 0).unwrap_or(false)) {
            // A torn frame followed by more segments: only the last segment may have one,
            // unless the later ones hold no frame at all.
            let later_frames: usize = segs[i + 1..].iter().map(|t| read_segment(&t.path).map(|r| r.frames.len()).unwrap_or(0)).sum();
            if later_frames > 0 {
                return Err(StoreError::Corrupt(format!("bad frame in the middle of the log: {} at byte {}", s.path.display(), r.end)));
            }
        }
        for &(at, len) in &r.frames {
            let fr =
                parse_body(&r.data[at..at + len]).ok_or_else(|| StoreError::Corrupt(format!("bad frame body in {}", s.path.display())))?;
            f(s, fr)?;
            n += 1;
        }
    }
    Ok(n)
}
