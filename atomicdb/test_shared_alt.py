"""SHARED_ALT shadow: it finds a constructed transposition, it respects its
caps, it writes nothing when the switch is off, and the walk's answer is the
same with the switch on and off (§ atomicdb.shared_alt)."""

import hashlib
from unittest.mock import patch

from django.test import override_settings

from . import ingest, proof, shared_alt
from .models import AnalysisTask, DBEvent, Edge, Position, ProofCampaign
from .testing import TestCase

VALUE = {'ATOMICDB_DESCENT': 'value'}
ON = {'ATOMICDB_SHARED_ALT_SHADOW': '1'}
OFF = {'ATOMICDB_SHARED_ALT_SHADOW': ''}
MIXED = {'primary': 0.6, 'backup': 0.3, 'explore': 0.1}


def _key(name):
    return hashlib.sha256(name.encode()).hexdigest()


def _pos(name, stm='w', **fields):
    return Position.objects.create(
        key=_key(name), fen=f'4k3/8/8/8/8/8/8/4K3 {stm} - - 0 1', **fields)


def _edge(parent, child, uci):
    return Edge.objects.create(parent=parent, move_uci=uci, child=child)


def _campaign(root, policy=None):
    ProofCampaign.objects.filter(active=True).update(active=False)
    return ProofCampaign.objects.create(
        name='shared-alt-test', root=root, goal=ProofCampaign.Goal.WHITE_WIN,
        algorithm_version=proof.ALGORITHM_VERSION,
        repertoire_policy=dict(policy or {'primary': 1.0, 'backup': 0.0,
                                          'explore': 0.0}))


def _events():
    return DBEvent.objects.filter(kind=shared_alt.SHARED_ALT_EVENT)


class _Reset:
    def setUp(self):
        super().setUp()
        shared_alt._memo.clear()
        shared_alt._minute[0] = shared_alt._minute[1] = 0.0


def _tree(transposes=True):
    """R (OR) -a-> A (AND) -b-> W (OR), and W -s-> S (spine), W -c-> C.

    A also has the reply ``x`` to X (OR), whose ``backed_move`` is ``y`` to
    C.  So C is required without W: R -a-> A (obligatory, OR backed_move),
    A -x-> X (AND, any open reply), X -y-> C (OR backed_move).  With
    ``transposes=False`` X's backed_move points elsewhere and C is not.
    """
    root = _pos('R', 'w', eval_cp=900, backed_eval=900, backed_move='a',
                expanded=True)
    a = _pos('A', 'b', eval_cp=900, backed_eval=900, backed_move='b',
             expanded=True)
    w = _pos('W', 'w', eval_cp=900, backed_eval=900, backed_move='s',
             expanded=True)
    s = _pos('S', 'b', eval_cp=900)
    c = _pos('C', 'b', eval_cp=850)
    weak = _pos('weak', 'b', eval_cp=100)       # not competitive
    virgin = _pos('virgin', 'b', eval_cp=880)   # for the explore arm
    x = _pos('X', 'w', eval_cp=850, backed_eval=850,
             backed_move='y' if transposes else 'z', expanded=True)
    z = _pos('Z', 'b', eval_cp=860)
    _edge(root, a, 'a')
    _edge(a, w, 'b')
    _edge(a, x, 'x')
    _edge(w, s, 's')
    _edge(w, c, 'c')
    _edge(w, weak, 'k')
    _edge(w, virgin, 'v')
    _edge(x, c, 'y')
    _edge(x, z, 'z')
    return root, {'A': a, 'W': w, 'S': s, 'C': c, 'X': x, 'Z': z}


@override_settings(**VALUE, **ON)
class FindsTheTranspositionTests(_Reset, TestCase):

    def test_the_constructed_transposition_is_logged(self):
        root, nodes = _tree()
        campaign = _campaign(root)
        proof.descend_value(campaign)
        event = _events().get()
        payload = event.payload
        self.assertEqual((payload['w'], payload['s'], payload['c']),
                         (nodes['W'].key, nodes['S'].key, nodes['C'].key))
        self.assertEqual(payload['diff_cp'], 50)
        self.assertEqual(payload['witness_plies'], 2)    # C <- X <- A
        self.assertEqual(payload['ply'], 2)
        self.assertIn('pn_s', payload)
        self.assertIn('pn_c', payload)

    def test_no_obligatory_path_no_event(self):
        root, _nodes = _tree(transposes=False)
        proof.descend_value(_campaign(root))
        self.assertFalse(_events().exists())

    def test_a_path_through_w_does_not_count(self):
        """C's only other parent hangs below W's spine: excluded."""
        root, nodes = _tree(transposes=False)
        _edge(nodes['S'], nodes['C'], 'q')
        proof.descend_value(_campaign(root))
        self.assertFalse(_events().exists())

    def test_the_same_pair_is_written_once_per_hour(self):
        root, _nodes = _tree()
        campaign = _campaign(root)
        proof.descend_value(campaign)
        shared_alt._memo.clear()             # a new process, same hour
        proof.descend_value(campaign)
        self.assertEqual(_events().count(), 1)

    def test_the_hourly_cap_holds(self):
        root, _nodes = _tree()
        with patch.object(shared_alt, 'SHARED_ALT_HOURLY_CAP', 0):
            proof.descend_value(_campaign(root))
        self.assertFalse(_events().exists())

    def test_the_report_counts_events_and_tasks_under_s(self):
        import json
        from io import StringIO

        from django.core.management import call_command

        root, nodes = _tree()
        proof.descend_value(_campaign(root))
        AnalysisTask.objects.create(position=nodes['S'], budget_nodes=1,
                                    arm=ingest.VALUE_DESCEND_ARM)
        out = StringIO()
        call_command('shared_alt_report', '--json', stdout=out)
        summary = json.loads(out.getvalue())
        self.assertEqual((summary['events'], summary['distinct_w']), (1, 1))
        self.assertEqual(summary['diff_cp_buckets']['50..100'], 1)
        self.assertEqual(summary['descend_tasks_at_s'], 1)
        self.assertEqual(summary['pairs'][0]['at_s'], 1)
        call_command('shared_alt_report', stdout=StringIO())   # text form

    def test_the_event_stays_off_the_public_feed(self):
        from .views import FEED_HIDDEN_KINDS

        self.assertIn(shared_alt.SHARED_ALT_EVENT, FEED_HIDDEN_KINDS)


class CapsTests(_Reset, TestCase):
    """A chain of AND parents C <- P1 <- ... <- P10, P10 the only target."""

    def setUp(self):
        super().setUp()
        self.c = _pos('cap-C', 'b')
        self.w = _pos('cap-W', 'w')
        previous = self.c
        self.chain = []
        for index in range(1, 11):
            parent = _pos(f'cap-P{index}', 'b')    # AND: any reply obliges
            _edge(parent, previous, f'p{index}')
            self.chain.append(parent)
            previous = parent
        self.goal = ProofCampaign.Goal.WHITE_WIN
        self.targets = {self.chain[-1].key}

    def _run(self, **caps):
        budget = shared_alt.Budget(**caps)
        found = shared_alt.required_without(self.goal, self.c.key, self.w.key,
                                            self.targets, budget)
        return found, budget

    def test_found_within_the_caps(self):
        found, budget = self._run(plies=40, nodes=300, seconds=10)
        self.assertEqual(found, 10)
        self.assertEqual(budget.used_nodes, 10)

    def test_the_ply_cap(self):
        found, _budget = self._run(plies=5, nodes=300, seconds=10)
        self.assertIsNone(found)

    def test_the_node_cap(self):
        found, budget = self._run(plies=40, nodes=3, seconds=10)
        self.assertIsNone(found)
        self.assertTrue(budget.capped)
        self.assertLessEqual(budget.used_nodes, 3)

    def test_the_wall_time_cap(self):
        ticks = iter(range(0, 1000))
        budget = shared_alt.Budget(plies=40, nodes=300, seconds=2.5,
                                   clock=lambda: next(ticks))
        found = shared_alt.required_without(self.goal, self.c.key, self.w.key,
                                            self.targets, budget)
        self.assertIsNone(found)
        self.assertTrue(budget.capped)
        self.assertLess(budget.used_nodes, 10)

    def test_w_is_never_climbed_through(self):
        _edge(self.chain[-1], self.w, 'via-w')
        found, _budget = self._run(plies=40, nodes=300, seconds=10)
        self.assertEqual(found, 10)      # still the chain, never via W
        self.targets = {self.w.key}
        found, _budget = self._run(plies=40, nodes=300, seconds=10)
        self.assertIsNone(found)

    def test_a_capped_walk_logs_nothing_and_remembers_nothing(self):
        root, _nodes = _tree()
        campaign = _campaign(root)
        with override_settings(**VALUE, **ON), \
                patch.object(shared_alt, 'SHARED_ALT_MAX_NODES', 0):
            proof.descend_value(campaign)
        self.assertFalse(_events().exists())
        self.assertEqual(shared_alt._memo, {})

    def test_the_per_minute_allowance(self):
        root, _nodes = _tree()
        shared_alt._minute[0] = shared_alt.time.monotonic()
        shared_alt._minute[1] = shared_alt.SHARED_ALT_SECONDS_PER_MINUTE
        with override_settings(**VALUE, **ON), \
                patch.object(shared_alt, 'required_without') as check:
            proof.descend_value(_campaign(root))
        check.assert_not_called()
        self.assertFalse(_events().exists())


@override_settings(**VALUE)
class SwitchTests(_Reset, TestCase):

    def test_off_by_default(self):
        with override_settings(ATOMICDB_SHARED_ALT_SHADOW=None):
            with patch.dict('os.environ', {}, clear=False) as env:
                env.pop(shared_alt.SHARED_ALT_SETTING, None)
                self.assertFalse(shared_alt.enabled())

    def test_only_explicit_yes_turns_it_on(self):
        for raw in ('', '0', 'no', 'off', 'false', 'shadow', 'value'):
            with override_settings(ATOMICDB_SHARED_ALT_SHADOW=raw):
                self.assertFalse(shared_alt.enabled(), msg=raw)
        for raw in ('1', 'true', 'YES', 'on'):
            with override_settings(ATOMICDB_SHARED_ALT_SHADOW=raw):
                self.assertTrue(shared_alt.enabled(), msg=raw)

    @override_settings(**OFF)
    def test_no_event_and_no_query_when_off(self):
        root, _nodes = _tree()
        with patch.object(shared_alt, 'required_without') as check:
            proof.descend_value(_campaign(root))
        check.assert_not_called()
        self.assertFalse(_events().exists())

    @override_settings(**ON)
    def test_an_exception_never_reaches_the_walk(self):
        root, _nodes = _tree()
        campaign = _campaign(root)
        with override_settings(**OFF):
            expected = proof.descend_value(campaign)
        with patch.object(shared_alt, 'required_without',
                          side_effect=RuntimeError('boom')), \
                self.assertLogs('atomicdb.shared_alt', level='ERROR'):
            got = proof.descend_value(campaign)
        self.assertEqual(got, expected)
        self.assertFalse(_events().exists())
        # and the surrounding transaction is still usable
        self.assertTrue(Position.objects.filter(key=root.key).exists())


@override_settings(**VALUE)
class SameAnswerTests(_Reset, TestCase):
    """THE no-semantics-change test: same target, same tasks, on and off."""

    def setUp(self):
        super().setUp()
        self.root, self.nodes = _tree()
        self.campaign = _campaign(self.root, policy=MIXED)

    def _targets(self):
        out = []
        for counter in range(60):
            target = proof.descend_value(self.campaign, counter=counter)
            out.append(None if target is None else (
                target.position.key, target.plies, target.arm,
                target.floor_nodes, target.line_plies))
        return out

    def test_every_walk_selects_the_same_target(self):
        with override_settings(**OFF):
            off = self._targets()
        with override_settings(**ON):
            on = self._targets()
        self.assertTrue(_events().exists())      # the shadow did fire
        self.assertEqual(on, off)
        self.assertGreater(len({row[2] for row in off if row}), 1)

    def test_the_minted_tasks_are_the_same(self):
        def mint():
            tasks = ingest._next_tasks_by_proof(3)
            out = sorted((task.position_id, task.budget_nodes, task.arm,
                          task.multipv) for task in tasks)
            AnalysisTask.objects.all().delete()
            return out

        with override_settings(**OFF):
            off = mint()
        with override_settings(**ON):
            on = mint()
        self.assertTrue(off)
        self.assertEqual(on, off)
