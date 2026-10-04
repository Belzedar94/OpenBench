import os
import sys
from pathlib import Path

from django.contrib.auth.models import User
from django.test import RequestFactory, TestCase

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'Client'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'OpenSite.settings')

import OpenBench.config
import OpenBench.variant_contract
import OpenBench.views
from OpenBench.datagen import MAX_LEGACY_DATAGEN_GAMES, initialize_chunks
from OpenBench.models import Engine, Machine, Profile, Test
from OpenBench.workloads import get_workload


class BooklessVariantContractTests(TestCase):

    ## A DATAGEN workload may name no book at all. 'NONE' is the sentinel for
    ## that, and the engines settle the variant contract by themselves when it
    ## is set. Reading the sentinel as an unregistered book made every bookless
    ## Horde workload raise on the assignment path, which is served to every
    ## worker in the fleet and not only to the one that wanted that test.

    def setUp(self):
        self.author = User.objects.create_user('bookless-author', password='pass')
        self.engine = Engine.objects.create(
            name='bookless-branch',
            source='https://example.test/archive.zip',
            sha='b' * 40,
            bench=4321,
        )

    def make_datagen_test(self, engine='Horde-Stockfish', book_name='NONE',
                          total=4, per_chunk=2):
        test = Test.objects.create(
            author=self.author.username,
            book_name=book_name,
            dev=self.engine,
            base=self.engine,
            dev_repo='https://github.com/example/engine',
            base_repo='https://github.com/example/engine',
            dev_engine=engine,
            base_engine=engine,
            dev_options='',
            base_options='',
            dev_time_control='',
            base_time_control='',
            syzygy_wdl='DISABLED',
            syzygy_adj='DISABLED',
            test_mode='DATAGEN',
            datagen_command=(
                'datagen seed {SEED} count {COUNT} threads {THREADS} '
                'book {BOOK} out {OUT}'
            ),
            datagen_total_count=total,
            datagen_positions_per_chunk=per_chunk,
            datagen_base_seed=100,
            max_games=min(total, MAX_LEGACY_DATAGEN_GAMES),
            throughput=1000,
            approved=True,
            variant_contract=self.contract_for(engine, book_name),
        )
        initialize_chunks(test)
        return test

    def contract_for(self, engine, book_name):
        # Mirrors what create_workload persists at creation time.
        return OpenBench.variant_contract.configured_variant_contract(
            OpenBench.config.OPENBENCH_CONFIG, engine, engine, book_name
        ) or ''

    def make_machine(self, username='bookless-worker',
                     supported=('Horde-Stockfish',), threads=2):
        user = User.objects.create_user(username, password='pass')
        Profile.objects.create(user=user, enabled=True)
        info = {
            'supported': list(supported),
            'concurrency': threads,
            'physical_cores': threads,
            'sockets': 1,
            'focus': [],
            'client_ver': OpenBench.config.OPENBENCH_CONFIG['client_version'],
            'tablebases': {
                'standard': 0,
                'atomic': {'max': 0, 'manifest_sha256': None},
            },
            'syzygy_max': 0,
        }
        return Machine.objects.create(user=user, secret='secret', info=info)

    def post(self, machine, **extra):
        payload = {
            'machine_id': machine.id,
            'secret': machine.secret,
            'blacklist': [],
        }
        payload.update(extra)
        return RequestFactory().post('/clientGetWorkload/', payload)

    # The regression vector: the workload the fleet could not be served

    def test_bookless_horde_datagen_is_served_end_to_end(self):
        test = self.make_datagen_test()
        machine = self.make_machine()

        response = get_workload.get_workload(self.post(machine), machine)

        self.assertEqual(response['workload']['test']['id'], test.id)
        self.assertEqual(
            response['workload']['test']['variant_contract'], 'LICHESS_HORDE_V1'
        )
        self.assertEqual(response['workload']['test']['book']['name'], 'NONE')
        self.assertIsNotNone(response['workload']['test']['datagen'])
        machine.refresh_from_db()
        self.assertEqual(machine.workload, test.id)

    def test_an_unfocused_request_landing_on_the_bookless_test_is_served(self):
        # The contract of whichever test the picker lands on is resolved before
        # anything is written, so an unservable test took down the request
        # itself rather than just its own assignment.
        test = self.make_datagen_test(total=80, per_chunk=2)
        machine = self.make_machine()

        for _ in range(5):
            response = get_workload.get_workload(self.post(machine), machine)
            self.assertEqual(response['workload']['test']['id'], test.id)

    def test_a_bookless_test_in_the_pool_does_not_break_other_workers(self):
        # What took the fleet down was not that this test went unserved: it was
        # that every worker sharing the pool with it lost its request too,
        # whatever engine that worker had actually come for.
        bookless = self.make_datagen_test(total=80, per_chunk=2)
        spell = self.make_datagen_test(
            engine='Spell-Stockfish', book_name='spell_openings.epd',
            total=80, per_chunk=2,
        )
        machine = self.make_machine(
            supported=('Horde-Stockfish', 'Spell-Stockfish')
        )

        served = set()
        for _ in range(20):
            response = get_workload.get_workload(self.post(machine), machine)
            served.add(response['workload']['test']['id'])

        self.assertTrue(served)
        self.assertLessEqual(served, {bookless.id, spell.id})

    # The contract resolution itself

    def test_bookless_workload_takes_its_contract_from_the_engines(self):
        self.assertEqual(
            OpenBench.variant_contract.configured_variant_contract(
                OpenBench.config.OPENBENCH_CONFIG,
                'Horde-Stockfish', 'Horde-Stockfish', 'NONE',
            ),
            'LICHESS_HORDE_V1',
        )

    def test_bookless_workload_without_a_variant_stays_uncontracted(self):
        self.assertIsNone(
            OpenBench.variant_contract.configured_variant_contract(
                OpenBench.config.OPENBENCH_CONFIG,
                'Spell-Stockfish', 'Spell-Stockfish', 'NONE',
            )
        )

    # The guards the sentinel must not weaken

    def test_a_named_book_still_has_to_agree_with_the_engines(self):
        with self.assertRaises(OpenBench.variant_contract.VariantContractError):
            OpenBench.variant_contract.configured_variant_contract(
                OpenBench.config.OPENBENCH_CONFIG,
                'Horde-Stockfish', 'Horde-Stockfish', 'spell_openings.epd',
            )

    def test_an_unregistered_book_is_still_a_disagreement(self):
        with self.assertRaises(OpenBench.variant_contract.VariantContractError):
            OpenBench.variant_contract.configured_variant_contract(
                OpenBench.config.OPENBENCH_CONFIG,
                'Horde-Stockfish', 'Horde-Stockfish', 'not_a_registered_book.epd',
            )

    def test_a_bookless_horde_workload_still_needs_its_contract_persisted(self):
        test = self.make_datagen_test()
        test.variant_contract = ''
        test.save(update_fields=['variant_contract'])

        with self.assertRaises(OpenBench.variant_contract.VariantContractError):
            OpenBench.variant_contract.persisted_variant_contract(
                OpenBench.config.OPENBENCH_CONFIG, test
            )
