# Atomic Syzygy on OpenBench

OpenBench can route Atomic tablebase workloads only to workers that own the
authenticated 3--6-man Atomic corpus. This is worker capability plumbing; it
does not add a new workload type, result protocol, database table, or stopping
rule. Atomic Syzygy measurements use ordinary fixed-game (`GAMES`) workloads.

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

## Four fixed-game measurements

Run four ordinary `GAMES` workloads, each targeting 2,000 games (the final
concurrent result batch may finish slightly above that threshold):

| Evaluation | Control | Time control | Hash |
| --- | --- | --- | --- |
| Classical | STC | `8.0+0.08` | 32 MB |
| Classical | LTC | `40.0+0.4` | 128 MB |
| NNUE | STC | `8.0+0.08` | 32 MB |
| NNUE | LTC | `40.0+0.4` | 128 MB |

Common settings:

- `test_mode=GAMES`, `test_max_games=2000`;
- the same Atomic-Stockfish source commit, bench and network on both sides;
- `Threads=1`, `syzygy_wdl=6-MAN`, `syzygy_adj=DISABLED`;
- `SyzygyProbeDepth=1 Syzygy50MoveRule=true` on both sides;
- dev has `SyzygyProbeLimit=6`, base has `SyzygyProbeLimit=0`;
- classical adds `Use NNUE=false` on both sides;
- NNUE adds `Use NNUE=true` on both sides and assigns the same frozen network;
- never use `Use NNUE=pure` in playing tests; `pure` is data-generation only;
- use an opening book whose configured filename contains `ATOMIC`, so the
  worker selects cutechess-ob's Atomic referee.

Use the current OpenBench priority controls normally; no special result gate or
ordered-pair upload path exists. Engine-level conformance must already have
verified WDL/DTZ decoding, probe-limit behavior, non-zero `tbhits`, and oracle
positions against this same inventory. These four matches measure playing
impact and do not replace that conformance evidence.

## Deployment

This capability bumps the worker protocol version so workers auto-update and
re-register with the new tablebase advertisement. Restart the server after the
configuration lands, then restart capable workers with both Atomic flags. No
database migration or `--run-syncdb` step is introduced by this change.
