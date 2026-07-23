"""Fail-closed importer for untrusted Atomic opening-theory hints.

The importer deliberately writes only ``SchedulingCohort`` and
``CohortMembership``.  It never imports ``Position``, ``Edge`` or
``AnalysisTask`` rows and never treats a study, an evaluation or a Discord
claim as a closure.

The three manifests and exact 21-file PGN bundle are content-pinned.  Their
prose is not copied into the database: only source identifiers, public URLs,
artifact hashes and the legally regenerated root-path provenance are retained.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

import pyffish as pf
from django.conf import settings
from django.db import transaction

from atomicdb import logic


POLICY_VERSION = 'atomic-theory-shadow-v1'
MAX_MANIFEST_BYTES = 5 * 1024 * 1024
MAX_STUDY_PGN_BYTES = 20 * 1024 * 1024

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RESEARCH_ROOT = PROJECT_ROOT / 'research'
DEFAULT_STUDY_ROOT = DEFAULT_RESEARCH_ROOT / 'the-house-atomic-study-pgn'
DEFAULT_COHORT_MANIFEST = (
    DEFAULT_RESEARCH_ROOT / 'the-house-atomic-study-cohorts.json')
DEFAULT_PRIORITY_MANIFEST = (
    DEFAULT_RESEARCH_ROOT / 'the-house-atomic-priority-evidence.json')
DEFAULT_SCHEDULER_MANIFEST = (
    DEFAULT_RESEARCH_ROOT / 'the-house-atomic-scheduler-seeds.json')

DEFAULT_COHORT_MANIFEST_SHA256 = (
    '8ff04562fd5d5486b4543141a9d5083d2e660750e81f62acbc2e944c5a1ac2a4')
DEFAULT_PRIORITY_MANIFEST_SHA256 = (
    'c6deabe6abbb903bef48b124d3ea22ea24e3e07fb23707347e34fc1a03d04a2a')
DEFAULT_SCHEDULER_MANIFEST_SHA256 = (
    'fd8ef7637deb52ee8fa0a22626b0db789ee9ae97429163f8712cbcaeb0f116ef')
DEFAULT_STUDY_BUNDLE_SHA256 = (
    '83132f3d01995e4e94b98eff9340fffe7c6588e629b4c8dba4ff2d432501d776')
DEFAULT_IMPORT_BUNDLE_SHA256 = (
    'a6261fbf26b2eb4a80fac2b4ae545e16297c17db074c698d563bff7ba4790464')

_HEX64 = re.compile(r'^[0-9a-f]{64}$')
_SOURCE_ID = re.compile(r'^[A-Za-z0-9_-]{1,64}$')
_SLUG = re.compile(r'^[a-z0-9][a-z0-9-]{0,63}$')
_MOVE_NUMBER = re.compile(r'^\d+\.(?:\.\.)?')
_RESULT_TOKENS = {'1-0', '0-1', '1/2-1/2', '*'}
_EVIDENCE_LEVELS = ('E0', 'E1', 'E2', 'E3', 'E4')
_REQUIRED_COHORT_FIELDS = {
    'slug', 'label', 'root_fen', 'root_key', 'priority_level',
    'evidence_level', 'manifest_sha256', 'policy_version', 'decay_policy',
    'metadata', 'active',
}
_REQUIRED_MEMBERSHIP_FIELDS = {
    'cohort', 'position_key', 'fen', 'ply', 'role', 'source_id',
    'source_url', 'artifact_sha256', 'path_uci', 'path_sha256',
    'provenance_kind', 'metadata',
}


class TheoryImportError(ValueError):
    """Raised before any database write when source or model checks fail."""


@dataclass(frozen=True)
class PathPosition:
    ply: int
    fen: str
    position_key: str
    path_uci: str
    role: str


@dataclass(frozen=True)
class MembershipSpec:
    position_key: str
    fen: str
    ply: int
    role: str
    source_id: str
    source_url: str
    artifact_sha256: str
    path_uci: str
    path_sha256: str
    provenance_kind: str
    metadata: dict[str, Any]


@dataclass(frozen=True)
class CohortSpec:
    slug: str
    label: str
    root_fen: str
    root_key: str
    priority_level: str
    evidence_level: str
    manifest_sha256: str
    policy_version: str
    decay_policy: dict[str, Any]
    metadata: dict[str, Any]
    active: bool
    memberships: tuple[MembershipSpec, ...]


@dataclass(frozen=True)
class ImportPlan:
    cohort_manifest_sha256: str
    priority_manifest_sha256: str
    scheduler_manifest_sha256: str
    study_bundle_sha256: str
    bundle_manifest_sha256: str
    source_paths: dict[str, str]
    cohorts: tuple[CohortSpec, ...]

    @property
    def membership_count(self) -> int:
        return sum(len(cohort.memberships) for cohort in self.cohorts)

    def receipt(self, mode: str, database: dict[str, int] | None = None):
        """Return a compact receipt without copying source prose."""
        return {
            'schema': 'atomic-theory-shadow-import-receipt-v1',
            'generated_at_utc': datetime.now(timezone.utc).isoformat(),
            'mode': mode,
            'policy_version': POLICY_VERSION,
            'source_manifests': {
                'study_cohorts': {
                    'path': self.source_paths['study_cohorts'],
                    'sha256': self.cohort_manifest_sha256,
                },
                'priority_evidence': {
                    'path': self.source_paths['priority_evidence'],
                    'sha256': self.priority_manifest_sha256,
                },
                'scheduler_seeds': {
                    'path': self.source_paths['scheduler_seeds'],
                    'sha256': self.scheduler_manifest_sha256,
                },
                'study_bundle': {
                    'path': self.source_paths['study_bundle'],
                    'sha256': self.study_bundle_sha256,
                    'files': 21,
                },
                'bundle_sha256': self.bundle_manifest_sha256,
            },
            'counts': {
                'cohorts': len(self.cohorts),
                'memberships': self.membership_count,
                **(database or {}),
            },
            'cohorts': [
                {
                    'slug': cohort.slug,
                    'root_key': cohort.root_key,
                    'root_fen': cohort.root_fen,
                    'priority_level': cohort.priority_level,
                    'evidence_level': cohort.evidence_level,
                    'memberships': len(cohort.memberships),
                }
                for cohort in self.cohorts
            ],
            'safety': {
                'shadow_models_only': True,
                'position_writes': 0,
                'edge_writes': 0,
                'analysis_task_writes': 0,
                'closure_effect': 'none',
            },
        }


def _require(condition: bool, message: str):
    if not condition:
        raise TheoryImportError(message)


def _require_mapping(value: Any, label: str) -> dict[str, Any]:
    _require(isinstance(value, dict), f'{label} must be an object')
    return value


def _require_list(value: Any, label: str) -> list[Any]:
    _require(isinstance(value, list), f'{label} must be an array')
    return value


def _require_sha256(value: Any, label: str, allow_blank: bool = False) -> str:
    if allow_blank and (value is None or value == ''):
        return ''
    _require(isinstance(value, str), f'{label} must be a string')
    normalized = value.lower()
    _require(bool(_HEX64.fullmatch(normalized)),
             f'{label} must be a lowercase SHA-256 hex digest')
    return normalized


def _require_canonical_fen(
        value: Any,
        label: str,
        *,
        require_exact: bool = False,
) -> str:
    _require(isinstance(value, str), f'{label} must be a FEN string')
    try:
        canonical = logic.canonical_fen(value)
    except Exception as exc:
        raise TheoryImportError(f'invalid {label}: {exc}') from exc
    if require_exact:
        _require(canonical == value, f'{label} must be canonical')
    return canonical


def _no_duplicate_object_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise TheoryImportError(f'duplicate JSON key: {key}')
        result[key] = value
    return result


def _reject_json_constant(value):
    raise TheoryImportError(f'non-finite JSON number is forbidden: {value}')


def load_pinned_json(path: str | Path, expected_sha256: str):
    """Read a small, strict UTF-8 JSON object after verifying its exact hash."""
    path = Path(path)
    expected = _require_sha256(expected_sha256, f'{path.name} expected hash')
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise TheoryImportError(f'cannot stat manifest {path}: {exc}') from exc
    _require(0 < size <= MAX_MANIFEST_BYTES,
             f'manifest size outside limits: {path} ({size} bytes)')
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise TheoryImportError(f'cannot read manifest {path}: {exc}') from exc
    actual = hashlib.sha256(payload).hexdigest()
    _require(actual == expected,
             f'manifest hash mismatch for {path.name}: '
             f'expected {expected}, got {actual}')
    try:
        text = payload.decode('utf-8', errors='strict')
    except UnicodeDecodeError as exc:
        raise TheoryImportError(
            f'manifest is not strict UTF-8: {path.name}') from exc
    _require(not text.startswith('\ufeff'),
             f'UTF-8 BOM is not accepted: {path.name}')
    try:
        data = json.loads(
            text, object_pairs_hook=_no_duplicate_object_pairs,
            parse_constant=_reject_json_constant)
    except (json.JSONDecodeError, TheoryImportError) as exc:
        raise TheoryImportError(f'invalid JSON in {path.name}: {exc}') from exc
    return _require_mapping(data, path.name), actual


def load_pinned_canonical_json(path: str | Path, expected_sha256: str):
    """Load a self-hashed JSON object using its documented canonical form."""
    path = Path(path)
    expected = _require_sha256(expected_sha256, f'{path.name} expected hash')
    try:
        size = path.stat().st_size
        _require(0 < size <= MAX_MANIFEST_BYTES,
                 f'manifest size outside limits: {path} ({size} bytes)')
        payload = path.read_bytes()
    except OSError as exc:
        raise TheoryImportError(f'cannot read manifest {path}: {exc}') from exc
    try:
        text = payload.decode('utf-8', errors='strict')
    except UnicodeDecodeError as exc:
        raise TheoryImportError(
            f'manifest is not strict UTF-8: {path.name}') from exc
    _require(not text.startswith('\ufeff'),
             f'UTF-8 BOM is not accepted: {path.name}')
    try:
        data = json.loads(
            text, object_pairs_hook=_no_duplicate_object_pairs,
            parse_constant=_reject_json_constant)
    except (json.JSONDecodeError, TheoryImportError) as exc:
        raise TheoryImportError(f'invalid JSON in {path.name}: {exc}') from exc
    data = _require_mapping(data, path.name)
    declared = _require_sha256(
        data.get('content_sha256'), f'{path.name}.content_sha256')
    unsigned = dict(data)
    del unsigned['content_sha256']
    canonical = json.dumps(
        unsigned, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode('utf-8')
    actual = hashlib.sha256(canonical).hexdigest()
    _require(declared == expected == actual,
             f'canonical manifest hash mismatch for {path.name}: '
             f'expected {expected}, declared {declared}, got {actual}')
    return data, actual


def verify_study_bundle(
        study_root: str | Path,
        cohort_data: dict[str, Any],
) -> str:
    """Authenticate the exact 21-file PGN corpus without importing its prose."""
    root = Path(study_root)
    _require(root.exists() and root.is_dir() and not root.is_symlink(),
             f'study root must be a real directory: {root}')
    expected = {
        f"{study['study_id']}.pgn": study['pgn_sha256']
        for study in cohort_data['studies']
    }
    try:
        entries = list(root.iterdir())
    except OSError as exc:
        raise TheoryImportError(
            f'cannot enumerate study root {root}: {exc}') from exc
    actual_names = {entry.name for entry in entries}
    _require(len(entries) == len(actual_names),
             'study root contains case-colliding names')
    _require(actual_names == set(expected),
             'study root must contain exactly the 21 pinned PGNs; '
             f'missing={sorted(set(expected) - actual_names)}, '
             f'extra={sorted(actual_names - set(expected))}')

    verified = []
    for name in sorted(expected):
        path = root / name
        _require(path.is_file() and not path.is_symlink(),
                 f'study artifact must be a regular non-symlink file: {name}')
        try:
            before = path.stat()
            _require(0 < before.st_size <= MAX_STUDY_PGN_BYTES,
                     f'study artifact size outside limits: {name}')
            digest = hashlib.sha256()
            with path.open('rb') as handle:
                while chunk := handle.read(1024 * 1024):
                    digest.update(chunk)
            after = path.stat()
        except OSError as exc:
            raise TheoryImportError(
                f'cannot authenticate study artifact {name}: {exc}') from exc
        _require(
            (before.st_size, before.st_mtime_ns, before.st_ino)
            == (after.st_size, after.st_mtime_ns, after.st_ino),
            f'study artifact changed while hashing: {name}')
        actual = digest.hexdigest()
        _require(actual == expected[name],
                 f'study artifact hash mismatch for {name}: '
                 f'expected {expected[name]}, got {actual}')
        verified.append((name[:-4], actual))

    bundle = hashlib.sha256()
    # Contract sealed by the executable scheduler manifest: study_id, NUL,
    # lowercase PGN SHA-256, LF; sorted by study_id, with no domain prefix.
    for study_id, actual in verified:
        bundle.update(study_id.encode('ascii'))
        bundle.update(b'\0')
        bundle.update(actual.encode('ascii'))
        bundle.update(b'\n')
    return bundle.hexdigest()


def _sum_field(rows: Iterable[dict[str, Any]], field: str) -> int:
    total = 0
    for index, row in enumerate(rows):
        value = row.get(field)
        _require(isinstance(value, int) and not isinstance(value, bool)
                 and value >= 0,
                 f'row {index} has invalid {field}')
        total += value
    return total


def validate_cohort_manifest(data: dict[str, Any]):
    _require(data.get('schema') == 'atomic-the-house-study-cohort-audit-v1',
             'unsupported study-cohort schema')
    scope = _require_mapping(data.get('scope'), 'scope')
    corpus = _require_mapping(data.get('corpus_totals'), 'corpus_totals')
    studies = _require_list(data.get('studies'), 'studies')
    cohorts = _require_list(data.get('cohort_totals'), 'cohort_totals')
    _require(scope.get('study_count') == 21, 'expected exactly 21 studies')
    _require(scope.get('chapter_count') == 161,
             'expected exactly 161 chapters')
    _require(corpus.get('studies') == len(studies) == 21,
             'study count does not reconcile')
    _require(corpus.get('chapters') == 161,
             'corpus chapter count does not reconcile')
    _require(corpus.get('parse_errors') == 0,
             'source corpus contains parse errors')
    _require(data.get('unavailable_studies') == [],
             'source corpus contains unavailable studies')

    ids = set()
    study_aggregate_fields = (
        'chapters', 'move_nodes', 'comments', 'rav_side_branches',
        'branch_points', 'parse_errors', 'bytes',
    )
    cohort_aggregate_fields = (
        'chapters', 'move_nodes', 'comments', 'rav_side_branches',
        'branch_points', 'parse_errors',
    )
    for index, study_value in enumerate(studies):
        study = _require_mapping(study_value, f'studies[{index}]')
        study_id = study.get('study_id')
        _require(isinstance(study_id, str)
                 and bool(re.fullmatch(r'[A-Za-z0-9]{8}', study_id)),
                 f'invalid study id at studies[{index}]')
        _require(study_id not in ids, f'duplicate study id: {study_id}')
        ids.add(study_id)
        _require(study.get('url') == f'https://lichess.org/study/{study_id}',
                 f'non-canonical study URL for {study_id}')
        _require_sha256(study.get('pgn_sha256'),
                        f'{study_id}.pgn_sha256')
        _require(study.get('parse_errors') == 0,
                 f'{study_id} contains parse errors')

    for field in study_aggregate_fields:
        _require(_sum_field(studies, field) == corpus.get(field),
                 f'per-study {field} does not match corpus total')
    for field in cohort_aggregate_fields:
        _require(_sum_field(cohorts, field) == corpus.get(field),
                 f'per-cohort {field} does not match corpus total')

    empty = _require_list(
        data.get('valid_but_empty_chapters'), 'valid_but_empty_chapters')
    _require(len(empty) == corpus.get('empty_chapters') == 2,
             'empty chapter count does not reconcile')

    canonical = _require_mapping(data.get('canonical_2n'), 'canonical_2n')
    _require(canonical.get('san') == '1.Nf3 f6 2.Nc3',
             'canonical 2N SAN drift')
    _require(canonical.get('explicitly_not') == '1.Nf3 f6 2.Nd4',
             'canonical 2N exclusion drift')
    canonical_fen = _require_canonical_fen(
        canonical.get('fen'), 'canonical 2N FEN')
    _require(canonical_fen == canonical_fen_from_san(canonical.get('san')),
        'canonical 2N FEN does not match the legal SAN line')
    return data


def _safe_public_url(value: Any, label: str) -> str:
    _require(isinstance(value, str), f'{label} must be a string')
    parsed = urlsplit(value)
    _require(parsed.scheme == 'https' and parsed.username is None
             and parsed.password is None,
             f'{label} must be a credential-free HTTPS URL')
    _require(parsed.hostname in {'discord.com', 'lichess.org'},
             f'{label} host is not allowlisted')
    _require(not parsed.query and not parsed.fragment,
             f'{label} must not contain query or fragment data')
    return value


def _priority_level(candidate: dict[str, Any]) -> str:
    role = candidate.get('portfolio_role', '')
    if isinstance(role, str):
        match = re.search(r'\bP([0-3])\b', role)
        if match:
            return f'P{match.group(1)}'
    rank = candidate['rank']
    if rank <= 3:
        return 'P0'
    if rank <= 5:
        return 'P1'
    return 'P2'


def _normalize_san(value: str) -> str:
    value = value.replace('0-0-0', 'O-O-O').replace('0-0', 'O-O')
    return re.sub(r'[!?]+$', '', value)


def _san_tokens(line: str) -> list[str]:
    tokens = []
    for raw in line.split():
        token = _MOVE_NUMBER.sub('', raw.strip())
        if not token or token in _RESULT_TOKENS:
            continue
        _require(not any(char in token for char in '{}()[]'),
                 f'annotations/variations are not accepted in seed SAN: {line}')
        tokens.append(_normalize_san(token))
    _require(bool(tokens), f'no SAN moves in seed line: {line}')
    return tokens


@lru_cache(maxsize=256)
def resolve_san_line(
        line: str,
        initial_fen: str | None = None,
) -> tuple[PathPosition, ...]:
    """Regenerate a SAN line with AtomicDB's legal move/FEN primitives."""
    supplied_fen = initial_fen or logic.start_fen()
    fen = _require_canonical_fen(supplied_fen, 'seed initial FEN')
    path = []
    positions = []
    san_tokens = _san_tokens(line)
    for ply, san in enumerate(san_tokens, start=1):
        matches = []
        try:
            legal_moves = logic.legal_moves(fen)
        except Exception as exc:
            raise TheoryImportError(
                f'cannot enumerate legal moves at ply {ply}: {exc}') from exc
        for uci in legal_moves:
            try:
                generated = pf.get_san(logic.VARIANT, fen, uci)
            except Exception as exc:
                raise TheoryImportError(
                    f'cannot render SAN for legal move {uci}: {exc}') from exc
            if _normalize_san(generated) == san:
                matches.append(uci)
        _require(len(matches) == 1,
                 f'SAN {san!r} at ply {ply} resolved to '
                 f'{len(matches)} legal Atomic moves')
        uci = matches[0]
        _require(uci in legal_moves,
                 f'resolved move is not legal at ply {ply}: {uci}')
        try:
            child = logic.apply_move(fen, uci)
        except Exception as exc:
            raise TheoryImportError(
                f'failed to apply {uci} at ply {ply}: {exc}') from exc
        path.append(uci)
        role = 'SEED_ROOT' if ply == len(san_tokens) else 'ANCESTOR'
        positions.append(PathPosition(
            ply=ply,
            fen=child,
            position_key=logic.key_of(child),
            path_uci=' '.join(path),
            role=role,
        ))
        fen = child
    return tuple(positions)


def canonical_fen_from_san(line: str) -> str:
    return resolve_san_line(line)[-1].fen


def _provenance_kind(url: str) -> str:
    host = urlsplit(url).hostname
    if host == 'lichess.org':
        return 'LICHESS_STUDY'
    if host == 'discord.com':
        return 'DISCORD_EVIDENCE'
    return 'OTHER'


def _bundle_hash(
        cohort_sha256: str,
        priority_sha256: str,
        scheduler_sha256: str,
        study_bundle_sha256: str,
) -> str:
    digest = hashlib.sha256()
    digest.update(b'atomic-theory-shadow-v1\0')
    for item in (
            cohort_sha256, priority_sha256, scheduler_sha256,
            study_bundle_sha256):
        digest.update(bytes.fromhex(item))
    return digest.hexdigest()


def _validate_cross_manifest(cohort_data, priority_data):
    cohort_two_n = cohort_data['canonical_2n']
    priority_two_n = priority_data['notation']['2N']
    _require(cohort_two_n['san'] == priority_two_n['canonical_line'],
             '2N SAN differs between source manifests')
    _require(logic.canonical_fen(cohort_two_n['fen'])
             == logic.canonical_fen(priority_two_n['canonical_fen']),
             '2N FEN differs between source manifests')

    study_hashes = {
        study['study_id']: study['pgn_sha256']
        for study in cohort_data['studies']
    }
    for candidate in priority_data['ranked_candidates']:
        for evidence in candidate['evidence']:
            source_id = evidence['source_id']
            artifact_hash = evidence.get('artifact_sha256', '')
            if source_id in study_hashes and artifact_hash:
                _require(artifact_hash == study_hashes[source_id],
                         f'artifact hash drift for study {source_id}')


def validate_scheduler_manifest(
        data: dict[str, Any],
        cohort_sha256: str,
        priority_sha256: str,
        study_bundle_sha256: str,
        priority_data: dict[str, Any],
) -> dict[str, tuple[PathPosition, ...]]:
    """Validate the sole executable source of theory cohort seed paths."""
    _require(data.get('schema') == 'atomic-theory-scheduler-seeds-v1',
             'unsupported scheduler-seed schema')
    _require(data.get('policy_version') == POLICY_VERSION,
             'scheduler policy version drift')
    sources = _require_mapping(
        data.get('source_manifests'), 'scheduler.source_manifests')
    for field, expected in (
            ('study_cohorts_sha256', cohort_sha256),
            ('priority_evidence_sha256', priority_sha256),
            ('study_artifacts_sha256', study_bundle_sha256)):
        _require_sha256(sources.get(field), f'scheduler.{field}')
        _require(sources[field] == expected,
                 f'scheduler {field} does not match authenticated input')
    _require(sources.get('study_count') == 21,
             'scheduler study count must be exactly 21')

    _require(priority_data.get('schema_version') == 1,
             'unsupported priority-provenance schema')
    levels = _require_mapping(
        priority_data.get('evidence_levels'), 'evidence_levels')
    _require(tuple(levels) == _EVIDENCE_LEVELS,
             'evidence level ordering drift')
    notation = _require_mapping(
        priority_data.get('notation'), 'priority.notation')
    two_knights = _require_mapping(notation.get('2N'), 'priority.notation.2N')
    _require(two_knights.get('canonical_line') == '1.Nf3 f6 2.Nc3',
             'priority provenance 2N notation drift')
    _require(two_knights.get('must_not_be_confused_with')
             == '1.Nf3 f6 2.Nd4',
             'priority provenance 2N exclusion drift')
    canonical_two_n = _require_canonical_fen(
        two_knights.get('canonical_fen'), 'priority canonical 2N FEN')
    _require(canonical_two_n
        == canonical_fen_from_san(two_knights['canonical_line']),
        'priority provenance canonical 2N FEN drift')

    narrative_candidates = _require_list(
        priority_data.get('ranked_candidates'), 'ranked_candidates')
    _require(len(narrative_candidates) == 7,
             'priority provenance must contain exactly 7 candidates')
    narrative_by_id = {}
    for index, candidate_value in enumerate(narrative_candidates):
        candidate = _require_mapping(
            candidate_value, f'ranked_candidates[{index}]')
        candidate_id = candidate.get('candidate_id')
        _require(isinstance(candidate_id, str)
                 and bool(_SLUG.fullmatch(candidate_id)),
                 f'invalid narrative candidate id at index {index}')
        _require(candidate_id not in narrative_by_id,
                 f'duplicate narrative candidate id: {candidate_id}')
        _require(candidate.get('rank') == index + 1,
                 'priority-provenance ranks must be contiguous and ordered')
        narrative_by_id[candidate_id] = candidate

    seeds = _require_list(data.get('entries'), 'scheduler.entries')
    _require(len(seeds) == 7,
             'scheduler must contain exactly 7 executable seeds')
    paths = {}
    for seed_index, seed_value in enumerate(seeds):
        seed = _require_mapping(
            seed_value, f'scheduler.entries[{seed_index}]')
        candidate_id = seed.get('candidate_id')
        _require(isinstance(candidate_id, str)
                 and bool(_SLUG.fullmatch(candidate_id)),
                 f'invalid scheduler candidate id at index {seed_index}')
        _require(candidate_id in narrative_by_id,
                 f'scheduler candidate lacks pinned provenance: '
                 f'{candidate_id}')
        _require(candidate_id not in paths,
                 f'duplicate scheduler candidate id: {candidate_id}')
        narrative = narrative_candidates[seed_index]
        _require(candidate_id == narrative['candidate_id'],
                 'scheduler/provenance candidate rank ordering differs')
        _require(seed.get('priority_level') in {'P0', 'P1', 'P2', 'P3'},
                 f'invalid scheduler priority for {candidate_id}')
        _require(seed['priority_level'] == _priority_level(narrative),
                 f'scheduler priority does not reconcile for {candidate_id}')
        evidence_level = seed.get('evidence_level')
        _require(evidence_level in _EVIDENCE_LEVELS,
                 f'invalid scheduler evidence level for {candidate_id}')

        evidence = _require_list(
            narrative_by_id[candidate_id].get('evidence'),
            f'{candidate_id}.evidence')
        _require(bool(evidence),
                 f'{candidate_id} has no provenance evidence')
        source_ids = set()
        highest = 'E0'
        for item_index, item_value in enumerate(evidence):
            item = _require_mapping(
                item_value, f'{candidate_id}.evidence[{item_index}]')
            source_id = item.get('source_id')
            _require(isinstance(source_id, str)
                     and bool(_SOURCE_ID.fullmatch(source_id)),
                     f'invalid source id in {candidate_id}')
            _require(source_id not in source_ids,
                     f'duplicate source id {source_id} in {candidate_id}')
            source_ids.add(source_id)
            _safe_public_url(
                item.get('url'), f'{candidate_id}.{source_id}.url')
            level = item.get('level')
            _require(level in _EVIDENCE_LEVELS,
                     f'invalid evidence level in {candidate_id}')
            if _EVIDENCE_LEVELS.index(level) > \
                    _EVIDENCE_LEVELS.index(highest):
                highest = level
            _require_sha256(
                item.get('artifact_sha256', ''),
                f'{candidate_id}.{source_id}.artifact_sha256',
                allow_blank=True)
        _require(highest == evidence_level,
                 f'scheduler evidence level does not reconcile for '
                 f'{candidate_id}')

        san = seed.get('san')
        _require(isinstance(san, str) and san.strip() == san,
                 f'invalid executable SAN for {candidate_id}')
        _require(',' not in san and ';' not in san,
                 f'narrative SAN is forbidden for {candidate_id}')
        start_fen = _require_canonical_fen(
            seed.get('start_fen'), f'{candidate_id} start FEN',
            require_exact=True)
        resolved = resolve_san_line(san, initial_fen=start_fen)
        uci = _require_list(seed.get('uci'), f'{candidate_id}.uci')
        _require(uci == resolved[-1].path_uci.split(),
                 f'UCI path does not match legal SAN for {candidate_id}')
        root_fen = _require_canonical_fen(
            seed.get('root_fen'), f'{candidate_id} root FEN',
            require_exact=True)
        root_key = seed.get('root_key')
        _require(isinstance(root_key, str)
                 and bool(_HEX64.fullmatch(root_key)),
                 f'invalid root key for {candidate_id}')
        _require(resolved[-1].fen == root_fen,
                 f'root FEN does not match legal SAN/UCI for {candidate_id}')
        _require(resolved[-1].position_key == root_key,
                 f'root key does not match root FEN for {candidate_id}')
        label = seed.get('label')
        _require(isinstance(label, str) and 0 < len(label) <= 160,
                 f'invalid label for {candidate_id}')
        paths[candidate_id] = resolved

    _require(set(paths) == set(narrative_by_id),
             'scheduler/provenance candidate sets differ')
    return paths


def build_import_plan(
        cohort_data: dict[str, Any],
        priority_data: dict[str, Any],
        scheduler_data: dict[str, Any],
        cohort_sha256: str,
        priority_sha256: str,
        scheduler_sha256: str,
        study_bundle_sha256: str,
        source_paths: dict[str, str] | None = None,
) -> ImportPlan:
    validate_cohort_manifest(cohort_data)
    cohort_sha256 = _require_sha256(
        cohort_sha256, 'study-cohort manifest hash')
    priority_sha256 = _require_sha256(
        priority_sha256, 'priority-evidence manifest hash')
    scheduler_sha256 = _require_sha256(
        scheduler_sha256, 'scheduler-seed manifest hash')
    study_bundle_sha256 = _require_sha256(
        study_bundle_sha256, 'study PGN bundle hash')
    scheduler_paths = validate_scheduler_manifest(
        scheduler_data, cohort_sha256, priority_sha256,
        study_bundle_sha256, priority_data)
    _validate_cross_manifest(cohort_data, priority_data)
    bundle = _bundle_hash(
        cohort_sha256, priority_sha256, scheduler_sha256,
        study_bundle_sha256)
    cohorts = []

    provenance_by_id = {
        candidate['candidate_id']: candidate
        for candidate in priority_data['ranked_candidates']
    }
    for seed_index, seed in enumerate(scheduler_data['entries']):
        candidate_id = seed['candidate_id']
        candidate = provenance_by_id[candidate_id]
        path_positions = scheduler_paths[candidate_id]
        priority_level = seed['priority_level']
        memberships = []
        for evidence in candidate['evidence']:
            source_url = _safe_public_url(
                evidence['url'],
                f"{candidate_id}.{evidence['source_id']}.url")
            artifact_sha = _require_sha256(
                evidence.get('artifact_sha256', ''),
                f"{candidate_id}.{evidence['source_id']}.hash",
                allow_blank=True)
            position = path_positions[-1]
            path_sha256 = hashlib.sha256(
                position.path_uci.encode('utf-8')).hexdigest()
            memberships.append(MembershipSpec(
                position_key=position.position_key,
                fen=position.fen,
                ply=position.ply,
                role='SEED_ROOT',
                source_id=evidence['source_id'],
                source_url=source_url,
                artifact_sha256=artifact_sha,
                path_uci=position.path_uci,
                path_sha256=path_sha256,
                provenance_kind=_provenance_kind(source_url),
                metadata={
                    'candidate_id': candidate_id,
                    'source_evidence_level': evidence['level'],
                    'start_fen': seed['start_fen'],
                    'root_only': True,
                    'shadow_only': True,
                },
            ))
        identity = {
            (item.position_key, item.source_id, item.path_sha256)
            for item in memberships
        }
        _require(len(identity) == len(memberships),
                 f"duplicate membership provenance in "
                 f"{candidate_id}")
        root = path_positions[-1]
        cohorts.append(CohortSpec(
            slug=candidate_id,
            label=seed['label'],
            root_fen=root.fen,
            root_key=root.position_key,
            priority_level=priority_level,
            evidence_level=seed['evidence_level'],
            manifest_sha256=bundle,
            policy_version=POLICY_VERSION,
            decay_policy={
                'kind': 'linear-lifetime-compute',
                'attempts': {
                    'decay_starts': 3,
                    'zero_at': 5,
                },
                'core_hours': {
                    'decay_starts': 25,
                    'zero_at': 50,
                },
                'aggregation': 'max_not_sum',
            },
            metadata={
                'rank': seed_index + 1,
                'scheduler_san': seed['san'],
                'start_fen': seed['start_fen'],
                'source_count': len(candidate['evidence']),
                'source_manifest_sha256s': {
                    'study_cohorts': cohort_sha256,
                    'priority_evidence': priority_sha256,
                    'scheduler_seeds': scheduler_sha256,
                    'study_bundle': study_bundle_sha256,
                },
                'shadow_only': True,
                'truth_effect': 'none',
                'partial_topology': True,
            },
            active=True,
            memberships=tuple(memberships),
        ))
    return ImportPlan(
        cohort_manifest_sha256=cohort_sha256,
        priority_manifest_sha256=priority_sha256,
        scheduler_manifest_sha256=scheduler_sha256,
        study_bundle_sha256=study_bundle_sha256,
        bundle_manifest_sha256=bundle,
        source_paths=source_paths or {
            'study_cohorts': '<in-memory>',
            'priority_evidence': '<in-memory>',
            'scheduler_seeds': '<in-memory>',
            'study_bundle': '<in-memory>',
        },
        cohorts=tuple(cohorts),
    )


def load_import_plan(
        cohort_manifest: str | Path = DEFAULT_COHORT_MANIFEST,
        priority_manifest: str | Path = DEFAULT_PRIORITY_MANIFEST,
        scheduler_manifest: str | Path = DEFAULT_SCHEDULER_MANIFEST,
        study_root: str | Path = DEFAULT_STUDY_ROOT,
        cohort_sha256: str = DEFAULT_COHORT_MANIFEST_SHA256,
        priority_sha256: str = DEFAULT_PRIORITY_MANIFEST_SHA256,
        scheduler_sha256: str = DEFAULT_SCHEDULER_MANIFEST_SHA256,
) -> ImportPlan:
    cohort_data, cohort_actual = load_pinned_json(
        cohort_manifest, cohort_sha256)
    priority_data, priority_actual = load_pinned_json(
        priority_manifest, priority_sha256)
    scheduler_data, scheduler_actual = load_pinned_canonical_json(
        scheduler_manifest, scheduler_sha256)
    validate_cohort_manifest(cohort_data)
    study_bundle_actual = verify_study_bundle(study_root, cohort_data)
    _require(study_bundle_actual == DEFAULT_STUDY_BUNDLE_SHA256,
             'study bundle differs from the runtime-pinned corpus')
    source_paths = {
        'study_cohorts': str(Path(cohort_manifest).resolve(strict=True)),
        'priority_evidence': str(Path(priority_manifest).resolve(strict=True)),
        'scheduler_seeds': str(
            Path(scheduler_manifest).resolve(strict=True)),
        'study_bundle': str(Path(study_root).resolve(strict=True)),
    }
    plan = build_import_plan(
        cohort_data, priority_data, scheduler_data, cohort_actual,
        priority_actual, scheduler_actual, study_bundle_actual,
        source_paths=source_paths)
    _require(plan.bundle_manifest_sha256 == DEFAULT_IMPORT_BUNDLE_SHA256,
             'combined import bundle differs from the runtime pin')
    return plan


def _model_field_names(model) -> set[str]:
    try:
        return {field.name for field in model._meta.get_fields()}
    except Exception as exc:
        raise TheoryImportError(
            f'{model!r} does not expose Django model metadata') from exc


def _require_model_interface(model, required_fields, label):
    missing = sorted(required_fields - _model_field_names(model))
    _require(not missing, f'{label} missing required fields: {missing}')


def resolve_shadow_models():
    """Late import keeps dry-run usable before the migration is installed."""
    try:
        from atomicdb.models import CohortMembership, SchedulingCohort
    except ImportError as exc:
        raise TheoryImportError(
            'SchedulingCohort/CohortMembership are not installed; '
            'dry-run remains available') from exc
    _require_model_interface(
        SchedulingCohort, _REQUIRED_COHORT_FIELDS, 'SchedulingCohort')
    _require_model_interface(
        CohortMembership, _REQUIRED_MEMBERSHIP_FIELDS, 'CohortMembership')
    return SchedulingCohort, CohortMembership


def _object_matches(obj, values: dict[str, Any]) -> bool:
    return all(getattr(obj, field) == value for field, value in values.items())


def _get_or_create_immutable(model, identity, values, label):
    obj, created = model.objects.get_or_create(**identity, defaults=values)
    if not created and not _object_matches(obj, values):
        raise TheoryImportError(
            f'existing {label} conflicts with the pinned import plan')
    return obj, created


def _cohort_identity(spec: CohortSpec) -> dict[str, Any]:
    return {
        'policy_version': spec.policy_version,
        'slug': spec.slug,
    }


def _cohort_values(spec: CohortSpec) -> dict[str, Any]:
    return {
        'label': spec.label,
        'root_fen': spec.root_fen,
        'root_key': spec.root_key,
        'priority_level': spec.priority_level,
        'evidence_level': spec.evidence_level,
        'manifest_sha256': spec.manifest_sha256,
        'decay_policy': spec.decay_policy,
        'metadata': spec.metadata,
        'active': spec.active,
    }


def _membership_identity(
        cohort,
        membership: MembershipSpec,
) -> dict[str, Any]:
    return {
        'cohort': cohort,
        'position_key': membership.position_key,
        'source_id': membership.source_id,
        'path_sha256': membership.path_sha256,
    }


def _membership_values(membership: MembershipSpec) -> dict[str, Any]:
    return {
        'fen': membership.fen,
        'ply': membership.ply,
        'role': membership.role,
        'source_url': membership.source_url,
        'artifact_sha256': membership.artifact_sha256,
        'path_uci': membership.path_uci,
        'provenance_kind': membership.provenance_kind,
        'metadata': membership.metadata,
    }


def _assert_sealed_database_set(
        plan: ImportPlan,
        cohort_model,
        membership_model,
        *,
        allow_missing: bool,
):
    """Lock and verify the active policy slice against the sealed plan."""
    cohort_manager = cohort_model.objects
    membership_manager = membership_model.objects
    if not (hasattr(cohort_manager, 'select_for_update')
            and hasattr(membership_manager, 'select_for_update')):
        # Lightweight injected test doubles still exercise immutable upserts.
        return

    cohort_rows = list(cohort_manager.select_for_update().filter(
        policy_version=POLICY_VERSION))
    expected = {spec.slug: spec for spec in plan.cohorts}
    active_slugs = {row.slug for row in cohort_rows if row.active}
    unexpected = active_slugs - set(expected)
    _require(not unexpected,
             f'unexpected active cohorts for {POLICY_VERSION}: '
             f'{sorted(unexpected)}')
    if not allow_missing:
        _require(active_slugs == set(expected),
                 'active cohort set does not match sealed plan')

    by_slug = {}
    for row in cohort_rows:
        _require(row.slug not in by_slug,
                 f'duplicate cohort row for {POLICY_VERSION}/{row.slug}')
        by_slug[row.slug] = row
    for slug, spec in expected.items():
        cohort = by_slug.get(slug)
        if cohort is None:
            _require(allow_missing, f'missing sealed cohort: {slug}')
            continue
        _require(_object_matches(
            cohort, {**_cohort_identity(spec), **_cohort_values(spec)}),
            f'existing cohort {slug} conflicts with the sealed import plan')
        members = list(
            membership_manager.select_for_update().filter(cohort=cohort))
        expected_members = {
            (
                item.position_key,
                item.source_id,
                item.path_sha256,
            ): item
            for item in spec.memberships
        }
        actual_identities = set()
        for member in members:
            identity = (
                member.position_key,
                member.source_id,
                member.path_sha256,
            )
            _require(identity in expected_members,
                     f'unexpected membership in sealed cohort {slug}: '
                     f'{identity}')
            _require(identity not in actual_identities,
                     f'duplicate membership in sealed cohort {slug}: '
                     f'{identity}')
            actual_identities.add(identity)
            expected_member = expected_members[identity]
            _require(_object_matches(
                member,
                {
                    **_membership_identity(cohort, expected_member),
                    **_membership_values(expected_member),
                }),
                f'existing membership conflicts with the sealed plan: '
                f'{slug}/{identity}')
        if not allow_missing:
            _require(actual_identities == set(expected_members),
                     f'membership set does not match sealed cohort {slug}')


def apply_import_plan(
        plan: ImportPlan,
        cohort_model=None,
        membership_model=None,
        atomic_context=None,
) -> dict[str, int]:
    """Idempotently apply a validated plan to the two shadow-only models."""
    configured_policy = getattr(
        settings, 'ATOMICDB_THEORY_POLICY_VERSION', None)
    _require(configured_policy == POLICY_VERSION,
             'configured theory policy does not match the pinned import: '
             f'{configured_policy!r} != {POLICY_VERSION!r}')
    _require(
        {spec.policy_version for spec in plan.cohorts} == {POLICY_VERSION},
        'import plan contains an unexpected policy version')
    configured_bundle = getattr(
        settings, 'ATOMICDB_THEORY_BUNDLE_SHA256', None)
    _require(configured_bundle == plan.bundle_manifest_sha256,
             'configured theory bundle does not match the pinned import: '
             f'{configured_bundle!r} != '
             f'{plan.bundle_manifest_sha256!r}')
    if cohort_model is None or membership_model is None:
        resolved_cohort, resolved_membership = resolve_shadow_models()
        cohort_model = cohort_model or resolved_cohort
        membership_model = membership_model or resolved_membership
    _require_model_interface(
        cohort_model, _REQUIRED_COHORT_FIELDS, 'SchedulingCohort')
    _require_model_interface(
        membership_model, _REQUIRED_MEMBERSHIP_FIELDS, 'CohortMembership')
    context = atomic_context or transaction.atomic
    counts = {
        'cohorts_created': 0,
        'cohorts_reused': 0,
        'memberships_created': 0,
        'memberships_reused': 0,
    }
    with context():
        _assert_sealed_database_set(
            plan, cohort_model, membership_model, allow_missing=True)
        for spec in plan.cohorts:
            cohort, created = _get_or_create_immutable(
                cohort_model, _cohort_identity(spec), _cohort_values(spec),
                f'cohort {spec.slug}')
            counts['cohorts_created' if created else 'cohorts_reused'] += 1
            for membership in spec.memberships:
                _require(logic.key_of(membership.fen)
                         == membership.position_key,
                         f'membership key/FEN mismatch in {spec.slug}')
                _, member_created = _get_or_create_immutable(
                    membership_model,
                    _membership_identity(cohort, membership),
                    _membership_values(membership),
                    f'membership {spec.slug}/{membership.source_id}/'
                    f'{membership.path_uci}')
                key = ('memberships_created' if member_created
                       else 'memberships_reused')
                counts[key] += 1
        _assert_sealed_database_set(
            plan, cohort_model, membership_model, allow_missing=False)
    return counts


def write_receipt(path: str | Path, receipt: dict[str, Any]) -> str:
    """Write a UTF-8 receipt atomically and return its SHA-256."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise TheoryImportError(f'receipt already exists: {path}')
    payload = (json.dumps(
        receipt, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode(
            'utf-8')
    descriptor, temporary_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f'.{path.name}.', suffix='.tmp')
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        # Hard-link publication is atomic and refuses an existing target.
        os.link(temporary, path)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise TheoryImportError(f'cannot write receipt {path}: {exc}') from exc
    temporary.unlink(missing_ok=True)
    return hashlib.sha256(payload).hexdigest()


def preflight_receipt_path(final_path: str | Path) -> Path:
    """Return the deterministic no-overwrite companion for an apply receipt."""
    final_path = Path(final_path)
    if final_path.suffix:
        name = (
            f'{final_path.name[:-len(final_path.suffix)]}'
            f'.preflight{final_path.suffix}')
    else:
        name = f'{final_path.name}.preflight'
    return final_path.with_name(name)
