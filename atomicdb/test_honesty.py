import re

from . import ingest, logic
from .testing import TestCase


class PublicHonestyTests(TestCase):

    def test_home_and_method_expose_practical_trust(self):
        home = self.client.get('/atomicdb/')
        self.assertContains(home, 'practically solved')
        self.assertNotContains(home, 'solved exactly')
        method = self.client.get('/atomicdb/method/')
        self.assertContains(method, 'PRACTICAL')
        self.assertContains(method, 'ANDOR')
        self.assertContains(method, 'ENGINE')
        self.assertContains(method, '≤M4')

    def test_query_contract_has_fixed_tier_trust_and_history_scope(self):
        pos = ingest.get_or_create_position(logic.start_fen())
        pos.status, pos.closure, pos.proof = 'WHITE_WIN', 'MATE_PV', 'ANDOR'
        pos.save()
        response = self.client.get('/atomicdb/api/query', {'fen': pos.fen})
        payload = response.json()
        self.assertEqual(payload['tier'], 'PRACTICAL')
        self.assertEqual(payload['trust'], 'ANDOR')
        self.assertEqual(payload['history_scope'],
                         'COUNTERS_AND_REPETITION_IGNORED')

    def test_terminal_trust_is_verified(self):
        pos = ingest.get_or_create_position(logic.start_fen())
        pos.status, pos.closure = 'DRAW', 'TERMINAL'
        pos.save()
        response = self.client.get('/atomicdb/api/query', {'fen': pos.fen})
        self.assertEqual(response.json()['trust'], 'VERIFIED')

    def test_historical_mate_without_proof_is_unclassified(self):
        pos = ingest.get_or_create_position(logic.start_fen())
        pos.status, pos.closure, pos.proof = 'WHITE_WIN', 'MATE_PV', None
        pos.save()
        response = self.client.get('/atomicdb/api/query', {'fen': pos.fen})
        self.assertEqual(response.json()['trust'], 'UNCLASSIFIED')

    def test_disputed_unknown_is_visible(self):
        pos = ingest.get_or_create_position(logic.start_fen())
        pos.proof = 'DISPUTED'
        pos.save()
        response = self.client.get('/atomicdb/api/query', {'fen': pos.fen})
        self.assertEqual(response.json()['trust'], 'DISPUTED')


class OnboardingInstructionsTests(TestCase):
    """Las instrucciones de /method/ tienen que poder COPIARSE.

    La pagina se sirve minificada (``HTML_MINIFY``, sin exclusiones), y el
    minificador aplana el texto suelto: tres ordenes escritas en tres lineas
    llegaban al navegador como una sola linea invalida.  Quien entra por aqui
    es justo quien no puede diagnosticar eso.  El patron ``rawline`` de
    explore.html pone cada orden en su bloque, que el minificador respeta y
    el portapapeles tambien.
    """

    COMMANDS = ('curl -O ', 'pip install requests chess',
                'python atomicdb_worker.py ')

    def test_each_install_command_survives_the_minifier_on_its_own_line(self):
        body = self.client.get('/atomicdb/method/').content.decode()

        block = re.search(r'<div class="raw">(.*?)</div>\s*</div>', body,
                          re.S)
        self.assertIsNotNone(block, 'el bloque de instalacion no esta')
        lines = re.findall(r'<div class="rawline">(.*?)</div>',
                           block.group(1) + '</div>', re.S)
        self.assertEqual(len(lines), len(self.COMMANDS))
        for command, line in zip(self.COMMANDS, lines):
            self.assertTrue(line.startswith(command), line)
            self.assertNotIn('\n', line)

    def test_the_page_really_is_being_minified(self):
        # Si el minificador dejara de correr, el test de arriba pasaria por
        # el motivo equivocado y no probaria nada.
        body = self.client.get('/atomicdb/method/').content.decode()

        self.assertNotIn('\n<p ', body)
