# Vendored Fairy-Stockfish

These are the C++ sources of the `pyffish` that AtomicDB v1 runs, so that v2
asks the very same movegen (plan section 6, "Rules of chess").

- Repository: https://github.com/Belzedar94/KOTH-Stockfish (the owner's fork
  of Fairy-Stockfish, renamed from `Belzedar94/Fairy-Stockfish`).
- Commit: `b523fd1d9cb93cf7a151cb9bebe03001dcdd640c`, branch
  `pyffish-variant-cache` (PR #102, "pyffish: skip rebuilding the variant
  tables on every call"), on top of `f69f4878d4996737f1578285fdd4420cbdf786c5`
  (branch `pyffish-0.0.89`, the "Bump versions (#1000)" commit that PyPI's
  `pyffish==0.0.89` is built from).  The patch touches `src/pyffish.cpp` only.
- Production (checked read-only on 28-Nov-2026): `/opt/openbench/.venv` runs
  `pyffish 0.0.89+variantcache` built from that commit
  (`/root/pyffish-wheels/pyffish-0.0.89+variantcache-cp312-cp312-linux_x86_64.whl`,
  sha256 `3f62111a...6a16`; banner `Fairy-Stockfish 231126 LB`).
- Copied: `src/*.cpp`, `src/*.h` and the `syzygy/`, `nnue/`, `incbin/`
  subdirectories, unchanged, except `main.cpp`, `pyffish.cpp` and
  `ffishjs.cpp` (the Python and JavaScript bindings and the UCI entry point,
  replaced here by `../shim/av2fsf.cpp`), plus `Copying.txt` (GPL-3.0) and
  `AUTHORS`.
- Build flags: pyffish's `setup.py` (C++17, `LARGEBOARDS ALLVARS
  PRECOMPUTED_MAGICS NNUE_EMBEDDING_OFF IS_64BIT`) plus Python's `NDEBUG` and,
  with gcc/clang, `-fwrapv`.  See `../build.rs`.

To update: check out the new commit, copy the same files over, rerun the
equivalence check against the pyffish production runs (`tools/golden.py`,
`examples/equiv.rs`) and record the result in the PR.
