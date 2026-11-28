#!/usr/bin/env bash
# AtomicDB v2 phase 0: the storage benchmark on the production box, small
# scale (real slice + 20M synthetic), pure-Rust stores (no C toolchain on
# the box).  Run inside:
#   systemd-run --scope -p MemoryMax=8G -p CPUQuota=400% nice -n 19 ionice -c3 bash run.sh
set -uo pipefail
W=/root/v2bench-tmp
EXE=$W/atomicdb-v2-bench
OUT=$W/results
mkdir -p $W/data $W/db $OUT
cd $W
log() { echo "[$(date -u +%T)] $*"; }

log "host"
uname -a; nproc; grep 'model name' /proc/cpuinfo | head -1; grep MemTotal /proc/meminfo
findmnt -no FSTYPE,SOURCE,OPTIONS /; cat /proc/mdstat | head -3
for d in nvme0n1 nvme1n1; do echo "$d scheduler: $(cat /sys/block/$d/queue/scheduler)"; done
cat /proc/loadavg

log "fdatasync probe"
python3 $W/fsync_probe.py $W/db/fsync_probe.bin > $OUT/fsync.json
cat $OUT/fsync.json

if [ ! -d data/ball ]; then
  log "import ball"
  zstd -dqf ball_pos.csv.zst -o ball_pos.csv && zstd -dqf ball_edges.csv.zst -o ball_edges.csv
  $EXE import --out data/ball --pos ball_pos.csv --edges ball_edges.csv
  rm -f ball_pos.csv ball_edges.csv
fi
if [ ! -d data/syn20m ]; then
  log "gen syn20m"
  /usr/bin/time -v $EXE gen --out data/syn20m --nodes 20000000 --seed 1 2>&1 | grep -E 'Elapsed|Maximum resident' || true
fi
du -sh data/*

STORES="${STORES:-mem memwal mmap redb fjall}"
for name in ball syn20m; do
  for k in $STORES; do
    db="$W/db/${name}_$k"; rm -rf "$db"
    log "$name $k: load"
    if [ "$k" != mem ]; then
      $EXE load --store $k --data data/$name --db $db --out $OUT/linux-$name.jsonl --tag linux --cache-mb 2048 >/dev/null || log "  load FAILED"
    fi
    log "$name $k: suite"
    $EXE suite --store $k --data data/$name --db $db --out $OUT/linux-$name.jsonl --tag linux --cache-mb 2048 \
      --descents 2000 --read-seconds 5 --readers 8 --ingest1 500 --ingest32 200 --climb32 200 --mixed-batches 100 \
      > $OUT/suite-$name-$k.log 2>&1 || { log "  suite FAILED"; tail -5 $OUT/suite-$name-$k.log; }
    rm -rf "$db"
  done
done

log "crash tests (ball)"
export MMAP_WAL_CKPT_MB=2
for k in ${CRASH_STORES:-mmap memwal redb fjall}; do
  db="$W/db/ball_crash_$k"; rm -rf "$db"
  $EXE load --store $k --data data/ball --db $db >/dev/null
  log "crash $k"
  $EXE crash --store $k --data data/ball --db $db --trials ${TRIALS:-20} --out $OUT/linux-ball-crash.jsonl \
    > $OUT/crash-$k.log 2>&1 || log "  crash FAILED"
  grep crash_summary $OUT/crash-$k.log || tail -3 $OUT/crash-$k.log
  rm -rf "$db"
done
cat /proc/loadavg
log "done"
