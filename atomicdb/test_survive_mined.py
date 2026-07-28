"""A certificate our fork MINED, checked by the move generator it did not use.

This is the end of the chain the whole subsystem exists to close, and the only
test in the suite where all three implementations meet:

    Atomic-Stockfish   mines the region and writes the certificate
    Fairy-Stockfish    (native, pinned upstream) checks it
    pyffish            (the same upstream, through Python) checks it

The fixture is not hand-written and not round-tripped through anything: it is
the literal output of ``survive50_mine 6r1/k7/8/8/8/8/8/7K w - - 0 1`` on the
fable/survive50 branch -- 263 states and 844 edges of White's bare king boxed
onto the h-file, with every one of White's legal moves enumerated and one
Black reply per state.  Nothing in this repository produced it, which is the
point: a move generator bug would have to exist identically in our fork and in
upstream to get past here.

Committed as a file rather than regenerated, because a test that needs the
engine built is a test that silently stops running.  If the emitter's format
drifts, this fails; if the emitter's SEMANTICS drift, this fails harder,
because both verifiers re-derive every legal move and every terminal.
"""

import json
import pathlib
import subprocess
import tempfile

from django.test import SimpleTestCase

from . import survive

FIXTURE = (pathlib.Path(__file__).resolve().parent / 'data' / 'survive50'
           / 'mined_king_walk.cert')
ROOT = '6r1/k7/8/8/8/8/8/7K w - - 0 1'

BINARY = (pathlib.Path(__file__).resolve().parent.parent
          / 'tools' / 'survive50-verify' / 'survive50-verify.exe')
if not BINARY.exists():
    BINARY = BINARY.with_suffix('')

# What the engine reported when it wrote this file.  Pinned so that a change
# in either verifier's accounting shows up as a disagreement with the MINER,
# not just as the two verifiers agreeing with each other about something new.
EXPECTED = {
    'result': 'DISPROVED_WHITE_WIN',
    'root_tau': 0,
    'entry_clock': 0,
    'states': 263,
    'edges': 844,
    'reachable': 263,
    'zeroing_edges': 0,
    'terminal_exits': 0,
    'max_tau': 0,
}


class MinedCertificateTests(SimpleTestCase):

    def test_the_fixture_is_the_engine_output_we_think_it_is(self):
        text = FIXTURE.read_text(encoding='utf-8')
        self.assertTrue(text.startswith('# ' + survive.CERTIFICATE_FORMAT))
        header, _ = survive.parse_header(text)
        self.assertEqual(header['root'], ROOT)
        self.assertEqual(header['states'], '263')
        self.assertEqual(header['terminal_precedence'],
                         survive.TERMINAL_PRECEDENCE_ID)

    def test_the_reference_accepts_what_our_fork_mined(self):
        """pyffish, which shares no code with the engine that wrote this."""
        report = survive.verify_certificate(
            FIXTURE.read_text(encoding='utf-8'), root_fen=ROOT)
        for field, value in EXPECTED.items():
            self.assertEqual(report[field], value, f'{field} disagrees')

    def test_the_native_verifier_accepts_it_too_and_agrees_on_the_counts(self):
        if not BINARY.exists():
            self.skipTest(f'{BINARY.name} not built; '
                          'run make -C tools/survive50-verify')
        with tempfile.TemporaryDirectory() as folder:
            path = pathlib.Path(folder) / 'certificate.txt'
            path.write_text(FIXTURE.read_text(encoding='utf-8'),
                            encoding='utf-8')
            finished = subprocess.run(
                [str(BINARY), str(path), '--root', ROOT],
                capture_output=True, text=True, timeout=300)
        self.assertEqual(finished.returncode, 0, finished.stdout)
        payload = json.loads(finished.stdout.strip().splitlines()[-1])
        self.assertTrue(payload['ok'], payload)
        for field, value in EXPECTED.items():
            self.assertEqual(payload[field], value, f'{field} disagrees')

    def test_a_mined_certificate_is_still_only_a_disproof(self):
        """The line the proof database cannot recover from if it is blurred."""
        report = survive.verify_certificate(
            FIXTURE.read_text(encoding='utf-8'), root_fen=ROOT)
        self.assertEqual(report['result'], 'DISPROVED_WHITE_WIN')
        self.assertNotEqual(report['result'], 'PROVEN_BLACK_WIN')

    def test_corrupting_one_mined_White_move_is_caught(self):
        """The fixture is real, so the tamper has to be real too.

        Drop a single White move out of 844 edges and the certificate must
        die: the verifier regenerates the legal set at that state and finds
        one missing.  This is what "every legal move is a universal
        obligation" costs an attacker who wants to hide one.
        """
        lines = FIXTURE.read_text(encoding='utf-8').split('\n')
        victim = next(i for i, line in enumerate(lines)
                      if line.startswith('W '))
        tampered = lines[:victim] + lines[victim + 1:]
        text = '\n'.join(
            f'edges {EXPECTED["edges"] - 1}' if line.startswith('edges ')
            else line for line in tampered)
        with self.assertRaises(survive.CertificateError) as caught:
            survive.verify_certificate(text, root_fen=ROOT)
        self.assertEqual(getattr(caught.exception, 'code', None),
                         'white-coverage-mismatch')
