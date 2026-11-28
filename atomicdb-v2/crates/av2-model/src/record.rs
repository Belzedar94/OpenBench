//! The two per-position records of the graph columns.
//!
//! **Core** (32 bytes, every position): what a climb reads of every child.
//! **Ext** (64 bytes, only positions that were searched, backed, given a
//! witness line or tagged by a campaign): exact counters and blob references.
//! A core record points at its ext record by index (40 bits, 0 = none).
//!
//! Layouts are fixed byte offsets, little-endian.  `encode`/`decode` are the
//! only code that knows them.  The record format version is stored in the
//! store's control file; changing a layout means bumping it and writing a
//! migration (or replaying the log into a fresh store).
//!
//! The plan (section 3.2) sketches a 20-byte core and a 48-byte ext.  The
//! core here is 32 bytes because it also carries the 48-bit version (the
//! sequence number of the record's last change, plan section 2.2 point 4) and
//! 5 reserved bytes for the rules port; see `docs/store.md`.
//!
//! Fields not stored at all, on purpose:
//! - `priority`: computed by the selector in memory (owner decision D5).
//! - `updated`: replaced by `version` for correctness; wall-clock times live
//!   in the facts.
//! - `fen`: in its own column (`PackedFen`), not in the record.

use crate::id::{get_u40, get_u48, put_u40, put_u48};
use crate::mv::Move;

// ---------------------------------------------------------------- enums

/// v1 `Position.status` (`Status` text choices).
#[derive(Clone, Copy, PartialEq, Eq, Debug, Default, Hash)]
#[repr(u8)]
pub enum Status {
    #[default]
    Unknown = 0,
    WhiteWin = 1,
    BlackWin = 2,
    Draw = 3,
}

/// v1 `Position.closure` (`Closure` text choices, NULL = `None`).
#[derive(Clone, Copy, PartialEq, Eq, Debug, Default, Hash)]
#[repr(u8)]
pub enum Closure {
    #[default]
    None = 0,
    Tb = 1,
    MatePv = 2,
    Minimax = 3,
    Terminal = 4,
    Solve = 5,
}

/// v1 `Position.proof` (`Proof` text choices, NULL = `None`).
#[derive(Clone, Copy, PartialEq, Eq, Debug, Default, Hash)]
#[repr(u8)]
pub enum Proof {
    #[default]
    None = 0,
    AndOr = 1,
    Engine = 2,
    Disputed = 3,
    TbLine = 4,
}

impl Status {
    pub fn from_bits(b: u16) -> Status {
        match b & 3 {
            0 => Status::Unknown,
            1 => Status::WhiteWin,
            2 => Status::BlackWin,
            _ => Status::Draw,
        }
    }
    pub fn v1(self) -> &'static str {
        match self {
            Status::Unknown => "UNKNOWN",
            Status::WhiteWin => "WHITE_WIN",
            Status::BlackWin => "BLACK_WIN",
            Status::Draw => "DRAW",
        }
    }
    pub fn from_v1(s: &str) -> Option<Status> {
        Some(match s {
            "UNKNOWN" | "" => Status::Unknown,
            "WHITE_WIN" => Status::WhiteWin,
            "BLACK_WIN" => Status::BlackWin,
            "DRAW" => Status::Draw,
            _ => return None,
        })
    }
}

impl Closure {
    pub fn from_bits(b: u16) -> Option<Closure> {
        Some(match b {
            0 => Closure::None,
            1 => Closure::Tb,
            2 => Closure::MatePv,
            3 => Closure::Minimax,
            4 => Closure::Terminal,
            5 => Closure::Solve,
            _ => return None,
        })
    }
    pub fn v1(self) -> &'static str {
        match self {
            Closure::None => "",
            Closure::Tb => "TB",
            Closure::MatePv => "MATE_PV",
            Closure::Minimax => "MINIMAX",
            Closure::Terminal => "TERMINAL",
            Closure::Solve => "SOLVE",
        }
    }
    pub fn from_v1(s: &str) -> Option<Closure> {
        Some(match s {
            "" => Closure::None,
            "TB" => Closure::Tb,
            "MATE_PV" => Closure::MatePv,
            "MINIMAX" => Closure::Minimax,
            "TERMINAL" => Closure::Terminal,
            "SOLVE" => Closure::Solve,
            _ => return None,
        })
    }
}

impl Proof {
    pub fn from_bits(b: u16) -> Option<Proof> {
        Some(match b {
            0 => Proof::None,
            1 => Proof::AndOr,
            2 => Proof::Engine,
            3 => Proof::Disputed,
            4 => Proof::TbLine,
            _ => return None,
        })
    }
    pub fn v1(self) -> &'static str {
        match self {
            Proof::None => "",
            Proof::AndOr => "ANDOR",
            Proof::Engine => "ENGINE",
            Proof::Disputed => "DISPUTED",
            Proof::TbLine => "TB_LINE",
        }
    }
    pub fn from_v1(s: &str) -> Option<Proof> {
        Some(match s {
            "" => Proof::None,
            "ANDOR" => Proof::AndOr,
            "ENGINE" => Proof::Engine,
            "DISPUTED" => Proof::Disputed,
            "TB_LINE" => Proof::TbLine,
            _ => return None,
        })
    }
}

// ---------------------------------------------------------------- core

pub const CORE_SIZE: usize = 32;
/// Sentinel of a NULL centipawn value (v1 `eval_cp` / `backed_eval` NULL).
pub const NO_CP: i16 = i16::MIN;
/// Sentinel of a NULL `mate_in`.
pub const NO_MATE: u16 = u16::MAX;
/// Sentinel of a NULL `clock_slack`.
pub const NO_SLACK: u8 = u8::MAX;
/// `backed_nodes` value v1 uses for a backed value that rests on a proof
/// (`2**60`, seen in production data).  TODO(rules): confirm the constant's
/// name and every place it is produced in `ingest.py`.
pub const PROVEN_QUALITY: u64 = 1 << 60;

// flags (u16 at offset 0)
const F_STATUS: u16 = 0b11; // bits 0-1
const F_CLOSURE_SHIFT: u16 = 2; // bits 2-4
const F_PROOF_SHIFT: u16 = 5; // bits 5-7
pub const F_EXPANDED: u16 = 1 << 8;
pub const F_REACHABLE: u16 = 1 << 9;
pub const F_BLACK_TO_MOVE: u16 = 1 << 10;
/// Bits 11-15 are reserved for the rules port (TODO(rules)).
pub const F_RESERVED: u16 = 0b11111 << 11;

/// The record every position has.  Byte layout (offset: field):
///
/// | off | size | field | v1 column |
/// |---:|---:|---|---|
/// | 0 | 2 | `flags` | `status` (bits 0-1), `closure` (2-4), `proof` (5-7), `expanded` (8), `reachable` (9), side to move from `fen` (10) |
/// | 2 | 2 | `eval_cp` | `eval_cp` (white POV, NULL = `NO_CP`) |
/// | 4 | 2 | `backed_eval` | `backed_eval` (white POV, NULL = `NO_CP`) |
/// | 6 | 2 | `backed_move` | `backed_move` (UCI, NULL = `Move::NONE`) |
/// | 8 | 2 | `best_move` | `best_move` (UCI, NULL = `Move::NONE`) |
/// | 10 | 2 | `backed_plies` | `backed_plies` (default 0) |
/// | 12 | 2 | `mate_in` | `mate_in` (plies, NULL = `NO_MATE`) |
/// | 14 | 1 | `clock_slack` | `clock_slack` (0..100, NULL = `NO_SLACK`) |
/// | 15 | 1 | reserved | |
/// | 16 | 5 | `ext` | index+1 of the ext record, 0 = none |
/// | 21 | 6 | `version` | sequence number of the last batch that changed this record |
/// | 27 | 5 | reserved | TODO(rules) |
#[derive(Clone, Copy, PartialEq, Eq, Debug, Hash)]
pub struct Core {
    pub flags: u16,
    pub eval_cp: i16,
    pub backed_eval: i16,
    pub backed_move: Move,
    pub best_move: Move,
    pub backed_plies: u16,
    pub mate_in: u16,
    pub clock_slack: u8,
    /// 0 = no ext record, else index + 1.  Managed by the store.
    pub ext: u64,
    /// Managed by the store: set to the batch's sequence number on every write.
    pub version: u64,
}

impl Default for Core {
    fn default() -> Core {
        Core {
            flags: 0,
            eval_cp: NO_CP,
            backed_eval: NO_CP,
            backed_move: Move::NONE,
            best_move: Move::NONE,
            backed_plies: 0,
            mate_in: NO_MATE,
            clock_slack: NO_SLACK,
            ext: 0,
            version: 0,
        }
    }
}

impl Core {
    #[inline]
    pub fn status(&self) -> Status {
        Status::from_bits(self.flags & F_STATUS)
    }
    #[inline]
    pub fn closure(&self) -> Closure {
        Closure::from_bits((self.flags >> F_CLOSURE_SHIFT) & 7).unwrap_or(Closure::None)
    }
    #[inline]
    pub fn proof(&self) -> Proof {
        Proof::from_bits((self.flags >> F_PROOF_SHIFT) & 7).unwrap_or(Proof::None)
    }
    #[inline]
    pub fn set_status(&mut self, s: Status, c: Closure, p: Proof) {
        self.flags = (self.flags & !(F_STATUS | (7 << F_CLOSURE_SHIFT) | (7 << F_PROOF_SHIFT)))
            | s as u16
            | ((c as u16) << F_CLOSURE_SHIFT)
            | ((p as u16) << F_PROOF_SHIFT);
    }
    #[inline]
    pub fn is_open(&self) -> bool {
        self.status() == Status::Unknown
    }
    #[inline]
    pub fn flag(&self, f: u16) -> bool {
        self.flags & f != 0
    }
    #[inline]
    pub fn set_flag(&mut self, f: u16, on: bool) {
        if on { self.flags |= f } else { self.flags &= !f }
    }
    #[inline]
    pub fn expanded(&self) -> bool {
        self.flag(F_EXPANDED)
    }
    #[inline]
    pub fn black_to_move(&self) -> bool {
        self.flag(F_BLACK_TO_MOVE)
    }
    #[inline]
    pub fn eval(&self) -> Option<i16> {
        (self.eval_cp != NO_CP).then_some(self.eval_cp)
    }
    #[inline]
    pub fn backed(&self) -> Option<i16> {
        (self.backed_eval != NO_CP).then_some(self.backed_eval)
    }
    #[inline]
    pub fn mate(&self) -> Option<u16> {
        (self.mate_in != NO_MATE).then_some(self.mate_in)
    }
    #[inline]
    pub fn slack(&self) -> Option<u8> {
        (self.clock_slack != NO_SLACK).then_some(self.clock_slack)
    }
    /// Index of the ext record, if any.
    #[inline]
    pub fn ext_index(&self) -> Option<u64> {
        (self.ext != 0).then(|| self.ext - 1)
    }

    pub fn encode(&self, b: &mut [u8]) {
        let b = &mut b[..CORE_SIZE];
        b[0..2].copy_from_slice(&self.flags.to_le_bytes());
        b[2..4].copy_from_slice(&self.eval_cp.to_le_bytes());
        b[4..6].copy_from_slice(&self.backed_eval.to_le_bytes());
        b[6..8].copy_from_slice(&self.backed_move.0.to_le_bytes());
        b[8..10].copy_from_slice(&self.best_move.0.to_le_bytes());
        b[10..12].copy_from_slice(&self.backed_plies.to_le_bytes());
        b[12..14].copy_from_slice(&self.mate_in.to_le_bytes());
        b[14] = self.clock_slack;
        b[15] = 0;
        put_u40(&mut b[16..21], self.ext);
        put_u48(&mut b[21..27], self.version);
        b[27..32].fill(0);
    }

    pub fn decode(b: &[u8]) -> Core {
        let b = &b[..CORE_SIZE];
        let u16_at = |o: usize| u16::from_le_bytes([b[o], b[o + 1]]);
        Core {
            flags: u16_at(0),
            eval_cp: u16_at(2) as i16,
            backed_eval: u16_at(4) as i16,
            backed_move: Move(u16_at(6)),
            best_move: Move(u16_at(8)),
            backed_plies: u16_at(10),
            mate_in: u16_at(12),
            clock_slack: b[14],
            ext: get_u40(&b[16..21]),
            version: get_u48(&b[21..27]),
        }
    }

    pub fn to_bytes(&self) -> [u8; CORE_SIZE] {
        let mut b = [0u8; CORE_SIZE];
        self.encode(&mut b);
        b
    }
}

// ---------------------------------------------------------------- ext

pub const EXT_SIZE: usize = 64;

/// The record of positions with more to say.  Byte layout:
///
/// | off | size | field | v1 column |
/// |---:|---:|---|---|
/// | 0 | 8 | `nodes_invested` | `nodes_invested` |
/// | 8 | 8 | `backed_nodes` | `backed_nodes` (2^60 = proven) |
/// | 16 | 8 | `time_invested` | `time_invested` (f64 bits, seconds) |
/// | 24 | 4 | `depth_invested` | `depth_invested` |
/// | 28 | 4 | `visits` | `visits` |
/// | 32 | 4 | `campaign` | `campaign_id` (0 = NULL) |
/// | 36 | 4 | reserved | TODO(rules): showcase bound (budget / width of the search the page stands behind) |
/// | 40 | 8 | `analysis` | blob: `last_analysis` JSON as v1 stores it |
/// | 48 | 8 | `passes` | blob: `analysis_passes` JSON |
/// | 56 | 8 | `won_line` | blob: `won_line` text |
///
/// TODO(rules): the plan wants the showcase's line heads here so that
/// seeding does not have to parse the analysis blob; decide in the stage (a)
/// port.  Blob references are opaque to the rules: they are written with
/// `GraphWrite::put_blob` and read with `GraphRead::blob`.
#[derive(Clone, Copy, PartialEq, Debug, Default)]
pub struct Ext {
    pub nodes_invested: u64,
    pub backed_nodes: u64,
    pub time_invested: f64,
    pub depth_invested: i32,
    pub visits: i32,
    pub campaign: u32,
    pub analysis: crate::graph::BlobRef,
    pub passes: crate::graph::BlobRef,
    pub won_line: crate::graph::BlobRef,
}

impl Ext {
    pub fn encode(&self, b: &mut [u8]) {
        let b = &mut b[..EXT_SIZE];
        b[0..8].copy_from_slice(&self.nodes_invested.to_le_bytes());
        b[8..16].copy_from_slice(&self.backed_nodes.to_le_bytes());
        b[16..24].copy_from_slice(&self.time_invested.to_bits().to_le_bytes());
        b[24..28].copy_from_slice(&self.depth_invested.to_le_bytes());
        b[28..32].copy_from_slice(&self.visits.to_le_bytes());
        b[32..36].copy_from_slice(&self.campaign.to_le_bytes());
        b[36..40].fill(0);
        b[40..48].copy_from_slice(&self.analysis.0.to_le_bytes());
        b[48..56].copy_from_slice(&self.passes.0.to_le_bytes());
        b[56..64].copy_from_slice(&self.won_line.0.to_le_bytes());
    }

    pub fn decode(b: &[u8]) -> Ext {
        let b = &b[..EXT_SIZE];
        let u64_at = |o: usize| u64::from_le_bytes(b[o..o + 8].try_into().unwrap());
        let u32_at = |o: usize| u32::from_le_bytes(b[o..o + 4].try_into().unwrap());
        Ext {
            nodes_invested: u64_at(0),
            backed_nodes: u64_at(8),
            time_invested: f64::from_bits(u64_at(16)),
            depth_invested: u32_at(24) as i32,
            visits: u32_at(28) as i32,
            campaign: u32_at(32),
            analysis: crate::graph::BlobRef(u64_at(40)),
            passes: crate::graph::BlobRef(u64_at(48)),
            won_line: crate::graph::BlobRef(u64_at(56)),
        }
    }

    pub fn to_bytes(&self) -> [u8; EXT_SIZE] {
        let mut b = [0u8; EXT_SIZE];
        self.encode(&mut b);
        b
    }

    /// True when every field is at its v1 default (such a position needs no
    /// ext record).
    pub fn is_default(&self) -> bool {
        self.to_bytes() == [0u8; EXT_SIZE]
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn core_roundtrip() {
        let mut c = Core {
            eval_cp: -10000,
            backed_eval: 37,
            backed_move: Move::parse("e7e8q").unwrap(),
            best_move: Move::parse("a2a4").unwrap(),
            backed_plies: 78,
            mate_in: 71,
            clock_slack: 100,
            ext: 123_456_789_012,
            version: (1 << 47) + 5,
            ..Core::default()
        };
        c.set_status(Status::BlackWin, Closure::Solve, Proof::TbLine);
        c.set_flag(F_EXPANDED, true);
        c.set_flag(F_BLACK_TO_MOVE, true);
        let d = Core::decode(&c.to_bytes());
        assert_eq!(c, d);
        assert_eq!(d.status(), Status::BlackWin);
        assert_eq!(d.closure(), Closure::Solve);
        assert_eq!(d.proof(), Proof::TbLine);
        assert!(d.expanded() && d.black_to_move() && !d.flag(F_REACHABLE));
        assert_eq!(Core::decode(&Core::default().to_bytes()), Core::default());
        assert_eq!(Core::default().eval(), None);
    }

    #[test]
    fn ext_roundtrip() {
        let e = Ext {
            nodes_invested: 18_896_000_000,
            backed_nodes: PROVEN_QUALITY,
            time_invested: 1234.5,
            depth_invested: 52,
            visits: 13,
            campaign: 7,
            analysis: crate::graph::BlobRef(99),
            passes: crate::graph::BlobRef(0),
            won_line: crate::graph::BlobRef(1 << 40),
        };
        assert_eq!(Ext::decode(&e.to_bytes()), e);
        assert!(Ext::default().is_default());
        assert!(!e.is_default());
    }

    #[test]
    fn v1_strings() {
        for s in [Status::Unknown, Status::WhiteWin, Status::BlackWin, Status::Draw] {
            assert_eq!(Status::from_v1(s.v1()), Some(s));
        }
        for c in [Closure::None, Closure::Tb, Closure::MatePv, Closure::Minimax, Closure::Terminal, Closure::Solve] {
            assert_eq!(Closure::from_v1(c.v1()), Some(c));
        }
        for p in [Proof::None, Proof::AndOr, Proof::Engine, Proof::Disputed, Proof::TbLine] {
            assert_eq!(Proof::from_v1(p.v1()), Some(p));
        }
    }
}
