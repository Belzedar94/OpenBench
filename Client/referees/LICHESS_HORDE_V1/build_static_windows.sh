#!/usr/bin/env bash

set -euo pipefail

workspace="$PWD"
source_dir="${CUTECHESS_SOURCE:-$workspace/cutechess}"
build_dir="$workspace/.horde-referee-build-windows"
output_dir="$workspace/horde-referee-artifacts/windows"
export SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-1786036843}"

if [[ ! -d "$source_dir/.git" ]]; then
    echo "Missing patched cutechess source: $source_dir" >&2
    exit 1
fi
if [[ -e "$build_dir" ]]; then
    echo "Refusing to reuse build directory: $build_dir" >&2
    exit 1
fi

expect_package() {
    local package="$1"
    local version="$2"
    local installed
    installed="$(pacman -Q "$package")"
    if [[ "$installed" != "$package $version" ]]; then
        echo "Unexpected package version: $installed" >&2
        exit 1
    fi
}

expect_package mingw-w64-x86_64-cmake 4.3.4-1
expect_package mingw-w64-x86_64-ninja 1.13.2-1
expect_package mingw-w64-x86_64-gcc 16.1.0-5
expect_package mingw-w64-x86_64-crt 14.0.0.r179.g24aaa6147-1
expect_package mingw-w64-x86_64-headers 14.0.0.r179.g24aaa6147-1
expect_package mingw-w64-x86_64-winpthreads 14.0.0.r179.g24aaa6147-1
expect_package mingw-w64-x86_64-qt5-static 5.15.19-1

cmake -S "$source_dir" -B "$build_dir" -G Ninja \
    -DWITH_GUI=OFF \
    -DWITH_TESTS=ON \
    -DWITH_STATIC_RUNTIME=ON \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_DISABLE_FIND_PACKAGE_Qt6=TRUE \
    -DCMAKE_PREFIX_PATH=/mingw64/qt5-static
cmake --build "$build_dir" --parallel 4

python Client/referees/LICHESS_HORDE_V1/run_tests.py \
    "$build_dir/test_chessboard.exe"
ctest --test-dir "$build_dir" -E chessboard --output-on-failure

strip --strip-all "$build_dir/cutechess-cli.exe"
file "$build_dir/cutechess-cli.exe"
imports="$(objdump -p "$build_dir/cutechess-cli.exe" | grep 'DLL Name')"
printf '%s\n' "$imports"
if printf '%s\n' "$imports" | grep -Eiq \
    'Qt5|libgcc|libstdc|libwinpthread|zlib|zstd'; then
    echo "Static Windows referee has a forbidden runtime dependency" >&2
    exit 1
fi

mkdir -p "$output_dir"
install -m 0755 "$build_dir/cutechess-cli.exe" \
    "$output_dir/cutechess-ob.exe"
(
    cd "$output_dir"
    sha256sum cutechess-ob.exe > SHA256SUMS
)

{
    gcc --version | head -n 1
    cmake --version | head -n 1
    ninja --version
    "$output_dir/cutechess-ob.exe" --version
} > "$output_dir/toolchain.txt"
