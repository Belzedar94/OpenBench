//! Compiles the vendored Fairy-Stockfish sources and the C shim into a static
//! library, with pyffish 0.0.89's flags (its `setup.py`): C++17,
//! `LARGEBOARDS ALLVARS PRECOMPUTED_MAGICS NNUE_EMBEDDING_OFF IS_64BIT`, no
//! popcnt/pext, plus what Python's own build adds to every extension
//! (`NDEBUG`, and `-fwrapv` with gcc/clang).  The C++ is always optimised:
//! an unoptimised movegen makes the tests crawl.

use std::path::{Path, PathBuf};

fn sources(dir: &Path, out: &mut Vec<PathBuf>) {
    let mut entries: Vec<_> = std::fs::read_dir(dir).unwrap().map(|e| e.unwrap().path()).collect();
    entries.sort();
    for p in entries {
        if p.is_dir() {
            sources(&p, out);
        } else if p.extension().is_some_and(|e| e == "cpp") {
            out.push(p);
        }
    }
}

fn main() {
    let root = PathBuf::from(std::env::var("CARGO_MANIFEST_DIR").unwrap());
    let fsf = root.join("fsf").join("src");
    let mut files = Vec::new();
    sources(&fsf, &mut files);
    files.push(root.join("shim").join("av2fsf.cpp"));

    let mut b = cc::Build::new();
    b.cpp(true)
        .files(&files)
        .include(&fsf)
        .define("LARGEBOARDS", None)
        .define("ALLVARS", None)
        .define("PRECOMPUTED_MAGICS", None)
        .define("NNUE_EMBEDDING_OFF", None)
        .define("IS_64BIT", None)
        .define("NDEBUG", None)
        .opt_level(2)
        .debug(false)
        .warnings(false);
    if b.get_compiler().is_like_msvc() {
        b.flag("/std:c++17").flag("/EHsc").flag("/bigobj");
    } else {
        b.flag("-std=c++17").flag("-fwrapv").flag("-Wno-date-time");
    }
    b.compile("av2fsf");

    println!("cargo:rerun-if-changed=shim/av2fsf.cpp");
    println!("cargo:rerun-if-changed=fsf/src");
    println!("cargo:rerun-if-changed=build.rs");
}
