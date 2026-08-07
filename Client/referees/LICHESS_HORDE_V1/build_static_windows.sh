#!/usr/bin/env bash

set -euo pipefail

workspace="$PWD"
source_dir="${CUTECHESS_SOURCE:-$workspace/cutechess}"
build_dir="${HORDE_WINDOWS_BUILD_DIR:-$workspace/.horde-referee-build-windows}"
output_dir="${HORDE_WINDOWS_OUTPUT_DIR:-$workspace/horde-referee-artifacts/windows}"
export SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-1786036843}"
lock_file="$workspace/Client/referees/LICHESS_HORDE_V1/windows-toolchain-lock.json"
expected_lock_sha256="5074d87a427170d0992a6c6ff1db2f8bcc7e8fdc951162892b9b9482970a3d43"
expected_binary_sha256="1c0bbab69e15a277c0b68bf032848b513f706749999cd5f6d09a1fb60f05b8a6"
expected_binary_bytes="7511040"
pacman_config="/etc/horde-pacman.conf"

actual_lock_sha256="$(sha256sum "$lock_file" | cut -d ' ' -f 1)"
if [[ "$actual_lock_sha256" != "$expected_lock_sha256" ]]; then
    echo "Windows toolchain lock SHA-256 mismatch" >&2
    exit 1
fi
if [[ ! -f "$pacman_config" ]]; then
    echo "Missing repository-free pacman configuration" >&2
    exit 1
fi

if [[ ! -e "$source_dir/.git" ]]; then
    echo "Missing patched cutechess source: $source_dir" >&2
    exit 1
fi
if [[ -e "$build_dir" ]]; then
    echo "Refusing to reuse build directory: $build_dir" >&2
    exit 1
fi

# Qt's resource compiler records input mtimes. Normalize the patched checkout so
# independent runner checkouts and build paths produce byte-identical resources.
find "$source_dir" -type f ! -path "$source_dir/.git/*" \
    -exec touch -d "@$SOURCE_DATE_EPOCH" {} +

expect_package() {
    local package="$1"
    local version="$2"
    local installed
    installed="$(pacman --config "$pacman_config" -Q "$package")"
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

ctest --test-dir "$build_dir" -E chessboard --output-on-failure

strip --strip-all "$build_dir/cutechess-cli.exe"
binary_sha256="$(sha256sum "$build_dir/cutechess-cli.exe" | cut -d ' ' -f 1)"
binary_bytes="$(wc -c < "$build_dir/cutechess-cli.exe" | tr -d ' ')"
if [[ "$binary_sha256" != "$expected_binary_sha256" ]] \
    || [[ "$binary_bytes" != "$expected_binary_bytes" ]]; then
    echo "Static Windows referee does not match the reproducible binary lock" >&2
    exit 1
fi
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
    printf '%s  cutechess-ob.exe\n' "$binary_sha256" > SHA256SUMS
)

{
    gcc --version | head -n 1
    cmake --version | head -n 1
    ninja --version
    printf 'windows-toolchain-lock sha256:%s\n' "$actual_lock_sha256"
    "$output_dir/cutechess-ob.exe" --version
} > "$output_dir/toolchain.txt"
