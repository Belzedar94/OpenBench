# Crazyhouse referee Windows runtime notices

The DLLs in this directory are unmodified app-local runtime dependencies of
the qualified `cutechess-cli.exe`. They were taken from the MSYS2 MinGW-w64
packages listed below. Exact byte counts and SHA-256 digests are locked in the
contract manifest and rechecked by the worker before every assignment starts.

| Files | MSYS2 package and version | License |
| --- | --- | --- |
| `libdouble-conversion.dll` | `mingw-w64-x86_64-double-conversion` 3.4.0-1 | BSD-3-Clause |
| `libgcc_s_seh-1.dll`, `libstdc++-6.dll` | `mingw-w64-x86_64-gcc-libs` 16.1.0-5 | GPL-3.0-or-later with GCC Runtime Library Exception 3.1 |
| `libicudt78.dll`, `libicuin78.dll`, `libicuuc78.dll` | `mingw-w64-x86_64-icu` 78.3-3 | ICU |
| `libpcre2-16-0.dll` | `mingw-w64-x86_64-pcre2` 10.47-1 | BSD-3-Clause |
| `libwinpthread-1.dll` | `mingw-w64-x86_64-winpthreads` 14.0.0.r179.g24aaa6147-1 | MIT and BSD-3-Clause-Clear |
| `libzstd.dll` | `mingw-w64-x86_64-zstd` 1.5.7-1 | BSD-3-Clause or GPL-2.0-or-later |
| `Qt5Core.dll` | `mingw-w64-x86_64-qt5-base` 5.15.19+kde+r96-1 | LGPL-3.0-only with Qt exception, or GPL-2.0-only/GPL-3.0-only |
| `zlib1.dll` | `mingw-w64-x86_64-zlib` 1.3.1-1 | Zlib |

The corresponding license texts are preserved under `licenses/` with unique
filenames so legacy OpenBench bootstrap clients cannot overwrite one package's
notice with another while flattening an update archive. Package metadata and
source links are published by the [MSYS2 package repository](https://packages.msys2.org/).

The Crazyhouse referee itself is licensed and sourced separately. Its exact
corresponding source is public at
[`Belzedar94/Crazyhouse-cutechess`](https://github.com/Belzedar94/Crazyhouse-cutechess),
commit `d25294c1b1084f8854c0dc026ca3b150c911b4ee`.
