"""Fail-closed process-start validation for theory scheduler settings."""

import os
import subprocess
import sys

from django.test import SimpleTestCase


class TheorySettingsTests(SimpleTestCase):

    @staticmethod
    def _import_settings(**overrides):
        environment = os.environ.copy()
        for key in (
                'ATOMICDB_THEORY_SCHEDULER_MODE',
                'ATOMICDB_THEORY_POLICY_VERSION',
                'ATOMICDB_THEORY_BUNDLE_SHA256',
                'ATOMICDB_THEORY_ACTIVE_ACK'):
            environment.pop(key, None)
        environment.update(overrides)
        return subprocess.run(
            [sys.executable, '-c', 'import OpenSite.settings'],
            cwd=os.path.dirname(os.path.dirname(__file__)),
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_empty_or_overlong_policy_is_rejected(self):
        empty = self._import_settings(
            ATOMICDB_THEORY_POLICY_VERSION='')
        self.assertNotEqual(empty.returncode, 0)
        self.assertIn(
            'ATOMICDB_THEORY_POLICY_VERSION', empty.stderr)

        overlong = self._import_settings(
            ATOMICDB_THEORY_POLICY_VERSION='x' * 33)
        self.assertNotEqual(overlong.returncode, 0)
        self.assertIn(
            'ATOMICDB_THEORY_POLICY_VERSION', overlong.stderr)

    def test_bundle_hash_must_be_exact_lowercase_sha256(self):
        for invalid in ('', 'a' * 63, 'A' * 64, 'g' * 64):
            result = self._import_settings(
                ATOMICDB_THEORY_BUNDLE_SHA256=invalid)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(
                'ATOMICDB_THEORY_BUNDLE_SHA256', result.stderr)

    def test_active_requires_exact_nonempty_ack(self):
        rejected = self._import_settings(
            ATOMICDB_THEORY_SCHEDULER_MODE='ACTIVE',
            ATOMICDB_THEORY_POLICY_VERSION='atomic-theory-shadow-v1',
            ATOMICDB_THEORY_ACTIVE_ACK='')
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn('ACTIVE theory scheduling requires', rejected.stderr)

        accepted = self._import_settings(
            ATOMICDB_THEORY_SCHEDULER_MODE='ACTIVE',
            ATOMICDB_THEORY_POLICY_VERSION='atomic-theory-shadow-v1',
            ATOMICDB_THEORY_ACTIVE_ACK='atomic-theory-shadow-v1')
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
