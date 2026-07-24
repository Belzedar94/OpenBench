# Atomic opening catalogue

`atomic_openings_v1.json` is a deterministic, static catalogue of factual
Atomic opening name/position associations. It is deliberately not stored in a
Django model: serving a name is a read-only position-key lookup and must not
add load or migrations to AtomicDB.

The committed artifact contains:

- every legal Encyclopedia of Atomic Openings (EAO) record;
- 23 separately audited modern canonical names and community aliases,
  including the named The House theory index and dedicated studies; and
- when supplied to the compiler, ATOMIX names as lower-priority
  `legacy_alias` evidence.

Display precedence is:

`canonical` > `canonical_candidate` > `community_alias` > EAO `legacy` >
ATOMIX `legacy_alias`.

Lower-priority names and every factual evidence URL/label/hash remain attached
to the position. Source commentary is not copied.

Build from audited source artifacts:

```powershell
python Scripts\build_atomic_openings_catalog.py `
  --eao <eao-openings.json> `
  --modern <modern-confirmed-openings.json> `
  --atomix <atomix-legacy-aliases.json> `
  --check
```

Remove `--check` only when intentionally rebuilding the committed artifact.
An unreviewed equal-precedence naming conflict fails the build; the three
known historical conflicts have explicit primary-record overrides in
`atomicdb.openings`, while all other names remain visible as aliases.

Validate the committed catalogue by replaying every line under PyFFish Atomic
rules and recalculating `atomicdb.logic.key_of()`:

```powershell
python manage.py validate_atomic_openings
```

Use `--no-deep` only for a fast structural/digest check. Runtime loading always
checks schema, digest, audited baseline counts, position identities, aliases,
and display precedence; it fails closed on drift.
