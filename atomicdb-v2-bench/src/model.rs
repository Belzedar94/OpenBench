//! The data model shared by every store in the race.
//!
//! One fixed 32-byte record per position (the columns the climbs read and
//! write), out-edges as a block per parent, in-edges per child, and a map from
//! the 128-bit position key to the dense id.  Ids are dense `u32`s assigned in
//! creation order, so the children a parent created together are neighbours.

pub const NODE_SIZE: usize = 32;
pub const NO_EVAL: i16 = i16::MIN;
pub const PROVEN_QUALITY: u64 = 1 << 60;
pub const WIN_CP: i16 = 10_000;

pub const ST_UNKNOWN: u16 = 0;
pub const ST_WHITE: u16 = 1;
pub const ST_BLACK: u16 = 2;
pub const ST_DRAW: u16 = 3;
pub const F_STATUS: u16 = 0b11;
pub const F_CLOSURE_SHIFT: u16 = 2; // 3 bits
pub const CL_NONE: u16 = 0;
pub const CL_MATE_PV: u16 = 1;
pub const CL_TERMINAL: u16 = 2;
pub const CL_MINIMAX: u16 = 3;
pub const CL_TB: u16 = 4;
pub const F_EXPANDED: u16 = 1 << 5;
pub const F_BLACK_TO_MOVE: u16 = 1 << 6;
pub const F_REACHABLE: u16 = 1 << 7;

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
#[repr(C)]
pub struct Node {
    pub flags: u16,
    pub eval: i16,         // white point of view; NO_EVAL when absent
    pub backed: i16,       // white point of view; NO_EVAL when absent
    pub backed_move: u16,
    pub best_move: u16,
    pub backed_plies: u16,
    pub mate_in: u16,
    pub slack: u8,
    pub visits: u8,
    pub nodes: u64,        // nodes_invested
    pub backed_nodes: u64, // quality of the backed value
}

const _: () = assert!(std::mem::size_of::<Node>() == NODE_SIZE);

impl Default for Node {
    fn default() -> Self {
        Node { flags: 0, eval: NO_EVAL, backed: NO_EVAL, backed_move: 0, best_move: 0, backed_plies: 0,
               mate_in: 0, slack: 0, visits: 0, nodes: 0, backed_nodes: 0 }
    }
}

impl Node {
    #[inline] pub fn status(&self) -> u16 { self.flags & F_STATUS }
    #[inline] pub fn is_open(&self) -> bool { self.status() == ST_UNKNOWN }
    #[inline] pub fn expanded(&self) -> bool { self.flags & F_EXPANDED != 0 }
    #[inline] pub fn black_to_move(&self) -> bool { self.flags & F_BLACK_TO_MOVE != 0 }
    /// +1 when White is to move, -1 when Black is: multiplies a white-POV value into the mover's.
    #[inline] pub fn sign(&self) -> i32 { if self.black_to_move() { -1 } else { 1 } }
    #[inline] pub fn set_status(&mut self, st: u16, closure: u16) {
        self.flags = (self.flags & !(F_STATUS | (0b111 << F_CLOSURE_SHIFT))) | st | (closure << F_CLOSURE_SHIFT);
    }
    #[inline] pub fn closure(&self) -> u16 { (self.flags >> F_CLOSURE_SHIFT) & 0b111 }
    #[inline] pub fn to_bytes(&self) -> [u8; NODE_SIZE] { unsafe { std::mem::transmute(*self) } }
    #[inline] pub fn from_bytes(b: &[u8]) -> Node {
        let mut a = [0u8; NODE_SIZE];
        a.copy_from_slice(&b[..NODE_SIZE]);
        unsafe { std::mem::transmute(a) }
    }
    /// The four backed columns, the unit the ascent compares and writes.
    #[inline] pub fn backed_tuple(&self) -> (i16, u16, u16, u64) { (self.backed, self.backed_move, self.backed_plies, self.backed_nodes) }
}

/// Status that means "the side to move at a node with this sign has won".
#[inline] pub fn win_status_for(sign: i32) -> u16 { if sign > 0 { ST_WHITE } else { ST_BLACK } }
/// White-POV centipawn value of a proven status.
#[inline] pub fn proven_value(st: u16) -> i16 { match st { ST_WHITE => WIN_CP, ST_BLACK => -WIN_CP, _ => 0 } }

#[derive(Clone, Copy, PartialEq, Eq, Debug, Default)]
#[repr(C)]
pub struct Edge {
    pub child: u32,
    pub mv: u16,
}
pub const EDGE_SIZE: usize = 6;

#[inline] pub fn edges_to_bytes(edges: &[Edge], out: &mut Vec<u8>) {
    out.clear();
    out.reserve(edges.len() * EDGE_SIZE);
    for e in edges {
        out.extend_from_slice(&e.child.to_le_bytes());
        out.extend_from_slice(&e.mv.to_le_bytes());
    }
}
#[inline] pub fn edges_from_bytes(b: &[u8], out: &mut Vec<Edge>) {
    for c in b.chunks_exact(EDGE_SIZE) {
        out.push(Edge { child: u32::from_le_bytes([c[0], c[1], c[2], c[3]]), mv: u16::from_le_bytes([c[4], c[5]]) });
    }
}

/// The 128-bit position key.  Production keys are sha256 of the canonical FEN,
/// i.e. uniformly random; the harness derives an equally random key from the id
/// so that a "transposition" lookup of an existing position can be generated
/// without keeping 200M keys around.
///
/// The first four bytes are a bijective scramble of the id (still uniformly
/// spread), the other twelve a hash.  No store uses the inverse: it only lets
/// the v1-schema baseline, whose edges hold text keys, hand ids back to the
/// driver without an extra lookup that production would not make either.
#[inline] pub fn key_of(id: u32) -> [u8; 16] {
    let h = xxhash_rust::xxh3::xxh3_128_with_seed(&id.to_le_bytes(), 0xA70B_1CD8).to_le_bytes();
    let mut k = [0u8; 16];
    k[..4].copy_from_slice(&scramble(id).to_be_bytes());
    k[4..].copy_from_slice(&h[..12]);
    k
}
const MUL: u32 = 0x9E37_79B1;
const fn mul_inverse(x: u32) -> u32 { let mut inv = x; let mut i = 0; while i < 5 { inv = inv.wrapping_mul(2u32.wrapping_sub(x.wrapping_mul(inv))); i += 1; } inv }
const MUL_INV: u32 = mul_inverse(MUL);
#[inline] pub fn scramble(id: u32) -> u32 { (id ^ 0x5BD1_E995).wrapping_mul(MUL) }
#[inline] pub fn unscramble(x: u32) -> u32 { x.wrapping_mul(MUL_INV) ^ 0x5BD1_E995 }
/// The 64-hex-character key the v1 schema uses (sha256 hex in production),
/// derived from the 16-byte key alone.
pub fn hex_key(key: &[u8; 16]) -> String {
    let tail = xxhash_rust::xxh3::xxh3_128_with_seed(key, 0x5EED_0002);
    let mut s = String::with_capacity(64);
    for b in key { s.push_str(&format!("{b:02x}")); }
    s.push_str(&format!("{tail:032x}"));
    s
}
#[inline] pub fn hex_key_of(id: u32) -> String { hex_key(&key_of(id)) }
/// Visit the keys of ids `0..n` in ascending key order (what a B-tree bulk load
/// wants).  The key prefix is a bijection of the id, so no 200M-key sort is needed.
pub fn for_each_key_sorted(n: u32, mut f: impl FnMut(&[u8; 16], u32)) {
    if n <= 64_000_000 {
        let mut v: Vec<(u32, u32)> = (0..n).map(|id| (scramble(id), id)).collect();
        v.sort_unstable();
        for (_, id) in v { f(&key_of(id), id); }
    } else {
        for p in 0..=u32::MAX { let id = unscramble(p); if id < n { f(&key_of(id), id); } }
    }
}
#[inline] pub fn id_of_hex_key(s: &str) -> u32 { unscramble(u32::from_str_radix(&s[..8], 16).unwrap()) }

/// What one committed batch changes.  Computed by the single writer against the
/// committed state plus a private overlay, then handed to the store, which must
/// make it durable atomically before returning.
#[derive(Default, Clone)]
pub struct Batch {
    pub seq: u64,
    /// Id of the first new position; new positions take consecutive ids.
    pub first_new: u32,
    pub new_nodes: Vec<Node>,
    /// Rewrites of positions that already existed, sorted by id.
    pub puts: Vec<(u32, Node)>,
    /// New out-edge blocks (the parent had none before), sorted by parent.
    pub children: Vec<(u32, Vec<Edge>)>,
}

impl Batch {
    /// Bytes of record payload this batch changes: the denominator of write amplification.
    pub fn logical_bytes(&self) -> u64 {
        let edges: usize = self.children.iter().map(|(_, e)| e.len()).sum();
        (self.new_nodes.len() * (NODE_SIZE + 16 + 4) + self.puts.len() * NODE_SIZE + edges * (EDGE_SIZE + 4)) as u64
    }
    pub fn edge_count(&self) -> usize { self.children.iter().map(|(_, e)| e.len()).sum() }
}

/// splitmix64 / xoshiro256**: small, fast, and identical on every platform.
#[derive(Clone)]
pub struct Rng { s: [u64; 4] }
impl Rng {
    pub fn new(seed: u64) -> Rng {
        let mut z = seed;
        let mut next = || { z = z.wrapping_add(0x9E3779B97F4A7C15); let mut x = z; x = (x ^ (x >> 30)).wrapping_mul(0xBF58476D1CE4E5B9); x = (x ^ (x >> 27)).wrapping_mul(0x94D049BB133111EB); x ^ (x >> 31) };
        Rng { s: [next(), next(), next(), next()] }
    }
    #[inline] pub fn next_u64(&mut self) -> u64 {
        let r = self.s[1].wrapping_mul(5).rotate_left(7).wrapping_mul(9);
        let t = self.s[1] << 17;
        self.s[2] ^= self.s[0]; self.s[3] ^= self.s[1]; self.s[1] ^= self.s[2]; self.s[0] ^= self.s[3];
        self.s[2] ^= t; self.s[3] = self.s[3].rotate_left(45);
        r
    }
    #[inline] pub fn below(&mut self, n: u64) -> u64 { ((self.next_u64() as u128 * n as u128) >> 64) as u64 }
    #[inline] pub fn unit(&mut self) -> f64 { (self.next_u64() >> 11) as f64 / (1u64 << 53) as f64 }
    #[inline] pub fn chance(&mut self, p: f64) -> bool { self.unit() < p }
    /// Approximately normal (sum of uniforms), enough for eval noise.
    #[inline] pub fn normal(&mut self, mean: f64, sd: f64) -> f64 {
        let s: f64 = (0..6).map(|_| self.unit()).sum::<f64>() - 3.0; // variance 0.5
        mean + sd * s * std::f64::consts::SQRT_2
    }
}

/// Out-degree of an expanded position, shaped on the production sample
/// (mean 28.7, p5 12, p50 29, p95 42, max 57).
pub fn sample_out_degree(rng: &mut Rng) -> usize {
    rng.normal(29.0, 8.5).round().clamp(2.0, 57.0) as usize
}
/// Search budget of a landing, shaped on production (8M, 128M and a tail).
pub fn sample_budget(rng: &mut Rng) -> u64 {
    let r = rng.unit();
    let base = if r < 0.20 { 8_000_000 } else if r < 0.93 { 128_000_000 } else if r < 0.99 { 512_000_000 } else { 2_000_000_000 };
    base + rng.below(20_000)
}
/// Length of a materialised mate line (production mate_in: p50 8, p90 23, p99 35).
pub fn sample_chain(rng: &mut Rng) -> usize {
    let mut n = 1usize;
    while n < 50 && !rng.chance(1.0 / 19.0) { n += 1; }
    n
}

/// Calibration of the synthetic workload against production (27-Nov-2026 sample).
pub const P_TRANSPOSE: f64 = 0.062;   // fraction of a new parent's edges that land on an existing position
pub const P_WIN: f64 = 0.27;          // the landing proves a win for the side to move
pub const P_LOSS_CHILD: f64 = 0.17;   // the landing proves one reply lost, the position stays open
/// When no sibling is expanded, a transposition lands on a position created
/// shortly after the parent (ids only ever point forward, so the graph stays acyclic).
pub const TRANSPOSE_WINDOW: u64 = 200_000;
pub const SEEDED_LINES: usize = 5;    // MultiPV lines whose head move gets a seeded eval
pub const EVAL_NOISE_CP: f64 = 28.0;  // how far a new search lands from what the position showed
pub const EPSILON_CP: i32 = 10;       // BACKED_EPSILON_CP
pub const MAX_LEVELS: usize = 64;     // BACKED_MAX_PLIES
pub const CASCADE_GUARD: usize = 100_000;
