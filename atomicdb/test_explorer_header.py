"""The approver's moderation link, the request limit that is gone, and the
line that tells what this node actually knows."""

import hashlib

from django.conf import settings
from django.contrib.auth.models import User

from . import ingest, logic, views
from .models import (AnalysisTask, Edge, OpeningNameSuggestion, Position,
                     RequestLog)
from .testing import TestCase


class SuggestionsBadgeTests(TestCase):

    def _user(self, name, approver):
        from OpenBench.models import Profile
        user = User.objects.create_user(name, password='pw')
        Profile.objects.create(user=user, approver=approver)
        return user

    def _pending(self, count):
        pos = ingest.get_or_create_position(logic.start_fen())
        for index in range(count):
            OpeningNameSuggestion.objects.create(
                position=pos, proposed_name=f'name {index}',
                ip='127.0.0.1',
                status=OpeningNameSuggestion.SState.PENDING)
        return pos

    def test_an_approver_sees_the_queue_and_its_size(self):
        self._user('mod', approver=True)
        pos = self._pending(2)
        self.client.login(username='mod', password='pw')

        body = self.client.get(
            f'/atomicdb/explore/{pos.key}/').content.decode()

        self.assertIn('/atomicdb/suggestions/', body)
        self.assertIn('Suggestions (2)', body)

    def test_a_plain_user_sees_nothing_at_all(self):
        self._user('plain', approver=False)
        pos = self._pending(2)
        self.client.login(username='plain', password='pw')

        body = self.client.get(
            f'/atomicdb/explore/{pos.key}/').content.decode()

        self.assertNotIn('/atomicdb/suggestions/', body)

    def test_an_anonymous_visitor_sees_nothing_either(self):
        pos = self._pending(2)
        body = self.client.get(
            f'/atomicdb/explore/{pos.key}/').content.decode()
        self.assertNotIn('/atomicdb/suggestions/', body)

    def test_only_pending_ones_are_counted(self):
        self._user('mod', approver=True)
        pos = self._pending(1)
        OpeningNameSuggestion.objects.create(
            position=pos, proposed_name='done', ip='127.0.0.1',
            status=OpeningNameSuggestion.SState.APPROVED)
        OpeningNameSuggestion.objects.create(
            position=pos, proposed_name='no', ip='127.0.0.1',
            status=OpeningNameSuggestion.SState.REJECTED)
        self.client.login(username='mod', password='pw')

        body = self.client.get(
            f'/atomicdb/explore/{pos.key}/').content.decode()

        self.assertIn('Suggestions (1)', body)

    def test_an_empty_queue_shows_the_link_without_a_number(self):
        """A permanent "(0)" is noise that trains people to ignore a badge."""
        self._user('mod', approver=True)
        pos = ingest.get_or_create_position(logic.start_fen())
        self.client.login(username='mod', password='pw')

        body = self.client.get(
            f'/atomicdb/explore/{pos.key}/').content.decode()

        self.assertIn('/atomicdb/suggestions/', body)
        self.assertNotIn('Suggestions (0)', body)

    def test_the_flat_cached_pages_never_carry_it(self):
        """``map`` and ``method`` cache WITHOUT varying on cookie, so an
        approver's render there would be handed to every visitor."""
        self._user('mod', approver=True)
        self._pending(2)
        self.client.login(username='mod', password='pw')

        for path in ('/atomicdb/map/', '/atomicdb/method/'):
            body = self.client.get(path).content.decode()
            self.assertNotIn('/atomicdb/suggestions/', body, path)


class NoHourlyRequestLimitTests(TestCase):

    def test_many_requests_from_one_ip_are_all_served(self):
        """The owner removed the hourly allowance: what bounds the spend is
        the per-position dedup and the queue ceiling, not a counter per IP."""
        root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(root)
        targets = list(Position.objects.filter(status='UNKNOWN')
                       .exclude(key=root.key)[:20])
        self.assertGreaterEqual(len(targets), 20)

        for position in targets:
            response = self.client.post(f'/atomicdb/request/{position.key}/')
            self.assertNotEqual(response.status_code, 429)
            self.assertNotEqual(response.json().get('status'), 'rate-limited')

    def test_the_same_position_is_still_deduplicated(self):
        pos = ingest.get_or_create_position(logic.start_fen())
        first = self.client.post(f'/atomicdb/request/{pos.key}/')
        second = self.client.post(f'/atomicdb/request/{pos.key}/')
        self.assertEqual(first.json()['status'], 'queued')
        self.assertEqual(second.json()['status'], 'already-requested')

    def test_a_pile_of_receipts_no_longer_blocks_anything(self):
        root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(root)
        other = Position.objects.filter(status='UNKNOWN').exclude(
            key=root.key).first()
        for _ in range(60):
            RequestLog.objects.create(ip='127.0.0.1', position=root)

        response = self.client.post(f'/atomicdb/request/{other.key}/')

        self.assertEqual(response.status_code, 200)


def _key(name):
    return hashlib.sha256(name.encode()).hexdigest()


def _pos(name, stm='w', **kw):
    """Posicion sintetica: solo importan el turno y lo que cuenta arriba."""
    return Position.objects.create(
        key=_key(name), fen=f'4k3/8/8/8/8/8/8/4K3 {stm} - - 0 1', **kw)


def _edge(parent, child, uci):
    return Edge.objects.create(parent=parent, move_uci=uci, child=child)


class NodeKnowledgeSummaryTests(TestCase):
    """La cabecera cuenta la historia del conocimiento del nodo.

    Reporte de comunidad, verbatim, sobre una posicion con cinco lineas
    vigentes y 136 MILLONES de nodos invertidos: "here I can request remaining
    moves while no analysis is done in the node ... and just a bunch of walked
    evals ... feels wrong idk, how I'm supposed to cover such node by actual
    analysis, by clicking every move? ... why there are so many moves evaluated
    if it only has multipv 5".  Todo el dato estaba en la pagina — repartido
    entre una linea gris debajo del FEN y veintitantos chips sueltos — y aun
    asi la lectura que quedaba era la contraria.
    """

    def _mixed(self):
        """Un nodo muy buscado y una tabla hecha de las tres cosas."""
        parent = _pos('KSUM-P', 'w', expanded=True,
                      nodes_invested=136_400_000, visits=27)
        searched = _pos('KSUM-A', 'b', eval_cp=20,
                        nodes_invested=128_000_000)
        seeded_one = _pos('KSUM-B', 'b', eval_cp=900)
        seeded_two = _pos('KSUM-C', 'b', eval_cp=-40)
        waiting = _pos('KSUM-D', 'b')
        for child, uci in ((searched, 'd7d5'), (seeded_one, 'e7e6'),
                           (seeded_two, 'g8f6'), (waiting, 'b8c6')):
            _edge(parent, child, uci)
        AnalysisTask.objects.create(position=waiting, budget_nodes=8_000_000,
                                    state=AnalysisTask.TState.PENDING)
        return parent

    def test_the_three_counts_split_searched_seeded_and_queued(self):
        parent = self._mixed()

        summary = views._moves_summary(views._child_moves(parent))

        self.assertEqual(
            summary, 'Moves here: 1 searched · 2 from lines only · 1 queued')

    def test_the_page_carries_the_summary_in_the_header(self):
        parent = self._mixed()

        body = self.client.get(
            f'/atomicdb/explore/{parent.key}/').content.decode()

        self.assertIn('Moves here: 1 searched · 2 from lines only · 1 queued',
                      body)

    def test_a_leased_search_counts_as_queued_like_the_status_line(self):
        """Vivo es lo mismo aqui que en la linea de peticion (§ live_request):
        esperando en la cola o ya en manos de un worker."""
        parent = _pos('KLEASE-P', 'w', expanded=True)
        child = _pos('KLEASE-C', 'b')
        _edge(parent, child, 'd7d5')
        AnalysisTask.objects.create(position=child, budget_nodes=8_000_000,
                                    state=AnalysisTask.TState.LEASED)

        summary = views._moves_summary(views._child_moves(parent))

        self.assertEqual(summary, 'Moves here: 0 searched · 1 queued')

    def test_a_finished_task_is_not_in_flight_any_more(self):
        parent = _pos('KDONE-P', 'w', expanded=True)
        child = _pos('KDONE-C', 'b')
        _edge(parent, child, 'd7d5')
        AnalysisTask.objects.create(position=child, budget_nodes=8_000_000,
                                    state=AnalysisTask.TState.COMPLETED)

        summary = views._moves_summary(views._child_moves(parent))

        self.assertEqual(summary, 'Moves here: 0 searched')

    def test_queued_is_its_own_axis_not_a_share_of_the_others(self):
        """Una jugada ya buscada puede tener OTRO pase en vuelo: los tres
        numeros no son una particion y el resumen no finge que lo sean."""
        parent = _pos('KAXIS-P', 'w', expanded=True)
        child = _pos('KAXIS-C', 'b', eval_cp=20, nodes_invested=8_000_000)
        _edge(parent, child, 'd7d5')
        AnalysisTask.objects.create(position=child, budget_nodes=64_000_000,
                                    state=AnalysisTask.TState.PENDING)

        summary = views._moves_summary(views._child_moves(parent))

        self.assertEqual(summary, 'Moves here: 1 searched · 1 queued')

    def test_the_empty_segments_are_left_out_but_searched_never_is(self):
        """Un "0 searched" ES la historia del nodo del reporte y no se calla.
        Los otros dos a cero no dicen nada y no se pintan."""
        parent = _pos('KZERO-P', 'w', expanded=True)
        _edge(parent, _pos('KZERO-C', 'b'), 'd7d5')

        summary = views._moves_summary(views._child_moves(parent))

        self.assertEqual(summary, 'Moves here: 0 searched')

    def test_a_position_with_no_children_says_nothing_at_all(self):
        pos = _pos('KNONE-P', 'w')

        response = self.client.get(f'/atomicdb/explore/{pos.key}/')

        self.assertEqual(response.context['moves_summary'], '')
        self.assertNotIn('Moves here:', response.content.decode())

    def test_the_two_sentences_share_one_line_and_keep_two_tooltips(self):
        """Ahora que la procedencia no gasta una palabra por fila, la cabecera
        tampoco gasta dos renglones en decir de donde viene lo de abajo.  Una
        linea, dos frases, y cada una con su propia explicacion al pasar el
        raton: el resumen de la tabla la necesita, la del nodo no."""
        parent = self._mixed()

        body = self.client.get(
            f'/atomicdb/explore/{parent.key}/').content.decode()

        self.assertRegex(
            body,
            r'<p class="dim"[^>]*>'
            r'<span>This position: analysed \(136\.4M nodes across 27 '
            r'passes\)</span> — <span title="searched = an engine looked at '
            r'that move itself[^"]*">'
            r'Moves here: 1 searched · 2 from lines only · 1 queued'
            r'</span></p>')

    def test_the_queue_count_is_one_statement_for_the_whole_table(self):
        """El unico numero que no viaja ya en la fila entra por un IN, no por
        una pregunta por hijo: una tabla abierta son treinta y tantas."""
        parent = self._mixed()
        rows = views._child_moves(parent)

        with self.assertNumQueries(1,
                                   using=settings.ATOMICDB_DATABASE_ALIAS):
            views._queued_children(rows)


class BreadthButtonCopyTests(TestCase):
    """El boton de anchura dice QUE compra y CUANTAS jugadas.

    "how I'm supposed to cover such node by actual analysis, by clicking every
    move?" — con este click, y el boton tiene que decirlo sin que haya que
    pasarle el raton por encima.
    """

    def test_a_single_move_is_not_asked_for_in_plural(self):
        parent = _pos('KBTN-P', 'w', expanded=True)
        _edge(parent, _pos('KBTN-C', 'b'), 'd7d5')

        body = self.client.get(
            f'/atomicdb/explore/{parent.key}/').content.decode()

        self.assertIn('Analyse the 1 unanalysed move<', body)


class NodeStoryTests(TestCase):
    """Con busqueda propia, la cabecera lo dice arriba y con sus dos numeros.

    Los nodos invertidos vivian en una linea gris debajo del FEN, junto a las
    visitas y los segundos, y la cabecera del nodo del reporte se leyo entera
    como "no analysis is done in the node".
    """

    def test_a_searched_position_says_what_it_cost_and_over_how_many_passes(
            self):
        pos = _pos('KSTORY-A', 'w', eval_cp=180, expanded=True,
                   nodes_invested=136_400_000, visits=27)

        body = self.client.get(
            f'/atomicdb/explore/{pos.key}/').content.decode()

        self.assertIn(
            'This position: analysed (136.4M nodes across 27 passes)', body)

    def test_a_single_pass_is_not_pluralised(self):
        pos = _pos('KSTORY-B', 'w', eval_cp=180, expanded=True,
                   nodes_invested=8_000_000, visits=1)

        self.assertEqual(views._node_story(pos),
                         'This position: analysed (8.0M nodes across 1 pass)')

    def test_nodes_without_a_pass_counter_still_report_the_nodes(self):
        pos = _pos('KSTORY-C', 'w', nodes_invested=2_000, visits=0)

        self.assertEqual(views._node_story(pos),
                         'This position: analysed (2,000 nodes)')

    def test_a_seeded_position_keeps_the_walked_chip_and_gains_no_story(self):
        """El caso sembrado ya lo cuenta su chip; repetirlo seria decir dos
        veces lo mismo con dos redacciones."""
        pos = _pos('KSTORY-D', 'w', eval_cp=180, expanded=True)

        response = self.client.get(f'/atomicdb/explore/{pos.key}/')
        body = response.content.decode()

        self.assertEqual(response.context['node_story'], '')
        self.assertNotIn('This position: analysed', body)
        self.assertIn('>walked</span>', body)
