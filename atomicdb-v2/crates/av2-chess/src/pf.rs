//! One function per pyffish call that v1 makes, with the variant fixed to
//! `atomic` and an empty move list where v1 passes one.  The answers are the
//! library's, unchanged (see `shim/av2fsf.cpp`).

use crate::ffi;
use crate::{ChessError, Result};
use std::ffi::CString;
use std::os::raw::{c_char, c_int};
use std::sync::OnceLock;

static INIT: OnceLock<()> = OnceLock::new();

/// Initialise the library (once per process; every call does it on demand).
pub fn init() {
    INIT.get_or_init(|| {
        let r = unsafe { ffi::av2fsf_init() };
        assert_eq!(r, 0, "Fairy-Stockfish failed to initialise ({r})");
    });
}

fn cstr(s: &str) -> Result<CString> {
    CString::new(s).map_err(|_| ChessError::Nul)
}

/// Copy the shim's thread-local output.  The movegen only writes ASCII.
fn take(ptr: *const c_char, len: usize) -> String {
    if len == 0 {
        return String::new();
    }
    let bytes = unsafe { std::slice::from_raw_parts(ptr as *const u8, len) };
    String::from_utf8_lossy(bytes).into_owned()
}

/// `pyffish.info()`: the engine banner ("Fairy-Stockfish 010526 LB ...").
pub fn info() -> String {
    init();
    let mut len = 0usize;
    let p = unsafe { ffi::av2fsf_info(&mut len) };
    take(p, len)
}

/// `pyffish.start_fen('atomic')`, as the variant declares it.
pub fn start_fen() -> String {
    init();
    let mut len = 0usize;
    let p = unsafe { ffi::av2fsf_start_fen(&mut len) };
    take(p, len)
}

/// `pyffish.validate_fen(fen, 'atomic')`: 1 (`FEN_OK`) or a negative code.
pub fn validate_fen(fen: &str) -> i32 {
    init();
    match cstr(fen) {
        Ok(c) => unsafe { ffi::av2fsf_validate_fen(c.as_ptr()) },
        Err(_) => -100,
    }
}

/// `pyffish.legal_moves('atomic', fen, [])`, movegen order.
pub fn legal_moves(fen: &str) -> Result<Vec<String>> {
    init();
    let c = cstr(fen)?;
    let mut p: *const c_char = std::ptr::null();
    let mut len = 0usize;
    let n = unsafe { ffi::av2fsf_legal_moves(c.as_ptr(), &mut p, &mut len) };
    if n < 0 {
        return Err(ChessError::Engine("legal_moves"));
    }
    if n == 0 {
        return Ok(Vec::new());
    }
    Ok(take(p, len).split(' ').map(str::to_string).collect())
}

/// `pyffish.get_fen('atomic', fen, moves)`.
pub fn get_fen(fen: &str, moves: &[&str]) -> Result<String> {
    init();
    let c = cstr(fen)?;
    for m in moves {
        if m.is_empty() || m.contains(char::is_whitespace) {
            return Err(ChessError::IllegalMove { index: 0, uci: m.to_string() });
        }
    }
    let ms = cstr(&moves.join(" "))?;
    let mut p: *const c_char = std::ptr::null();
    let mut len = 0usize;
    let r = unsafe { ffi::av2fsf_get_fen(c.as_ptr(), ms.as_ptr(), &mut p, &mut len) };
    match r {
        0 => Ok(take(p, len)),
        k if k > 0 => {
            let index = (k - 1) as usize;
            Err(ChessError::IllegalMove { index, uci: moves[index].to_string() })
        }
        _ => Err(ChessError::Engine("get_fen")),
    }
}

/// Every legal move with `get_fen(fen, [move])`, movegen order: one call for
/// what v1 asks with `1 + n` calls.
pub fn children(fen: &str) -> Result<Vec<(String, String)>> {
    init();
    let c = cstr(fen)?;
    let mut p: *const c_char = std::ptr::null();
    let mut len = 0usize;
    let n = unsafe { ffi::av2fsf_children(c.as_ptr(), &mut p, &mut len) };
    if n < 0 {
        return Err(ChessError::Engine("children"));
    }
    let text = take(p, len);
    let mut out = Vec::with_capacity(n as usize);
    for line in text.lines() {
        let (uci, fen) = line.split_once(' ').ok_or(ChessError::Engine("children"))?;
        out.push((uci.to_string(), fen.to_string()));
    }
    debug_assert_eq!(out.len(), n as usize);
    Ok(out)
}

/// `pyffish.is_immediate_game_end('atomic', fen, [])`: `(ended, result)`.
pub fn is_immediate_game_end(fen: &str) -> Result<(bool, i32)> {
    init();
    let c = cstr(fen)?;
    let mut res: c_int = 0;
    let r = unsafe { ffi::av2fsf_is_immediate_game_end(c.as_ptr(), &mut res) };
    if r < 0 {
        return Err(ChessError::Engine("is_immediate_game_end"));
    }
    Ok((r == 1, res))
}

/// `pyffish.game_result('atomic', fen, [])`, side-to-move POV.  Meant for
/// positions without a legal move, as in pyffish.
pub fn game_result(fen: &str) -> Result<i32> {
    init();
    let c = cstr(fen)?;
    let mut res: c_int = 0;
    let r = unsafe { ffi::av2fsf_game_result(c.as_ptr(), &mut res) };
    if r < 0 {
        return Err(ChessError::Engine("game_result"));
    }
    Ok(res)
}
