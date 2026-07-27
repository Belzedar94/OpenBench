"""Plain-text status of every AtomicDB proof campaign.

Three numbers matter and none of them is a percentage.  "% solved" is a lie
whose denominator grows every time a new legal obligation is discovered; pn/dn
are not probabilities either.  What this prints is:

* pn/dn of the root — the estimated remaining effort to PROVE the campaign's
  goal and to REFUTE it.  A pn that keeps climbing while dn stalls is the
  earliest honest signal that the conjecture might be false.
* the oldest open AND obligations — defender replies that must ALL be refuted
  and that nothing has touched in the longest time.  This is the compulsory
  work, as opposed to the attractive work.
* the most-proving frontier — the ten open nodes with the smallest pn, i.e.
  where a SOLVE budget would buy the most proof per node.
"""

from django.core.management.base import BaseCommand

from atomicdb import proof
from atomicdb.models import ProofCampaign, ProofNode


def _number(value):
    """pn/dn saturado se imprime como INF, no como 4611686018427387904."""
    if value is None:
        return '-'
    if value >= proof.PROOF_INFINITY:
        return 'INF'
    return f'{value:,}'


class Command(BaseCommand):
    help = 'Print pn/dn, open AND obligations and the most-proving frontier.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--campaign', default=None,
            help='Only this campaign name (default: every active one).')
        parser.add_argument(
            '--all', action='store_true',
            help='Include inactive campaigns.')
        parser.add_argument(
            '--top', type=int, default=10,
            help='How many frontier and obligation rows to print.')

    def handle(self, *args, **options):
        campaigns = ProofCampaign.objects.all().select_related('root')
        if options['campaign']:
            campaigns = campaigns.filter(name=options['campaign'])
        elif not options['all']:
            campaigns = campaigns.filter(active=True)
        campaigns = list(campaigns.order_by('id'))
        if not campaigns:
            self.stdout.write('no matching proof campaign')
            return

        top = max(1, int(options['top']))
        for campaign in campaigns:
            self._report(campaign, top)

    def _report(self, campaign, top):
        pn, dn = proof.root_numbers(campaign)
        nodes = ProofNode.objects.filter(campaign=campaign)
        self.stdout.write('')
        self.stdout.write(f'campaign {campaign.name}  goal={campaign.goal}  '
                          f'active={campaign.active}  '
                          f'algorithm=v{campaign.algorithm_version}')
        self.stdout.write(f'  root       {campaign.root_id[:16]}  '
                          f'status={campaign.root.status}')
        self.stdout.write(f'  root pn    {_number(pn)}')
        self.stdout.write(f'  root dn    {_number(dn)}')
        self.stdout.write(f'  proof nodes {nodes.count():,}  '
                          f'(expanded {nodes.filter(expanded_in_proof=True).count():,})')
        policy = proof.normalized_policy(campaign)
        self.stdout.write(
            '  repertoire  primary={primary:.0%} backup={backup:.0%} '
            'explore={explore:.0%}'.format(**policy))

        obligations = proof.open_and_obligations(campaign, limit=top)
        self.stdout.write(f'  oldest open AND obligations ({len(obligations)}):')
        for node in obligations:
            self.stdout.write(
                f'    {node.position_id[:16]}  pn={_number(node.pn):>12}  '
                f'dn={_number(node.dn):>12}  seen={node.updated:%Y-%m-%d %H:%M}')
        if not obligations:
            self.stdout.write('    (none)')

        frontier = proof.most_proving_frontier(campaign, limit=top)
        self.stdout.write(f'  most-proving frontier ({len(frontier)}):')
        for node in frontier:
            self.stdout.write(
                f'    {node.position_id[:16]}  pn={_number(node.pn):>12}  '
                f'dn={_number(node.dn):>12}  '
                f'eval={node.position.eval_cp if node.position.eval_cp is not None else "-"}')
        if not frontier:
            self.stdout.write('    (none)')
