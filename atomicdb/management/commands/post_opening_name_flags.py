"""Post the opening names under review to Discord (§ name_flags_discord).

Run by ``atomicdb-opening-name-flags.timer``.  ``--dry-run`` prints the
messages it would post and touches neither Discord nor the token.
"""

from django.core.management.base import BaseCommand

from atomicdb import name_flags_discord


class Command(BaseCommand):
    help = 'Post the opening names the votes put under review to Discord.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='print the posts instead of sending them')
        parser.add_argument('--limit', type=int,
                            default=name_flags_discord.PER_RUN,
                            help='most polls posted in one run')

    def handle(self, *args, **options):
        new, stale, posted, failed = name_flags_discord.run(
            limit=max(0, options['limit']), dry_run=options['dry_run'],
            out=self.stdout.write)
        self.stdout.write(f'flags new={new} stale={stale} '
                          f'posted={posted} failed={failed}')
        if failed:
            raise SystemExit(1)
