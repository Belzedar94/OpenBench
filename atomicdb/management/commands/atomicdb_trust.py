"""Manage the accounts whose unproven mate lines may close positions.

A submitted line that ends in mate (or in a tablebase win) and that the
server cannot prove exhaustively in its online budget closes a position only
when the account that delivered it is trusted.  From any other account the
line is kept as a pending claim.  See ``ingest.account_trusted``.

    manage.py atomicdb_trust list
    manage.py atomicdb_trust add Torom BManBJ --note "long history"
    manage.py atomicdb_trust remove someone
    manage.py atomicdb_trust pending [--limit 20] [--account NAME]

Changes take effect on the next applied result; no restart is needed.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count, Q

from atomicdb.models import PendingClaim, TrustedAccount


class Command(BaseCommand):
    help = ('List, add or remove trusted AtomicDB accounts, and show the '
            'pending claims of the others.')

    def add_arguments(self, parser):
        sub = parser.add_subparsers(dest='action', required=True)
        sub.add_parser('list', help='Trusted accounts and pending claims.')
        add = sub.add_parser('add', help='Trust one or more accounts.')
        add.add_argument('usernames', nargs='+')
        add.add_argument('--note', default='',
                         help='Why the account is trusted (kept with it).')
        remove = sub.add_parser('remove', help='Stop trusting accounts.')
        remove.add_argument('usernames', nargs='+')
        pending = sub.add_parser('pending', help='Open pending claims.')
        pending.add_argument('--limit', type=int, default=20)
        pending.add_argument('--account', default='')

    def handle(self, *args, **options):
        action = options['action']
        if action == 'add':
            for name in options['usernames']:
                _row, created = TrustedAccount.objects.get_or_create(
                    username=name, defaults={'note': options['note'][:200]})
                self.stdout.write(f'{"added" if created else "already trusted"}'
                                  f': {name}')
        elif action == 'remove':
            for name in options['usernames']:
                gone, _ = TrustedAccount.objects.filter(username=name).delete()
                self.stdout.write(f'{"removed" if gone else "not trusted"}'
                                  f': {name}')
        elif action == 'list':
            self._list()
        elif action == 'pending':
            self._pending(options['limit'], options['account'])
        else:                                   # pragma: no cover
            raise CommandError(f'unknown action {action}')

    def _list(self):
        rows = list(TrustedAccount.objects.all())
        self.stdout.write(f'trusted accounts: {len(rows)}')
        for row in rows:
            note = f'  ({row.note})' if row.note else ''
            self.stdout.write(f'  {row.username}  since '
                              f'{row.added:%Y-%m-%d}{note}')
        totals = PendingClaim.objects.aggregate(
            open=Count('id', filter=Q(resolved__isnull=True)),
            confirmed=Count('id', filter=Q(
                resolution=PendingClaim.Resolution.CONFIRMED)),
            contradicted=Count('id', filter=Q(
                resolution=PendingClaim.Resolution.CONTRADICTED)),
            refuted=Count('id', filter=Q(
                resolution=PendingClaim.Resolution.REFUTED)))
        self.stdout.write('pending claims: {open} open, {confirmed} '
                          'confirmed, {contradicted} contradicted, '
                          '{refuted} refuted'.format(**totals))

    def _pending(self, limit, account):
        rows = PendingClaim.objects.filter(resolved__isnull=True)
        if account:
            rows = rows.filter(account=account)
        for row in rows.order_by('-created')[:max(1, limit)]:
            self.stdout.write(
                f'{row.created:%Y-%m-%d %H:%M} {row.account:<20} '
                f'{row.kind:<4} {row.winner:<5} task={row.task_id} '
                f'engine={row.engine_sha[:12] or "-"} '
                f'{row.position_key[:12]} {row.pv}')
