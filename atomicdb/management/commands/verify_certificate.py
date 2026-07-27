"""Replay a SOLVE certificate with the server's own rules. REPLAYS IN FULL.

A certificate is checked here or it is not checked at all: sampling a proof
tree gives confidence, and a closure needs a proof.  The replay runs on
``pyffish`` while the certificate was produced by Atomic-Stockfish's own move
generator, so a movegen bug would have to exist identically in both
implementations to survive this.

Used three ways:

* ``--task <id>`` — re-verify a stored certificate, e.g. after a rules or
  movegen change invalidated the assumptions it was accepted under.
* ``--file <path>`` — verify a certificate produced by hand, straight out of
  the engine's ``solve ... certfile``.
* ``--all`` — sweep every stored certificate.  This is the pass that answers
  "is anything we closed by SOLVE still true".

``--close`` is the only mode that writes, and it writes only what verifies.
"""

import pathlib

from django.core.management.base import BaseCommand

from atomicdb import ingest, solve
from atomicdb.database import atomic
from atomicdb.models import DBEvent, Position, SolveTask


class Command(BaseCommand):
    help = 'Replay SOLVE certificates in full against the server movegen.'

    def add_arguments(self, parser):
        parser.add_argument('--task', type=int, default=None,
                            help='Verify the certificate stored on this task.')
        parser.add_argument('--file', default=None,
                            help='Verify a certificate file (plain or .gz).')
        parser.add_argument('--all', action='store_true',
                            help='Verify every stored certificate.')
        parser.add_argument('--close', action='store_true',
                            help='Apply the closure for what verifies.')
        parser.add_argument('--goal', default=None,
                            help='Expected goal when verifying a file.')
        parser.add_argument('--root', default=None,
                            help='Expected root FEN when verifying a file.')

    def handle(self, *args, **options):
        if options['file']:
            self._verify_file(options)
            return
        tasks = SolveTask.objects.exclude(certificate=None)
        if options['task'] is not None:
            tasks = tasks.filter(pk=options['task'])
        elif not options['all']:
            raise ValueError('give --task, --file or --all')
        counts = {'verified': 0, 'rejected': 0, 'closed': 0}
        for task in tasks.select_related('position').order_by('pk'):
            counts_for = self._verify_task(task, options['close'])
            for name, value in counts_for.items():
                counts[name] += value
        self.stdout.write(
            'verify_certificate: '
            + ' '.join(f'{name}={value}' for name, value in counts.items()))

    def _verify_file(self, options):
        path = pathlib.Path(options['file'])
        raw = path.read_bytes()
        text = (solve.decompress(raw) if path.suffix == '.gz'
                else raw.decode('utf-8', errors='replace'))
        try:
            report = solve.verify_certificate(
                text, root_fen=options['root'], goal=options['goal'])
        except solve.CertificateError as error:
            self.stdout.write(f'REJECTED {path}: {error}')
            return
        self.stdout.write(
            f'VERIFIED {path}: goal={report["goal"]} nodes={report["nodes"]} '
            f'depth={report["depth"]} run={report["worst_reversible_run"]} '
            f'clock_slack={report["clock_slack"]}')

    def _verify_task(self, task, close):
        try:
            text = solve.decompress(task.certificate)
            report = solve.verify_certificate(
                text, root_fen=task.position.fen, goal=task.goal)
        except solve.CertificateError as error:
            self.stdout.write(f'REJECTED task {task.pk}: {error}')
            with atomic():
                SolveTask.objects.filter(pk=task.pk).update(
                    verified=False, reject_reason=str(error)[:2000])
                DBEvent.objects.create(kind='SOLVE_REJECTED', payload={
                    'task': task.pk, 'key': task.position_id,
                    'reason': str(error)[:500], 'source': 'verify_certificate'})
            return {'verified': 0, 'rejected': 1, 'closed': 0}

        self.stdout.write(
            f'VERIFIED task {task.pk}: nodes={report["nodes"]} '
            f'depth={report["depth"]} clock_slack={report["clock_slack"]}')
        closed = 0
        if close:
            with atomic():
                if Position.objects.filter(key=task.position_id,
                                           status='UNKNOWN').exists():
                    closed = int(ingest._close_by_certificate(task, report))
            if closed:
                ingest.backup_cascade([task.position_id])
                ingest.backup_backed_evals([task.position_id])
        return {'verified': 1, 'rejected': 0, 'closed': closed}
