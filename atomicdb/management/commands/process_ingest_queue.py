"""Aplica al arbol los submits que la cola durable tiene pendientes.

Pensado para correr como servicio (ver Documentation/atomicdb-ingest.service).
Un solo proceso basta y es lo recomendado: SQLite serializa las escrituras de
todos modos, y un unico consumidor mantiene el orden de llegada.  Correr dos
no corrompe nada (cada trabajo se reclama con una fila bloqueada), pero
tampoco acelera.
"""

import json
import signal
import time

from django.core.management.base import BaseCommand

from atomicdb import ingest_queue


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

        totals = {'done': 0, 'failed': 0}
        while not stopping['now']:
            result = ingest_queue.drain(limit=options['limit'])
            totals['done'] += result['done']
            totals['failed'] += result['failed']
            if options['once']:
                break
            if result['done'] == 0 and result['failed'] == 0:
                # Un sueno corto, cortado por la senal de systemd.
                deadline = time.monotonic() + max(0.1,
                                                  options['idle_seconds'])
                while not stopping['now'] and time.monotonic() < deadline:
                    time.sleep(0.1)
        self.stdout.write(json.dumps(totals, sort_keys=True))
