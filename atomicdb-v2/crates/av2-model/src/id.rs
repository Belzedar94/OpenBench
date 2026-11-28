//! Position ids.
//!
//! Dense, assigned in creation order (the loader orders v1 positions by their
//! first incoming edge id, plan section 8.2), `u64` in code and 40 bits on
//! disk: a trillion positions, against 2.6 billion a year at today's growth.
//! Dense ids are what makes "the 29 children of this position" one contiguous
//! read: siblings are created together and land next to each other.

use serde::{Deserialize, Serialize};

/// Bits of an id on disk.
pub const ID_BITS: u32 = 40;
/// Largest id that fits in 40 bits.  One value is kept free so that `id + 1`
/// (how the columns encode "present") still fits.
pub const MAX_ID: u64 = (1u64 << ID_BITS) - 2;

#[derive(Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Debug, Default, Serialize, Deserialize)]
#[serde(transparent)]
pub struct PosId(pub u64);

impl PosId {
    #[inline]
    pub const fn get(self) -> u64 {
        self.0
    }
    #[inline]
    pub const fn index(self) -> usize {
        self.0 as usize
    }
}

impl std::fmt::Display for PosId {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "#{}", self.0)
    }
}

/// Write the low 40 bits of `v` at `b[..5]`.
#[inline]
pub fn put_u40(b: &mut [u8], v: u64) {
    debug_assert!(v < (1u64 << 40), "value {v} does not fit in 40 bits");
    b[..5].copy_from_slice(&v.to_le_bytes()[..5]);
}

/// Read a 40-bit little-endian value from `b[..5]`.
#[inline]
pub fn get_u40(b: &[u8]) -> u64 {
    let mut a = [0u8; 8];
    a[..5].copy_from_slice(&b[..5]);
    u64::from_le_bytes(a)
}

/// Write the low 48 bits of `v` at `b[..6]`.
#[inline]
pub fn put_u48(b: &mut [u8], v: u64) {
    debug_assert!(v < (1u64 << 48), "value {v} does not fit in 48 bits");
    b[..6].copy_from_slice(&v.to_le_bytes()[..6]);
}

#[inline]
pub fn get_u48(b: &[u8]) -> u64 {
    let mut a = [0u8; 8];
    a[..6].copy_from_slice(&b[..6]);
    u64::from_le_bytes(a)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn u40_roundtrip() {
        let mut b = [0u8; 5];
        for v in [0u64, 1, 255, 1 << 32, MAX_ID, MAX_ID + 1] {
            put_u40(&mut b, v);
            assert_eq!(get_u40(&b), v);
        }
    }

    #[test]
    fn u48_roundtrip() {
        let mut b = [0u8; 6];
        for v in [0u64, 7, (1 << 48) - 1] {
            put_u48(&mut b, v);
            assert_eq!(get_u48(&b), v);
        }
    }
}
