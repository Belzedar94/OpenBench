# RELABEL: rescoring an existing training set

RELABEL is a DATAGEN sub-mode. Instead of generating new positions, the fleet
searches every position of an existing set at a fixed depth (or node count)
and returns the same records with a new score. The first use is Alice T349:
the self-play labels there come from a depth-6 search, and the Spell pilot
showed that deeper labels on the same positions were worth +114 Elo.

## How it works

- **Input set.** The operator cuts the training set into chunks of
  fixed-size records with `Scripts/relabel_split.py` and installs them on the
  server under `Media/relabel/inputs/<set>/` (`chunk_<i>.bz2` plus
  `manifest.json`). For every chunk the manifest holds the record count, the
  SHA-256 of the compressed file, the SHA-256 of the raw records, and the
  SHA-256 of the position keys: each record without the bytes a relabel may
  change. One input set can serve several workloads, for example one at depth
  10 and one at depth 12.
- **Workload.** `/newDatagen/` with *Relabel → Input Set* filled in creates a
  DATAGEN workload whose chunk map is the input set. The manifest's hash and
  each chunk's input identity are frozen on the workload. The command is a
  descriptor such as `RELABEL set=t349-train-v1 format=alice-v2-record-v1
  depth=12 nodes=0`; it has no placeholders and no generator reads it.
  Priority, throughput, approval, leases (5 minutes, renewed by heartbeat),
  retries, requeue on error and progress all work as they do for DATAGEN.
- **Who gets it.** Only workers that advertise `relabel_protocols: [1]` and the
  record format in `relabel_formats` are offered a RELABEL chunk. Older
  workers never see one, and the focus-test message says why.
- **Worker.** The worker builds the ordinary playing engine (not the DATAGEN
  build), checks its bench once, downloads the leased input chunk from
  `POST /clientRelabelInput/` (lease owner only), checks it against the frozen
  SHA-256s, and decodes every record before starting any engine: a corrupt or
  wrong-variant record fails the chunk. It then starts one engine per worker
  thread, capped by the engine's `worker_max_concurrency` and by the memory
  check games use, each with `Threads=1`, the workload's `Hash`, and
  `EvalFile` set to the workload's network. The chunk is split into
  contiguous slices, one per engine, so consecutive positions of a game share
  a transposition table. Each position is searched with
  `position fen <fen>` and `go depth N` (or `go nodes N`). Progress and
  positions per second are printed every minute.
- **What changes in a record.** Only the score, plus the record checksum that
  covers it. The position, the stored move, the game ply and the game result
  are kept byte for byte. The move is **not** replaced by the engine's best
  move: the legacy Alice trainer does not read it, and keeping it makes it
  part of the key the server checks.
- **Score convention.** Records store the engine's internal value from the
  side to move, as the T349 generator wrote it (`int(search.value)`). The
  worker takes the last exact score of the main line from the UCI output and
  converts it back to internal units: `mate N` becomes `32000 - (2N - 1)`
  (`-32000 - 2N` when mated), and `cp X` becomes `round(X * a / 100)`, where
  `a` is the engine's own win-rate parameter for the position's material
  (`uci.cpp`, `win_rate_params`). The rounding error is under two internal
  units. Scores are clamped to ±32000.
- **Server check.** Before an upload is accepted, the server decompresses the
  input and the output in bounded blocks, and checks that the input still
  matches its frozen hashes, that the record count is the same, that every
  byte outside the score and checksum is unchanged, that every score is within
  ±32000, and that every record checksum is valid. A failed check releases the
  lease at once (`last_error` and an event say why) and nothing is stored.
  Accepted chunks are stored as `Media/relabel/<test>/out/chunk_<i>.bz2` and
  can be downloaded with `GET /api/datagen/<test>/<i>/`. When the workload is
  complete, `GET /api/datagen/<test>/` also lists the input set and each
  chunk's frozen input identity.

Supported formats live in `Client/relabel.py` (`FORMATS`, with the decoder)
and `OpenBench/relabel.py` (`RELABEL_FORMATS`); a test keeps the two tables
identical. An engine opts in with `"relabel_formats": [...]` in
`Engines/<engine>.json`. Today the only format is `alice-v2-record-v1`: the
84-byte ALICEV2_RECORD_V1 record of the Alice native datagen, declared by
Alice-Stockfish.

## Measured cost (Alice, run2rl)

Measured on the 5950X with the Alice playing build of the T349 commit
(`ef7f2692`, bench 202963, the same bench as `main`), the run2rl network,
`Threads=1 Hash=64` per engine, on contiguous T349 slices. The machine was
about 80% busy with other jobs, so these numbers are on the pessimistic side.

| depth | 1 engine | 4 engines (per engine) | 8 engines (per engine) |
|---|---|---|---|
| 10 | 40.7 pos/s | 35.0 | 27.7 |
| 12 | 9.8 pos/s | 11.1 | 6.4 |

Planning figures: about **35 positions per second per thread at depth 10** and
**10 per thread at depth 12**. Depth 12 costs 3.5 to 4 times depth 10.

For the 48,000,000 records of the T349 train chunks (about 45.8M remain after
the trainer's mate filter):

| depth | thread-hours | 64 threads | 128 threads | 256 threads | one t8 worker |
|---|---|---|---|---|---|
| 10 | ~380 | ~6 h | ~3 h | ~1.5 h | ~2.5 days |
| 12 | ~1,330 | ~21 h | ~10 h | ~5 h | ~11 days |

Depth-6 check: relabelling 2,000 T349 records at depth 6 reproduces the
stored scores with slope 0.994, intercept +1.4 and correlation 0.989, which
confirms the sign and unit convention. The median absolute difference is 76
internal units; two depth-6 UCI runs that differ only in their transposition
table history already differ by a median of 27, and the generator's search
also carried its history across whole games.

## Runbook: T349 at depth 12

The steps below use the T349 data already audited under `D:\alice-train\t349`.

### 0. Deploy (owner)

- Merge the PR and deploy the server as usual. The migration is
  `OpenBench 0012_relabel_mode`, on the **default** database:
  `./.venv/bin/python manage.py migrate --database default` (not
  `--database=atomicdb`: these are OpenBench core models). Restart the service so `Engines/Alice-Stockfish.json`
  (`relabel_formats`) is reloaded.
- Publish a worker that speaks RELABEL. The PR does not change the client
  version or the pin. Either bump `CLIENT_VERSION` in `Client/worker.py` and
  `client_version` in `Config/config.json` to 54 together with
  `client_repo_ref` pointing at a commit that contains this worker (and the
  version pins in the onboarding tests), as the earlier "Pin client" commits
  did; or, for a pilot, run one worker from a checkout of this branch's
  `Client/`: it still reports client 53, the server accepts it, and only it
  advertises RELABEL.

### 1. Cut the input set (tower)

Train chunks 0 to 47, all records (mate-scored ones included), 100,000 records
per input chunk: 480 chunks, each one about 20 to 30 minutes at depth 12 on an
8-thread worker. Add `chunk_48.bin` (the validation chunk) to the list if a
relabelled validation set is wanted.

```bash
cd /d/ob-pub/relabel        # any checkout of the merged branch
python Scripts/relabel_split.py --set t349-train-v1 \
    --format alice-v2-record-v1 --container alice-v2-chunk \
    --records-per-chunk 100000 --jobs 4 \
    --out D:/alice-train/relabel/t349-train-v1 \
    D:/alice-train/t349/chunks/chunk_{0..47}.bin
```

The script authenticates every T349 chunk header and payload, decodes every
record as an Alice position, and writes `chunk_<i>.bz2` plus `manifest.json`
(about 1.1 GB in total).

### 2. Install it on the server

```bash
scp -r D:/alice-train/relabel/t349-train-v1 \
    root@94.130.239.23:/opt/openbench/Media/relabel/inputs/
ssh root@94.130.239.23 'chown -R --reference=/opt/openbench/Media \
    /opt/openbench/Media/relabel && cd /opt/openbench && \
    ./.venv/bin/python manage.py relabel_check_input t349-train-v1'
```

The check must
print `RELABEL input set t349-train-v1 OK: 480 chunks, 48000000 records`.

### 3. Create the workload

On https://belzedar.duckdns.org/newDatagen/:

| field | value |
|---|---|
| Engine | Alice-Stockfish |
| Repository | https://github.com/Belzedar94/Alice-Stockfish |
| Branch | `590839557d88ac9ab948f034a79e86d0fb1ff934` (main) |
| Bench | 202963 |
| Network | alice_run2rl_e40_l09.nnue (9F9E5570) |
| Options | `Threads=1 Hash=64` |
| Relabel → Input Set | `t349-train-v1` |
| Relabel → Depth | `12` (or `10`) |
| Relabel → Nodes | `0` |
| Priority / Throughput | per the current priority scheme |

The command template, counts, seed, book and publication fields are ignored
when an input set is given. Approve the workload as usual.

### 4. Watch it

- `/datagen/<test>/` shows chunks and progress; each worker log prints
  `RELABEL i/n positions, x pos/s` every minute and the per-engine rate at the
  end of each chunk.
- A rejected upload appears as an event `RELABEL chunk i rejected: <reason>`,
  and the chunk returns to the queue. Any rejection means a worker bug, not
  noise: stop and look before letting it continue.

### 5. Merge into a training bin (tower)

```bash
python Scripts/relabel_merge.py \
    --inputs D:/alice-train/relabel/t349-train-v1 \
    --outputs D:/alice-train/relabel/t349-train-v1-d12-out \
    --fetch https://belzedar.duckdns.org --test <test> \
    --records-out D:/alice-train/relabel/t349-train-v1-d12.records \
    --alice-legacy-psv D:/alice-train/relabel/psv-d12 \
    --alice-tools D:/alice-train/tools --psv-prefix t349_d12
```

The merge downloads the missing output chunks, checks each against its input
chunk and the manifest's position-key SHA-256 (any difference stops it), and
writes:

- `t349-train-v1-d12.records`: all relabelled ALICEV2 records, in input order;
- `psv-d12/t349_d12_chunk_<n>.bin`: the 72-byte legacy PackedSfenValue files,
  one per T349 source chunk, converted with `encode_block` from
  `t349_to_legacy.py` and the same mate filter (|score| >= 10000 dropped),
  plus a `.json` with kept/dropped counts and the SHA-256. These replace
  `D:/alice-train/t349/psv/t349_chunk_<n>.bin` in the run3rl pipeline.

It ends with a JSON summary: records, how many scores changed, the mean
absolute change, and mate counts before and after.

### 6. Clean up

After the merge is verified, purge `Media/relabel/<test>/out/` from the
server as for DATAGEN chunks. Keep the input set while another depth may be
run on it.

## Local checks

```bash
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test OpenBench.tests.test_relabel_mode OpenBench.tests.test_datagen_mode
python -m unittest UnitTests.test_relabel_worker UnitTests.test_datagen_worker
```
