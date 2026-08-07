#!/bin/sh

set -eu

workspace="${WORKSPACE:-/work}"
source_dir="${CUTECHESS_SOURCE:-$workspace/cutechess}"
build_root="${BUILD_ROOT:-/tmp/horde-referee-build}"
output_dir="$workspace/horde-referee-artifacts/linux"
qt_commit="c59ae95d8aed879768fe09e0de04f693724e6319"
export SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-1786036843}"

if [ ! -d "$source_dir/.git" ]; then
    echo "Missing patched cutechess source: $source_dir" >&2
    exit 1
fi
if [ -e "$build_root" ]; then
    echo "Refusing to reuse build directory: $build_root" >&2
    exit 1
fi

# Qt's resource compiler records input mtimes, and every workflow run gets a
# fresh checkout with fresh timestamps. Normalize them exactly like the Windows
# build does, otherwise the binary can never reproduce a recorded hash.
find "$source_dir" -type f ! -path "$source_dir/.git/*" \
    -exec touch -d "@$SOURCE_DATE_EPOCH" {} +

# Pin the requested toolchain by exact version. ``alpine:3.22.1`` is pinned by
# digest but its package repository keeps moving, so an unpinned ``apk add``
# silently changed the compiler between builds and made the recorded binary
# hash unreproducible. ``ninja`` is a virtual name provided by ``samurai``, so
# the provider is what can carry a version.
apk add --no-cache \
    "cmake=3.31.7-r1" \
    "file=5.46-r2" \
    "g++=14.2.0-r6" \
    "git=2.49.1-r0" \
    "linux-headers=6.14.2-r0" \
    "make=4.4.1-r3" \
    "mesa-dev=25.1.9-r0" \
    "samurai=1.2-r7" \
    "perl=5.40.4-r0" \
    "python3=3.12.13-r0"

# Full installed inventory, so the transitive closure is auditable from the
# artifact and a drifting dependency is visible instead of silent.
apk info -v | sort > /tmp/horde-referee-packages.txt

mkdir -p "$build_root/qt-source" "$build_root/qt-build" \
    "$build_root/qt-install"
git clone --filter=blob:none --no-checkout \
    https://github.com/qt/qt5.git "$build_root/qt-source"
git -C "$build_root/qt-source" checkout --detach "$qt_commit"
(
    cd "$build_root/qt-source"
    perl init-repository --module-subset=qtbase
)
(
    cd "$build_root/qt-build"
    "$build_root/qt-source/configure" \
        -static -release -opensource -confirm-license \
        -prefix "$build_root/qt-install" \
        -nomake examples -nomake tests -nomake tools
    make -j2
    make install
)

cmake -S "$source_dir" -B "$build_root/cutechess-build" -G Ninja \
    -DWITH_GUI=OFF \
    -DWITH_TESTS=ON \
    -DWITH_STATIC_RUNTIME=ON \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_DISABLE_FIND_PACKAGE_Qt6=TRUE \
    -DCMAKE_PREFIX_PATH="$build_root/qt-install"
cmake --build "$build_root/cutechess-build" --parallel 2

python3 "$workspace/Client/referees/LICHESS_HORDE_V1/run_tests.py" \
    "$build_root/cutechess-build/test_chessboard"
ctest --test-dir "$build_root/cutechess-build" \
    -E chessboard --output-on-failure

strip --strip-all "$build_root/cutechess-build/cutechess-cli"
file "$build_root/cutechess-build/cutechess-cli" | grep 'statically linked'

binary_sha256="$(sha256sum "$build_root/cutechess-build/cutechess-cli" \
    | cut -d ' ' -f 1)"
binary_bytes="$(wc -c < "$build_root/cutechess-build/cutechess-cli" | tr -d ' ')"

# Fail closed on a drifting binary exactly like the Windows job does. The
# manifest carries no expected Linux hash until the first reproducible build
# has been recorded; until then the values are printed for that purpose and
# the gate stays inert rather than silently accepting anything forever.
expected_binary_sha256="$(python3 -c "import json,sys; print((json.load(open(sys.argv[1]))['static_build']['linux'].get('expected_referee_sha256') or '').lower())" \
    "$workspace/Client/referees/LICHESS_HORDE_V1/manifest.json")"
expected_binary_bytes="$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['static_build']['linux'].get('expected_referee_bytes') or '')" \
    "$workspace/Client/referees/LICHESS_HORDE_V1/manifest.json")"
echo "LINUX_REFEREE_SHA256=$binary_sha256"
echo "LINUX_REFEREE_BYTES=$binary_bytes"
if [ -n "$expected_binary_sha256" ] || [ -n "$expected_binary_bytes" ]; then
    if [ "$binary_sha256" != "$expected_binary_sha256" ] \
        || [ "$binary_bytes" != "$expected_binary_bytes" ]; then
        echo "Static Linux referee does not match the reproducible binary lock" >&2
        exit 1
    fi
fi

mkdir -p "$output_dir"
install -m 0755 "$build_root/cutechess-build/cutechess-cli" \
    "$output_dir/cutechess-ob"
(
    cd "$output_dir"
    printf '%s  cutechess-ob\n' "$binary_sha256" > SHA256SUMS
)

{
    g++ --version | head -n 1
    cmake --version | head -n 1
    ninja --version
    "$build_root/qt-install/bin/qmake" -v
    "$output_dir/cutechess-ob" --version
    echo '--- apk inventory ---'
    cat /tmp/horde-referee-packages.txt
} > "$output_dir/toolchain.txt"

# The build must run as root for ``apk``, but the artifact lives in a bind
# mount owned by the caller. Hand it back so the receipt step (and any local
# user) can write ``artifact-receipt.json`` beside the binary.
if [ -n "${HOST_UID:-}" ] && [ -n "${HOST_GID:-}" ]; then
    chown -R "$HOST_UID:$HOST_GID" "$workspace/horde-referee-artifacts"
fi
