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
        let mut it = fen.split(' ');
        let mut parts = [""; 6];
        for p in parts.iter_mut() {
            *p = it.next()?;
        }
        if it.next().is_some() || parts[4] != "0" || parts[5] != "1" {
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
        let mut out = [0u8; 96];
        let n = p.unpack_into(&mut out);
        (&out[..n] == fen.as_bytes()).then_some(p)
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
        let mut out = [0u8; 96];
        let n = self.unpack_into(&mut out);
        Some(String::from_utf8_lossy(&out[..n]).into_owned())
    }

    /// Write the FEN of a packed entry into `out` (at most 92 bytes); returns its length.
    fn unpack_into(&self, out: &mut [u8; 96]) -> usize {
        let b = &self.0;
        let mut n = 0usize;
        let mut push = |c: u8| {
            out[n] = c;
            n += 1;
        };
        for r in 0..8 {
            if r > 0 {
                push(b'/');
            }
            let mut empty = 0u8;
            for f in 0..8 {
                let at = r * 8 + f;
                let code = (b[4 + at / 2] >> ((at % 2) * 4)) & 15;
                if code == 0 {
                    empty += 1;
                } else {
                    if empty > 0 {
                        push(b'0' + empty);
                        empty = 0;
                    }
                    push(PIECES[code as usize]);
                }
            }
            if empty > 0 {
                push(b'0' + empty);
            }
        }
        push(b' ');
        push(if b[1] & 1 == 1 { b'b' } else { b'w' });
        push(b' ');
        if b[1] >> 1 == 0 {
            push(b'-');
        } else {
            for (bit, c) in [(1, b'K'), (2, b'Q'), (3, b'k'), (4, b'q')] {
                if b[1] & (1 << bit) != 0 {
                    push(c);
                }
            }
        }
        push(b' ');
        if b[2] == 0xFF {
            push(b'-');
        } else {
            push(b'a' + b[2] % 8);
            push(b'1' + b[2] / 8);
        }
        for c in *b" 0 1" {
            push(c);
        }
        n
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
