import datetime
import math
import statistics
import threading
from collections import deque

from django.db.models import F, Min, Q, Sum
from django.utils import timezone

from OpenBench.models import DatagenChunk, Machine, Result, Test


LIVE_MACHINE_WINDOW = datetime.timedelta(minutes=3)
RESULT_RATE_WINDOW = datetime.timedelta(minutes=10)
METRICS_CACHE_TTL = datetime.timedelta(seconds=30)
SPRT_GAMES_FALLBACK = 15000


_cache_timestamp = None
_cache_value = None
_result_samples = deque()
_sprt_expected_games = None
_state_lock = threading.Lock()


def reset_index_metrics_state():
    """Clear process-local state. Public so deterministic tests can reset it."""

    global _cache_timestamp, _cache_value, _sprt_expected_games
    with _state_lock:
        _cache_timestamp = None
        _cache_value = None
        _result_samples.clear()
        _sprt_expected_games = None


def get_index_metrics(now=None):
    """Return the four cached capacity metrics rendered on the main index."""

    global _cache_timestamp, _cache_value
    now = now or timezone.now()

    with _state_lock:
        cache_age = now - _cache_timestamp if _cache_timestamp else None
        if (
            _cache_value is not None
            and cache_age is not None
            and datetime.timedelta(0) <= cache_age < METRICS_CACHE_TTL
        ):
            return _cache_value

        _cache_value = _compute_index_metrics(now)
        _cache_timestamp = now
        return _cache_value


def _compute_index_metrics(now):
    machines = list(
        Machine.objects.filter(updated__gte=now - LIVE_MACHINE_WINDOW)
        .values_list('info', 'mnps')
    )

    cores = 0
    nodes_per_second = 0.0
    for info, mnps in machines:
        concurrency = _nonnegative_int((info or {}).get('concurrency', 0))
        per_thread_nps = max(_number(mnps), 0.0) * 1e6
        cores += concurrency
        # clientSubmitNPS is the mean speed of one of the concurrent benches.
        # Expanding it by worker concurrency yields the machine's total rate.
        nodes_per_second += concurrency * per_thread_nps

    games_per_minute, games_rate_source = _sample_games_per_minute(now)
    expected_sprt_games = _historical_sprt_games()
    work = _remaining_work(now, expected_sprt_games)

    remaining_seconds = _remaining_seconds(
        bool(machines),
        games_per_minute,
        work['game_remaining'],
        work['datagen_seconds'],
        work['datagen_rate_unknown'],
        work['excluded_spsa'],
    )

    if not machines:
        remaining_display = '—'
    elif remaining_seconds is None:
        remaining_display = '—'
    elif math.isinf(remaining_seconds):
        remaining_display = '∞'
    else:
        remaining_display = _format_duration(remaining_seconds)

    games_tooltip = (
        '10-minute rolling delta of Result.games sampled in memory; '
        'the database has only each row\'s latest updated timestamp.'
        if games_rate_source == 'rolling'
        else
        '10-minute rate warming up: this first process sample counts only '
        'recently updated results from tests created inside the window.'
    )

    partial = (
        ' %d SPSA workload(s) without usable iterations/pairs_per are excluded.'
        % work['excluded_spsa']
        if work['excluded_spsa']
        else ''
    )
    time_tooltip = (
        'ESTIMATE: generic DATAGEN uses remaining positions divided by each '
        'test\'s measured positions/sec; gameplay uses remaining expected games '
        'divided by the 10-minute games/min rate. Active SPRTs assume %.1f total '
        'games (resolved-history median; %d fallback).%s'
        % (expected_sprt_games, SPRT_GAMES_FALLBACK, partial)
    )
    if not machines:
        time_tooltip += ' No machine heartbeat was seen in the last 3 minutes.'

    cards = [
        {
            'slug': 'cores',
            'label': 'Cores',
            'value': str(cores),
            'tooltip': 'Worker threads from machines updated in the last 3 minutes.',
        },
        {
            'slug': 'nodes-sec',
            'label': 'Nodes/sec',
            'value': _format_si(nodes_per_second),
            'tooltip': (
                'Sum of clientSubmitNPS across live worker threads '
                '(reported per-thread speed multiplied by concurrency).'
            ),
        },
        {
            'slug': 'games-min',
            'label': 'Games/min',
            'value': _format_rate(games_per_minute),
            'tooltip': games_tooltip,
        },
        {
            'slug': 'time-remaining',
            'label': 'Time remaining',
            'value': remaining_display,
            'tooltip': time_tooltip,
        },
    ]

    return {
        'cards': cards,
        'cores': cores,
        'live_machines': len(machines),
        'nodes_per_second': nodes_per_second,
        'games_per_minute': games_per_minute,
        'games_rate_source': games_rate_source,
        'sprt_expected_games': expected_sprt_games,
        'game_remaining': work['game_remaining'],
        'datagen_remaining_positions': work['datagen_remaining_positions'],
        'datagen_positions_per_second': work['datagen_positions_per_second'],
        'excluded_spsa': work['excluded_spsa'],
        'time_remaining_seconds': remaining_seconds,
    }


def _sample_games_per_minute(now):
    """Measure Result.games deltas over ten minutes without schema changes.

    Result is cumulative and stores only its last update timestamp. A process-local
    sequence of snapshots therefore provides real deltas after warm-up. On the
    first sample, only tests created within the window have a fully attributable
    game count; older cumulative rows are deliberately not guessed.
    """

    cutoff = now - RESULT_RATE_WINDOW
    active_results = Q(
        test__approved=True,
        test__awaiting=False,
        test__finished=False,
        test__deleted=False,
    )
    rows = list(
        Result.objects.filter(Q(updated__gte=cutoff) | active_results)
        .values('id', 'games', 'updated', 'test__creation')
    )
    snapshot = {
        row['id']: (max(row['games'], 0), row['updated'])
        for row in rows
    }

    while _result_samples and _result_samples[0][0] < cutoff:
        _result_samples.popleft()
    _result_samples.append((now, snapshot))

    if len(_result_samples) >= 2:
        baseline_time, baseline = _result_samples[0]
        elapsed_minutes = (now - baseline_time).total_seconds() / 60.0
        if elapsed_minutes > 0:
            games_delta = sum(
                max(games - baseline.get(result_id, (0, None))[0], 0)
                for result_id, (games, _updated) in snapshot.items()
            )
            return games_delta / elapsed_minutes, 'rolling'

    attributable = [
        row for row in rows
        if row['updated'] >= cutoff and row['test__creation'] >= cutoff
    ]
    if not attributable:
        return 0.0, 'cold-start'

    earliest = min(row['test__creation'] for row in attributable)
    elapsed_minutes = (now - earliest).total_seconds() / 60.0
    if elapsed_minutes <= 0:
        return 0.0, 'cold-start'
    return (
        sum(max(row['games'], 0) for row in attributable) / elapsed_minutes,
        'cold-start',
    )


def _historical_sprt_games():
    """Load the resolved-SPRT median once per server process."""

    global _sprt_expected_games
    if _sprt_expected_games is not None:
        return _sprt_expected_games

    resolved = (
        Test.objects.filter(
            test_mode='SPRT',
            finished=True,
            deleted=False,
            games__gt=0,
            upperllr__gt=F('lowerllr'),
        )
        .filter(Q(currentllr__lte=F('lowerllr')) | Q(currentllr__gte=F('upperllr')))
        .values_list('games', flat=True)
    )
    history = list(resolved)
    _sprt_expected_games = (
        float(statistics.median(history)) if history else float(SPRT_GAMES_FALLBACK)
    )
    return _sprt_expected_games


def _remaining_work(now, expected_sprt_games):
    tests = list(
        Test.objects.filter(
            approved=True,
            awaiting=False,
            finished=False,
            deleted=False,
        ).only(
            'id', 'test_mode', 'games', 'max_games', 'spsa',
            'datagen_command', 'datagen_total_count',
        )
    )

    game_remaining = 0.0
    excluded_spsa = 0
    datagen_tests = []

    for test in tests:
        if test.is_generic_datagen():
            datagen_tests.append(test)
        elif test.test_mode == 'SPRT':
            game_remaining += max(expected_sprt_games - test.games, 0.0)
        elif test.test_mode == 'SPSA':
            iterations = _positive_int((test.spsa or {}).get('iterations'))
            pairs_per = _positive_int((test.spsa or {}).get('pairs_per'))
            if iterations is None or pairs_per is None:
                excluded_spsa += 1
                continue
            game_remaining += max(2 * iterations * pairs_per - test.games, 0)
        elif test.test_mode in ['GAMES', 'DATAGEN']:
            # Fixed-game tests and historical (non-generic) DATAGEN have an
            # exact target in max_games and share the normal result pipeline.
            game_remaining += max(test.max_games - test.games, 0)

    datagen_remaining = 0
    datagen_seconds = 0.0
    datagen_positions_per_second = 0.0
    datagen_rate_unknown = False

    if datagen_tests:
        completed = {
            row['test_id']: row
            for row in (
                DatagenChunk.objects.filter(
                    test_id__in=[test.id for test in datagen_tests],
                    status=DatagenChunk.COMPLETED,
                )
                .values('test_id')
                .annotate(
                    completed_positions=Sum('position_count'),
                    first_completed=Min('completed'),
                )
            )
        }

        for test in datagen_tests:
            total = max(test.datagen_total_count, 0)
            progress = completed.get(test.id, {})
            done = min(max(progress.get('completed_positions') or 0, 0), total)
            remaining = total - done
            datagen_remaining += remaining

            if remaining <= 0:
                continue

            first_completed = progress.get('first_completed')
            elapsed = (
                (now - first_completed).total_seconds()
                if first_completed is not None
                else 0.0
            )
            rate = done / elapsed if done > 0 and elapsed > 0 else 0.0
            if rate <= 0:
                datagen_rate_unknown = True
                continue

            datagen_positions_per_second += rate
            datagen_seconds += remaining / rate

    return {
        'game_remaining': game_remaining,
        'excluded_spsa': excluded_spsa,
        'datagen_remaining_positions': datagen_remaining,
        'datagen_positions_per_second': datagen_positions_per_second,
        'datagen_seconds': datagen_seconds,
        'datagen_rate_unknown': datagen_rate_unknown,
    }


def _remaining_seconds(
    has_live_machines,
    games_per_minute,
    game_remaining,
    datagen_seconds,
    datagen_rate_unknown,
    excluded_spsa,
):
    if not has_live_machines:
        return None

    if game_remaining > 0 and games_per_minute <= 0:
        return math.inf
    if datagen_rate_unknown:
        return math.inf

    known_seconds = datagen_seconds
    if game_remaining > 0:
        known_seconds += 60.0 * game_remaining / games_per_minute

    # If every active workload is an unestimable SPSA, avoid claiming zero.
    if excluded_spsa and known_seconds == 0:
        return None
    return known_seconds


def _number(value):
    if isinstance(value, bool):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _nonnegative_int(value):
    if isinstance(value, bool):
        return 0
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return 0


def _positive_int(value):
    parsed = _nonnegative_int(value)
    return parsed if parsed > 0 else None


def _format_si(value):
    value = max(_number(value), 0.0)
    for scale, suffix in [(1e12, 'T'), (1e9, 'G'), (1e6, 'M'), (1e3, 'K')]:
        if value >= scale:
            scaled = value / scale
            decimals = 0 if scaled >= 100 else 1 if scaled >= 10 else 2
            return _trim_decimal('%.*f' % (decimals, scaled)) + suffix
    return str(int(round(value)))


def _format_rate(value):
    value = max(_number(value), 0.0)
    decimals = 0 if value >= 100 else 1 if value >= 10 else 2
    return _trim_decimal('%.*f' % (decimals, value)) or '0'


def _trim_decimal(value):
    return value.rstrip('0').rstrip('.') if '.' in value else value


def _format_duration(seconds):
    seconds = max(int(round(seconds)), 0)
    if seconds == 0:
        return '0m'
    if seconds < 60:
        return '<1m'

    minutes = (seconds + 30) // 60
    days, minutes = divmod(minutes, 24 * 60)
    hours, minutes = divmod(minutes, 60)
    if days:
        return '%dd %dh' % (days, hours)
    if hours:
        return '%dh %dm' % (hours, minutes)
    return '%dm' % minutes
