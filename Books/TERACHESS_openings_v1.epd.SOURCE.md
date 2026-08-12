# TERACHESS_openings_v1.epd

This archive contains the first deterministic Terachess II opening book for
paired OpenBench validation and strength tests.

- 5,000 unique positions, generated at 8 through 16 plies.
- Generator: Terachess-Stockfish with the released net-2 bytes.
- Search: 5,000 nodes, MultiPV 6, fixed seed 2026.
- Candidate moves: no more than 150 internal centipawns below the top line.
- Final-position filter: absolute evaluation no greater than 800 internal
  centipawns.
- 12 candidate trajectories were discarded.
- Payload size: 911,542 bytes.
- Payload SHA-256:
  `1f117b0ed03049afad62481494fff9e3232774d188433a99ffff1454d84babe7`.
- Archive member: `TERACHESS_openings_v1.epd`.
- Archive size: 132,952 bytes.
- Archive SHA-256:
  `87ed4fba357de4020e42e711c16e9f9a08ec0d6eac12851f224699aafa2cb256`.

Both independent Terachess rule implementations parsed and round-tripped all
5,000 positions, agreed on every complete legal move set, and found zero
terminal positions or duplicate FENs. The opening-ply range was exactly 8-16.

The artifact supplies 5,000 opening pairs, or 10,000 games, before its first
wrap. Registration makes it available for official validation workloads; a
runner/time smoke remains mandatory before it is authorized for a strength
SPRT. Complete machine-readable provenance is in
`TERACHESS_openings_v1.RECEIPT.json`.
