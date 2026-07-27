"""Lazy server-side access to the Atomic Syzygy tablebases.

The public worker in ``Client/atomicdb_worker.py`` treats a probe as
unavailable when castling rights or an en-passant square are present, when
there are too many pieces, or when python-chess cannot probe the position.
This module deliberately keeps the same fail-closed contract: ``probe_wdl``
returns ``None`` for every non-result and a python-chess WDL integer otherwise.

``TABLEBASE_FACTORY`` and ``BOARD_FACTORY`` are module-level injection hooks.
Tests can replace them without installing python-chess or providing tablebase
files.  The production imports remain lazy, so importing the Django app does
not open files or require python-chess until the first eligible probe.
"""

import logging
import threading

from django.conf import settings


LOGGER = logging.getLogger(__name__)

_CACHE_UNSET = object()
_tablebase = _CACHE_UNSET
_tablebase_lock = threading.Lock()


def is_applicable(fen, max_pieces=6):
    """Return whether ``fen`` has the same probe shape accepted by the worker."""
    try:
        parts = fen.split()
        return (parts[2] == '-'
                and parts[3] == '-'
                and sum(ch.isalpha() for ch in parts[0]) <= max_pieces)
    except (AttributeError, IndexError):
        return False


def _open_tablebase(paths):
    """Open the configured directories with AtomicBoard semantics."""
    if not paths:
        return None

    import chess.syzygy
    import chess.variant

    tablebase = chess.syzygy.open_tablebase(
        paths[0], VariantBoard=chess.variant.AtomicBoard)
    for path in paths[1:]:
        tablebase.add_directory(path)
    return tablebase


def _atomic_board(fen):
    import chess.variant

    return chess.variant.AtomicBoard(fen)


# Public injection hooks.  Replace these before the first probe and call
# reset_cache() after each test that changes them.
TABLEBASE_FACTORY = _open_tablebase
BOARD_FACTORY = _atomic_board


def _configured_paths():
    configured = getattr(settings, 'ATOMICDB_TB_PATHS', ())
    if isinstance(configured, str):
        configured = configured.split(';')
    return tuple(path for path in configured if path)


def _get_tablebase():
    global _tablebase

    if _tablebase is not _CACHE_UNSET:
        return _tablebase

    with _tablebase_lock:
        if _tablebase is _CACHE_UNSET:
            try:
                _tablebase = TABLEBASE_FACTORY(_configured_paths())
            except Exception:
                # Cache the failure until process restart/reset.  A missing or
                # corrupt local TB must reject the authoritative server probe,
                # not repeatedly hit disk or accidentally trust the worker.
                LOGGER.exception('Atomic Syzygy tablebase could not be opened')
                _tablebase = None
    return _tablebase


def probe_wdl(fen, max_pieces=6):
    """Probe Atomic WDL from the side-to-move perspective, or return ``None``.

    This intentionally matches the WDL half of
    ``Client.atomicdb_worker.probe_tb``: WDL is the raw value returned by
    python-chess (normally -2..2), and all unsupported or failed probes are
    represented by ``None``.  The worker additionally reports DTZ when its
    tables can give it, which is what lets a TB closure say for which entry
    clocks it holds; the server does not re-derive DTZ here, so a closure
    without one keeps ``clock_slack`` at zero.
    """
    if not is_applicable(fen, max_pieces=max_pieces):
        return None

    tablebase = _get_tablebase()
    if tablebase is None:
        return None

    try:
        return tablebase.probe_wdl(BOARD_FACTORY(fen))
    except Exception:
        return None


def reset_cache():
    """Clear the lazy tablebase instance (primarily for isolated tests)."""
    global _tablebase

    with _tablebase_lock:
        current = _tablebase
        _tablebase = _CACHE_UNSET
    if current is not _CACHE_UNSET and current is not None:
        close = getattr(current, 'close', None)
        if close is not None:
            close()
