//! Memory-mapped files that can grow while readers hold the old mapping.
//!
//! Each file of `graph/` is one `Mapped`.  Growing a file creates a new
//! mapping of the same file at the new length and publishes a new `Maps`
//! (see `Shared::maps`); readers that still hold the old `Maps` keep a valid
//! mapping of the prefix they knew.  Both mappings show the same pages, so a
//! write through one is visible through the other.

use crate::layout::{GROW_ALIGN, NAMES, NFILES};
use memmap2::{MmapOptions, MmapRaw};
use std::fs::{File, OpenOptions};
use std::io;
use std::path::{Path, PathBuf};
use std::sync::Arc;

pub struct Mapped {
    pub file: File,
    map: Option<MmapRaw>,
    len: u64,
    pub path: PathBuf,
}

// All access goes through raw pointers; the writer is the only mutator and
// readers validate with the epoch (see `store.rs`).
unsafe impl Send for Mapped {}
unsafe impl Sync for Mapped {}

impl Mapped {
    /// Open (or create) `path`, extending it to at least `min_len`.
    pub fn open(path: &Path, min_len: u64) -> io::Result<Mapped> {
        let file = OpenOptions::new().read(true).write(true).create(true).truncate(false).open(path)?;
        let mut len = file.metadata()?.len();
        if len < min_len {
            len = min_len.next_multiple_of(GROW_ALIGN);
            file.set_len(len)?;
            file.sync_all()?;
        }
        let map = if len > 0 { Some(MmapOptions::new().len(len as usize).map_raw(&file)?) } else { None };
        Ok(Mapped { file, map, len, path: path.to_path_buf() })
    }

    #[inline]
    pub fn len(&self) -> u64 {
        self.len
    }

    #[inline]
    pub fn is_empty(&self) -> bool {
        self.len == 0
    }

    #[inline]
    pub fn ptr(&self) -> *mut u8 {
        self.map.as_ref().map(|m| m.as_mut_ptr()).unwrap_or(std::ptr::null_mut())
    }

    /// Bytes `[off, off+len)` if they are inside the mapping.
    #[inline]
    pub fn get(&self, off: u64, len: usize) -> Option<&[u8]> {
        if off.checked_add(len as u64)? > self.len {
            return None;
        }
        // SAFETY: in bounds of a live mapping; concurrent writes are tolerated
        // by the callers (the writer owns the data, readers re-validate).
        Some(unsafe { std::slice::from_raw_parts(self.ptr().add(off as usize), len) })
    }

    /// Write `bytes` at `off`.  Only the writer thread (or a loader that owns
    /// the store exclusively) calls this.
    #[inline]
    pub fn put(&self, off: u64, bytes: &[u8]) {
        assert!(
            off + bytes.len() as u64 <= self.len,
            "write past the end of {}: {}+{} > {}",
            self.path.display(),
            off,
            bytes.len(),
            self.len
        );
        // SAFETY: bounds checked above.
        unsafe { std::ptr::copy_nonoverlapping(bytes.as_ptr(), self.ptr().add(off as usize), bytes.len()) }
    }

    /// Make every write done through any mapping of this file durable.
    pub fn flush(&self) -> io::Result<()> {
        if let Some(m) = &self.map {
            m.flush()?;
        }
        self.file.sync_data()
    }

    /// Make `[off, off+len)` durable (used for regions written before the
    /// frame that makes them reachable).
    pub fn flush_range(&self, off: u64, len: u64) -> io::Result<()> {
        if let Some(m) = &self.map {
            m.flush_range(off as usize, len as usize)?;
        }
        self.file.sync_data()
    }
}

/// One mapping per file of `graph/`.
pub struct Maps {
    pub m: [Arc<Mapped>; NFILES],
}

impl Maps {
    pub fn open(dir: &Path, min_lens: &[u64; NFILES]) -> io::Result<Maps> {
        let mut v = Vec::with_capacity(NFILES);
        for i in 0..NFILES {
            v.push(Arc::new(Mapped::open(&dir.join(NAMES[i]), min_lens[i])?));
        }
        Ok(Maps { m: v.try_into().ok().unwrap() })
    }

    #[inline]
    pub fn f(&self, f: u8) -> &Mapped {
        &self.m[f as usize]
    }

    /// A copy of these maps with file `f` grown to at least `need` bytes
    /// (by at least half its size, so that growth is amortised).
    pub fn grown(&self, f: u8, need: u64) -> io::Result<Maps> {
        let cur = self.f(f);
        let target = need.max(cur.len() + cur.len() / 2).max(GROW_ALIGN);
        let new = Mapped::open(&cur.path, target)?;
        let mut m = self.m.clone();
        m[f as usize] = Arc::new(new);
        Ok(Maps { m })
    }

    #[inline]
    pub fn ctl(&self, off: usize) -> u64 {
        u64::from_le_bytes(self.f(crate::layout::F_CTL).get(off as u64, 8).unwrap().try_into().unwrap())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn grow_while_old_mapping_is_alive() {
        let d = tempfile::tempdir().unwrap();
        let p = d.path().join("x");
        let a = Mapped::open(&p, 100).unwrap();
        a.put(10, b"hello");
        let b = Mapped::open(&p, a.len() * 4).unwrap();
        assert!(b.len() >= a.len() * 4);
        assert_eq!(b.get(10, 5).unwrap(), b"hello");
        b.put(12, b"LL");
        assert_eq!(a.get(10, 5).unwrap(), b"heLLo", "both mappings show the same pages");
        b.put(b.len() - 1, b"z");
        assert!(a.get(a.len() - 1 + 5, 1).is_none());
        a.flush().unwrap();
        b.flush_range(0, 4096).unwrap();
    }
}
