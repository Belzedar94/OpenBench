"""Pure scheduling primitives for AtomicDB theory shadow scoring.

This module deliberately has no Django imports and performs no writes.  In
particular, neither engine evaluations nor theory priors can mutate a closure:
they only produce ordering components for a caller to observe or apply.

R0 keeps the production selector unchanged in shadow mode.  ``base_priority``
is the scalar formula currently used by ``atomicdb.ingest.refresh_priorities``;
``score_candidate`` additionally reports the bounded theory proposal while
returning the base value as ``selection_priority`` when ``shadow`` is true.
"""

from dataclasses import dataclass
import math
from types import MappingProxyType
from typing import Iterable, Mapping, Optional, Tuple, Union


MATE_BAND = 9_000
REGRET_WEIGHT = 3.0
DISCONNECTED_REGRET = 5.0
MAX_REGRET_CP = 3_000.0
MAX_EVAL_CP = 1_500
UNEXPANDED_BOOST = 2.0
VISIT_PENALTY = 1.5

THEORY_BOOST_CAP = 12.0
THEORY_TIER_WEIGHTS: Mapping[str, float] = MappingProxyType({
    "P0": 12.0,
    "P1": 8.0,
    "P2": 4.0,
    "P3": 1.0,
})

SOURCE_RANKS: Mapping[str, int] = MappingProxyType({
    "AUTO": 0,
    "THEORY": 1,
    "USER": 2,
})

DECAY_START_ATTEMPTS = 3.0
DECAY_ZERO_ATTEMPTS = 5.0
DECAY_START_CORE_HOURS = 25.0
DECAY_ZERO_CORE_HOURS = 50.0


@dataclass(frozen=True)
class TheoryCohort:
    """One independently identified theory cohort and its P0-P3 tier."""

    cohort_id: str
    tier: str


CohortInput = Union[TheoryCohort, str, Tuple[str, str]]


@dataclass(frozen=True)
class ScoreComponents:
    """Auditable output of one pure scheduling calculation."""

    base_priority: float
    uncapped_theory_boost: float
    capped_theory_boost: float
    decay_factor: float
    theory_boost: float
    proposed_priority: float
    selection_priority: float
    source: str
    source_rank: int
    shadow: bool


def base_priority(
        eval_cp: Optional[int],
        regret_cp: Optional[float],
        expanded: bool,
        visits: int) -> float:
    """Return the exact scalar priority currently computed by ingest.

    ``None`` and positive infinity both represent a position disconnected from
    the start-position regret graph.  Finite regret is capped at 3000 cp, as in
    the current selector.
    """

    evaluation = abs(eval_cp) if eval_cp is not None else 0
    disconnected = regret_cp is None or regret_cp == float("inf")
    regret_units = (
        DISCONNECTED_REGRET
        if disconnected
        else min(regret_cp, MAX_REGRET_CP) / 100.0
    )
    return (
        min(evaluation, MAX_EVAL_CP) / 100.0
        + (50.0 if evaluation >= MATE_BAND else 0.0)
        + (UNEXPANDED_BOOST if not expanded else 0.0)
        - REGRET_WEIGHT * regret_units
        - VISIT_PENALTY * visits
    )


def source_rank(source: str) -> int:
    """Return the explicit USER > THEORY > AUTO scheduling rank."""

    normalized = str(source).upper()
    try:
        return SOURCE_RANKS[normalized]
    except KeyError as exc:
        raise ValueError("unknown scheduling source: {!r}".format(source)) from exc


def _remaining_fraction(value: float, start: float, end: float) -> float:
    if value <= start:
        return 1.0
    if value >= end:
        return 0.0
    return (end - value) / (end - start)


def theory_decay_factor(
        attempts_since_progress: int = 0,
        core_hours_since_progress: float = 0.0) -> float:
    """Decay a stale theory prior along whichever limit is reached first.

    Decay begins after either three attempts or 25 core-hours without progress.
    It reaches zero at five attempts or 50 core-hours at the latest.  Taking the
    minimum remaining fraction makes both limits fail closed.
    """

    if attempts_since_progress < 0:
        raise ValueError("attempts_since_progress must be non-negative")
    if (not math.isfinite(core_hours_since_progress)
            or core_hours_since_progress < 0):
        raise ValueError("core_hours_since_progress must be finite and non-negative")

    attempt_fraction = _remaining_fraction(
        float(attempts_since_progress),
        DECAY_START_ATTEMPTS,
        DECAY_ZERO_ATTEMPTS,
    )
    hour_fraction = _remaining_fraction(
        float(core_hours_since_progress),
        DECAY_START_CORE_HOURS,
        DECAY_ZERO_CORE_HOURS,
    )
    return min(attempt_fraction, hour_fraction)


def _cohort_parts(cohort: CohortInput) -> Tuple[str, str]:
    if isinstance(cohort, TheoryCohort):
        cohort_id, tier = cohort.cohort_id, cohort.tier
    elif isinstance(cohort, str):
        cohort_id, tier = cohort, cohort
    else:
        try:
            cohort_id, tier = cohort
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "cohort must be a tier, TheoryCohort, or (id, tier)"
            ) from exc

    normalized_id = str(cohort_id)
    normalized_tier = str(tier).upper()
    if not normalized_id:
        raise ValueError("cohort_id must not be empty")
    if normalized_tier not in THEORY_TIER_WEIGHTS:
        raise ValueError("unknown theory tier: {!r}".format(tier))
    return normalized_id, normalized_tier


def normalize_theory_cohorts(
        cohorts: Iterable[CohortInput]) -> Tuple[TheoryCohort, ...]:
    """Deduplicate cohorts deterministically, retaining their strongest tier.

    Passing bare tiers is convenient for one cohort per tier.  Callers with
    several cohorts at the same tier should pass ``(cohort_id, tier)`` pairs.
    """

    strongest_by_id = {}
    for cohort in cohorts:
        cohort_id, tier = _cohort_parts(cohort)
        previous = strongest_by_id.get(cohort_id)
        if (previous is None
                or THEORY_TIER_WEIGHTS[tier] > THEORY_TIER_WEIGHTS[previous]):
            strongest_by_id[cohort_id] = tier
    return tuple(
        TheoryCohort(cohort_id, strongest_by_id[cohort_id])
        for cohort_id in sorted(strongest_by_id)
    )


def theory_boost_components(
        cohorts: Iterable[CohortInput],
        attempts_since_progress: int = 0,
        core_hours_since_progress: float = 0.0,
        max_boost: float = THEORY_BOOST_CAP,
        ) -> Tuple[float, float, float, float]:
    """Return uncapped, capped, decay, and final theory boosts."""

    if (not math.isfinite(max_boost)
            or not 0.0 <= max_boost <= THEORY_BOOST_CAP):
        raise ValueError("max_boost must be finite and between 0 and 12")
    normalized = normalize_theory_cohorts(cohorts)
    # Multiple studies are provenance, not independent votes.  Use the
    # strongest cohort once; additional sources may explain/tie-break but
    # cannot inflate the numeric prior.
    uncapped = max(
        (THEORY_TIER_WEIGHTS[item.tier] for item in normalized),
        default=0.0)
    capped = min(uncapped, max_boost)
    decay = theory_decay_factor(
        attempts_since_progress=attempts_since_progress,
        core_hours_since_progress=core_hours_since_progress,
    )
    return uncapped, capped, decay, capped * decay


def theory_boost(
        cohorts: Iterable[CohortInput],
        attempts_since_progress: int = 0,
        core_hours_since_progress: float = 0.0,
        max_boost: float = THEORY_BOOST_CAP) -> float:
    """Return the capped and decayed deterministic theory boost."""

    return theory_boost_components(
        cohorts,
        attempts_since_progress=attempts_since_progress,
        core_hours_since_progress=core_hours_since_progress,
        max_boost=max_boost,
    )[3]


def score_candidate(
        *,
        eval_cp: Optional[int],
        regret_cp: Optional[float],
        expanded: bool,
        visits: int,
        source: str = "AUTO",
        cohorts: Iterable[CohortInput] = (),
        attempts_since_progress: int = 0,
        core_hours_since_progress: float = 0.0,
        max_boost: float = THEORY_BOOST_CAP,
        shadow: bool = True) -> ScoreComponents:
    """Calculate base and theory scheduling components without side effects.

    In shadow mode the proposed theory-aware score is observable, while
    ``selection_priority`` remains exactly the current base priority.  Setting
    ``shadow=False`` opts into applying the proposed priority, but still cannot
    alter closures because this module has no persistence dependency.
    """

    normalized_source = str(source).upper()
    rank = source_rank(normalized_source)
    base = base_priority(eval_cp, regret_cp, expanded, visits)
    uncapped, capped, decay, boost = theory_boost_components(
        cohorts,
        attempts_since_progress=attempts_since_progress,
        core_hours_since_progress=core_hours_since_progress,
        max_boost=max_boost,
    )
    proposed = base + boost
    return ScoreComponents(
        base_priority=base,
        uncapped_theory_boost=uncapped,
        capped_theory_boost=capped,
        decay_factor=decay,
        theory_boost=boost,
        proposed_priority=proposed,
        selection_priority=base if shadow else proposed,
        source=normalized_source,
        source_rank=rank,
        shadow=bool(shadow),
    )


def scheduler_sort_key(
        candidate_id: str,
        score: ScoreComponents) -> Tuple[int, float, str]:
    """Ascending-sort key implementing USER > THEORY > AUTO deterministically."""

    return (-score.source_rank, -score.selection_priority, str(candidate_id))
