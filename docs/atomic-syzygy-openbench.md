# Atomic Syzygy on OpenBench

OpenBench can route Atomic tablebase workloads only to workers that own the
authenticated 3--6-man Atomic corpus. This is worker capability plumbing; it
supports ordinary fixed-game (`GAMES`) measurements and an opt-in v40
environment contract for generic `DATAGEN`. It does not add a statistical
stopping rule: GAMES still finishes by game count and DATAGEN by its immutable
chunk map.

## Worker-local corpus

The corpus is not an OpenBench download. It contains 1,020 `.atbw`/`.atbz`
files and 236,554,027,392 logical bytes, so each capable worker acquires and
verifies it out of band. Start the worker with the `combined` hardlink directory
and its exact inventory:

```powershell
python client.py -U <user> -P <pass> -S <server> -T <threads> -N 1 `
  --atomic-syzygy "F:\Atomic-Stockfish-artifacts\atomic-syzygy\combined" `
  --atomic-syzygy-manifest "C:\Users\djime\Documents\Chess_variants\Codex\Fairy-Stockfish organization\Atomic Project\baseline-artifacts\atomic-syzygy\source-manifests\remote-inventory.json"
```

Both flags are required together. At startup the client:

- finds the highest complete cardinality with both `.atbw` and `.atbz` files;
- validates every runtime name and byte count against the inventory;
- proves each runtime file is the same hardlink as its authenticated
  publisher-layout source;
- requires each source directory's `.acquisition-complete.json` marker to bind
  an official-MD5 pass to the exact inventory and to postdate its files; and
- advertises the Atomic maximum cardinality and inventory SHA-256.

Before every tablebase-backed DATAGEN launch, the client repeats the inventory,
hardlink, marker, and completeness checks. The resolved command is logged only
by SHA-256; worker-local paths are never printed.

The Atomic engine configuration pins that capability:

```json
"tablebase_family": "atomic",
"tablebase_manifest_sha256": "3d4b7fd0ab387f4f60da2078f612c9e8890e6026f551aebe8631efc157788f23"
```

The scheduler will not assign an explicit 6-man Atomic workload to a worker
with orthodox tables, an incomplete Atomic corpus, or a different inventory.
The worker supplies the Atomic path as `SyzygyPath` to both engines. Explicit
per-side `SyzygyProbeLimit` values are preserved.

Atomic workloads must set `syzygy_adj=DISABLED`: cutechess-ob's tablebase
adjudicator consumes orthodox `.rtbw` files. Engine probing remains enabled via
`syzygy_wdl=6-MAN`.

## Generic DATAGEN with Atomic tables

Tablebase-backed DATAGEN must use all four environment placeholders:

```text
syzygy "{SYZYGY}" syzygy_manifest_sha256 {SYZYGY_MANIFEST_SHA256} syzygy_max {SYZYGY_MAX} teacher_mode {TEACHER_MODE}
```

Publishable presets additionally pass
`producer_sha256 {PRODUCER_SHA256}` so OpenBench binds every chunk to the exact
content-addressed generator executable. This provenance option is separate from
the four-field Syzygy environment wire.

`syzygy_wdl` is an explicit N-MAN limit and `syzygy_adj` remains disabled.
Teacher mode is explicit and byte-exact: `pure` or `true`; `true` means the
legacy-playing teacher. The scheduler requires the exact Atomic manifest and
sufficient cardinality. At claim time the server freezes that authenticated
worker capability in a hashed lease; at upload it creates a hashed receipt
binding the lease to the exact output bytes. Neither document contains the
worker-local tablebase path.

The depth-7 `pure` and `true` presets are canaries, kept separate from the
legacy depth-6 default. Do not activate them until the Atomic engine bridge,
golden vectors, and a local A/B format check are green.

## Four fixed-game measurements

Run the four named Syzygy presets as ordinary `GAMES` workloads, each targeting
2,000 games. Applying a preset sets `test_max_games=2000` and selects `GAMES`
automatically; do not change the test mode afterwards.

| Evaluation | Control | Time control | Hash |
| --- | --- | --- | --- |
| Classical | STC | `8.0+0.08` | 32 MB |
| Classical | LTC | `40.0+0.4` | 128 MB |
| NNUE | STC | `8.0+0.08` | 32 MB |
| NNUE | LTC | `40.0+0.4` | 128 MB |

Common settings:

- `test_mode=GAMES` in the workload and `test_max_games=2000` from the preset;
- the same Atomic-Stockfish source commit, bench and network on both sides;
- `Threads=1`, `syzygy_wdl=6-MAN`, `syzygy_adj=DISABLED`;
- `SyzygyProbeDepth=1 Syzygy50MoveRule=true` on both sides;
- dev has `SyzygyProbeLimit=6`, base has `SyzygyProbeLimit=0`;
- classical adds `Use NNUE=false` on both sides;
- NNUE adds `Use NNUE=true` on both sides and assigns the same frozen network;
- never use `Use NNUE=pure` in playing tests; `pure` is data-generation only;
- use an opening book whose configured filename contains `ATOMIC`, so the
  worker selects cutechess-ob's Atomic referee.

The exact preset names are `Syzygy STC NNUE`, `Syzygy LTC NNUE`,
`Syzygy STC classical`, and `Syzygy LTC classical`. They inherit the current
Atomic defaults (`[1.00, 6.00]` bounds and `movecount=4 score=800` win
adjudication), but fixed-game completion -- not SPRT or LOS -- determines when
these four measurements finish.

Use the current OpenBench priority controls normally; no special result gate or
ordered-pair upload path exists. Engine-level conformance must already have
verified WDL/DTZ decoding, probe-limit behavior, non-zero `tbhits`, and oracle
positions against this same inventory. These four matches measure playing
impact and do not replace that conformance evidence.

## Deployment

This capability uses worker protocol version 40 so workers auto-update and
re-register with the tablebase advertisement. Migration `0007` adds frozen
campaign contracts and per-chunk leases/receipts; run it normally after a DB
and Media backup. Validate on an isolated `:8001` clone first. Restart the
production server and capable workers only in a coordinated idle window, and
never use `--fake` for `0007`.
