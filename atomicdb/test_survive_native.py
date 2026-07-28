"""The native verifier against the Python reference, case for case.

WHY A SECOND VERIFIER AT ALL
----------------------------
``atomicdb/survive.py`` is correct and stays the reference, but it walks
pyffish, and pyffish rebuilds the entire piece attack table on every position
it constructs -- ``UCI::init_variant`` is ``pieceMap.init(v)`` followed by
``Bitboards::init_pieces()``, and buildPosition calls it every time because
the variant is an argument of every pyffish call.  That, not the Python layer,
is where ~15 ms per position went.  ``tools/survive50-verify`` binds the
variant once and does the same work at roughly 90k positions/s, which is what
brings a 10k-state certificate back from tens of minutes to seconds.

The independence doctrine is unchanged and is the reason the tool is built
against UPSTREAM Fairy-Stockfish rather than our fork: our fork solves,
upstream verifies.  pyffish IS upstream with a Python skin, so what the
speedup drops is the skin, not the second implementation.

WHAT THIS ASSERTS
-----------------
Not "both accepted" and not "both rejected", but the same verdict AND the same
reason on every case in ``corruption_corpus``.  Two verifiers that reject the
same certificate on different grounds have not agreed about anything -- they
have disagreed while wearing a matching hat -- and on the acceptances they
must also report the same counts.

If the binary has not been built, these skip rather than fail: a checkout with
no compiler is a normal thing, and the reference is what the server falls back
to anyway.  Build it with ``make -C tools/survive50-verify``.
"""

import json
import pathlib
import subprocess
import tempfile

from django.test import SimpleTestCase

from . import survive
from .test_survive import corruption_corpus

BINARY = (pathlib.Path(__file__).resolve().parent.parent
          / 'tools' / 'survive50-verify' / 'survive50-verify.exe')
if not BINARY.exists():
    BINARY = BINARY.with_suffix('')


def _python_verdict(case):
    try:
        report = survive.verify_certificate(case['text'], root_fen=case['root'])
    except survive.CertificateError as error:
        return False, getattr(error, 'code', 'uncoded'), None
    return True, None, report


def _native_verdict(case, budget=None):
    with tempfile.TemporaryDirectory() as folder:
        path = pathlib.Path(folder) / 'certificate.txt'
        path.write_text(case['text'], encoding='utf-8')
        command = [str(BINARY), str(path)]
        if case['root']:
            command += ['--root', case['root']]
        if budget is not None:
            command += ['--budget', str(budget)]
        finished = subprocess.run(command, capture_output=True, text=True,
                                  timeout=120)
    payload = json.loads(finished.stdout.strip().splitlines()[-1])
    expected_exit = 0 if payload['ok'] else 1
    assert finished.returncode == expected_exit, (
        f'exit code {finished.returncode} contradicts the verdict {payload}')
    return payload['ok'], payload.get('code'), payload


class NativeDifferentialTests(SimpleTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.corpus = corruption_corpus() if BINARY.exists() else []

    def setUp(self):
        if not BINARY.exists():
            self.skipTest(f'{BINARY.name} not built; '
                          'run make -C tools/survive50-verify')

    def test_the_corpus_covers_both_outcomes(self):
        """A differential of rejections only would miss the worst failure."""
        accepted = [c for c in self.corpus if c['code'] is None]
        rejected = [c for c in self.corpus if c['code'] is not None]
        self.assertGreaterEqual(len(accepted), 4)
        self.assertGreaterEqual(len(rejected), 30)
        # Every distinct code should be reachable, or the vocabulary is fiction.
        self.assertGreaterEqual(len({c['code'] for c in rejected}), 25)

    def test_native_and_reference_agree_on_every_case(self):
        disagreements = []
        for case in self.corpus:
            py_ok, py_code, py_report = _python_verdict(case)
            native_ok, native_code, native_report = _native_verdict(case)

            if py_ok != native_ok or py_code != native_code:
                disagreements.append(
                    f"{case['name']}: reference={'accept' if py_ok else py_code}"
                    f" native={'accept' if native_ok else native_code}")
                continue
            if not py_ok:
                continue
            # On an acceptance the two must also have counted the same graph.
            for field in ('root_tau', 'entry_clock', 'states', 'edges',
                          'reachable', 'zeroing_edges', 'terminal_exits',
                          'max_tau'):
                if py_report[field] != native_report[field]:
                    disagreements.append(
                        f"{case['name']}: {field} reference={py_report[field]} "
                        f'native={native_report[field]}')
        self.assertEqual(disagreements, [],
                         'native verifier disagrees with the reference:\n'
                         + '\n'.join(disagreements))

    def test_the_reference_case_expectations_hold(self):
        """The corpus's own labels, checked against the reference.

        Without this the differential could pass with both implementations
        wrong in the same direction, which is exactly the common-mode failure
        the two-implementation rule exists to prevent.
        """
        wrong = []
        for case in self.corpus:
            ok, code, _ = _python_verdict(case)
            actual = None if ok else code
            if actual != case['code']:
                wrong.append(f"{case['name']}: expected {case['code']} "
                             f'got {actual}')
        self.assertEqual(wrong, [], '\n'.join(wrong))

    def test_the_native_budget_is_enforced_like_the_reference(self):
        case = next(c for c in self.corpus
                    if c['name'] == 'accept: a region whose White moves reset')
        ok, code, _ = _native_verdict(case, budget=12)
        self.assertFalse(ok)
        self.assertEqual(code, 'budget-exceeded')

        with self.assertRaises(survive.CertificateError) as caught:
            survive.verify_certificate(case['text'], root_fen=case['root'],
                                       max_positions=12)
        self.assertEqual(getattr(caught.exception, 'code', None),
                         'budget-exceeded')

    def test_the_native_verifier_is_fast_enough_to_change_the_budget(self):
        """The number the fleet ladder of doc 18 §5 was re-costed on.

        pyffish manages about 66 positions/s.  The target that makes a
        10k-state certificate affordable again is 10k/s; this asserts a
        conservative floor well under what the tool measures, so it fails on a
        real regression rather than on a busy machine.
        """
        case = next(c for c in self.corpus
                    if c['name'] == 'accept: a region whose White moves reset')
        with tempfile.TemporaryDirectory() as folder:
            path = pathlib.Path(folder) / 'certificate.txt'
            path.write_text(case['text'], encoding='utf-8')
            finished = subprocess.run(
                [str(BINARY), str(path), '--repeat', '400'],
                capture_output=True, text=True, timeout=300)
        payload = json.loads(finished.stdout.strip().splitlines()[-1])
        self.assertTrue(payload['ok'])
        self.assertGreater(payload['positions_per_second'], 10_000,
                           f'native throughput regressed: {payload}')
