//! Atomic Syzygy tablebases for AtomicDB v2 (rules TB-1.1, TB-1.2).
//!
//! v1 probes with python-chess (`atomicdb/tb.py`: `chess.syzygy` with
//! `VariantBoard=AtomicBoard`); v2 probes the same files with
//! `shakmaty-syzygy`.  The contract is v1's, fail-closed:
//!
//! - a position that is not applicable (more than `max_pieces` men, a
//!   castling right, an en-passant square, a malformed FEN) gives `None`;
//! - tables that cannot be opened give `None` for every probe;
//! - any probe error (missing table, a position the library refuses) gives
//!   `None`;
//! - otherwise the WDL integer from the side to move's point of view, -2..2,
//!   under the fifty-move rule with the counter at zero (python-chess
//!   `probe_wdl` = shakmaty-syzygy `probe_wdl_after_zeroing`).
//!
//! The server never derives DTZ (TB-1.2); [`Tablebases::probe_dtz`] exists
//! only for the comparison tools.
//!
//! Probing is thread-safe: a `Tablebases` is `Sync` and every probe builds its
//! own position.

use shakmaty::fen::Fen;
use shakmaty::variant::Atomic;
use shakmaty::{CastlingMode, PositionError};
use shakmaty_syzygy::Tablebase;
use std::path::Path;

/// `tb.is_applicable(fen, max_pieces)`: the probe shape the worker accepts.
/// Castling field `-`, en-passant field `-`, at most `max_pieces` letters in
/// the board field; anything malformed is not applicable.
pub fn is_applicable(fen: &str, max_pieces: usize) -> bool {
    let parts: Vec<&str> = fen.split_whitespace().collect();
    if parts.len() < 4 {
        return false;
    }
    parts[2] == "-" && parts[3] == "-" && parts[0].chars().filter(|c| c.is_alphabetic()).count() <= max_pieces
}

/// Parse a FEN into an atomic position the way the probe needs it.  `None`
/// when shakmaty refuses the position.
///
/// python-chess's `AtomicBoard` validates nothing before a probe, so the two
/// refusals that do not stop a probe are lifted: an "impossible" check (an
/// explosion can clear two lines at once, which shakmaty's standard-chess
/// heuristic does not expect) and too much material.  A position where the
/// side not to move is in check (shakmaty's "opposite check") stays refused:
/// no legal move leads there, so v1's tree never holds one.
pub fn atomic_position(fen: &str) -> Option<Atomic> {
    let fen: Fen = fen.parse().ok()?;
    fen.into_position(CastlingMode::Standard)
        .or_else(PositionError::ignore_impossible_check)
        .or_else(PositionError::ignore_too_much_material)
        .ok()
}

/// Why shakmaty refuses a position, for the comparison tools.
pub fn position_error(fen: &str) -> Option<String> {
    let fen: Fen = match fen.parse() {
        Ok(f) => f,
        Err(e) => return Some(format!("fen: {e}")),
    };
    let r: Result<Atomic, _> = fen.into_position(CastlingMode::Standard);
    r.err().map(|e| e.to_string())
}

/// How the table files are read.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Access {
    /// Positioned reads (`read_at`), safe whatever happens to the files.
    Read,
    /// Memory maps, like python-chess: no system call per read.  The files
    /// must not change while mapped.
    Mmap,
}

/// The configured tables (v1: `settings.ATOMICDB_TB_PATHS`).
pub struct Tablebases {
    tables: Option<Tablebase<Atomic>>,
    files: usize,
}

impl Tablebases {
    /// Open every directory in `paths` (empty entries skipped, as v1 drops
    /// them).  A directory that cannot be read makes the whole set unusable,
    /// like v1's cached open failure: every probe then answers `None`.
    pub fn open<P: AsRef<Path>>(paths: &[P]) -> Tablebases {
        Tablebases::open_with(paths, Access::Read)
    }

    /// [Tablebases::open] with a choice of file access (see [Access]).
    pub fn open_with<P: AsRef<Path>>(paths: &[P], access: Access) -> Tablebases {
        let mut tb = match access {
            Access::Read => Tablebase::<Atomic>::new(),
            // SAFETY: the table files are never modified or truncated while
            // the service runs (they are installed once, read-only), which is
            // the contract of a memory map; python-chess maps them the same way.
            Access::Mmap => unsafe { Tablebase::<Atomic>::with_mmap_filesystem() },
        };
        let mut files = 0;
        let mut any = false;
        for p in paths {
            let p = p.as_ref();
            if p.as_os_str().is_empty() {
                continue;
            }
            any = true;
            match tb.add_directory(p) {
                Ok(n) => files += n,
                Err(_) => return Tablebases { tables: None, files: 0 },
            }
        }
        Tablebases { tables: any.then_some(tb), files }
    }

    /// v1's `ATOMICDB_TB_PATHS` string form: `;`-separated.
    pub fn open_list(paths: &str) -> Tablebases {
        Tablebases::open_list_with(paths, Access::Read)
    }

    pub fn open_list_with(paths: &str, access: Access) -> Tablebases {
        let v: Vec<&str> = paths.split(';').collect();
        Tablebases::open_with(&v, access)
    }

    /// Number of table files found.
    pub fn files(&self) -> usize {
        self.files
    }

    pub fn is_open(&self) -> bool {
        self.tables.is_some()
    }

    /// `tb.probe_wdl(fen, max_pieces)` (TB-1.2).
    pub fn probe_wdl(&self, fen: &str, max_pieces: usize) -> Option<i32> {
        if !is_applicable(fen, max_pieces) {
            return None;
        }
        let tables = self.tables.as_ref()?;
        let pos = atomic_position(fen)?;
        let wdl = tables.probe_wdl_after_zeroing(&pos).ok()?;
        Some(i32::from(wdl))
    }

    /// DTZ for the comparison tools (`None` on any failure).
    pub fn probe_dtz(&self, fen: &str, max_pieces: usize) -> Option<i32> {
        if !is_applicable(fen, max_pieces) {
            return None;
        }
        let tables = self.tables.as_ref()?;
        let pos = atomic_position(fen)?;
        let dtz = tables.probe_dtz(&pos).ok()?;
        Some(i32::from(dtz.ignore_rounding()))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn applicability_matches_v1() {
        // tests.py::TablebaseTests::test_applicability and
        // test_tb.py::AtomicTablebaseTests::test_worker_compatible_applicability_guards
        assert!(is_applicable("8/8/8/8/8/8/1k6/K6Q w - - 0 1", 6));
        assert!(!is_applicable("8/8/8/8/8/8/1k6/K6Q w K - 0 1", 6));
        assert!(!is_applicable("8/8/8/8/8/8/1k6/K6Q w - e3 0 1", 6));
        assert!(!is_applicable("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w - - 0 1", 6));
        assert!(!is_applicable("8/8/8/8/8/1ppp4/1k6/K3QQQQ w - - 0 1", 6));
        assert!(is_applicable("8/8/8/8/8/1pp5/1k6/K3QQ2 w - - 0 1", 6));
        assert!(!is_applicable("8/8/8/8/8/1pp5/1k6/K3QQ2 w - - 0 1", 5));
        assert!(!is_applicable("junk", 6));
        assert!(!is_applicable("", 6));
    }

    #[test]
    fn no_tables_means_no_verdict() {
        let none = Tablebases::open::<&str>(&[]);
        assert!(!none.is_open());
        assert_eq!(none.probe_wdl("8/8/8/8/8/8/1k6/K6Q w - - 0 1", 6), None);
        let missing = Tablebases::open(&["/definitely/not/a/directory/av2"]);
        assert!(!missing.is_open());
        assert_eq!(missing.probe_wdl("8/8/8/8/8/8/1k6/K6Q w - - 0 1", 6), None);
    }
}
