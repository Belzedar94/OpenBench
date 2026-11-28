//! Position keys.
//!
//! The public identity of a position is v1's: `sha256(canonical_fen)` as 64
//! lowercase hex characters (`atomicdb/logic.py::key_of`, v1 column
//! `Position.key`).  URLs and the API keep using it.
//!
//! Inside the store a position is found by a 96-bit **fingerprint**: the first
//! 12 bytes of the key, i.e. its first 24 hex characters (the same prefix the
//! benchmark's production slice exported with `left(key, 24)`).  At 2.6
//! billion positions the chance of any two fingerprints colliding is about
//! 4e-11; where it matters (the public API), the full key is recomputed from
//! the stored FEN and compared.

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

/// Full 256-bit key.
#[derive(Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
pub struct Key(pub [u8; 32]);

/// The 96-bit prefix of a key that the key map stores.
#[derive(Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Default, Serialize, Deserialize)]
pub struct Fingerprint(pub [u8; 12]);

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct KeyParseError(pub String);

impl std::fmt::Display for KeyParseError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "bad key: {}", self.0)
    }
}
impl std::error::Error for KeyParseError {}

impl Key {
    /// `sha256` of an already canonical FEN, exactly as v1 computes it.
    ///
    /// Canonicalisation itself (`logic.canonical_fen`) needs the rules (it
    /// keeps the en-passant square only when the capture is executable): it
    /// is `av2_chess::canonical_fen`, and `av2_chess::key_of` is the whole of
    /// v1's `key_of`.  [`strip_counters`] is the part that needs no rules.
    pub fn of_canonical_fen(fen: &str) -> Key {
        Key(Sha256::digest(fen.as_bytes()).into())
    }

    pub fn from_hex(s: &str) -> Result<Key, KeyParseError> {
        if s.len() != 64 {
            return Err(KeyParseError(format!("expected 64 hex characters, got {}", s.len())));
        }
        let mut k = [0u8; 32];
        hex::decode_to_slice(s, &mut k).map_err(|e| KeyParseError(format!("{s}: {e}")))?;
        Ok(Key(k))
    }

    pub fn to_hex(&self) -> String {
        hex::encode(self.0)
    }

    #[inline]
    pub fn fingerprint(&self) -> Fingerprint {
        let mut f = [0u8; 12];
        f.copy_from_slice(&self.0[..12]);
        Fingerprint(f)
    }
}

impl Fingerprint {
    /// From a full 64-character key or from its first 24 characters.
    pub fn from_hex(s: &str) -> Result<Fingerprint, KeyParseError> {
        if s.len() != 24 && s.len() != 64 {
            return Err(KeyParseError(format!("expected 24 or 64 hex characters, got {}", s.len())));
        }
        let mut f = [0u8; 12];
        hex::decode_to_slice(&s[..24], &mut f).map_err(|e| KeyParseError(format!("{s}: {e}")))?;
        if s.len() == 64 {
            // Validate the rest too, so a corrupt key is not silently truncated.
            let mut rest = [0u8; 20];
            hex::decode_to_slice(&s[24..], &mut rest).map_err(|e| KeyParseError(format!("{s}: {e}")))?;
        }
        Ok(Fingerprint(f))
    }

    pub fn to_hex(&self) -> String {
        hex::encode(self.0)
    }

    /// First 8 bytes, big-endian: uniformly distributed (sha256), used to pick
    /// the key-map shard and slot.
    #[inline]
    pub fn hi64(&self) -> u64 {
        u64::from_be_bytes(self.0[..8].try_into().unwrap())
    }

    /// Last 4 bytes, used by the Bloom filter (independent of `hi64`).
    #[inline]
    pub fn lo32(&self) -> u32 {
        u32::from_be_bytes(self.0[8..12].try_into().unwrap())
    }
}

impl std::fmt::Debug for Key {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "Key({})", self.to_hex())
    }
}
impl std::fmt::Display for Key {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.to_hex())
    }
}
impl std::fmt::Debug for Fingerprint {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "Fp({})", self.to_hex())
    }
}

/// Replace the half-move and full-move counters by `0 1`, keeping the first
/// four fields as they are.  This is the rules-free half of
/// `logic.canonical_fen`; the en-passant check is the rules' job.
pub fn strip_counters(fen: &str) -> Option<String> {
    let parts: Vec<&str> = fen.split_whitespace().collect();
    if parts.len() < 4 {
        return None;
    }
    Some(format!("{} {} {} {} 0 1", parts[0], parts[1], parts[2], parts[3]))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn start_position_key_matches_v1() {
        // hashlib.sha256('rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1'.encode()).hexdigest()
        let fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";
        let k = Key::of_canonical_fen(fen);
        assert_eq!(k.to_hex(), "b1791d7fc9ae3d38966568c257ffb3a02cbf8394cdb4805bc70f64fc3c0b6879");
        assert_eq!(Key::from_hex(&k.to_hex()).unwrap(), k);
        assert_eq!(k.fingerprint(), Fingerprint::from_hex("b1791d7fc9ae3d38966568c2").unwrap());
        assert_eq!(k.fingerprint(), Fingerprint::from_hex(&k.to_hex()).unwrap());
    }

    #[test]
    fn counters_are_stripped() {
        assert_eq!(strip_counters("8/8/8/8/8/8/8/K6k b - - 12 40").unwrap(), "8/8/8/8/8/8/8/K6k b - - 0 1");
        assert!(strip_counters("8/8 w").is_none());
    }

    #[test]
    fn bad_hex_is_rejected() {
        assert!(Fingerprint::from_hex("zz791d7fc9ae3d38966568c2").is_err());
        assert!(Key::from_hex("b1791d").is_err());
    }
}
