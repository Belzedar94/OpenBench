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

use shakmaty::CastlingMode;
use shakmaty::fen::Fen;
use shakmaty::variant::Atomic;
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
pub fn atomic_position(fen: &str) -> Option<Atomic> {
    let fen: Fen = fen.parse().ok()?;
    fen.into_position(CastlingMode::Standard).ok()
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
        let mut tb = Tablebase::<Atomic>::new();
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
        let v: Vec<&str> = paths.split(';').collect();
        Tablebases::open(&v)
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
