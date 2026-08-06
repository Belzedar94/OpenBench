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

apk add --no-cache \
    cmake file g++ git linux-headers make mesa-dev ninja perl python3

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

mkdir -p "$output_dir"
install -m 0755 "$build_root/cutechess-build/cutechess-cli" \
    "$output_dir/cutechess-ob"
(
    cd "$output_dir"
    sha256sum cutechess-ob > SHA256SUMS
)

{
    g++ --version | head -n 1
    cmake --version | head -n 1
    ninja --version
    "$build_root/qt-install/bin/qmake" -v
    "$output_dir/cutechess-ob" --version
} > "$output_dir/toolchain.txt"
