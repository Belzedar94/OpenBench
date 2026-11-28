//! Checkpoint record and page-checksum manifest.
//!
//! `graph/ckpt` holds two 4 KiB slots.  A checkpoint writes the slot of its
//! generation parity and fsyncs; recovery takes the valid slot with the
//! highest generation.  A torn slot write therefore leaves the previous
//! checkpoint intact.
//!
//! `graph/manifest` holds, for each file, one xxh3 checksum per 1 MiB chunk
//! of its live part, taken by the checkpoint thread for chunks no batch
//! changed after the checkpoint's sequence number.  The scrubber recomputes
//! them; a mismatch on a chunk nothing has written since is silent
//! corruption.

use crate::layout::{CHUNK, NFILES};
use std::fs::{File, OpenOptions};
use std::io::{self, Read, Seek, SeekFrom, Write};
use std::path::Path;

const CKPT_MAGIC: u64 = 0x5450_4b43_3256_4124; // "$AV2CKPT"
const SLOT_BYTES: u64 = 4096;

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Ckpt {
    pub generation: u64,
    pub seq: u64,
}

fn encode_slot(c: Ckpt) -> [u8; 28] {
    let mut b = [0u8; 28];
    b[0..8].copy_from_slice(&CKPT_MAGIC.to_le_bytes());
    b[8..16].copy_from_slice(&c.generation.to_le_bytes());
    b[16..24].copy_from_slice(&c.seq.to_le_bytes());
    let crc = crc32fast::hash(&b[..24]);
    b[24..28].copy_from_slice(&crc.to_le_bytes());
    b
}

fn decode_slot(b: &[u8]) -> Option<Ckpt> {
    if b.len() < 28 || u64::from_le_bytes(b[0..8].try_into().unwrap()) != CKPT_MAGIC {
        return None;
    }
    if crc32fast::hash(&b[..24]) != u32::from_le_bytes(b[24..28].try_into().unwrap()) {
        return None;
    }
    Some(Ckpt { generation: u64::from_le_bytes(b[8..16].try_into().unwrap()), seq: u64::from_le_bytes(b[16..24].try_into().unwrap()) })
}

pub fn read(path: &Path) -> io::Result<Option<Ckpt>> {
    let mut data = Vec::new();
    match File::open(path) {
        Ok(mut f) => {
            f.read_to_end(&mut data)?;
        }
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(None),
        Err(e) => return Err(e),
    }
    let a = decode_slot(&data);
    let b = data.get(SLOT_BYTES as usize..).and_then(decode_slot);
    Ok(match (a, b) {
        (Some(x), Some(y)) => Some(if x.generation >= y.generation { x } else { y }),
        (x, y) => x.or(y),
    })
}

pub fn write(path: &Path, c: Ckpt) -> io::Result<()> {
    let mut f = OpenOptions::new().read(true).write(true).create(true).truncate(false).open(path)?;
    if f.metadata()?.len() < 2 * SLOT_BYTES {
        f.set_len(2 * SLOT_BYTES)?;
    }
    f.seek(SeekFrom::Start((c.generation % 2) * SLOT_BYTES))?;
    f.write_all(&encode_slot(c))?;
    f.sync_all()
}

/// Checksums of the live part of every file at one checkpoint.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Manifest {
    pub seq: u64,
    /// Per file, per chunk: `(xxh3 of the hashed bytes, hashed length)`;
    /// length 0 = unknown (the chunk changed after `seq`).
    pub files: Vec<Vec<(u64, u32)>>,
}

impl Manifest {
    pub fn empty() -> Manifest {
        Manifest { seq: 0, files: vec![Vec::new(); NFILES] }
    }

    pub fn encode(&self) -> Vec<u8> {
        let mut b = Vec::new();
        b.extend_from_slice(b"AV2MANI1");
        b.extend_from_slice(&self.seq.to_le_bytes());
        b.extend_from_slice(&(self.files.len() as u32).to_le_bytes());
        for f in &self.files {
            b.extend_from_slice(&(f.len() as u64).to_le_bytes());
            for (h, l) in f {
                b.extend_from_slice(&h.to_le_bytes());
                b.extend_from_slice(&l.to_le_bytes());
            }
        }
        let crc = crc32fast::hash(&b);
        b.extend_from_slice(&crc.to_le_bytes());
        b
    }

    pub fn decode(b: &[u8]) -> Option<Manifest> {
        if b.len() < 24 || &b[..8] != b"AV2MANI1" {
            return None;
        }
        let (body, crc) = b.split_at(b.len() - 4);
        if crc32fast::hash(body) != u32::from_le_bytes(crc.try_into().unwrap()) {
            return None;
        }
        let seq = u64::from_le_bytes(body[8..16].try_into().unwrap());
        let nf = u32::from_le_bytes(body[16..20].try_into().unwrap()) as usize;
        let mut p = 20usize;
        let mut files = Vec::with_capacity(nf);
        for _ in 0..nf {
            let n = u64::from_le_bytes(body.get(p..p + 8)?.try_into().unwrap()) as usize;
            p += 8;
            let mut v = Vec::with_capacity(n);
            for _ in 0..n {
                let h = u64::from_le_bytes(body.get(p..p + 8)?.try_into().unwrap());
                let l = u32::from_le_bytes(body.get(p + 8..p + 12)?.try_into().unwrap());
                v.push((h, l));
                p += 12;
            }
            files.push(v);
        }
        Some(Manifest { seq, files })
    }

    pub fn read(path: &Path) -> io::Result<Option<Manifest>> {
        match std::fs::read(path) {
            Ok(b) => Ok(Manifest::decode(&b)),
            Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(None),
            Err(e) => Err(e),
        }
    }

    /// Atomic replace: write a temporary file, fsync, rename.
    pub fn write(&self, path: &Path) -> io::Result<()> {
        let tmp = path.with_extension("tmp");
        {
            let mut f = File::create(&tmp)?;
            f.write_all(&self.encode())?;
            f.sync_all()?;
        }
        std::fs::rename(&tmp, path)?;
        if let Some(d) = path.parent() {
            crate::log::sync_dir(d)?;
        }
        Ok(())
    }
}

/// Chunk index range covering `[0, live)`.
pub fn chunks(live: u64) -> u64 {
    live.div_ceil(CHUNK)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn slots_survive_a_torn_write() {
        let d = tempfile::tempdir().unwrap();
        let p = d.path().join("ckpt");
        assert_eq!(read(&p).unwrap(), None);
        write(&p, Ckpt { generation: 1, seq: 10 }).unwrap();
        write(&p, Ckpt { generation: 2, seq: 20 }).unwrap();
        assert_eq!(read(&p).unwrap(), Some(Ckpt { generation: 2, seq: 20 }));
        // Tear the slot generation 3 would write (parity 1, offset 4096).
        let mut b = std::fs::read(&p).unwrap();
        b[4096 + 8] ^= 0xFF;
        std::fs::write(&p, &b).unwrap();
        assert_eq!(read(&p).unwrap(), Some(Ckpt { generation: 2, seq: 20 }));
    }

    #[test]
    fn manifest_roundtrip() {
        let mut m = Manifest::empty();
        m.seq = 9;
        m.files[3] = vec![(1, 2), (3, 0)];
        assert_eq!(Manifest::decode(&m.encode()), Some(m.clone()));
        let mut b = m.encode();
        b[30] ^= 1;
        assert_eq!(Manifest::decode(&b), None);
    }
}
