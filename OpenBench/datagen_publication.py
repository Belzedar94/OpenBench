import hashlib
import json
import os
import re
import stat
import string


DATAGEN_PUBLICATION_PROTOCOL = 41
DATAGEN_PUBLICATION_PROTOCOL_V42 = 42
DATAGEN_PUBLICATION_PROTOCOLS = frozenset({
    DATAGEN_PUBLICATION_PROTOCOL,
    DATAGEN_PUBLICATION_PROTOCOL_V42,
})
DATAGEN_PUBLICATION_CONTRACT_SCHEMA = (
    'openbench-datagen-publication-contract-v41'
)
DATAGEN_PUBLICATION_CONTRACT_SCHEMA_V42 = (
    'openbench-datagen-publication-contract-v42'
)
DATAGEN_PUBLICATION_LEASE_SCHEMA = 'openbench-datagen-publication-lease-v41'
DATAGEN_PUBLICATION_LEASE_SCHEMA_V42 = (
    'openbench-datagen-publication-lease-v42'
)
DATAGEN_PUBLICATION_RECEIPT_SCHEMA = 'openbench-datagen-publication-receipt-v41'
DATAGEN_PUBLICATION_RECEIPT_SCHEMA_V42 = (
    'openbench-datagen-publication-receipt-v42'
)
DATAGEN_PUBLICATION_MANIFEST_SCHEMA = (
    'openbench-datagen-publication-manifest-v41'
)
DATAGEN_PUBLICATION_MANIFEST_SCHEMA_V42 = (
    'openbench-datagen-publication-manifest-v42'
)
DATAGEN_PUBLICATION_MANIFEST_VERSION = 1
DATAGEN_SEED_METHOD = 'base-plus-chunk-index-v1'

PUBLICATION_IDENTITY_FIELDS = (
    'datagen_campaign_id',
    'datagen_external_workload_id',
    'datagen_role',
    'datagen_cohort',
)
_SLUG = re.compile(r'^[a-z0-9][a-z0-9._-]{0,127}$')
_SHA256 = re.compile(r'^[0-9a-f]{64}$')
_COMMIT = re.compile(r'^[0-9a-f]{40}$')
_OPENBENCH_NETWORK = re.compile(r'^[0-9A-F]{8}$')


class PublicationContractError(ValueError):
    pass


def canonical_json_bytes(document):
    try:
        return json.dumps(
            document,
            sort_keys=True,
            separators=(',', ':'),
            ensure_ascii=True,
            allow_nan=False,
        ).encode('utf-8')
    except (TypeError, ValueError) as error:
        raise PublicationContractError(
            'DATAGEN publication evidence is not canonical JSON'
        ) from error


def canonical_json_sha256(document):
    return hashlib.sha256(canonical_json_bytes(document)).hexdigest()


def publication_contract_schema(protocol):
    return {
        DATAGEN_PUBLICATION_PROTOCOL: DATAGEN_PUBLICATION_CONTRACT_SCHEMA,
        DATAGEN_PUBLICATION_PROTOCOL_V42: (
            DATAGEN_PUBLICATION_CONTRACT_SCHEMA_V42
        ),
    }.get(protocol)


def publication_lease_schema(protocol):
    return {
        DATAGEN_PUBLICATION_PROTOCOL: DATAGEN_PUBLICATION_LEASE_SCHEMA,
        DATAGEN_PUBLICATION_PROTOCOL_V42: DATAGEN_PUBLICATION_LEASE_SCHEMA_V42,
    }.get(protocol)


def publication_receipt_schema(protocol):
    return {
        DATAGEN_PUBLICATION_PROTOCOL: DATAGEN_PUBLICATION_RECEIPT_SCHEMA,
        DATAGEN_PUBLICATION_PROTOCOL_V42: (
            DATAGEN_PUBLICATION_RECEIPT_SCHEMA_V42
        ),
    }.get(protocol)


def publication_manifest_schema(protocol):
    return {
        DATAGEN_PUBLICATION_PROTOCOL: DATAGEN_PUBLICATION_MANIFEST_SCHEMA,
        DATAGEN_PUBLICATION_PROTOCOL_V42: (
            DATAGEN_PUBLICATION_MANIFEST_SCHEMA_V42
        ),
    }.get(protocol)


def network_none_identity():
    return {
        'kind': 'none',
        'name': None,
        'openbench_id': None,
        'sha256': None,
        'bytes': 0,
    }


def publication_requested(post):
    protocol = str(post.get('datagen_publication_protocol', '')).strip()
    identities = [str(post.get(field, '')).strip() for field in PUBLICATION_IDENTITY_FIELDS]
    return protocol in {
        str(item) for item in DATAGEN_PUBLICATION_PROTOCOLS
    } or any(identities)


def validate_publication_request(post):
    """Validate only request semantics; asset bytes are captured separately."""

    if not publication_requested(post):
        protocol = str(post.get('datagen_publication_protocol', '')).strip()
        return [] if protocol in {'', '0'} else [
            'DATAGEN publication protocol must be 41, 42, or omitted for legacy workloads'
        ]

    errors = []
    protocol_text = str(
        post.get('datagen_publication_protocol', '')
    ).strip()
    try:
        protocol = int(protocol_text)
    except ValueError:
        protocol = None
    if protocol not in DATAGEN_PUBLICATION_PROTOCOLS:
        errors.append(
            'DATAGEN publication identities require explicit protocol 41 or 42'
        )

    campaign_id = str(post.get('datagen_campaign_id', '')).strip()
    workload_id = str(post.get('datagen_external_workload_id', '')).strip()
    if not _SLUG.fullmatch(campaign_id):
        errors.append(
            'DATAGEN campaign id must be a lowercase ASCII slug of at most 128 characters'
        )
    if not _SLUG.fullmatch(workload_id):
        errors.append(
            'DATAGEN external workload id must be a lowercase ASCII slug of at most 128 characters'
        )
    role = str(post.get('datagen_role', '')).strip()
    cohort = str(post.get('datagen_cohort', '')).strip()
    if not _SLUG.fullmatch(role):
        errors.append(
            'DATAGEN publication role must be a lowercase ASCII slug of at most 128 characters'
        )
    if not _SLUG.fullmatch(cohort):
        errors.append(
            'DATAGEN publication cohort must be a lowercase ASCII slug of at most 128 characters'
        )

    try:
        fields = {
            name for _literal, name, _format_spec, _conversion
            in string.Formatter().parse(
                str(post.get('datagen_command', '')).strip()
            )
            if name is not None
        }
    except ValueError:
        fields = set()
    if protocol == DATAGEN_PUBLICATION_PROTOCOL:
        if not str(post.get('dev_network', '')).strip():
            errors.append('DATAGEN publication protocol 41 requires a network')
        required_assets = {'BOOK', 'NETWORK'}
        book_identity = {'BOOK_SHA256', 'BOOK_SHA256_CANONICAL'}
        network_identity = {'NETWORK_SHA256', 'NETWORK_SHA256_CANONICAL'}
        if (
            not required_assets.issubset(fields)
            or not fields.intersection(book_identity)
            or not fields.intersection(network_identity)
        ):
            errors.append(
                'DATAGEN publication protocol 41 requires {BOOK}, {NETWORK}, '
                'one book SHA-256 placeholder and one network SHA-256 placeholder'
            )

    if protocol == DATAGEN_PUBLICATION_PROTOCOL_V42:
        if str(post.get('dev_network', '')).strip():
            errors.append(
                'DATAGEN publication protocol 42 requires network:none'
            )
        if str(post.get('book_name', '')).strip() != 'NONE':
            errors.append(
                'DATAGEN publication protocol 42 requires builtin startpos'
            )
        teacher_id = str(post.get('datagen_teacher_id', '')).strip()
        if not _SLUG.fullmatch(teacher_id):
            errors.append(
                'DATAGEN protocol 42 teacher id must be a lowercase ASCII '
                'slug of at most 128 characters'
            )
        required = {
            'BOOK', 'BOOK_SHA256', 'NETWORK', 'NETWORK_SHA256',
            'PRODUCER_SHA256', 'TEACHER_ID', 'ENGINE_COMMIT',
            'PUBLICATION_CONTRACT_SHA256',
        }
        if not required.issubset(fields):
            errors.append(
                'DATAGEN publication protocol 42 requires {BOOK}, '
                '{BOOK_SHA256}, {NETWORK}, {NETWORK_SHA256}, '
                '{PRODUCER_SHA256}, {TEACHER_ID}, {ENGINE_COMMIT}, and '
                '{PUBLICATION_CONTRACT_SHA256}'
            )
        forbidden = fields & {
            'SYZYGY', 'SYZYGY_MANIFEST_SHA256', 'SYZYGY_MAX', 'TEACHER_MODE',
        }
        if forbidden:
            errors.append(
                'DATAGEN publication protocol 42 does not allow tablebase '
                'or teacher-mode placeholders'
            )

    return errors


def capture_network_identity(network, media_root):
    """Hash the exact registered network bytes without trusting its 32-bit id."""

    openbench_id = str(network.sha256).upper()
    if not _OPENBENCH_NETWORK.fullmatch(openbench_id):
        raise PublicationContractError(
            'DATAGEN publication network has an invalid OpenBench id'
        )
    path = os.path.join(media_root, openbench_id)
    try:
        digest = hashlib.sha256()
        byte_count = 0
        with open(path, 'rb') as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise PublicationContractError(
                    'DATAGEN publication network is not a regular file'
                )
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(block)
                byte_count += len(block)
            after = os.fstat(stream.fileno())
    except (OSError, TypeError) as error:
        raise PublicationContractError(
            'DATAGEN publication network bytes are unavailable'
        ) from error
    if (
        before.st_size <= 0
        or before.st_size != after.st_size
        or byte_count != after.st_size
        or getattr(before, 'st_mtime_ns', None) != getattr(after, 'st_mtime_ns', None)
    ):
        raise PublicationContractError(
            'DATAGEN publication network changed while it was authenticated'
        )
    full_sha256 = digest.hexdigest()
    if not full_sha256.upper().startswith(openbench_id):
        raise PublicationContractError(
            'DATAGEN publication network does not match its OpenBench id'
        )
    return {
        'name': str(network.name),
        'openbench_id': openbench_id,
        'sha256': full_sha256,
        'bytes': byte_count,
    }


def capture_book_identity(book_name, books):
    if book_name == 'NONE':
        return {
            'kind': 'builtin-startpos',
            'name': 'NONE',
            'source': None,
            'text_sha256': None,
            'raw_sha256': None,
        }
    try:
        book = books[book_name]
        text_sha256 = book['sha']
        raw_sha256 = book['raw_sha']
        source = book['source']
    except (KeyError, TypeError) as error:
        raise PublicationContractError(
            'DATAGEN publication books require source, text SHA-256, and raw SHA-256'
        ) from error
    if (
        not isinstance(text_sha256, str)
        or not isinstance(raw_sha256, str)
        or not isinstance(source, str)
        or not _SHA256.fullmatch(text_sha256.lower())
        or not _SHA256.fullmatch(raw_sha256.lower())
        or not source.strip()
        or len(source) > 2048
    ):
        raise PublicationContractError(
            'DATAGEN publication book identity is incomplete or malformed'
        )
    return {
        'kind': 'file',
        'name': book_name,
        'source': source,
        'text_sha256': text_sha256.lower(),
        'raw_sha256': raw_sha256.lower(),
    }


def _validate_network_identity(network):
    if not isinstance(network, dict) or set(network) != {
        'name', 'openbench_id', 'sha256', 'bytes',
    }:
        raise PublicationContractError('DATAGEN publication network identity is malformed')
    if (
        not isinstance(network['name'], str)
        or not network['name']
        or not isinstance(network['openbench_id'], str)
        or not _OPENBENCH_NETWORK.fullmatch(network['openbench_id'])
        or not isinstance(network['sha256'], str)
        or not _SHA256.fullmatch(network['sha256'])
        or not network['sha256'].upper().startswith(network['openbench_id'])
        or type(network['bytes']) is not int
        or network['bytes'] <= 0
    ):
        raise PublicationContractError('DATAGEN publication network identity is malformed')


def _validate_network_none_identity(network):
    if network != network_none_identity():
        raise PublicationContractError(
            'DATAGEN publication network:none identity is malformed'
        )


def _validate_book_identity(book, expected_name):
    if not isinstance(book, dict) or set(book) != {
        'kind', 'name', 'source', 'text_sha256', 'raw_sha256',
    }:
        raise PublicationContractError('DATAGEN publication book identity is malformed')
    if book['name'] != expected_name:
        raise PublicationContractError('DATAGEN publication book name changed')
    if expected_name == 'NONE':
        if book != {
            'kind': 'builtin-startpos',
            'name': 'NONE',
            'source': None,
            'text_sha256': None,
            'raw_sha256': None,
        }:
            raise PublicationContractError('DATAGEN startpos identity is malformed')
        return
    if (
        book['kind'] != 'file'
        or not isinstance(book['source'], str)
        or not book['source'].strip()
        or len(book['source']) > 2048
        or not isinstance(book['text_sha256'], str)
        or not _SHA256.fullmatch(book['text_sha256'])
        or not isinstance(book['raw_sha256'], str)
        or not _SHA256.fullmatch(book['raw_sha256'])
    ):
        raise PublicationContractError('DATAGEN publication book identity is malformed')


def build_publication_contract(test, network, book):
    protocol = test.datagen_publication_protocol
    if protocol == DATAGEN_PUBLICATION_PROTOCOL:
        _validate_network_identity(network)
    elif protocol == DATAGEN_PUBLICATION_PROTOCOL_V42:
        _validate_network_none_identity(network)
    else:
        raise PublicationContractError(
            'DATAGEN publication protocol is unsupported'
        )
    _validate_book_identity(book, test.book_name)
    campaign_id = str(test.datagen_campaign_id)
    workload_id = str(test.datagen_external_workload_id)
    commit = str(test.dev.sha).lower()
    engine_strings = (
        test.dev_engine,
        test.dev_repo,
        test.dev.source,
        test.dev.name,
        test.dev_options,
    )
    if (
        not _SLUG.fullmatch(campaign_id)
        or not _SLUG.fullmatch(workload_id)
        or not _SLUG.fullmatch(str(test.datagen_role))
        or not _SLUG.fullmatch(str(test.datagen_cohort))
        or not _COMMIT.fullmatch(commit)
        or not all(isinstance(value, str) for value in engine_strings)
        or not all(engine_strings[:4])
        or type(test.dev.bench) is not int
        or test.dev.bench <= 0
    ):
        raise PublicationContractError(
            'DATAGEN publication workload identity is incomplete or malformed'
        )
    if protocol == DATAGEN_PUBLICATION_PROTOCOL and (
        test.dev_network.upper() != network['openbench_id']
        or test.dev_netname != network['name']
    ):
        raise PublicationContractError(
            'DATAGEN publication network changed'
        )
    if protocol == DATAGEN_PUBLICATION_PROTOCOL_V42 and (
        test.dev_network
        or test.dev_netname
        or test.book_name != 'NONE'
        or not _SLUG.fullmatch(str(test.datagen_teacher_id))
    ):
        raise PublicationContractError(
            'DATAGEN protocol 42 builtin identities are malformed'
        )
    command = test.datagen_command
    if not isinstance(command, str) or not command:
        raise PublicationContractError('DATAGEN publication command is missing')

    total_count = test.datagen_total_count
    positions_per_chunk = test.datagen_positions_per_chunk
    base_seed = test.datagen_base_seed
    if (
        type(total_count) is not int
        or not 0 < total_count <= 2**63 - 1
        or type(positions_per_chunk) is not int
        or not 0 < positions_per_chunk <= 2**63 - 1
        or type(base_seed) is not int
        or not 0 <= base_seed <= 2**63 - 1
    ):
        raise PublicationContractError(
            'DATAGEN publication count or seed identity is malformed'
        )
    chunks = (total_count + positions_per_chunk - 1) // positions_per_chunk
    if base_seed + chunks - 1 > 2**63 - 1:
        raise PublicationContractError(
            'DATAGEN publication chunk seeds exceed signed 64-bit range'
        )

    producer_required = test.datagen_producer_required
    producer_contract = test.datagen_producer_contract_sha256
    environment_contract = test.datagen_environment_contract_sha256
    tablebase_required = test.datagen_tablebase_required
    if (
        type(producer_required) is not bool
        or not isinstance(producer_contract, str)
        or not _SHA256.fullmatch(producer_contract)
        or type(tablebase_required) is not bool
        or not isinstance(environment_contract, str)
        or not _SHA256.fullmatch(environment_contract)
    ):
        raise PublicationContractError(
            'DATAGEN publication producer or environment identity is malformed'
        )
    if (
        protocol == DATAGEN_PUBLICATION_PROTOCOL_V42
        and not producer_required
    ):
        raise PublicationContractError(
            'DATAGEN protocol 42 requires an authenticated producer'
        )
    if tablebase_required:
        if (
            test.datagen_tablebase_family != 'atomic'
            or type(test.datagen_tablebase_max) is not int
            or test.datagen_tablebase_max not in range(3, 7)
            or not isinstance(test.datagen_tablebase_manifest_sha256, str)
            or not _SHA256.fullmatch(test.datagen_tablebase_manifest_sha256)
            or test.datagen_teacher_mode not in {'pure', 'true'}
        ):
            raise PublicationContractError(
                'DATAGEN publication Syzygy identity is malformed'
            )
    elif (
        test.datagen_tablebase_family
        or test.datagen_tablebase_max != 0
        or test.datagen_tablebase_manifest_sha256
        or test.datagen_teacher_mode
    ):
        raise PublicationContractError(
            'DATAGEN publication has unexpected Syzygy identity'
        )

    if (
        protocol == DATAGEN_PUBLICATION_PROTOCOL_V42
        and (
            tablebase_required
            or test.datagen_teacher_mode
            or test.datagen_tablebase_family
            or test.datagen_tablebase_max != 0
            or test.datagen_tablebase_manifest_sha256
        )
    ):
        raise PublicationContractError(
            'DATAGEN protocol 42 requires the builtin material teacher'
        )

    document = {
        'schema': publication_contract_schema(protocol),
        'protocol': protocol,
        'campaign_id': campaign_id,
        'external_workload_id': workload_id,
        'role': test.datagen_role,
        'cohort': test.datagen_cohort,
        'engine': {
            'name': test.dev_engine,
            'repo': test.dev_repo,
            'source': test.dev.source,
            'requested_ref': test.dev.name,
            'commit': commit,
            'bench': test.dev.bench,
            'options': test.dev_options,
        },
        'network': dict(network),
        'book': dict(book),
        'generation': {
            'command': command,
            'command_sha256': hashlib.sha256(command.encode('utf-8')).hexdigest(),
            'total_count': total_count,
            'positions_per_chunk': positions_per_chunk,
            'base_seed': base_seed,
            'seed_method': DATAGEN_SEED_METHOD,
        },
        'producer': {
            'required': producer_required,
            'contract_sha256': producer_contract,
        },
        'teacher': (
            {'mode': test.datagen_teacher_mode or None}
            if protocol == DATAGEN_PUBLICATION_PROTOCOL else
            {
                'kind': 'builtin-evaluator',
                'id': test.datagen_teacher_id,
            }
        ),
        'syzygy': {
            'required': tablebase_required,
            'family': test.datagen_tablebase_family or None,
            'max': test.datagen_tablebase_max,
            'manifest_sha256': (
                test.datagen_tablebase_manifest_sha256 or None
            ),
            'environment_contract_sha256': (
                environment_contract
            ),
        },
    }
    if protocol == DATAGEN_PUBLICATION_PROTOCOL_V42:
        document['format'] = {
            'id': 'TK01-v1',
            'max_game_plies': 20000,
            'repetition': 'fourth-position-v1',
        }
    return document


def frozen_asset_identities(test):
    if (
        test.datagen_publication_protocol
        == DATAGEN_PUBLICATION_PROTOCOL_V42
    ):
        if test.datagen_network_sha256 or test.datagen_network_bytes:
            raise PublicationContractError(
                'DATAGEN network:none frozen identity is malformed'
            )
        network = network_none_identity()
    else:
        network = {
            'name': test.dev_netname,
            'openbench_id': test.dev_network.upper(),
            'sha256': test.datagen_network_sha256,
            'bytes': test.datagen_network_bytes,
        }
    if test.datagen_book_kind == 'builtin-startpos':
        if (
            test.book_name != 'NONE'
            or test.datagen_book_source
            or test.datagen_book_text_sha256
            or test.datagen_book_raw_sha256
        ):
            raise PublicationContractError(
                'DATAGEN startpos frozen identity is malformed'
            )
        book = {
            'kind': 'builtin-startpos',
            'name': 'NONE',
            'source': None,
            'text_sha256': None,
            'raw_sha256': None,
        }
    else:
        book = {
            'kind': test.datagen_book_kind,
            'name': test.book_name,
            'source': test.datagen_book_source,
            'text_sha256': test.datagen_book_text_sha256,
            'raw_sha256': test.datagen_book_raw_sha256,
        }
    return network, book


def publication_contract_is_current(test):
    protocol = getattr(test, 'datagen_publication_protocol', 0)
    publication_fields = (
        getattr(test, 'datagen_campaign_id', ''),
        getattr(test, 'datagen_external_workload_id', ''),
        getattr(test, 'datagen_role', ''),
        getattr(test, 'datagen_cohort', ''),
        getattr(test, 'datagen_publication_contract', {}),
        getattr(test, 'datagen_publication_contract_sha256', ''),
        getattr(test, 'datagen_network_sha256', ''),
        getattr(test, 'datagen_network_bytes', 0),
        getattr(test, 'datagen_book_kind', ''),
        getattr(test, 'datagen_book_source', ''),
        getattr(test, 'datagen_book_text_sha256', ''),
        getattr(test, 'datagen_book_raw_sha256', ''),
        getattr(test, 'datagen_teacher_id', ''),
    )
    if protocol == 0:
        return not any(publication_fields)
    if protocol not in DATAGEN_PUBLICATION_PROTOCOLS:
        return False
    document = getattr(test, 'datagen_publication_contract', None)
    digest = getattr(test, 'datagen_publication_contract_sha256', None)
    if (
        not isinstance(document, dict)
        or not isinstance(digest, str)
        or not _SHA256.fullmatch(digest)
    ):
        return False
    try:
        network, book = frozen_asset_identities(test)
        expected = build_publication_contract(test, network, book)
        return (
            document == expected
            and canonical_json_sha256(document) == digest
        )
    except (AttributeError, PublicationContractError, TypeError, ValueError):
        return False


def add_manifest_hash(document):
    if 'manifest_sha256' in document:
        raise PublicationContractError('DATAGEN manifest already contains a self hash')
    result = dict(document)
    result['manifest_sha256'] = canonical_json_sha256(document)
    return result


def rendered_datagen_command(test, chunk, producer_sha256, threads):
    """Render the exact v42 command independently of the client."""

    if (
        test.datagen_publication_protocol
        != DATAGEN_PUBLICATION_PROTOCOL_V42
    ):
        raise PublicationContractError(
            'Rendered command evidence is only defined for protocol 42'
        )
    if (
        not isinstance(producer_sha256, str)
        or not _SHA256.fullmatch(producer_sha256)
        or type(threads) is not int
        or threads <= 0
    ):
        raise PublicationContractError(
            'DATAGEN protocol 42 generation evidence is malformed'
        )
    output_path = 'Datagen/test_%d_chunk_%d.bin' % (test.id, chunk.idx)
    values = {
        'SEED': str(chunk.seed()),
        'COUNT': str(chunk.position_count),
        'OUT': output_path,
        'THREADS': str(threads),
        'BOOK': 'NONE',
        'BOOK_SHA256': 'NONE',
        'NETWORK': 'NONE',
        'NETWORK_SHA256': 'NONE',
        'PRODUCER_SHA256': producer_sha256,
        'SYZYGY': 'NONE',
        'SYZYGY_MANIFEST_SHA256': 'NONE',
        'SYZYGY_MAX': '0',
        'TEACHER_MODE': 'NONE',
        'TEACHER_ID': test.datagen_teacher_id,
        'ENGINE_COMMIT': str(test.dev.sha).lower(),
        'PUBLICATION_CONTRACT_SHA256': (
            test.datagen_publication_contract_sha256.lower()
        ),
    }
    try:
        return test.datagen_command.format_map(values)
    except (KeyError, ValueError) as error:
        raise PublicationContractError(
            'DATAGEN protocol 42 command cannot be rendered'
        ) from error


def rendered_datagen_command_sha256(test, chunk, producer_sha256, threads):
    command = rendered_datagen_command(
        test, chunk, producer_sha256, threads,
    )
    return hashlib.sha256(command.encode('utf-8')).hexdigest()
