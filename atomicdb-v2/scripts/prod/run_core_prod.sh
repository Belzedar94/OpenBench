#!/usr/bin/env bash
# AtomicDB v2 storage core on the production box: commit, group commit and
# read path on the real disk (md RAID1, ext4), with the read-path hints.
#
# Usage (as root, outside 04:30-07:15 UTC):
#   run_core_prod.sh <dir with the static musl binaries> <dir with ball_pos.csv and ball_edges.csv> <work dir on the md RAID1>
#
# Build the binaries on the tower:
#   CARGO_TARGET_X86_64_UNKNOWN_LINUX_MUSL_LINKER=rust-lld \
#     cargo build --release -p av2-tools -p av2-server --target x86_64-unknown-linux-musl
#
# Everything runs under a memory cap, niced, with idle-class I/O; one JSON
# line per measurement on stdout.  Takes about 15 minutes; the work dir needs
# about 3 GB and is removed at the end.
set -euo pipefail
BIN=${1:?binaries}
DATA=${2:?export dir}
WORK=${3:?work dir on the md RAID1}

now=$(date -u +%H%M)
if [ "$now" -ge 0430 ] && [ "$now" -lt 0715 ]; then
  echo "refusing to run during the pg_dump window (04:30-07:15 UTC)" >&2
  exit 1
fi

run() { systemd-run --scope --quiet -p MemoryMax=6G nice -n 10 ionice -c3 "$@"; }

mkdir -p "$WORK"
rm -rf "$WORK/ball" "$WORK/pw" "$WORK/svc"
run "$BIN/av2-load" --pos "$DATA/ball_pos.csv" --edges "$DATA/ball_edges.csv" --out "$WORK/ball"
run "$BIN/av2-check" --db "$WORK/ball" --pos "$DATA/ball_pos.csv" --structure --footprint

# Read path: the same perf run with each mapping hint (plan: MAP_POPULATE,
# madvise; hugepages only apply where the filesystem supports them).
for hint in none random populate hugepage; do
  case $hint in
    none) envs=() ;;
    random) envs=(AV2_MMAP_ADVICE=random) ;;
    populate) envs=(AV2_MMAP_POPULATE=1) ;;
    hugepage) envs=(AV2_MMAP_ADVICE=hugepage) ;;
  esac
  echo "{\"stage\":\"hint\",\"hint\":\"$hint\"}"
  run env "${envs[@]}" "$BIN/av2-perf" --src "$WORK/ball" --work "$WORK/pw" --readers 4 --secs 5 --b1 300 --b32 100 --recovery-frames 500
done

# Commits on the real fsync: sequential and pipelined, full and lean landings.
for mode in "" "--pipeline" "--lean" "--lean --pipeline"; do
  # shellcheck disable=SC2086
  run "$BIN/av2-perf" --src "$WORK/ball" --work "$WORK/pw" --landings-only --b32 100 --repeat 2 $mode
done

# Group commit through the writer service: closed-loop clients, and an open
# load near and far above production's peak (13 searches a second).
for c in 1 16 128 512; do
  rm -rf "$WORK/svc"; cp -a "$WORK/ball" "$WORK/svc"
  run "$BIN/av2-server" --db "$WORK/svc" --listen 127.0.0.1:0 --bench-secs 15 --clients "$c"
done
for rate in 1 10; do
  rm -rf "$WORK/svc"; cp -a "$WORK/ball" "$WORK/svc"
  run "$BIN/av2-server" --db "$WORK/svc" --listen 127.0.0.1:0 --bench-secs 15 --clients 64 --rate "$rate"
done

# kill -9 on Linux, half of the trials pipelined.
run "$BIN/av2-crashtest" --dir "$WORK/crash" --trials 200 --seed "$(date +%s)"

rm -rf "$WORK/ball" "$WORK/pw" "$WORK/svc" "$WORK/crash"
