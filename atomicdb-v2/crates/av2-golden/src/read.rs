//! Reading the recorder's files: `.jsonl`, `.jsonl.gz` (gzip members) and
//! `.jsonl.zst` (one zstd frame per case), one JSON case per line.
//!
//! The recorder appends one compressed frame per case to a file that stays
//! open for an hour, so a file copied from production usually ends in a torn
//! frame: reading stops there and says so (`Cases::truncated`).

use serde_json::Value;
use std::io::{BufRead, BufReader, Read};
use std::path::{Path, PathBuf};

/// The case files under `paths` (files are taken as given, directories are
/// walked for `*.jsonl`, `*.jsonl.gz`, `*.jsonl.zst`), sorted by path.
pub fn case_files(paths: &[PathBuf]) -> Vec<PathBuf> {
    let mut out = Vec::new();
    for p in paths {
        if p.is_dir() {
            walk(p, &mut out);
        } else {
            out.push(p.clone());
        }
    }
    out.sort();
    out
}

fn walk(dir: &Path, out: &mut Vec<PathBuf>) {
    let Ok(rd) = std::fs::read_dir(dir) else { return };
    for e in rd.flatten() {
        let p = e.path();
        if p.is_dir() {
            walk(&p, out);
        } else {
            let n = p.file_name().and_then(|n| n.to_str()).unwrap_or("");
            if n.ends_with(".jsonl") || n.ends_with(".jsonl.gz") || n.ends_with(".jsonl.zst") {
                out.push(p);
            }
        }
    }
}

/// The cases of one file.
pub struct Cases {
    lines: Box<dyn Iterator<Item = Result<Vec<u8>, String>>>,
    /// Set when the file ended in a torn frame or line.
    pub truncated: Option<String>,
}

impl Iterator for Cases {
    type Item = Result<Value, String>;
    fn next(&mut self) -> Option<Self::Item> {
        loop {
            match self.lines.next()? {
                Err(e) => {
                    self.truncated = Some(e);
                    return None;
                }
                Ok(line) => {
                    let t = line.trim_ascii();
                    if t.is_empty() {
                        continue;
                    }
                    return Some(serde_json::from_slice(t).map_err(|e| format!("bad JSON case: {e}")));
                }
            }
        }
    }
}

pub fn open(path: &Path) -> Result<Cases, String> {
    let name = path.to_string_lossy().to_string();
    let file = std::fs::File::open(path).map_err(|e| format!("{name}: {e}"))?;
    let lines: Box<dyn Iterator<Item = Result<Vec<u8>, String>>> = if name.ends_with(".zst") {
        let mut bytes = Vec::new();
        BufReader::new(file).read_to_end(&mut bytes).map_err(|e| format!("{name}: {e}"))?;
        Box::new(ZstFrames { bytes: std::io::Cursor::new(bytes), pending: Vec::new().into_iter(), done: false })
    } else if name.ends_with(".gz") {
        Box::new(Lines(Box::new(BufReader::new(flate2::read::MultiGzDecoder::new(file)))))
    } else {
        Box::new(Lines(Box::new(BufReader::new(file))))
    };
    Ok(Cases { lines, truncated: None })
}

struct Lines(Box<dyn BufRead>);

impl Iterator for Lines {
    type Item = Result<Vec<u8>, String>;
    fn next(&mut self) -> Option<Self::Item> {
        let mut buf = Vec::new();
        match self.0.read_until(b'\n', &mut buf) {
            Ok(0) => None,
            Ok(_) if buf.last() != Some(&b'\n') => {
                // A last line without its newline: complete JSON or a torn write.
                if serde_json::from_slice::<serde::de::IgnoredAny>(buf.trim_ascii()).is_ok() {
                    Some(Ok(buf))
                } else {
                    Some(Err(format!("torn last line ({} bytes)", buf.len())))
                }
            }
            Ok(_) => Some(Ok(buf)),
            Err(e) => Some(Err(format!("read error: {e}"))),
        }
    }
}

/// Concatenated zstd frames, each decoded whole and split into lines.
struct ZstFrames {
    bytes: std::io::Cursor<Vec<u8>>,
    pending: std::vec::IntoIter<Vec<u8>>,
    done: bool,
}

impl Iterator for ZstFrames {
    type Item = Result<Vec<u8>, String>;
    fn next(&mut self) -> Option<Self::Item> {
        loop {
            if let Some(l) = self.pending.next() {
                return Some(Ok(l));
            }
            if self.done || self.bytes.position() as usize >= self.bytes.get_ref().len() {
                return None;
            }
            let at = self.bytes.position();
            let mut out = Vec::new();
            let r = ruzstd::decoding::StreamingDecoder::new(&mut self.bytes)
                .map_err(|e| e.to_string())
                .and_then(|mut d| d.read_to_end(&mut out).map_err(|e| e.to_string()));
            if let Err(e) = r {
                self.done = true;
                return Some(Err(format!("torn zstd frame at byte {at}: {e}")));
            }
            if self.bytes.position() == at {
                self.done = true;
                return None;
            }
            self.pending = out.split(|&b| b == b'\n').filter(|l| !l.is_empty()).map(<[u8]>::to_vec).collect::<Vec<_>>().into_iter();
        }
    }
}
