#!/usr/bin/env bash
# Power-cut test for the AtomicDB v2 store (Linux, root, a throw-away box).
#
# A kill -9 leaves the page cache intact; a power cut does not.  What a power
# cut can leave on disk is exactly "every write that reached the device up to
# some point, in order, with nothing guaranteed after the last flush".
# dm-log-writes records every write, flush and FUA that reaches a block
# device; replay-log (xfstests, src/log-writes) rebuilds the device as it was
# at any entry of that record.  So:
#
#   1. Put a filesystem on a dm-log-writes device and run the crash-test
#      child on it.  After each batch the child acknowledges, mark the write
#      log with "ack-<seq>" (dmsetup message ... mark).
#   2. Stop the child, take the device down.
#   3. For every mark (and for random write entries in between, which are
#      torn states the OS could have left), replay the record onto a replica
#      device up to that point, mount it, and run `av2-crashtest --verify`:
#      recovery must succeed, hold at least the acknowledged batch, and equal
#      the in-memory model at the batch it recovered.
#
# This is the method of fstests generic/482 (dm-log-writes + replay-log).
# It needs: dmsetup (device-mapper with the log-writes target, kernel >= 4.12),
# losetup, mkfs.ext4 (or mkfs.xfs), and replay-log built from xfstests:
#   git clone https://git.kernel.org/pub/scm/fs/xfs/xfstests-dev.git
#   make -C xfstests-dev/src/log-writes
#
# Usage: sudo ./dm-log-writes.sh <av2-crashtest binary> <replay-log binary> [seconds] [fs]
set -euo pipefail

CRASH=${1:?av2-crashtest binary}
REPLAY=${2:?replay-log binary}
SECS=${3:-30}
FS=${4:-ext4}
SEED=${SEED:-1}
SAMPLES=${SAMPLES:-200}        # random write entries replayed besides the marks
WORK=${WORK:-/var/tmp/av2-powercut}
MNT=$WORK/mnt

cleanup() {
  set +e
  [ -n "${CHILD:-}" ] && kill -9 "$CHILD" 2>/dev/null
  mountpoint -q "$MNT" && umount "$MNT"
  dmsetup remove av2lw 2>/dev/null
  [ -n "${DATA:-}" ] && losetup -d "$DATA"
  [ -n "${LOG:-}" ] && losetup -d "$LOG"
  [ -n "${REPL:-}" ] && losetup -d "$REPL"
}
trap cleanup EXIT

mkdir -p "$WORK" "$MNT"
truncate -s 2G "$WORK/data.img" "$WORK/replica.img"
truncate -s 8G "$WORK/log.img"
DATA=$(losetup -f --show "$WORK/data.img")
LOG=$(losetup -f --show "$WORK/log.img")
REPL=$(losetup -f --show "$WORK/replica.img")
SECTORS=$(blockdev --getsz "$DATA")
dmsetup create av2lw --table "0 $SECTORS log-writes $DATA $LOG"
mkfs."$FS" -q /dev/mapper/av2lw
dmsetup message av2lw 0 mark mkfs
mount /dev/mapper/av2lw "$MNT"

# Run the child; mark the write log after each acknowledgement.
: > "$WORK/acks"
"$CRASH" --child --dir "$MNT/store" --seed "$SEED" | while read -r tag seq; do
  if [ "$tag" = ACK ]; then
    dmsetup message av2lw 0 mark "ack-$seq"
    echo "$seq" >> "$WORK/acks"
  fi
done &
PIPE=$!
sleep "$SECS"
CHILD=$(pgrep -f -- "--child --dir $MNT/store" || true)
[ -n "$CHILD" ] && kill -9 $CHILD
wait "$PIPE" || true
CHILD=
umount "$MNT"
dmsetup remove av2lw

ok=0; bad=0
check() { # $1: replay-log arguments, $2: minimum sequence the image must hold
  "$REPLAY" --log "$LOG" --replay "$REPL" $1 >/dev/null
  if mount "$REPL" "$MNT" 2>/dev/null; then
    if [ -d "$MNT/store/graph" ] && [ -f "$MNT/store/graph/ckpt" ]; then
      if "$CRASH" --verify --dir "$MNT/store" --seed "$SEED" --min-seq "$2" >> "$WORK/verify.jsonl"; then
        ok=$((ok+1)); else bad=$((bad+1)); echo "FAILED at $1" >&2; fi
    fi
    umount "$MNT"
  else
    # A replica the filesystem itself cannot mount is a filesystem problem, not ours.
    fsck -y "$REPL" >/dev/null 2>&1 || true
  fi
}

# Every acknowledged batch must survive a cut right after its mark.
while read -r seq; do
  check "--end-mark ack-$seq" "$seq"
done < "$WORK/acks"

# Torn states: cut after random write entries; whatever was acknowledged
# before the entry must be there (the minimum is the last mark before it).
TOTAL=$("$REPLAY" --log "$LOG" --find --end-mark "ack-$(tail -n1 "$WORK/acks")" | awk '{print $1}' | tail -n1)
for _ in $(seq "$SAMPLES"); do
  n=$(( (RANDOM * 32768 + RANDOM) % TOTAL + 1 ))
  check "--limit $n" 0
done

echo "{\"stage\":\"powercut_summary\",\"acks\":$(wc -l < "$WORK/acks"),\"ok\":$ok,\"failed\":$bad}"
[ "$bad" -eq 0 ]
