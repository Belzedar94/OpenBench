# HORDE_openings_v3.epd

This archive contains the current Horde opening book for paired OpenBench
strength tests. Its selection objective is paired assignment sensitivity, not
an artificial 50/50 White/Black game score.

- Generation campaign: 128 deterministic shards of 256 records, for 32,768
  generated records.
- Source engine SHA-256:
  `f276f1abb1dfbb491986cc256d8476cc19eeff2145e472eba1543ca7cf5f7dd1`.
- Run 6B network SHA-256:
  `b71108587968ac544eb2e62c2333feca880da5aca52866787f1402163444adf7`.
- Merged pool: 17,784 unique positions, split 8,892/8,892 by side to move.
- Deep screen: 20,000 nodes, MultiPV 2, one thread, and 16 MiB hash.
- Selection: exact root gap zero and a White-relative evaluation from +80
  through +200 centipawns, inclusive.
- Final size: 1,500 unique positions, split 750/750 by side to move.
- Prefix families: 49, with at most 75 records in one family.
- Payload size: 117,653 bytes.
- Payload SHA-256:
  `e09630e8a0a7d28028e2b02131e82a176dd2d8b18634d671f93e230f9d14f304`.
- Archive member: `HORDE_openings_v3.epd`.
- Archive size: 21,568 bytes.
- Archive SHA-256:
  `050ce8da10907d59cee8fcc879f692e2a1a8daf66d85fc31d2ae68ac1f020b4c`.

The final artifact was checked on 80 games from 40 complete, unique pairs with
the same engine and network at 50,000 nodes versus 40,000 nodes. It produced
pentanomial `[9, 1, 24, 1, 5]`, a 40.0% assignment-decisive rate,
`U = 0.3625`, and a 55.0% Black-Black rate. All 80 games terminated normally.

For comparison, the interim book produced `[5, 0, 27, 3, 5]`, a 32.5%
assignment-decisive rate, and `U = 0.26875` under the same probe. The sample is
an engineering validation rather than a precise population estimate, but it
supports replacing the interim artifact without claiming forced color balance.

This book supplies 1,500 pairs, or 3,000 games, before its first wrap. The
complete machine-readable provenance and validation receipt is in
`HORDE_openings_v3.RECEIPT.json`. The archive is distributed under GPL-3.0
with this repository.
