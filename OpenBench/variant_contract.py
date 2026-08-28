KNOWN_VARIANT_CONTRACTS = {
    'LICHESS_HORDE_V1',
    'LICHESS_THREECHECK_V1',
}

HORDE_ENGINE_NAMES = {
    'Horde-Stockfish',
    'Fairy-Stockfish-Hordetest-Baseline',
}

THREECHECK_ENGINE_NAMES = {
    '3Check-Stockfish',
}

BOOKLESS = 'NONE'


class VariantContractError(ValueError):
    pass


def configured_variant_contract(config, dev_engine, base_engine, book_name):

    engines = config['engines']
    books = config['books']
    contracts = {
        engines[dev_engine].get('variant_contract'),
        engines[base_engine].get('variant_contract'),
    }
    if book_name != BOOKLESS:
        contracts.add(books.get(book_name, {}).get('variant_contract'))

    if len(contracts) != 1:
        raise VariantContractError(
            'Engine/book variant contracts disagree for this workload'
        )

    contract = contracts.pop()
    horde_workload = (
        dev_engine in HORDE_ENGINE_NAMES
        or base_engine in HORDE_ENGINE_NAMES
        or 'HORDE' in book_name.upper()
    )

    if horde_workload and contract != 'LICHESS_HORDE_V1':
        raise VariantContractError(
            'Horde workloads require variant_contract=LICHESS_HORDE_V1'
        )

    threecheck_workload = (
        dev_engine in THREECHECK_ENGINE_NAMES
        or base_engine in THREECHECK_ENGINE_NAMES
        or '3CHECK' in book_name.upper()
        or 'THREECHECK' in book_name.upper()
    )
    if threecheck_workload and contract != 'LICHESS_THREECHECK_V1':
        raise VariantContractError(
            '3Check workloads require variant_contract=LICHESS_THREECHECK_V1'
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
