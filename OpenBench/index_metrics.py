import datetime
import re
import statistics
import threading
from collections import defaultdict, deque

from django.db.models import F, Q
from django.utils import timezone

from OpenBench.models import DatagenChunk, Machine, Result, Test


LIVE_MACHINE_WINDOW = datetime.timedelta(minutes=3)
RESULT_RATE_WINDOW = datetime.timedelta(minutes=10)
METRICS_CACHE_TTL = datetime.timedelta(seconds=30)
SPRT_GAMES_FALLBACK = 15000

# Steady-state datagen rate: sliding window over chunk completion timestamps.
DATAGEN_RATE_WINDOW = datetime.timedelta(hours=3)
# Fallback for datagen without a measurable or historical rate: kept positions
# cost about this many times the nominal `nodes` per position once filters,
# random-move replays and discarded games are paid for. Measured on run8
# (2026-07-22): fleet 15.1M nps, nodes 10000, 84 positions/sec -> 17.96.
DATAGEN_NODES_OVERHEAD = 18.0
# Last-resort nodes assumption when the command has no parsable `nodes` token.
DATAGEN_ASSUMED_NODES = 10000
# Historical per-test rates need a real time span: same-second or backfilled
# completion timestamps would otherwise fabricate absurd rates.
DATAGEN_HISTORY_MIN_SPAN = datetime.timedelta(minutes=30)
# Capacity fallback when no gameplay rate was ever measured: one game occupies
# one worker thread for roughly this long at the STC-ish controls in use here.
SECONDS_PER_GAME_THREAD = 45.0
# Below this fraction of LLR progress an SPRT is treated as stalled and floored
# instead of extrapolated (games/progress explodes as progress -> 0).
SPRT_MIN_LLR_PROGRESS = 0.2
SPRT_STALLED_FLOOR = 0.25
SPRT_EXPECTED_CAP = 4.0

_NODES_RE = re.compile(r'\bnodes\s+(\d+)')


_cache_timestamp = None
_cache_value = None
_result_samples = deque()
_sprt_expected_games = None
_historical_datagen_rates = None
_last_gameplay_rate = None
_state_lock = threading.Lock()


def reset_index_metrics_state():
    """Clear process-local state. Public so deterministic tests can reset it."""

    global _cache_timestamp, _cache_value, _sprt_expected_games
    global _historical_datagen_rates, _last_gameplay_rate
    with _state_lock:
        _cache_timestamp = None
        _cache_value = None
        _result_samples.clear()
        _sprt_expected_games = None
        _historical_datagen_rates = None
        _last_gameplay_rate = None


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
    global _last_gameplay_rate

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
    if games_per_minute > 0:
        _last_gameplay_rate = (games_per_minute, now)

    expected_sprt_games = _historical_sprt_games()
    work = _remaining_work(now, expected_sprt_games, nodes_per_second)

    gameplay_seconds, gameplay_estimated = _gameplay_seconds(
        cores, games_per_minute, work['game_remaining']
    )

    # Absolute by design: every queued workload contributes its best (and,
    # when uncertain, pessimistic) estimate. No bounds, no infinities.
    if not machines:
        remaining_seconds = None
        remaining_display = '—'
    else:
        remaining_seconds = work['datagen_seconds'] + gameplay_seconds
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
        ' %d SPSA workload(s) without usable iterations/pairs_per are '
        'assumed at the SPRT-median games.'
        % work['excluded_spsa']
        if work['excluded_spsa']
        else ''
    )
    time_tooltip = (
        'ESTIMATE: full queue, priority-sequential, always absolute — when a '
        'rate is uncertain the pessimistic candidate wins. Generic DATAGEN '
        'uses the measured chunk-completion rate (3h sliding window, first '
        'chunk anchors the clock); without 2 completed chunks it uses the '
        'SAME variant\'s historical rate (per-engine median over tests with '
        '2+ completed chunks spanning 30m+, idle time included), else '
        'fleet-NPS / (nodes × %.0f), assuming %d nodes when the command '
        'names none. '
        'Gameplay divides remaining expected games by the live 10-minute '
        'rate, else the last measured rate, else a %.0fs-per-game-thread '
        'capacity model. Active SPRTs assume the resolved-history median '
        '(%.1f; %d fallback), extrapolated by LLR progress and floored at '
        '%.0f%% of the median when stalled.%s'
        % (
            DATAGEN_NODES_OVERHEAD,
            DATAGEN_ASSUMED_NODES,
            SECONDS_PER_GAME_THREAD,
            expected_sprt_games,
            SPRT_GAMES_FALLBACK,
            100 * SPRT_STALLED_FLOOR,
            partial,
        )
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
        'gameplay_estimated': gameplay_estimated,
        'datagen_remaining_positions': work['datagen_remaining_positions'],
        'datagen_positions_per_second': work['datagen_positions_per_second'],
        'datagen_estimated': work['datagen_estimated'],
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


def _historical_datagen_rates_per_engine():
    """Median historical datagen rate per engine (variant), pos/sec.

    Datagen pipelines differ wildly per variant (measured on this server:
    spell run7 36.8 pos/s over 6 days including worker downtime, atomic 375M
    positions at 7192 pos/s in 14h), so a cross-variant prior is useless.
    Each test with 2+ completed chunks spanning at least 30 minutes yields
    one all-time interval rate — idle time priced in, deliberately
    pessimistic — and each engine takes the median over its own tests.
    Computed once per server process.
    """

    global _historical_datagen_rates
    if _historical_datagen_rates is not None:
        return _historical_datagen_rates

    per_test = defaultdict(list)
    rows = (
        DatagenChunk.objects.filter(status=DatagenChunk.COMPLETED)
        .exclude(completed=None)
        .order_by('completed')
        .values_list('test_id', 'completed', 'position_count')
    )
    for test_id, completed, count in rows:
        per_test[test_id].append((completed, max(count, 0)))

    engines = dict(
        Test.objects.filter(id__in=list(per_test))
        .values_list('id', 'dev_engine')
    )

    rates_per_engine = defaultdict(list)
    for test_id, completions in per_test.items():
        if len(completions) < 2:
            continue
        span = (completions[-1][0] - completions[0][0]).total_seconds()
        if span < DATAGEN_HISTORY_MIN_SPAN.total_seconds():
            continue
        positions = sum(count for _completed, count in completions[1:])
        if positions > 0 and engines.get(test_id):
            rates_per_engine[engines[test_id]].append(positions / span)

    _historical_datagen_rates = {
        engine: statistics.median(rates)
        for engine, rates in rates_per_engine.items()
    }
    return _historical_datagen_rates


def _sprt_remaining_games(test, median):
    """Expected further games for one active SPRT.

    An SPRT with LLR well on its way is extrapolated linearly (LLR drift is
    linear in games for a fixed true elo), capped so a lucky early spike cannot
    predict a 100x runtime. A stalled SPRT (LLR hovering near zero) is the
    expensive case: it gets the median profile, floored so tests already past
    the median never report zero remaining work.
    """

    progress = 0.0
    if test.games > 0:
        if test.upperllr > 0:
            progress = max(progress, test.currentllr / test.upperllr)
        if test.lowerllr < 0:
            progress = max(progress, test.currentllr / test.lowerllr)
    progress = min(max(progress, 0.0), 1.0)

    if progress >= SPRT_MIN_LLR_PROGRESS:
        expected_total = min(test.games / progress, SPRT_EXPECTED_CAP * median)
        return max(expected_total - test.games, 0.0)
    return max(median - test.games, SPRT_STALLED_FLOOR * median)


def _measured_datagen_rate(completions, now):
    """Positions/sec from inter-completion intervals, None below 2 samples.

    The first completed chunk only anchors the clock: its positions were
    generated before its timestamp, so counting them would inflate the rate
    (the pre-fix bug: right after the first chunk the card claimed hours for
    a week-long run). A sliding window keeps the estimate honest when the
    fleet grows or shrinks mid-run.
    """

    if len(completions) < 2:
        return None
    recent = [c for c in completions if c[0] >= now - DATAGEN_RATE_WINDOW]
    if len(recent) < 2:
        recent = completions
    span = (recent[-1][0] - recent[0][0]).total_seconds()
    if span <= 0:
        return None
    positions = sum(count for _completed, count in recent[1:])
    return positions / span if positions > 0 else None


def _heuristic_datagen_rate(command, fleet_nps):
    """Fleet-capacity guess from the command's nodes-per-position budget."""

    if fleet_nps <= 0:
        return None
    match = _NODES_RE.search(command or '')
    nodes = int(match.group(1)) if match else DATAGEN_ASSUMED_NODES
    if nodes <= 0:
        nodes = DATAGEN_ASSUMED_NODES
    return fleet_nps / (nodes * DATAGEN_NODES_OVERHEAD)


def _fallback_datagen_rate(test, fleet_nps):
    """Best estimate for datagen without a measured rate.

    Real history of the SAME variant beats any guess: it embeds the true
    filter overheads and worker downtime of that pipeline. Only a variant
    with no datagen history at all falls back to the nodes heuristic.
    """

    variant_rate = _historical_datagen_rates_per_engine().get(test.dev_engine)
    if variant_rate:
        return variant_rate
    return _heuristic_datagen_rate(test.datagen_command, fleet_nps)


def _remaining_work(now, expected_sprt_games, fleet_nps):
    tests = list(
        Test.objects.filter(
            approved=True,
            awaiting=False,
            finished=False,
            deleted=False,
        ).only(
            'id', 'test_mode', 'games', 'max_games', 'spsa',
            'currentllr', 'lowerllr', 'upperllr', 'dev_engine',
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
            game_remaining += _sprt_remaining_games(test, expected_sprt_games)
        elif test.test_mode == 'SPSA':
            iterations = _positive_int((test.spsa or {}).get('iterations'))
            pairs_per = _positive_int((test.spsa or {}).get('pairs_per'))
            if iterations is None or pairs_per is None:
                # No metadata to size it: charge the SPRT-median profile
                # rather than dropping the workload from the total.
                excluded_spsa += 1
                game_remaining += expected_sprt_games
                continue
            game_remaining += max(2 * iterations * pairs_per - test.games, 0)
        elif test.test_mode in ['GAMES', 'DATAGEN']:
            # Fixed-game tests and historical (non-generic) DATAGEN have an
            # exact target in max_games and share the normal result pipeline.
            game_remaining += max(test.max_games - test.games, 0)

    datagen_remaining = 0
    datagen_seconds = 0.0
    datagen_positions_per_second = 0.0
    datagen_estimated = False

    if datagen_tests:
        completions = defaultdict(list)
        rows = (
            DatagenChunk.objects.filter(
                test_id__in=[test.id for test in datagen_tests],
                status=DatagenChunk.COMPLETED,
            )
            .exclude(completed=None)
            .order_by('completed')
            .values_list('test_id', 'completed', 'position_count')
        )
        for test_id, completed, count in rows:
            completions[test_id].append((completed, max(count, 0)))

        for test in datagen_tests:
            total = max(test.datagen_total_count, 0)
            done = min(
                sum(count for _completed, count in completions[test.id]),
                total,
            )
            remaining = total - done
            datagen_remaining += remaining

            if remaining <= 0:
                continue

            rate = _measured_datagen_rate(completions[test.id], now)
            if rate is None:
                rate = _fallback_datagen_rate(test, fleet_nps)
                if rate is None:
                    # Only reachable with zero live fleet NPS and no history;
                    # the card renders '—' without live machines anyway.
                    continue
                datagen_estimated = True

            datagen_positions_per_second += rate
            datagen_seconds += remaining / rate

    return {
        'game_remaining': game_remaining,
        'excluded_spsa': excluded_spsa,
        'datagen_remaining_positions': datagen_remaining,
        'datagen_positions_per_second': datagen_positions_per_second,
        'datagen_seconds': datagen_seconds,
        'datagen_estimated': datagen_estimated,
    }


def _gameplay_seconds(cores, games_per_minute, game_remaining):
    """Seconds for queued gameplay -> (seconds, heuristic flag). Never None.

    While a max-priority datagen monopolizes every worker the live gameplay
    rate is zero, but the queued SPRTs still cost real time once the datagen
    drains. Prefer the last measured rate this process saw; fall back to a
    per-thread capacity model (at least one thread) so the queue always
    contributes an absolute number.
    """

    if game_remaining <= 0:
        return 0.0, False
    if games_per_minute > 0:
        return 60.0 * game_remaining / games_per_minute, False
    if _last_gameplay_rate is not None:
        rate, _measured_at = _last_gameplay_rate
        return 60.0 * game_remaining / rate, True
    capacity = max(cores, 1) * 60.0 / SECONDS_PER_GAME_THREAD
    return 60.0 * game_remaining / capacity, True


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
