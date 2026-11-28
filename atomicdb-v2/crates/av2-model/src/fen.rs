//! Packed canonical FEN (36 bytes per position, column `fen.col`).
//!
//! v1 keeps the canonical FEN as text (`Position.fen`, 49 to 68 bytes in
//! production).  The packing needs no chess rules: 64 squares as nibbles, the
//! side to move, the castling letters and the en-passant square.  A FEN is
//! packed only if unpacking gives back the very same string (canonical FENs
//! always end in `0 1`); anything else (an unusual castling notation, extra
//! fields) is kept verbatim in the blob arena and the column holds a
//! reference.  Exactness is therefore by construction, not by assumption.
//!
//! Layout: byte 0 kind (0 none, 1 packed, 2 blob), byte 1 side to move (bit 0)
//! and castling `KQkq` (bits 1-4), byte 2 en-passant square (0xFF none),
//! byte 3 reserved, bytes 4..36 the board, two squares per byte, ranks 8 to 1
//! and files a to h (FEN order).  For kind 2, bytes 4..12 are the blob
//! reference.

use crate::graph::BlobRef;

pub const FEN_SIZE: usize = 36;
const KIND_NONE: u8 = 0;
const KIND_PACKED: u8 = 1;
const KIND_BLOB: u8 = 2;
const PIECES: &[u8; 16] = b".PNBRQK..pnbrqk.";

#[derive(Clone, Copy, PartialEq, Eq, Debug, Hash)]
pub struct PackedFen(pub [u8; FEN_SIZE]);

/// What a FEN column entry holds.
#[derive(Clone, PartialEq, Eq, Debug)]
pub enum FenSlot {
    None,
    Text(String),
    Blob(BlobRef),
}

impl Default for PackedFen {
    fn default() -> Self {
        PackedFen([0u8; FEN_SIZE])
    }
}

impl PackedFen {
    /// Pack `fen`, or `None` if it cannot be packed exactly (store it as a blob).
    pub fn pack(fen: &str) -> Option<PackedFen> {
        let parts: Vec<&str> = fen.split(' ').collect();
        if parts.len() != 6 || parts[4] != "0" || parts[5] != "1" {
            return None;
        }
        let mut b = [0u8; FEN_SIZE];
        b[0] = KIND_PACKED;
        let mut sq = 0usize;
        for (ri, rank) in parts[0].split('/').enumerate() {
            if ri >= 8 {
                return None;
            }
            let mut file = 0usize;
            for c in rank.bytes() {
                if c.is_ascii_digit() {
                    let n = (c - b'0') as usize;
                    if n == 0 || n > 8 {
                        return None;
                    }
                    file += n;
                } else {
                    let code = PIECES.iter().position(|&p| p == c && p != b'.')?;
                    let at = ri * 8 + file;
                    if file >= 8 {
                        return None;
                    }
                    b[4 + at / 2] |= (code as u8) << ((at % 2) * 4);
                    file += 1;
                }
                if file > 8 {
                    return None;
                }
            }
            if file != 8 {
                return None;
            }
            sq += 8;
        }
        if sq != 64 {
            return None;
        }
        b[1] = match parts[1] {
            "w" => 0,
            "b" => 1,
            _ => return None,
        };
        if parts[2] != "-" {
            for c in parts[2].bytes() {
                let bit = match c {
                    b'K' => 1,
                    b'Q' => 2,
                    b'k' => 3,
                    b'q' => 4,
                    _ => return None,
                };
                b[1] |= 1 << bit;
            }
        }
        b[2] = if parts[3] == "-" {
            0xFF
        } else {
            let e = parts[3].as_bytes();
            if e.len() != 2 || !(b'a'..=b'h').contains(&e[0]) || !(b'1'..=b'8').contains(&e[1]) {
                return None;
            }
            (e[1] - b'1') * 8 + (e[0] - b'a')
        };
        let p = PackedFen(b);
        // Exactness check: only accept what comes back byte for byte.
        (p.unpack().as_deref() == Some(fen)).then_some(p)
    }

    pub fn blob(r: BlobRef) -> PackedFen {
        let mut b = [0u8; FEN_SIZE];
        b[0] = KIND_BLOB;
        b[4..12].copy_from_slice(&r.0.to_le_bytes());
        PackedFen(b)
    }

    pub fn slot(&self) -> FenSlot {
        match self.0[0] {
            KIND_PACKED => self.unpack().map(FenSlot::Text).unwrap_or(FenSlot::None),
            KIND_BLOB => FenSlot::Blob(BlobRef(u64::from_le_bytes(self.0[4..12].try_into().unwrap()))),
            _ => FenSlot::None,
        }
    }

    pub fn is_none(&self) -> bool {
        self.0[0] == KIND_NONE
    }

    /// Side to move of a packed FEN (`None` for blobs / empty).
    pub fn black_to_move(&self) -> Option<bool> {
        (self.0[0] == KIND_PACKED).then_some(self.0[1] & 1 == 1)
    }

    fn unpack(&self) -> Option<String> {
        if self.0[0] != KIND_PACKED {
            return None;
        }
        let b = &self.0;
        let mut s = String::with_capacity(72);
        for r in 0..8 {
            if r > 0 {
                s.push('/');
            }
            let mut empty = 0u8;
            for f in 0..8 {
                let at = r * 8 + f;
                let code = (b[4 + at / 2] >> ((at % 2) * 4)) & 15;
                if code == 0 {
                    empty += 1;
                } else {
                    if empty > 0 {
                        s.push((b'0' + empty) as char);
                        empty = 0;
                    }
                    s.push(PIECES[code as usize] as char);
                }
            }
            if empty > 0 {
                s.push((b'0' + empty) as char);
            }
        }
        s.push(' ');
        s.push(if b[1] & 1 == 1 { 'b' } else { 'w' });
        s.push(' ');
        if b[1] >> 1 == 0 {
            s.push('-');
        } else {
            for (bit, c) in [(1, 'K'), (2, 'Q'), (3, 'k'), (4, 'q')] {
                if b[1] & (1 << bit) != 0 {
                    s.push(c);
                }
            }
        }
        s.push(' ');
        if b[2] == 0xFF {
            s.push('-');
        } else {
            s.push((b'a' + b[2] % 8) as char);
            s.push((b'1' + b[2] / 8) as char);
        }
        s.push_str(" 0 1");
        Some(s)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn packs_canonical_fens_exactly() {
        for fen in [
            "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
            "rnbqkbnr/ppp1pppp/8/3pP3/8/8/PPPP1PPP/RNBQKBNR w Kq d6 0 1",
            "8/8/8/8/8/8/8/K6k b - - 0 1",
            "r3k2r/8/8/8/8/8/8/R3K2R b Qk - 0 1",
        ] {
            let p = PackedFen::pack(fen).unwrap_or_else(|| panic!("{fen}"));
            assert_eq!(p.slot(), FenSlot::Text(fen.to_string()));
        }
    }

    #[test]
    fn refuses_what_would_not_come_back() {
        for fen in [
            "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 3 7",  // counters
            "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w qkQK - 0 1",  // castling order
            "rnbqkbnr/pppppppp/44/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1", // split empties
            "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w HAha - 0 1",
            "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP w KQkq - 0 1",
            "rnbqkbnr/pppppppp/9/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
            "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1 extra",
        ] {
            assert!(PackedFen::pack(fen).is_none(), "{fen}");
        }
        assert_eq!(PackedFen::blob(BlobRef(77)).slot(), FenSlot::Blob(BlobRef(77)));
        assert_eq!(PackedFen::default().slot(), FenSlot::None);
    }
}
