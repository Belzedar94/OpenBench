# HORDE_openings_v3_validation.epd

This archive contains the held-out validation partition of
`HORDE_openings_v3.epd` for Horde NNUE data generation. It is not an
independently selected opening book.

The 1,500 records in the source book were assigned by canonical position group
so that horizontally reflected positions cannot cross the train/validation
boundary. The canonical key is the lexicographically smaller of the first four
normalized FEN fields and their horizontal reflection. The first eight bytes
of its SHA-256 digest are interpreted as an unsigned big-endian integer;
residue zero modulo five is held out for validation and all other residues are
used for training.

- Source records: 1,500.
- Validation records: 297.
- Payload size: 23,386 bytes.
- Payload SHA-256:
  `81fd618f879f04d732d462713bcb75bc6f157c9b4cd9e4d6829d3169dad0bf4a`.
- Archive member: `HORDE_openings_v3_validation.epd`.
- Archive size: 4,972 bytes.
- Archive SHA-256:
  `6f46bf5ec74cf5144deb91e31d54bce45401f7fb55ded257012924be717d4823`.

The train and validation partitions are disjoint and their union is exactly
the source book. The machine-readable proof and packaging identities are in
`HORDE_openings_v3_training_split.RECEIPT.json`.

The archive is distributed under GPL-3.0 with this repository.
