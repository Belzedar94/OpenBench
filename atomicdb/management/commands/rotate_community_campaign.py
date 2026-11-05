"""Make the most-voted open campaign the one the auto queue pushes.

Run by atomicdb-community-campaign.timer every 10 minutes
(§ atomicdb.community_campaign).
"""

import json

from django.core.management.base import BaseCommand

from atomicdb import community_campaign


class Command(BaseCommand):
    help = 'Activate the most-voted open campaign and pause the others.'

    def handle(self, *args, **options):
        self.stdout.write(json.dumps(community_campaign.rotate(),
                                     sort_keys=True))
