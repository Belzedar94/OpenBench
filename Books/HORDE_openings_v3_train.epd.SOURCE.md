# HORDE_openings_v3_train.epd

This archive contains the training partition of `HORDE_openings_v3.epd` for
Horde NNUE data generation. It is not an independently selected opening book.

The 1,500 records in the source book were assigned by canonical position group
so that horizontally reflected positions cannot cross the train/validation
boundary. The canonical key is the lexicographically smaller of the first four
normalized FEN fields and their horizontal reflection. The first eight bytes
of its SHA-256 digest are interpreted as an unsigned big-endian integer;
residue zero modulo five is held out for validation and all other residues are
used for training.

- Source records: 1,500.
- Training records: 1,203.
- Payload size: 94,267 bytes.
- Payload SHA-256:
  `1dc2dd829962cd29fa98cafc1b0613ac2fcaaff088b65977e10aaeaf76e96a55`.
- Archive member: `HORDE_openings_v3_train.epd`.
- Archive size: 17,598 bytes.
- Archive SHA-256:
  `acfc4516726045451bac6200f70d7cc5435e34536ccd3cdad01e0a0ae665c3f4`.

The train and validation partitions are disjoint and their union is exactly
the source book. The machine-readable proof and packaging identities are in
`HORDE_openings_v3_training_split.RECEIPT.json`.

The archive is distributed under GPL-3.0 with this repository.
