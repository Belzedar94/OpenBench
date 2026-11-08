"""Aplica al arbol los submits que la cola durable tiene pendientes.

Pensado para correr como servicio (ver Documentation/atomicdb-ingest.service).
Un solo proceso basta y es lo recomendado: SQLite serializa las escrituras de
todos modos, y un unico consumidor mantiene el orden de llegada.  Correr dos
no corrompe nada (cada trabajo se reclama con una fila bloqueada), pero
tampoco acelera.
"""

import json
import logging
import signal
import time

from django.core.management.base import BaseCommand
from django.db import DatabaseError

from atomicdb import ingest_queue, propagation_queue

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Apply queued AtomicDB worker submissions to the tree.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--once', action='store_true',
            help='Drain what is ready and exit instead of looping.')
        parser.add_argument(
            '--limit', type=int, default=None,
            help='Stop after this many jobs (per pass).')
        parser.add_argument(
            '--idle-seconds', type=float, default=2.0,
            help='Sleep between polls when the queue is empty.')
        parser.add_argument(
            '--retry-failed', action='store_true',
            help='Requeue every FAILED job (payloads are kept) and continue.')
        parser.add_argument(
            '--status', action='store_true',
            help='Print the queue depth by state and exit.')

    def handle(self, *args, **options):
        if options['status']:
            self.stdout.write(json.dumps(ingest_queue.queue_depth(),
                                         sort_keys=True))
            return
        if options['retry_failed']:
            requeued = ingest_queue.retry_failed()
            self.stdout.write(json.dumps({'requeued': requeued}))
            if options['once']:
                pass   # sigue y drena lo que acaba de reencolar

        stopping = {'now': False}

        def stop(signum, frame):
            del signum, frame
            stopping['now'] = True

        for name in ('SIGINT', 'SIGTERM'):
            handler = getattr(signal, name, None)
            if handler is not None:
                try:
                    signal.signal(handler, stop)
                except (ValueError, OSError):
                    pass   # sin hilo principal: el bucle sale por --once

        totals = {'done': 0, 'failed': 0, 'tb_closed': 0}
        next_tb_sweep = 0.0
        while not stopping['now']:
            result = ingest_queue.drain(limit=options['limit'])
            totals['done'] += result['done']
            totals['failed'] += result['failed']
            if time.monotonic() >= next_tb_sweep:
                next_tb_sweep = (time.monotonic()
                                 + ingest_queue.TB_SWEEP_SECONDS)
                try:
                    totals['tb_closed'] += (
                        ingest_queue.close_queued_by_tables())
                except DatabaseError:
                    # The next sweep tries again; the queue goes on.
                    logger.exception('AtomicDB tablebase sweep failed')
                # Climbs left in the propagation queue while deferral is
                # off (a rollback of ATOMICDB_DEFER_PROPAGATION, or the
                # drainer stopped with it) are never stranded: the writers
                # take them a batch at a time.  With deferral on they are
                # the drainer's alone (§ propagation_queue).
                if not propagation_queue.deferral_enabled():
                    try:
                        settled = propagation_queue.drain(
                            max_batches=1)['settled']
                        if settled:
                            totals['propagation_settled'] = (
                                totals.get('propagation_settled', 0)
                                + settled)
                    except DatabaseError:
                        logger.exception('AtomicDB leftover propagation '
                                         'failed')
            if options['once']:
                break
            if result['done'] == 0 and result['failed'] == 0:
                # Un sueno corto, cortado por la senal de systemd.
                deadline = time.monotonic() + max(0.1,
                                                  options['idle_seconds'])
                while not stopping['now'] and time.monotonic() < deadline:
                    time.sleep(0.1)
        self.stdout.write(json.dumps(totals, sort_keys=True))
