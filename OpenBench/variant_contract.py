KNOWN_VARIANT_CONTRACTS = {
    'LICHESS_CRAZYHOUSE_2026_08_12',
    'LICHESS_HORDE_V1',
}

HORDE_ENGINE_NAMES = {
    'Horde-Stockfish',
    'Fairy-Stockfish-Hordetest-Baseline',
}

CRAZYHOUSE_ENGINE_NAMES = {
    'Crazyhouse-Stockfish',
}

PROTECTED_VARIANT_FAMILIES = (
    (
        'Crazyhouse',
        CRAZYHOUSE_ENGINE_NAMES,
        'CRAZYHOUSE',
        'LICHESS_CRAZYHOUSE_2026_08_12',
    ),
    ('Horde', HORDE_ENGINE_NAMES, 'HORDE', 'LICHESS_HORDE_V1'),
)


class VariantContractError(ValueError):
    pass


def configured_variant_contract(config, dev_engine, base_engine, book_name):

    engines = config['engines']
    books = config['books']
    contracts = {
        engines[dev_engine].get('variant_contract'),
        engines[base_engine].get('variant_contract'),
        books.get(book_name, {}).get('variant_contract'),
    }

    if len(contracts) != 1:
        raise VariantContractError(
            'Engine/book variant contracts disagree for this workload'
        )

    contract = contracts.pop()
    required = {
        required_contract: family
        for family, engine_names, book_token, required_contract
        in PROTECTED_VARIANT_FAMILIES
        if (
            dev_engine in engine_names
            or base_engine in engine_names
            or book_token in book_name.upper()
        )
    }

    if len(required) > 1:
        raise VariantContractError(
            'Workload matches multiple protected variant families: %s'
            % ', '.join(sorted(required.values()))
        )

    if required:
        required_contract, family = next(iter(required.items()))
        if contract != required_contract:
            raise VariantContractError(
                '%s workloads require variant_contract=%s'
                % (family, required_contract)
            )

    if contract is not None and contract not in KNOWN_VARIANT_CONTRACTS:
        raise VariantContractError('Unknown variant contract: %s' % contract)

    return contract


def persisted_variant_contract(config, workload):

    configured = configured_variant_contract(
        config,
        workload.dev_engine,
        workload.base_engine,
        workload.book_name,
    )
    persisted = getattr(workload, 'variant_contract', '') or None

    if configured is not None and persisted is None:
        raise VariantContractError(
            'Variant contract was not persisted with this workload'
        )
    if configured != persisted:
        raise VariantContractError(
            'Persisted variant contract no longer matches configuration'
        )

    return persisted
