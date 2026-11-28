//! 16-bit move encoding.
//!
//! v1 stores moves as UCI text (`Edge.move_uci`, `Position.best_move`,
//! `Position.backed_move`, max 8 characters).  Atomic is played on 8x8 with
//! promotions only, so a move is `from` (6 bits), `to` (6 bits) and a
//! promotion piece (3 bits); bit 15 is reserved.  `Move::NONE` (all zero,
//! "a1a1") is never a legal move and stands for v1's NULL.
//!
//! Encoding and formatting round-trip exactly: `Move::parse(s)?.to_uci() == s`
//! for every string `parse` accepts.  Anything else (a drop, a 10x8 square, a
//! capitalised promotion) is rejected rather than approximated; the loader
//! counts and reports such rows.

use serde::{Deserialize, Serialize};

#[derive(Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Default, Serialize, Deserialize)]
#[serde(transparent)]
pub struct Move(pub u16);

/// Promotion letters in encoding order (index 0 = no promotion).  `k` is kept
/// for variants where a pawn may promote to a king; atomic never produces it.
const PROMO: [u8; 6] = [0, b'n', b'b', b'r', b'q', b'k'];

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MoveParseError(pub String);
impl std::fmt::Display for MoveParseError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "bad move: {:?}", self.0)
    }
}
impl std::error::Error for MoveParseError {}

impl Move {
    pub const NONE: Move = Move(0);

    #[inline]
    pub fn new(from: u8, to: u8, promo: u8) -> Move {
        debug_assert!(from < 64 && to < 64 && promo < 6);
        Move(((promo as u16) << 12) | ((from as u16) << 6) | to as u16)
    }
    #[inline]
    pub fn from_sq(self) -> u8 {
        ((self.0 >> 6) & 63) as u8
    }
    #[inline]
    pub fn to_sq(self) -> u8 {
        (self.0 & 63) as u8
    }
    #[inline]
    pub fn promo(self) -> u8 {
        ((self.0 >> 12) & 7) as u8
    }
    #[inline]
    pub fn is_none(self) -> bool {
        self.0 == 0
    }

    pub fn parse(s: &str) -> Result<Move, MoveParseError> {
        let b = s.as_bytes();
        let sq = |f: u8, r: u8| -> Option<u8> {
            if (b'a'..=b'h').contains(&f) && (b'1'..=b'8').contains(&r) { Some((r - b'1') * 8 + (f - b'a')) } else { None }
        };
        let err = || MoveParseError(s.to_string());
        if b.len() != 4 && b.len() != 5 {
            return Err(err());
        }
        let from = sq(b[0], b[1]).ok_or_else(err)?;
        let to = sq(b[2], b[3]).ok_or_else(err)?;
        let promo = if b.len() == 5 { PROMO.iter().position(|&c| c == b[4] && c != 0).ok_or_else(err)? as u8 } else { 0 };
        let m = Move::new(from, to, promo);
        if m.is_none() {
            return Err(err());
        }
        Ok(m)
    }

    /// `None` for v1's NULL / empty string.
    pub fn parse_opt(s: &str) -> Result<Move, MoveParseError> {
        if s.is_empty() { Ok(Move::NONE) } else { Move::parse(s) }
    }

    pub fn to_uci(self) -> String {
        if self.is_none() {
            return String::new();
        }
        let mut s = String::with_capacity(5);
        for sq in [self.from_sq(), self.to_sq()] {
            s.push((b'a' + sq % 8) as char);
            s.push((b'1' + sq / 8) as char);
        }
        if self.promo() != 0 {
            s.push(PROMO[self.promo() as usize] as char);
        }
        s
    }
}

impl std::fmt::Debug for Move {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        if self.is_none() { f.write_str("Move(-)") } else { write!(f, "Move({})", self.to_uci()) }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn roundtrip_all_squares() {
        for from in 0..64u8 {
            for to in 0..64u8 {
                for promo in 0..6u8 {
                    let m = Move::new(from, to, promo);
                    if m.is_none() {
                        continue;
                    }
                    let s = m.to_uci();
                    assert_eq!(Move::parse(&s).unwrap(), m, "{s}");
                }
            }
        }
    }

    #[test]
    fn rejects_what_it_cannot_represent() {
        for s in ["", "a1a1", "e2e", "e2e4Q", "e7e8x", "i1a1", "a9a1", "P@e4", "e2e4qq"] {
            assert!(Move::parse(s).is_err(), "{s}");
        }
        assert_eq!(Move::parse_opt("").unwrap(), Move::NONE);
        assert_eq!(Move::parse("e7e8q").unwrap().to_uci(), "e7e8q");
    }
}
