# LICHESS_HORDE_V1 referee

OpenBench client 44 uses a patched `cutechess-ob` referee for every workload
whose `variant_contract` is `LICHESS_HORDE_V1`.

The source patch is applied to the immutable
`AndyGrant/cutechess@24d4301152fb92ac442425e083a2658225f80720` base. Rule and
result semantics are pinned to
`lichess-org/scalachess@d5d47c16f65a005ca68e19bab702b02f66dd888c`.

The referee implements all Horde-specific terminal outcomes, the exact
side-specific winning-material predicate, first-rank pawn behavior, Black-only
castling, and automatic-draw precedence. The complete 21,996-row scalachess
material corpus is embedded in the patch and verified by SHA-256 before tests
run.

`run_tests.py` selects the 35 Horde-specific rule, result, and perft cases. The
remaining eight native test executables run separately. The full upstream
chessboard suite has one unrelated `grid pos2` mismatch on the pinned Windows
toolchain; the same mismatch is present in an unmodified checkout of the base
commit.

The static build scripts fail closed on source, patch, corpus, toolchain, and
linkage mismatches. Their output is uploaded for inspection and is not deployed
automatically.

Each platform artifact also contains `artifact-receipt.json`. The two receipts
must name the same workflow run, attempt, and source commit. Validate them before
installation with:

```text
python Client/referees/LICHESS_HORDE_V1/install_artifacts.py \
  --windows <windows-artifact-directory> \
  --linux <linux-artifact-directory> \
  --expected-source-commit <40-hex-commit> \
  --expected-run-id <run-id>
```

The command is check-only unless `--install` is supplied. Installation occurs
only after both binaries, checksum files, toolchain records, Horde markers, and
receipts have passed validation.
