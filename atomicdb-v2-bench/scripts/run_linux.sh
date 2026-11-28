#!/usr/bin/env bash
# Linux cross-check of the embedded stores: build, load, suite and crash test.
# Meant to run inside a container (see the benchmark report):
#   docker run --rm -v <repo>/atomicdb-v2-bench:/src:ro -v <dataset dir>:/data:ro \
#       -v <results dir>:/results -v atomicdb-bench-work:/work rust:1-bookworm bash /src/scripts/run_linux.sh ball
# /work must be a real Linux filesystem (a named volume), not a bind mount of a Windows path.
set -euo pipefail
NAME="${1:-ball}"
STORES="${STORES:-mem memwal mmap lmdb sqlite rocksdb redb fjall}"
CRASH_STORES="${CRASH_STORES:-mmap memwal lmdb sqlite rocksdb redb fjall}"
TRIALS="${TRIALS:-20}"
TAG="${TAG:-linux}"

export DEBIAN_FRONTEND=noninteractive
if ! command -v clang >/dev/null; then
  apt-get update -qq && apt-get install -y -qq clang libclang-dev >/dev/null
fi
mkdir -p /work/src /work/target /work/db /work/data
cp -r /src/Cargo.toml /src/Cargo.lock /src/src /work/src/ 2>/dev/null || cp -r /src/Cargo.toml /src/src /work/src/
cp -r "/data/$NAME" /work/data/
cd /work/src
export CARGO_TARGET_DIR=/work/target
export CARGO_BUILD_JOBS="${CARGO_BUILD_JOBS:-8}"
export LIBSQLITE3_FLAGS="-DSQLITE_MAX_MMAP_SIZE=1099511627776"
cargo build --release 2>&1 | tail -3
EXE=/work/target/release/atomicdb-v2-bench
OUT="/results/${TAG}-${NAME}.jsonl"
CRASH="/results/${TAG}-${NAME}-crash.jsonl"
rm -f "$OUT" "$CRASH"
uname -a; nproc; grep MemTotal /proc/meminfo; df -h /work | tail -1; findmnt -no FSTYPE,SOURCE /work || true

for k in $STORES; do
  db="/work/db/${NAME}_$k"
  rm -rf "$db"
  echo "[$(date +%T)] $k: load"
  if [ "$k" != mem ]; then $EXE load --store "$k" --data "/work/data/$NAME" --db "$db" --out "$OUT" --tag "$TAG" >/dev/null; fi
  echo "[$(date +%T)] $k: suite"
  $EXE suite --store "$k" --data "/work/data/$NAME" --db "$db" --out "$OUT" --tag "$TAG" >/dev/null || echo "  suite FAILED"
  rm -rf "$db"
done

for k in $CRASH_STORES; do
  db="/work/db/${NAME}_crash_$k"
  rm -rf "$db"
  echo "[$(date +%T)] $k: crash test"
  $EXE load --store "$k" --data "/work/data/$NAME" --db "$db" >/dev/null
  MMAP_WAL_CKPT_MB=2 $EXE crash --store "$k" --data "/work/data/$NAME" --db "$db" --trials "$TRIALS" --out "$CRASH" >/dev/null || echo "  crash FAILED"
  rm -rf "$db"
done
echo "[$(date +%T)] done"
