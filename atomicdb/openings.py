"""Versioned, position-based Atomic opening catalogue.

The source material is compiled into ``data/atomic_openings_v1.json``.  Runtime
lookups never depend on SAN spelling or a move-prefix: an opening is keyed by
AtomicDB's canonical position identity.  Consequently transpositions work
without special cases.  :func:`match_line` validates the complete route and
keeps the last explicitly catalogued name while the played line continues
through unnamed positions, matching the opening-name behaviour visitors expect
from chess explorers.

The compiler intentionally discards source commentary.  It keeps only factual
name/line associations and their minimal provenance (labels, URLs and hashes).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Mapping, Sequence

from atomicdb import logic


CATALOG_SCHEMA = 'atomicdb-opening-catalog-v1'
SOURCE_SCHEMA = 'atomic-openings-corpus-v1'
CATALOG_PATH = Path(__file__).with_name('data') / 'atomic_openings_v1.json'

# A modern name wins display precedence, but lower-priority records remain
# attached to the position as aliases and provenance.
STATUS_RANK = {
    'canonical': 4,
    'canonical_candidate': 3,
    'community_alias': 2,
    'legacy': 1,
    'legacy_alias': 0,
}
SOURCE_RANK = {'modern': 2, 'eao': 1, 'atomix': 0}
_SHA256_RE = re.compile(r'^[0-9a-f]{64}$')

# A precedence tie is never broken merely by lexicographic record ID.  The
# audited corpus contains three intentional same-position naming conflicts;
# these explicit choices determine only the display name.  Every other name
# remains attached as an alias with its provenance.
PRIMARY_RECORD_OVERRIDES = frozenset({
    'atomix-0127',                  # Center Attack
    'theory-two-knights-opening',   # over Two Knights Attack
    'theory-atomic-defence',        # over ZA
})

# Audited corpus floor.  These are exact, reviewed source counts rather than
# loose minimums: a source file cannot silently shrink, grow, or be swapped
# while retaining a freshly recomputed outer digest.
AUDITED_EAO_RECORDS = 208
AUDITED_EAO_UNIQUE_NAMES = 191
AUDITED_MODERN_RECORDS = 31
AUDITED_ATOMIX_RECORDS = 176
AUDITED_FULL_RECORDS = 415
AUDITED_FULL_POSITIONS = 356
AUDITED_MODERN_EAO_POSITIONS = 220

# Modern claims are deliberately pinned by source ID, exact Atomic position,
# and spelling.  Counts alone would let a same-sized orthodox opening import
# replace an audited Atomic association after recomputing the outer digest.
# Adding or renaming a modern record therefore requires an explicit code review
# of this allowlist as well as rebuilding the static artifact.
AUDITED_MODERN_IDENTITIES = frozenset({
    ('community-yokke', 'Yokke',
     '0ace7dd665cd3b49695dca7594510454acdb07134808e3c1afd6efc7d2224053'),
    ('community-wayward-queen', 'Wayward Queen',
     '140d50ef40a7c993ca386f1fc51afb0db9b9f0dfdd81449428c317f985eb1d3d'),
    ('theory-swapped-atomic-attack', 'Swapped Atomic Attack',
     '1ee388b727361c31c9dfef9e294ceae0fafa78f7ebcd3fb70ba5c26d33dd4cc0'),
    ('theory-vlasov-defence', 'Vlasov Defence',
     '25b5e56f91f8088692494649ef41e4f75e4e9c53c2252c6cd83f9663efcf8edc'),
    ('theory-classical-defence', 'Classical Defence',
     '25e93feb7e279d38f92407f9636dc1ad7f443e6e9a4f3c57d3cc289063f683d3'),
    ('community-trojanknight-opening', 'TrojanKnight Opening',
     '269150855fc38863f2c3e0e4ba057f2fa36c62d8d2650d5f7cd7365431d17d1c'),
    ('community-fake-cowboy-attack', 'Fake Cowboy Attack',
     '28b162c33ae8dde0adba14fa85dec5bbf6c01bcdd0e8d341900acccea0933a52'),
    ('theory-boat', 'Boat',
     '2eab1cfcdaa0e2cc50d8677b512fad27da05b0237c0f56463b880a5c1a447e82'),
    ('community-fantasy-knight', 'Fantasy Knight',
     '3588797ac5ca94f609d2f44799454bbb539cb57970bdf95229b61523a9410b94'),
    ('community-russian-attack', 'Russian Attack',
     '4e1fd5ca4be4c79eacb1a0782c24dd381ce0bec1237ba4f55e83bfce629d7655'),
    ('theory-reversed-boat', 'Reversed Boat',
     '5752b088578f963daa44e340afa1eb864cbc3d51ad92e9d1ff48f7545eb3bdaa'),
    ('community-cowboy-attack', 'Cowboy Attack',
     '5c194dfd93bba4fc772fb8ff7dc8e083cc730ceb46e24adbab7586dd682b40b9'),
    ('community-trk-spam', 'TRK Spam',
     '725ff5ff8cc3c35198f7350e8c318a83b04adfdc79701ba7dd40d689f69f53de'),
    ('community-mahiru', 'Mahiru',
     '755a6921f95ca306faa18c13533e9abbd9f06bb128c0df05a6f5d0837b07e5f0'),
    ('theory-apostador-defence', 'Apostador Defence',
     '75adcc2254ca2d9ad38d5ec1d1664c8e796523ac4b965d6fd5dc847bb4452fb1'),
    ('theory-two-pawns-defence', 'Two Pawns Defence',
     '7e11bbf6837e9b79f12a43e1f4947c4434541fd4d5b20a030fd03b8eb8c73ce9'),
    ('community-caveman-attack', 'Caveman Attack',
     '85bd447e0fdf1f2ceb93ce6a6b7b9cdbe4c51fe3aac602da967ca5944e9dd6cc'),
    ('community-xeransis-attack', 'Xeransis Attack',
     '92e9991a097f12893d60cc51e300ec1e188c6e2291cff4bf974281da6a0cd132'),
    ('theory-two-knights-opening', 'Two Knights Opening',
     '973d2a56a0c497e578aaf388fbc56c9104860eea0f0914bcc1957946cfc118a5'),
    ('community-two-knights-attack', 'Two Knights Attack',
     '973d2a56a0c497e578aaf388fbc56c9104860eea0f0914bcc1957946cfc118a5'),
    ('community-midnight', 'Midnight',
     '98fe63ed037a3692560832bece250a21cb57fa336d93e87a58bc4e7e789aef64'),
    ('theory-atomic-defence', 'Atomic Defence',
     'a1b0313caecdb14bca570efbab75fd0fdc70109aed6f77e2c9ac462e2c938c46'),
    ('community-za-d5', 'ZA',
     'a1b0313caecdb14bca570efbab75fd0fdc70109aed6f77e2c9ac462e2c938c46'),
    ('community-russian-attack-h3', 'Russian Attack',
     'a2dedf6617a90613d90e0e9545cea63c7221a99caee3f91b08e33dd2d4fe4634'),
    ('community-villager-defense', 'Villager Defense',
     'aa863834e8d555ba990fee76149fac3c2e2897564d94540db385cfe9e56d51d8'),
    ('theory-atomic-attack', 'Atomic Attack',
     'b3de0f0a5f0c458e42155134d98ae7e69f6c219e8e9a8e1431ee816f1a3a51c8'),
    ('theory-tipau-siggemannen-counterattack',
     'Tipau-Siggemannen Counterattack',
     'b76291a93e0620f34e73747e258c4774b958e3989e8fb4e27a6e7fbfc7fcfb01'),
    ('community-chronatog-scambit', 'Chronatog Scambit',
     'c55b8a57414a0332debd00ff972208de33684ad940e874b17f0774990866b6e4'),
    ('community-chronatog-gambit', 'Chronatog Gambit',
     'c55b8a57414a0332debd00ff972208de33684ad940e874b17f0774990866b6e4'),
    ('community-trojanknight-modern-defence',
     'TrojanKnight Opening: Modern Defence',
     'cc3652d8b5a97a140df33778b0b697fa8f63be323ac920c49a7ffbe7889cc682'),
    ('community-right-horse', 'Right Horse',
     'f26f35026eb72339c1557d077d5d00cfdac1fbd3fb832eccb1621605f6df96b2'),
})

_ROOT_FIELDS = frozenset({
    'schema', 'ruleset', 'sources', 'counts', 'entries', 'catalog_sha256',
})
_ENTRY_FIELDS = frozenset({
    'position_key', 'fen', 'name', 'status', 'confidence', 'names',
    'aliases', 'reference_line_uci', 'reference_line_san', 'records',
})
_RECORD_FIELDS = frozenset({
    'id', 'source_kind', 'name', 'status', 'confidence', 'code',
    'line_uci', 'line_san', 'source_line', 'evidence', 'issues', 'provenance',
})
_EVIDENCE_FIELDS = frozenset({'kind', 'label', 'url', 'sha256'})
_SOURCE_DESCRIPTOR_REQUIRED_FIELDS = frozenset({
    'kind', 'filename', 'sha256', 'records',
})
_SOURCE_DESCRIPTOR_FIELDS = frozenset({
    *_SOURCE_DESCRIPTOR_REQUIRED_FIELDS, 'source',
})
_PROVENANCE_FIELDS = frozenset({
    'source_line_number', 'source_row', 'same_atomix_position',
    'same_eao_position',
})


class OpeningCatalogError(ValueError):
    """The catalogue or one of its source records violated its contract."""


class InvalidOpeningLine(OpeningCatalogError):
    """A requested UCI line is not legal under PyFFish Atomic rules."""


@dataclass(frozen=True)
class OpeningEvidence:
    kind: str
    label: str
    url: str
    sha256: str | None = None

    def as_dict(self) -> dict[str, str]:
        result = {
            'kind': self.kind,
            'label': self.label,
            'url': self.url,
        }
        if self.sha256 is not None:
            result['sha256'] = self.sha256
        return result


@dataclass(frozen=True)
class OpeningSourceRecord:
    id: str
    source_kind: str
    name: str
    status: str
    confidence: str
    code: str | None
    line_uci: tuple[str, ...]
    line_san: str
    source_line: str | None
    evidence: tuple[OpeningEvidence, ...]
    issues: tuple[str, ...]
    provenance: Mapping[str, object]

    def as_dict(self) -> dict[str, object]:
        return {
            'id': self.id,
            'source_kind': self.source_kind,
            'name': self.name,
            'status': self.status,
            'confidence': self.confidence,
            'code': self.code,
            'line_uci': list(self.line_uci),
            'line_san': self.line_san,
            'source_line': self.source_line,
            'evidence': [item.as_dict() for item in self.evidence],
            'issues': list(self.issues),
            'provenance': dict(self.provenance),
        }


@dataclass(frozen=True)
class Opening:
    position_key: str
    fen: str
    name: str
    status: str
    confidence: str
    names: tuple[str, ...]
    aliases: tuple[str, ...]
    reference_line_uci: tuple[str, ...]
    reference_line_san: str
    records: tuple[OpeningSourceRecord, ...]

    @property
    def ply(self) -> int:
        return len(self.reference_line_uci)

    def as_dict(self) -> dict[str, object]:
        sources = [record.as_dict() for record in self.records]
        evidence = []
        seen_evidence = set()
        for record in self.records:
            for item in record.evidence:
                identity = (item.kind, item.label, item.url, item.sha256)
                if identity in seen_evidence:
                    continue
                seen_evidence.add(identity)
                evidence.append(item.as_dict())
        return {
            'position_key': self.position_key,
            'fen': self.fen,
            'name': self.name,
            'status': self.status,
            'confidence': self.confidence,
            'names': list(self.names),
            'aliases': list(self.aliases),
            'reference_line_uci': list(self.reference_line_uci),
            'reference_line_san': self.reference_line_san,
            'ply': self.ply,
            'sources': sources,
            'evidence': evidence,
        }


@dataclass(frozen=True)
class OpeningLineMatch:
    """The last named Atomic position reached while replaying a legal line."""

    opening: Opening
    matched_ply: int
    current_key: str

    @property
    def is_current_position(self) -> bool:
        return self.opening.position_key == self.current_key

    def as_dict(self) -> dict[str, object]:
        result = self.opening.as_dict()
        result.update({
            'matched_ply': self.matched_ply,
            'current_key': self.current_key,
            'exact': self.is_current_position,
        })
        return result


def _canonical_json(payload: object) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
    ).encode('utf-8')


def _catalog_digest(payload: Mapping[str, object]) -> str:
    unsigned = dict(payload)
    unsigned.pop('catalog_sha256', None)
    return hashlib.sha256(_canonical_json(unsigned)).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _require_exact_fields(
    value: Mapping[str, object], expected: frozenset[str], context: str,
) -> None:
    actual = frozenset(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise OpeningCatalogError(
            f'{context} fields differ: missing={missing} extra={extra}')


def _require_string(value: object, context: str, *, allow_empty=False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise OpeningCatalogError(f'{context} must be a non-empty string')
    return value


def _source_priority(
    record: Mapping[str, object],
) -> tuple[int, int, int, str]:
    status = str(record['status'])
    source_kind = str(record['source_kind'])
    return (
        STATUS_RANK.get(status, -1),
        SOURCE_RANK.get(source_kind, -1),
        int(str(record['id']) in PRIMARY_RECORD_OVERRIDES),
        str(record['id']),
    )


def _require_resolved_primary(
    records: Sequence[Mapping[str, object]], context: str,
) -> None:
    """Reject unreviewed display-name ties at the highest precedence."""
    best_rank = max(
        (
            STATUS_RANK.get(str(record['status']), -1),
            SOURCE_RANK.get(str(record['source_kind']), -1),
        )
        for record in records
    )
    contenders = [
        record for record in records
        if (
            STATUS_RANK.get(str(record['status']), -1),
            SOURCE_RANK.get(str(record['source_kind']), -1),
        ) == best_rank
    ]
    names = {str(record['name']) for record in contenders}
    if len(names) <= 1:
        return
    preferred = [
        record for record in contenders
        if str(record['id']) in PRIMARY_RECORD_OVERRIDES
    ]
    if len(preferred) != 1:
        raise OpeningCatalogError(
            f'{context} has unresolved equal-precedence opening names: '
            f'{sorted(names)}')


def _normalise_evidence(value: object, context: str) -> list[dict[str, str]]:
    if not isinstance(value, list) or not value:
        raise OpeningCatalogError(f'{context} must be a non-empty list')
    result = []
    for index, raw in enumerate(value):
        if not isinstance(raw, dict):
            raise OpeningCatalogError(f'{context}[{index}] must be an object')
        unknown = set(raw) - _EVIDENCE_FIELDS
        if unknown:
            raise OpeningCatalogError(
                f'{context}[{index}] has non-factual fields: {sorted(unknown)}')
        evidence = {
            'kind': _require_string(
                raw.get('kind'), f'{context}[{index}].kind'),
            'label': _require_string(
                raw.get('label'), f'{context}[{index}].label'),
            'url': _require_string(
                raw.get('url'), f'{context}[{index}].url'),
        }
        if raw.get('sha256') is not None:
            sha256 = _require_string(
                raw.get('sha256'), f'{context}[{index}].sha256')
            if not _SHA256_RE.fullmatch(sha256):
                raise OpeningCatalogError(
                    f'{context}[{index}].sha256 is not SHA-256')
            evidence['sha256'] = sha256
        result.append(evidence)
    return result


def _normalise_record(
    raw: Mapping[str, object], source_kind: str, context: str,
) -> dict[str, object]:
    record_id = _require_string(raw.get('id'), f'{context}.id')
    name = _require_string(raw.get('name'), f'{context}.name')
    status = _require_string(raw.get('status'), f'{context}.status')
    if status not in STATUS_RANK:
        raise OpeningCatalogError(f'{context}.status is unsupported: {status}')
    confidence = _require_string(
        raw.get('confidence'), f'{context}.confidence')
    moves = raw.get('line_uci')
    if not isinstance(moves, list) or not moves:
        raise OpeningCatalogError(f'{context}.line_uci must be non-empty')
    line_uci = [
        _require_string(move, f'{context}.line_uci[{index}]')
        for index, move in enumerate(moves)
    ]
    line_san = _require_string(raw.get('line_san'), f'{context}.line_san')
    result: dict[str, object] = {
        'id': record_id,
        'source_kind': source_kind,
        'name': name,
        'status': status,
        'confidence': confidence,
        'code': (
            _require_string(raw.get('code'), f'{context}.code')
            if raw.get('code') is not None else None
        ),
        'line_uci': line_uci,
        'line_san': line_san,
        'source_line': (
            _require_string(raw.get('source_line'), f'{context}.source_line')
            if raw.get('source_line') is not None else None
        ),
        'evidence': _normalise_evidence(
            raw.get('evidence'), f'{context}.evidence'),
        'issues': [],
        'provenance': {},
    }
    issues = raw.get('issues', [])
    if not isinstance(issues, list):
        raise OpeningCatalogError(f'{context}.issues must be a list')
    result['issues'] = [
        _require_string(issue, f'{context}.issues[{index}]')
        for index, issue in enumerate(issues)
    ]
    provenance = {
        key: raw[key] for key in _PROVENANCE_FIELDS if key in raw
    }
    try:
        # Reject custom Python values and detach from the source object while
        # retaining the audited JSON facts exactly.
        result['provenance'] = json.loads(json.dumps(provenance))
    except (TypeError, ValueError) as exc:
        raise OpeningCatalogError(
            f'{context}.provenance is not JSON-compatible') from exc
    return result


def _replay(
    moves: Sequence[str],
    prefix_cache: dict[tuple[str, ...], str] | None = None,
) -> str:
    """Replay a line once per unique prefix and return canonical terminal FEN."""
    cache = prefix_cache if prefix_cache is not None else {}
    cache.setdefault((), logic.start_fen())
    prefix: tuple[str, ...] = ()
    for ply, move in enumerate(moves, start=1):
        child_prefix = prefix + (move,)
        if child_prefix not in cache:
            fen = cache[prefix]
            legal = logic.legal_moves(fen)
            if move not in legal:
                raise InvalidOpeningLine(
                    f'illegal Atomic move at ply {ply}: {move} in {fen}')
            cache[child_prefix] = logic.apply_move(fen, move)
        prefix = child_prefix
    return cache[prefix]


def _source_descriptor(
    path: Path, source_kind: str, document: Mapping[str, object],
) -> dict[str, object]:
    descriptor: dict[str, object] = {
        'kind': source_kind,
        'filename': path.name,
        'sha256': _file_sha256(path),
        'records': len(document['openings']),
    }
    source = document.get('source')
    if source is not None:
        if not isinstance(source, dict):
            raise OpeningCatalogError(f'{path.name}.source must be an object')
        allowed = {
            'name', 'url', 'retrieved_via', 'sha256', 'license',
            'authors', 'version',
        }
        unknown = set(source) - allowed
        if unknown:
            raise OpeningCatalogError(
                f'{path.name}.source has unsupported fields: {sorted(unknown)}')
        normalised_source = {}
        for key, value in source.items():
            if key == 'authors':
                if not isinstance(value, list) or not value:
                    raise OpeningCatalogError(
                        f'{path.name}.source.authors must be a non-empty list')
                normalised_source[key] = [
                    _require_string(
                        author, f'{path.name}.source.authors[{index}]')
                    for index, author in enumerate(value)
                ]
            else:
                normalised_source[key] = _require_string(
                    value, f'{path.name}.source.{key}')
        descriptor['source'] = normalised_source
    return descriptor


def build_catalog(
    source_paths: Sequence[tuple[str, str | Path]],
) -> dict[str, object]:
    """Compile audited source JSON documents into one deterministic catalogue.

    ``source_paths`` contains ``(source_kind, path)`` pairs.  EAO and modern
    sources are mandatory.  ATOMIX is optional and is accepted only as
    ``legacy_alias`` evidence; it can name an otherwise unnamed position but
    can never displace an EAO or modern display name.
    """
    kinds = [kind for kind, _ in source_paths]
    if kinds.count('eao') != 1 or kinds.count('modern') != 1:
        raise OpeningCatalogError('exactly one eao and one modern source required')
    if len(set(kinds)) != len(kinds):
        raise OpeningCatalogError('source kinds must be unique')
    if set(kinds) - set(SOURCE_RANK):
        raise OpeningCatalogError(
            f'unsupported source kinds: {sorted(set(kinds) - set(SOURCE_RANK))}')

    prefix_cache: dict[tuple[str, ...], str] = {}
    records_by_key: dict[str, list[dict[str, object]]] = {}
    descriptors = []
    record_ids = set()

    for source_kind, raw_path in source_paths:
        path = Path(raw_path)
        try:
            document = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise OpeningCatalogError(f'cannot read {path}: {exc}') from exc
        if not isinstance(document, dict):
            raise OpeningCatalogError(f'{path.name} must contain an object')
        if document.get('schema') != SOURCE_SCHEMA:
            raise OpeningCatalogError(
                f'{path.name} schema must be {SOURCE_SCHEMA!r}')
        if not isinstance(document.get('openings'), list):
            raise OpeningCatalogError(f'{path.name}.openings must be a list')
        descriptors.append(_source_descriptor(path, source_kind, document))

        for index, raw in enumerate(document['openings']):
            if not isinstance(raw, dict):
                raise OpeningCatalogError(
                    f'{path.name}.openings[{index}] must be an object')
            record = _normalise_record(
                raw, source_kind, f'{path.name}.openings[{index}]')
            if record['id'] in record_ids:
                raise OpeningCatalogError(
                    f"duplicate source record id: {record['id']}")
            record_ids.add(record['id'])
            if source_kind == 'atomix':
                record['status'] = 'legacy_alias'

            fen = _replay(record['line_uci'], prefix_cache)
            key = logic.key_of(fen)
            records_by_key.setdefault(key, []).append(record)

    entries = []
    for key, records in records_by_key.items():
        _require_resolved_primary(records, f'position {key}')
        ordered = sorted(records, key=_source_priority, reverse=True)
        primary = ordered[0]
        names = list(dict.fromkeys(str(record['name']) for record in ordered))
        entries.append({
            'position_key': key,
            'fen': _replay(primary['line_uci'], prefix_cache),
            'name': primary['name'],
            'status': primary['status'],
            'confidence': primary['confidence'],
            'names': names,
            'aliases': [name for name in names if name != primary['name']],
            'reference_line_uci': primary['line_uci'],
            'reference_line_san': primary['line_san'],
            'records': ordered,
        })
    entries.sort(key=lambda entry: entry['position_key'])
    descriptors.sort(key=lambda source: SOURCE_RANK[source['kind']], reverse=True)

    all_records = [
        record for entry in entries for record in entry['records']
    ]
    eao_names = {
        record['name'] for record in all_records
        if record['source_kind'] == 'eao'
    }
    counts = {
        'positions': len(entries),
        'source_records': len(all_records),
        'unique_names': len({record['name'] for record in all_records}),
        'eao_records': sum(
            record['source_kind'] == 'eao' for record in all_records),
        'eao_unique_exact_names': len(eao_names),
        'modern_records': sum(
            record['source_kind'] == 'modern' for record in all_records),
        'atomix_records': sum(
            record['source_kind'] == 'atomix' for record in all_records),
    }
    payload: dict[str, object] = {
        'schema': CATALOG_SCHEMA,
        'ruleset': {
            'variant': logic.VARIANT,
            'implementation': 'pyffish',
            'position_identity': 'atomicdb.logic.key_of(canonical PyFFish FEN)',
        },
        'sources': descriptors,
        'counts': counts,
        'entries': entries,
    }
    payload['catalog_sha256'] = _catalog_digest(payload)
    validate_catalog(payload, deep=False, require_atomix='atomix' in kinds)
    return payload


def write_catalog(
    source_paths: Sequence[tuple[str, str | Path]],
    output: str | Path = CATALOG_PATH,
) -> dict[str, object]:
    payload = build_catalog(source_paths)
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, indent=2,
    ) + '\n'
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(encoded, encoding='utf-8', newline='\n')
    temporary.replace(path)
    return payload


def validate_catalog(
    payload: Mapping[str, object], *, deep: bool, require_atomix: bool = False,
) -> dict[str, int | str]:
    """Validate schema, precedence, counts and optionally every Atomic line."""
    if not isinstance(payload, dict):
        raise OpeningCatalogError('catalogue root must be an object')
    _require_exact_fields(payload, _ROOT_FIELDS, 'catalogue')
    if payload['schema'] != CATALOG_SCHEMA:
        raise OpeningCatalogError(f'unsupported schema: {payload["schema"]!r}')
    digest = _require_string(payload['catalog_sha256'], 'catalog_sha256')
    if not _SHA256_RE.fullmatch(digest):
        raise OpeningCatalogError('catalog_sha256 is not SHA-256')
    actual_digest = _catalog_digest(payload)
    if digest != actual_digest:
        raise OpeningCatalogError(
            f'catalog digest mismatch: recorded={digest} actual={actual_digest}')

    ruleset = payload['ruleset']
    if not isinstance(ruleset, dict) or ruleset != {
        'variant': 'atomic',
        'implementation': 'pyffish',
        'position_identity': 'atomicdb.logic.key_of(canonical PyFFish FEN)',
    }:
        raise OpeningCatalogError('catalogue has an unsupported ruleset identity')
    if not isinstance(payload['sources'], list):
        raise OpeningCatalogError('sources must be a list')
    source_kinds = []
    for index, source in enumerate(payload['sources']):
        if not isinstance(source, dict):
            raise OpeningCatalogError(f'sources[{index}] must be an object')
        missing = _SOURCE_DESCRIPTOR_REQUIRED_FIELDS - set(source)
        extra = set(source) - _SOURCE_DESCRIPTOR_FIELDS
        if missing or extra:
            raise OpeningCatalogError(
                f'sources[{index}] fields differ: '
                f'missing={sorted(missing)} extra={sorted(extra)}')
        _require_string(source['filename'], f'sources[{index}].filename')
        source_sha = _require_string(
            source['sha256'], f'sources[{index}].sha256')
        if not _SHA256_RE.fullmatch(source_sha):
            raise OpeningCatalogError(
                f'sources[{index}].sha256 is not SHA-256')
        if not isinstance(source['records'], int) or source['records'] <= 0:
            raise OpeningCatalogError(
                f'sources[{index}].records must be a positive integer')
        nested_source = source.get('source')
        if nested_source is not None and not isinstance(nested_source, dict):
            raise OpeningCatalogError(
                f'sources[{index}].source must be an object')
        if isinstance(nested_source, dict) \
                and nested_source.get('sha256') is not None \
                and not _SHA256_RE.fullmatch(str(nested_source['sha256'])):
            raise OpeningCatalogError(
                f'sources[{index}].source.sha256 is not SHA-256')
        source_kinds.append(source.get('kind'))
    if source_kinds.count('eao') != 1 or source_kinds.count('modern') != 1:
        raise OpeningCatalogError('catalogue must contain EAO and modern sources')
    if len(source_kinds) != len(set(source_kinds)):
        raise OpeningCatalogError('catalogue source kinds must be unique')
    if set(source_kinds) - set(SOURCE_RANK):
        raise OpeningCatalogError('catalogue contains an unknown source kind')
    if require_atomix and 'atomix' not in source_kinds:
        raise OpeningCatalogError('committed catalogue must contain ATOMIX aliases')

    entries = payload['entries']
    counts = payload['counts']
    if not isinstance(entries, list) or not isinstance(counts, dict):
        raise OpeningCatalogError('entries/counts have invalid types')
    keys = set()
    record_ids = set()
    source_records = []
    prefix_cache: dict[tuple[str, ...], str] = {}

    for entry_index, entry in enumerate(entries):
        context = f'entries[{entry_index}]'
        if not isinstance(entry, dict):
            raise OpeningCatalogError(f'{context} must be an object')
        _require_exact_fields(entry, _ENTRY_FIELDS, context)
        key = _require_string(entry['position_key'], f'{context}.position_key')
        if not _SHA256_RE.fullmatch(key):
            raise OpeningCatalogError(f'{context}.position_key is not SHA-256')
        if key in keys:
            raise OpeningCatalogError(f'duplicate position key: {key}')
        keys.add(key)
        fen = _require_string(entry['fen'], f'{context}.fen')
        if logic.key_of(fen) != key:
            raise OpeningCatalogError(f'{context}.fen does not hash to its key')
        records = entry['records']
        if not isinstance(records, list) or not records:
            raise OpeningCatalogError(f'{context}.records must be non-empty')
        for record_index, record in enumerate(records):
            record_context = f'{context}.records[{record_index}]'
            if not isinstance(record, dict):
                raise OpeningCatalogError(f'{record_context} must be an object')
            _require_exact_fields(record, _RECORD_FIELDS, record_context)
            record_id = _require_string(
                record['id'], f'{record_context}.id')
            if record_id in record_ids:
                raise OpeningCatalogError(
                    f'duplicate source record id: {record_id}')
            record_ids.add(record_id)
            if record['source_kind'] not in SOURCE_RANK:
                raise OpeningCatalogError(
                    f'{record_context}.source_kind is unsupported')
            if record['status'] not in STATUS_RANK:
                raise OpeningCatalogError(
                    f'{record_context}.status is unsupported')
            if record['source_kind'] == 'atomix' \
                    and record['status'] != 'legacy_alias':
                raise OpeningCatalogError(
                    f'{record_context}: ATOMIX must remain legacy_alias')
            _require_string(record['name'], f'{record_context}.name')
            if not isinstance(record['line_uci'], list) \
                    or not record['line_uci']:
                raise OpeningCatalogError(
                    f'{record_context}.line_uci must be non-empty')
            _normalise_evidence(
                record['evidence'], f'{record_context}.evidence')
            provenance = record['provenance']
            if not isinstance(provenance, dict) \
                    or set(provenance) - _PROVENANCE_FIELDS:
                raise OpeningCatalogError(
                    f'{record_context}.provenance has unsupported fields')
            if deep:
                replayed_fen = _replay(record['line_uci'], prefix_cache)
                if logic.key_of(replayed_fen) != key:
                    raise OpeningCatalogError(
                        f'{record_context} replays to a different key')
            source_records.append(record)

        _require_resolved_primary(records, context)
        expected_records = sorted(
            records, key=_source_priority, reverse=True)
        if records != expected_records:
            raise OpeningCatalogError(
                f'{context}.records are not in precedence order')
        primary = records[0]
        expected_names = list(dict.fromkeys(
            str(record['name']) for record in records))
        if entry['name'] != primary['name'] \
                or entry['status'] != primary['status'] \
                or entry['confidence'] != primary['confidence']:
            raise OpeningCatalogError(
                f'{context} display fields do not follow precedence')
        if entry['names'] != expected_names:
            raise OpeningCatalogError(f'{context}.names lost a source name')
        if entry['aliases'] != [
            name for name in expected_names if name != primary['name']
        ]:
            raise OpeningCatalogError(f'{context}.aliases are inconsistent')
        if entry['reference_line_uci'] != primary['line_uci'] \
                or entry['reference_line_san'] != primary['line_san']:
            raise OpeningCatalogError(
                f'{context} reference line is not the primary record line')

    expected_counts = {
        'positions': len(entries),
        'source_records': len(source_records),
        'unique_names': len({
            record['name'] for record in source_records}),
        'eao_records': sum(
            record['source_kind'] == 'eao' for record in source_records),
        'eao_unique_exact_names': len({
            record['name'] for record in source_records
            if record['source_kind'] == 'eao'
        }),
        'modern_records': sum(
            record['source_kind'] == 'modern' for record in source_records),
        'atomix_records': sum(
            record['source_kind'] == 'atomix' for record in source_records),
    }
    if counts != expected_counts:
        raise OpeningCatalogError(
            f'catalog counts differ: recorded={counts} actual={expected_counts}')
    modern_identities = frozenset(
        (
            str(record['id']),
            str(record['name']),
            str(entry['position_key']),
        )
        for entry in entries
        for record in entry['records']
        if record['source_kind'] == 'modern'
    )
    if modern_identities != AUDITED_MODERN_IDENTITIES:
        missing = sorted(AUDITED_MODERN_IDENTITIES - modern_identities)
        extra = sorted(modern_identities - AUDITED_MODERN_IDENTITIES)
        raise OpeningCatalogError(
            'audited modern Atomic identities differ: '
            f'missing={missing} extra={extra}')
    source_record_counts = {
        kind: sum(
            record['source_kind'] == kind for record in source_records)
        for kind in source_kinds
    }
    descriptor_record_counts = {
        source['kind']: source['records'] for source in payload['sources']
    }
    if descriptor_record_counts != source_record_counts:
        raise OpeningCatalogError(
            'source descriptor record counts differ: '
            f'recorded={descriptor_record_counts} '
            f'actual={source_record_counts}')
    # The audited baseline cannot silently shrink, even if a file and its
    # embedded digest were both edited.
    baseline_ok = (
        expected_counts['eao_records'] == AUDITED_EAO_RECORDS
        and expected_counts['eao_unique_exact_names']
        == AUDITED_EAO_UNIQUE_NAMES
        and expected_counts['modern_records'] == AUDITED_MODERN_RECORDS
    )
    if 'atomix' in source_kinds:
        baseline_ok = baseline_ok and (
            expected_counts['atomix_records'] == AUDITED_ATOMIX_RECORDS
            and expected_counts['source_records'] == AUDITED_FULL_RECORDS
            and expected_counts['positions'] == AUDITED_FULL_POSITIONS
        )
    else:
        baseline_ok = baseline_ok and (
            expected_counts['atomix_records'] == 0
            and expected_counts['source_records']
            == AUDITED_EAO_RECORDS + AUDITED_MODERN_RECORDS
            and expected_counts['positions'] == AUDITED_MODERN_EAO_POSITIONS
        )
    if not baseline_ok:
        raise OpeningCatalogError(
            f'audited baseline missing or changed: {expected_counts}')

    return {
        **expected_counts,
        'catalog_sha256': digest,
    }


def _to_evidence(raw: Mapping[str, object]) -> OpeningEvidence:
    return OpeningEvidence(
        kind=str(raw['kind']),
        label=str(raw['label']),
        url=str(raw['url']),
        sha256=str(raw['sha256']) if raw.get('sha256') else None,
    )


def _to_record(raw: Mapping[str, object]) -> OpeningSourceRecord:
    return OpeningSourceRecord(
        id=str(raw['id']),
        source_kind=str(raw['source_kind']),
        name=str(raw['name']),
        status=str(raw['status']),
        confidence=str(raw['confidence']),
        code=str(raw['code']) if raw.get('code') is not None else None,
        line_uci=tuple(str(move) for move in raw['line_uci']),
        line_san=str(raw['line_san']),
        source_line=(
            str(raw['source_line'])
            if raw.get('source_line') is not None else None
        ),
        evidence=tuple(_to_evidence(item) for item in raw['evidence']),
        issues=tuple(str(issue) for issue in raw['issues']),
        provenance=MappingProxyType(dict(raw['provenance'])),
    )


@lru_cache(maxsize=1)
def _load_catalog_document() -> Mapping[str, object]:
    try:
        payload = json.loads(CATALOG_PATH.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise OpeningCatalogError(
            f'cannot load opening catalogue {CATALOG_PATH}: {exc}') from exc
    validate_catalog(payload, deep=False, require_atomix=True)
    return MappingProxyType(payload)


def catalog_sha256() -> str:
    """Return the validated immutable catalogue identity."""
    return str(_load_catalog_document()['catalog_sha256'])


@lru_cache(maxsize=1)
def load_catalog() -> Mapping[str, Opening]:
    """Load the committed catalogue once, failing closed on any drift."""
    payload = _load_catalog_document()
    catalogue = {}
    for raw in payload['entries']:
        opening = Opening(
            position_key=str(raw['position_key']),
            fen=str(raw['fen']),
            name=str(raw['name']),
            status=str(raw['status']),
            confidence=str(raw['confidence']),
            names=tuple(str(name) for name in raw['names']),
            aliases=tuple(str(name) for name in raw['aliases']),
            reference_line_uci=tuple(
                str(move) for move in raw['reference_line_uci']),
            reference_line_san=str(raw['reference_line_san']),
            records=tuple(_to_record(record) for record in raw['records']),
        )
        catalogue[opening.position_key] = opening
    return MappingProxyType(catalogue)


def clear_catalog_cache() -> None:
    load_catalog.cache_clear()
    _load_catalog_document.cache_clear()


def match_key(position_key: str) -> Opening | None:
    """Return an exact opening match for an AtomicDB position key."""
    if not isinstance(position_key, str) \
            or not _SHA256_RE.fullmatch(position_key):
        return None
    return load_catalog().get(position_key)


def lookup_key(position_key: str) -> dict[str, object] | None:
    """Serializable exact-key lookup for views and JSON payload builders."""
    opening = match_key(position_key)
    if opening is None:
        return None
    result = opening.as_dict()
    result.update({
        'matched_ply': opening.ply,
        'current_key': opening.position_key,
        'exact': True,
    })
    return result


def match_fen(fen: str) -> Opening | None:
    """Return an exact opening match for a canonicalizable Atomic FEN."""
    try:
        return match_key(logic.key_of(fen))
    except (AttributeError, IndexError, TypeError, ValueError):
        return None


def match_line_object(ucis: Iterable[str]) -> OpeningLineMatch | None:
    """Return the last named position reached by a legal UCI line.

    The match is position-based.  A transposed route therefore recognizes the
    same opening.  An unnamed continuation retains the closest named ancestor,
    and a later exact match replaces it.  Illegal moves raise
    :class:`InvalidOpeningLine` rather than producing a misleading partial
    result.
    """
    fen = logic.start_fen()
    current_key = logic.key_of(fen)
    opening = match_key(current_key)
    matched_ply = 0
    for ply, move in enumerate(ucis, start=1):
        if not isinstance(move, str) or not move:
            raise InvalidOpeningLine(f'invalid UCI token at ply {ply}: {move!r}')
        legal = logic.legal_moves(fen)
        if move not in legal:
            raise InvalidOpeningLine(
                f'illegal Atomic move at ply {ply}: {move} in {fen}')
        fen = logic.apply_move(fen, move)
        current_key = logic.key_of(fen)
        exact = match_key(current_key)
        if exact is not None:
            opening = exact
            matched_ply = ply
    if opening is None:
        return None
    return OpeningLineMatch(
        opening=opening,
        matched_ply=matched_ply,
        current_key=current_key,
    )


def match_line(ucis: Iterable[str]) -> dict[str, object] | None:
    """Serializable last-known-opening lookup for a legal played line."""
    match = match_line_object(ucis)
    return match.as_dict() if match is not None else None


def match_line_keys_object(
        position_keys: Sequence[str]) -> OpeningLineMatch | None:
    """:func:`match_line_object` for a line that was ALREADY replayed.

    The catalogue is keyed by position, so replaying a line only ever serves
    to turn it into the sequence of keys it passes through.  Callers that
    already hold that sequence — the explorer validates ``play`` move by move
    and holds a key per prefix, and the canonical lineage comes out of the
    edges with a key per step — were paying a second, identical replay for
    nothing: one move generation and one position setup per ply, which is the
    single most expensive thing the explorer did (§ logic, "cuanto cuesta una
    pregunta al movegen").

    ``position_keys[i]`` is the position after ``i`` plies, so index 0 is the
    start position and the last element is the page being rendered.  The
    answer is identical to replaying the moves because the identity of a
    position is its key and nothing else: the name of ply ``i`` comes from
    ``match_key`` either way.

    What is NOT re-checked here is legality — the caller replayed the line and
    is responsible for that.  A malformed key sequence raises
    :class:`InvalidOpeningLine` rather than quietly naming the wrong position.
    """
    keys = list(position_keys)
    if not keys or any(not isinstance(key, str)
                       or not _SHA256_RE.fullmatch(key) for key in keys):
        raise InvalidOpeningLine('line is not a sequence of position keys')
    opening = match_key(keys[0])
    matched_ply = 0
    for ply, key in enumerate(keys[1:], start=1):
        exact = match_key(key)
        if exact is not None:
            opening = exact
            matched_ply = ply
    if opening is None:
        return None
    return OpeningLineMatch(
        opening=opening,
        matched_ply=matched_ply,
        current_key=keys[-1],
    )


def match_line_keys(
        position_keys: Sequence[str]) -> dict[str, object] | None:
    """Serializable :func:`match_line` for an already-replayed line."""
    match = match_line_keys_object(position_keys)
    return match.as_dict() if match is not None else None
