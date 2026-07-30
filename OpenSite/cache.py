"""A shared Redis cache that survives Redis not being there.

WHY SHARED AT ALL.  The site runs five gunicorn workers.  With a per-process
``LocMemCache`` every cached thing exists five times: five copies of the same
breadcrumb walk, five copies of the same page entry, and — worse — a visitor
whose page WAS cached lands on a different worker on the next request and
misses, four times out of five.  Everything the AtomicDB front page caches is
public, derived and short-lived, so one shared copy is strictly better: it is
computed once for the whole fleet, and a producer OUTSIDE the request path
(the selector service) can publish into it, which a per-process cache makes
impossible by construction.

WHY IT MUST NOT BE A HARD DEPENDENCY.  The same code runs on a laptop with no
Redis and inside the test suite, and "the cache is missing" must never be the
difference between a working site and a stack trace.  Django's ``RedisCache``
raises whatever ``redis-py`` raises, so an unreachable server would turn every
cached read on the front page into a 500.

WHAT THIS DOES.  Every cache operation goes to Redis; if Redis is missing,
unreachable or answers with an error, the operation is retried against a
process-local ``LocMemCache`` and Redis is left alone for
``DEGRADED_SECONDS``.  That is exactly the behaviour this deployment had
before Redis existed, so the worst case of a Redis outage is the old cache,
not an outage of the site.  The degradation is logged once per transition, at
WARNING, so it is visible without being a log flood.

NOT A HEALTH CHECK.  Nothing here pings Redis on startup: an empty cache and
an unreachable cache are the same thing to a caller, and a probe at import
time would only move the failure to a place where it cannot be handled.
"""

import logging
import time

from django.core.cache.backends.locmem import LocMemCache
from django.core.cache.backends.redis import RedisCache

logger = logging.getLogger(__name__)

# How long the process stays on the local fallback after a Redis failure.
# Long enough that a down Redis is not re-dialled on every single operation
# (each dial costs the connect timeout), short enough that a restarted Redis
# is picked up without restarting the web workers.
DEGRADED_SECONDS = 30.0

# Operations forwarded to the backing store.  Everything else BaseCache
# provides (``get_or_set``, ``incr_version``, key making) is built out of
# these, so wrapping them covers the whole surface.
_FORWARDED = ('add', 'get', 'set', 'touch', 'delete', 'get_many', 'has_key',
              'incr', 'set_many', 'delete_many', 'clear')


def is_transport_error(error):
    """Is this "Redis did not answer", or is it a bug in the caller?

    The distinction matters and getting it wrong is worse than not having a
    fallback at all.  ``RedisCache.incr`` raises a plain ``ValueError`` for a
    key that is not there, and ``set`` raises from ``pickle`` for a value that
    cannot be serialised: both are the backend working correctly and telling
    the caller something true.  Swallowing them would hide a real bug AND
    park the process on the local cache for half a minute over nothing.

    So only three families count: no ``redis`` package at all, anything the
    socket layer raises, and anything raised from inside ``redis-py`` — which
    is matched by module name because the package may not be importable here,
    which is itself one of the cases being handled.
    """
    if isinstance(error, (ImportError, OSError)):
        return True
    return type(error).__module__.split('.')[0] == 'redis'


class ResilientRedisCache(RedisCache):
    """``RedisCache`` that falls back to a per-process ``LocMemCache``."""

    def __init__(self, server, params):
        super().__init__(server, params)
        fallback_params = dict(params)
        fallback_params['OPTIONS'] = dict(params.get('FALLBACK_OPTIONS') or {})
        self._fallback = LocMemCache(
            params.get('FALLBACK_LOCATION', 'openbench-fallback'),
            fallback_params)
        self._degraded_until = 0.0

    # -- state ------------------------------------------------------------
    @property
    def degraded(self):
        """True while this process is serving from the local fallback."""
        return time.monotonic() < self._degraded_until

    def _degrade(self, operation, error):
        first = not self.degraded
        self._degraded_until = time.monotonic() + DEGRADED_SECONDS
        if first:
            logger.warning(
                'cache: Redis unavailable during %s (%s); serving from the '
                'process-local fallback for %.0fs',
                operation, error, DEGRADED_SECONDS)

    # -- dispatch ---------------------------------------------------------
    def _call(self, operation, *args, **kwargs):
        if not self.degraded:
            try:
                return getattr(super(), operation)(*args, **kwargs)
            except Exception as error:      # noqa: BLE001 - see module docstring
                # ImportError (no ``redis`` installed), ConnectionError,
                # TimeoutError, ResponseError: from here they are all one
                # thing, "the shared cache did not answer", and the answer to
                # all of them is the same.  Anything else is the backend
                # working and saying something true, and it goes up.
                if not is_transport_error(error):
                    raise
                self._degrade(operation, error)
        return getattr(self._fallback, operation)(*args, **kwargs)


def _forward(operation):
    def method(self, *args, **kwargs):
        return self._call(operation, *args, **kwargs)
    method.__name__ = operation
    method.__qualname__ = f'{ResilientRedisCache.__name__}.{operation}'
    method.__doc__ = f'``{operation}``, with the local fallback behind it.'
    return method


for _operation in _FORWARDED:
    setattr(ResilientRedisCache, _operation, _forward(_operation))
del _operation
