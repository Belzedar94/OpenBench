//! Raw bindings to `shim/av2fsf.cpp`.  Strings in are NUL-terminated; strings
//! out live in a thread-local buffer of the shim until the next call on the
//! same thread, so the safe wrappers copy them at once.

use std::os::raw::{c_char, c_int};

unsafe extern "C" {
    pub fn av2fsf_init() -> c_int;
    pub fn av2fsf_info(len: *mut usize) -> *const c_char;
    pub fn av2fsf_start_fen(len: *mut usize) -> *const c_char;
    pub fn av2fsf_validate_fen(fen: *const c_char) -> c_int;
    pub fn av2fsf_legal_moves(fen: *const c_char, out: *mut *const c_char, len: *mut usize) -> c_int;
    pub fn av2fsf_get_fen(fen: *const c_char, moves: *const c_char, out: *mut *const c_char, len: *mut usize) -> c_int;
    pub fn av2fsf_children(fen: *const c_char, out: *mut *const c_char, len: *mut usize) -> c_int;
    pub fn av2fsf_is_immediate_game_end(fen: *const c_char, result: *mut c_int) -> c_int;
    pub fn av2fsf_game_result(fen: *const c_char, result: *mut c_int) -> c_int;
}
