import json
import tarfile
import tempfile
from io import StringIO
from pathlib import Path

from django.core.management import call_command
from django.test import TestCase, override_settings

from OpenBench.models import PGN


class PgnArchiveCommandTests(TestCase):

    def setUp(self):
        self.media = tempfile.TemporaryDirectory()
        self.media_override = override_settings(MEDIA_ROOT=self.media.name)
        self.media_override.enable()

    def tearDown(self):
        self.media_override.disable()
        self.media.cleanup()

    def create_raw_pgn(self, test_id, result_id, book_index, payload):
        pgn = PGN.objects.create(
            test_id=test_id,
            result_id=result_id,
            book_index=book_index,
        )
        Path(self.media.name, pgn.filename()).write_bytes(payload)
        return pgn

    def run_once(self):
        stdout = StringIO()
        stderr = StringIO()
        call_command('process_pgns', once=True, stdout=stdout, stderr=stderr)
        return json.loads(stdout.getvalue()), stderr.getvalue()

    def test_archives_pending_pgn_under_media_root(self):
        pgn = self.create_raw_pgn(42, 7, 3, b'first-pgn')

        totals, errors = self.run_once()

        pgn.refresh_from_db()
        self.assertTrue(pgn.processed)
        self.assertFalse(Path(self.media.name, pgn.filename()).exists())
        self.assertEqual(totals, {'failed': 0, 'processed': 1})
        self.assertEqual(errors, '')

        archive = Path(self.media.name, 'PGNs', '42.pgn.tar')
        with tarfile.open(archive, 'r') as bundle:
            self.assertEqual(bundle.getnames(), [pgn.filename()])
            self.assertEqual(bundle.extractfile(pgn.filename()).read(), b'first-pgn')

    def test_appends_later_result_to_existing_workload_archive(self):
        first = self.create_raw_pgn(42, 7, 3, b'first-pgn')
        self.run_once()
        second = self.create_raw_pgn(42, 8, 4, b'second-pgn')

        totals, errors = self.run_once()

        self.assertEqual(totals, {'failed': 0, 'processed': 1})
        self.assertEqual(errors, '')
        archive = Path(self.media.name, 'PGNs', '42.pgn.tar')
        with tarfile.open(archive, 'r') as bundle:
            self.assertEqual(bundle.getnames(), [first.filename(), second.filename()])
            self.assertEqual(bundle.extractfile(second.filename()).read(), b'second-pgn')

    def test_missing_raw_file_remains_pending_and_is_reported(self):
        pgn = PGN.objects.create(test_id=99, result_id=1, book_index=0)

        totals, errors = self.run_once()

        pgn.refresh_from_db()
        self.assertFalse(pgn.processed)
        self.assertEqual(totals, {'failed': 1, 'processed': 0})
        self.assertIn(pgn.filename(), errors)
        self.assertFalse(Path(self.media.name, 'PGNs', '99.pgn.tar').exists())
