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
//! before any of its redo records is applied to the maps.  Segments are
//! preallocated (zero-filled) 4 MiB at a time, so that the per-commit
//! `fdatasync` does not have to journal a size change for most frames; a zero
//! length marks the end of the written part.
//! On recovery the last segment is cut at the first frame that is incomplete
//! or fails its CRC (a torn tail), and writing resumes in a fresh segment.

use crate::StoreError;
use std::fs::{File, OpenOptions};
use std::io::{self, Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};

pub const FRAME_HEADER: usize = 8;
/// Preallocation step of a segment.
pub const PREALLOC_STEP: u64 = 4 << 20;

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

pub struct LogWriter {
    dir: PathBuf,
    file: File,
    pub cur_first: u64,
    pos: u64,
    cap: u64,
    seg_bytes: u64,
    prealloc: bool,
    buf: Vec<u8>,
}

impl LogWriter {
    /// Start a new segment whose first frame will be `first`.
    pub fn create(dir: &Path, first: u64, seg_bytes: u64, prealloc: bool) -> io::Result<LogWriter> {
        let path = dir.join(seg_name(first));
        let file = OpenOptions::new().read(true).write(true).create(true).truncate(true).open(&path)?;
        let cap = if prealloc {
            let c = PREALLOC_STEP.min(seg_bytes.max(1));
            file.set_len(c)?;
            c
        } else {
            0
        };
        // No fsync of the empty file here: the first frame's fdatasync makes
        // its data and size durable; the directory entry is synced now.
        sync_dir(dir)?;
        Ok(LogWriter { dir: dir.to_path_buf(), file, cur_first: first, pos: 0, cap, seg_bytes, prealloc, buf: Vec::new() })
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
        let len = (FRAME_HEADER + body.len()) as u64;
        let mut rolled = None;
        if self.pos > 0 && self.pos + len > self.seg_bytes {
            self.file.sync_all()?;
            *self = LogWriter::create(&self.dir, seq, self.seg_bytes, self.prealloc)?;
            rolled = Some(Seg { first: seq, path: self.path() });
        }
        self.buf.clear();
        self.buf.extend_from_slice(&(body.len() as u32).to_le_bytes());
        self.buf.extend_from_slice(&crc32fast::hash(body).to_le_bytes());
        self.buf.extend_from_slice(body);
        let mut grew = false;
        if self.prealloc && self.pos + len > self.cap {
            self.cap = (self.pos + len).next_multiple_of(PREALLOC_STEP);
            self.file.set_len(self.cap)?;
            grew = true;
        }
        self.file.seek(SeekFrom::Start(self.pos))?;
        self.file.write_all(&self.buf)?;
        self.pos += len;
        // The size changed: the sync must cover metadata too.
        let full = grew || self.pos > self.cap;
        if self.pos > self.cap {
            self.cap = self.pos;
        }
        let pending = PendingSync { file: self.file.try_clone()?, full };
        Ok((Appended { rolled, write: t0.elapsed(), sync: std::time::Duration::ZERO }, pending))
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
    let mut data = Vec::new();
    File::open(path)?.read_to_end(&mut data)?;
    if path.extension().is_some_and(|e| e == "zst") {
        data = zstd::decode_all(&data[..])?;
    }
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
    if !torn && pos < data.len() && data[pos..].iter().any(|&b| b != 0) {
        torn = true;
    }
    Ok(SegRead { data, frames, end: pos, torn })
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
