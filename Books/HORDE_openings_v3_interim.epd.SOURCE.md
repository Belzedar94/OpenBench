# HORDE_openings_v3_interim.epd

This archive contains an immutable interim Horde opening book for paired
OpenBench strength tests. It prioritizes assignment sensitivity over an
artificially balanced White/Black game score.

- Source pool: the 5,608-record distinct-root, exact-gap-zero V2 pool.
- Source engine commit: `cee98c4d2f41295378c9cc02a9fb5153ae956d73`.
- Source-pool engine SHA-256:
  `4d2f611a859bd78cc72275f60749f1ce2a1dab16398b4a255bd47a3c4609cf76`.
- Deep-screen engine SHA-256:
  `f276f1abb1dfbb491986cc256d8476cc19eeff2145e472eba1543ca7cf5f7dd1`.
- Run 6B network SHA-256:
  `b71108587968ac544eb2e62c2333feca880da5aca52866787f1402163444adf7`.
- Selection: exact original root gap zero and a 20,000-node White-relative
  evaluation from +80 through +200 centipawns, inclusive.
- Records: 1,508 unique positions, split 754/754 by side to move.
- Prefix families: 67, with at most 76 records in one family.
- Payload size: 117,921 bytes.
- Payload SHA-256:
  `39f113beb9fda02531a614e0cd766893cb89cb0706df81c58f4a1637ee0fc814`.
- Archive member: `HORDE_openings_v3_interim.epd`.
- Archive size: 21,185 bytes.
- Archive SHA-256:
  `19bcbcdd8e99af52c9e10e4762ff2196aa555680b8524c26f3d179b059407706`.

The selection method was checked on 80 games from 40 complete, disjoint pairs.
It produced pentanomial `[5, 0, 27, 3, 5]`, a 32.5% assignment-decisive rate,
`U = 0.26875`, and a 67.5% Black-Black rate. The final payload excludes every
position observed in the audit and earlier discovery games.

This book supplies 1,508 pairs, or 3,016 games, before its first wrap. It is an
explicit short-term replacement for the low-information legacy book while a
larger V3 book is generated. It does not satisfy the 10,000-game no-wrap gate
and must not silently replace the eventual full-capacity V3 artifact.

The complete machine-readable receipt is in
`HORDE_openings_v3_interim.RECEIPT.json`. The archive is distributed under
GPL-3.0 with this repository.
