"""Opening-name votes: thumbs on the Openings page, the review flag, the
Discord poll and the approver's actions (owner, 11-Nov-2026).

What this file pins:

* one vote per voter and position, changeable and withdrawable, counted by
  account when signed in and by cookie otherwise, with the per-address caps;
* tallies count only votes on the name shown now;
* the escalation rule (minimum votes, three quarters down) creates ONE flag
  per position and name, and the poster posts it once;
* an approver's action is an ordinary approval of ``OpeningNameSuggestion``
  (same resolver as the queue), never an automatic change;
* the review heuristics of the 11-Nov review.
"""

import json
import re
from unittest import mock

from django.contrib.auth.models import User
from django.test import Client, override_settings
from django.utils import timezone

from OpenBench.models import Profile

from . import (community_names, ingest, logic, name_flags_discord,
               name_review, name_votes, openings, roots)
from .models import (Edge, OpeningNameFlag, OpeningNameSuggestion,
                     OpeningNameVote, Position)
from .testing import TestCase

CATALOGUED = 'King Knight Opening'          # the catalogue's name of 1.Nf3


def _position(route):
    parent = ingest.get_or_create_position(logic.start_fen())
    for uci in route:
        child = ingest.get_or_create_position(
            logic.apply_move(parent.fen, uci))
        Edge.objects.get_or_create(parent=parent, move_uci=uci,
                                   defaults={'child': child})
        parent = child
    return parent


def _approved(pos, name, kind=OpeningNameSuggestion.SKind.NEW, **fields):
    # With its approval time, like every approved row in production: the
    # latest approval wins by ``resolved_at``, and PostgreSQL sorts a NULL
    # after every date where SQLite sorts it first.
    fields.setdefault('resolved_at', timezone.now())
    return OpeningNameSuggestion.objects.create(
        position=pos, proposed_name=name, kind=kind, ip='127.0.0.1',
        status=OpeningNameSuggestion.SState.APPROVED, **fields)


def _user(name, approver=False):
    user = User.objects.create_user(name, password='pw')
    Profile.objects.create(user=user, enabled=True, approver=approver)
    client = Client()
    client.force_login(user)
    return client


class _Base(TestCase):

    def setUp(self):
        community_names.invalidate()
        name_review.invalidate()
        # An unnamed line, named by the community.
        self.pos = _position(('a2a3', 'a7a6', 'a1a2'))
        assert openings.lookup_key(self.pos.key) is None
        _approved(self.pos, 'Belfast Gambit', suggested_by='lesha')
        community_names.invalidate()
        self.nf3 = _position(('g1f3',))
        assert openings.lookup_key(self.nf3.key)['name'] == CATALOGUED

    def _vote(self, key, vote, client=None, ip='10.0.0.1', **extra):
        client = client or self.client
        return client.post(f'/atomicdb/names/vote/{key}/',
                           {'vote': vote, **extra},
                           HTTP_X_REQUESTED_WITH='fetch',
                           REMOTE_ADDR=ip)


class VotingTests(_Base):

    def test_a_vote_counts_once_and_can_change_and_be_withdrawn(self):
        data = self._vote(self.pos.key, 'up').json()
        self.assertEqual((data['status'], data['up'], data['down'],
                          data['mine']), ('voted', 1, 0, 'up'))
        # Again from the same visitor: still one vote.
        data = self._vote(self.pos.key, 'up').json()
        self.assertEqual((data['up'], data['down']), (1, 0))
        data = self._vote(self.pos.key, 'down').json()
        self.assertEqual((data['up'], data['down'], data['mine']),
                         (0, 1, 'down'))
        data = self._vote(self.pos.key, 'none').json()
        self.assertEqual((data['status'], data['up'], data['down'],
                          data['mine']), ('withdrawn', 0, 0, None))
        self.assertFalse(OpeningNameVote.objects.exists())

    def test_pressing_the_chosen_thumb_again_withdraws(self):
        self._vote(self.pos.key, 'down', toggle='1')
        data = self._vote(self.pos.key, 'down', toggle='1').json()
        self.assertEqual((data['status'], data['down']), ('withdrawn', 0))

    def test_the_first_vote_sets_the_voter_cookie(self):
        response = self._vote(self.pos.key, 'up')
        self.assertIn('atomicdb_voter', response.cookies)
        self.assertTrue(response.cookies['atomicdb_voter']['httponly'])

    def test_catalogue_names_can_be_voted_on(self):
        data = self._vote(self.nf3.key, 'down').json()
        self.assertEqual((data['status'], data['down']), ('voted', 1))
        row = OpeningNameVote.objects.get()
        self.assertEqual(row.name, CATALOGUED)

    def test_a_position_without_its_own_name_cannot_be_voted(self):
        unnamed = _position(('h2h3',))
        response = self._vote(unnamed.key, 'up')
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()['status'], 'unnamed')
        self.assertEqual(self._vote(self.pos.key, 'maybe').status_code, 400)

    def test_a_signed_in_voter_counts_by_account_and_absorbs_the_cookie(self):
        client = Client()
        self._vote(self.pos.key, 'up', client=client)
        user = User.objects.create_user('quasa', password='pw')
        client.force_login(user)
        data = self._vote(self.pos.key, 'down', client=client).json()
        self.assertEqual((data['up'], data['down']), (0, 1))
        self.assertEqual(
            list(OpeningNameVote.objects.values_list('voter', flat=True)),
            ['u:quasa'])
        # Another browser of the same account is the same vote.
        other = Client()
        other.force_login(user)
        data = self._vote(self.pos.key, 'up', client=other).json()
        self.assertEqual((data['up'], data['down']), (1, 0))

    def test_anonymous_votes_from_one_address_are_capped_per_name(self):
        for _ in range(name_votes.ANON_VOTES_PER_IP_POSITION):
            self.assertEqual(
                self._vote(self.pos.key, 'down', client=Client()).json()
                ['status'], 'voted')
        response = self._vote(self.pos.key, 'down', client=Client())
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()['down'],
                         name_votes.ANON_VOTES_PER_IP_POSITION)
        # Another address, or an account, still votes.
        self.assertEqual(self._vote(self.pos.key, 'down', client=Client(),
                                    ip='10.0.0.2').json()['status'], 'voted')
        signed = _user('lesha2')
        self.assertEqual(self._vote(self.pos.key, 'down', client=signed)
                         .json()['status'], 'voted')

    def test_cookieless_votes_are_capped_per_address_and_day(self):
        names = []
        for i in range(3):
            pos = _position(('a2a3', 'a7a6', 'a1a2', ('b7b6', 'c7c6',
                                                      'd7d6')[i]))
            _approved(pos, f'Test Line {i}')
            names.append(pos)
        community_names.invalidate()
        with mock.patch.object(name_votes, 'MINTED_VOTES_PER_IP_DAY', 2):
            statuses = [self._vote(pos.key, 'up', client=Client()).json()
                        ['status'] for pos in names]
        self.assertEqual(statuses, ['voted', 'voted', 'rate-limited'])

    def test_a_rename_starts_a_fresh_count(self):
        self._vote(self.pos.key, 'down')
        _approved(self.pos, 'Belfast Attack',
                  kind=OpeningNameSuggestion.SKind.EDIT,
                  previous_name='Belfast Gambit')
        community_names.invalidate()
        self.assertEqual(name_votes.tally(self.pos.key, 'Belfast Attack'),
                         (0, 0))
        data = self._vote(self.pos.key, 'up').json()
        self.assertEqual((data['up'], data['down']), (1, 0))

    def test_a_plain_form_post_goes_back_to_the_page(self):
        response = self.client.post(f'/atomicdb/names/vote/{self.pos.key}/',
                                    {'vote': 'up', 'sort': 'disputed'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response['Location'],
            f'/atomicdb/roots/?voted=voted&sort=disputed#op-{self.pos.key}')
        # A sort the page does not know is not carried.
        response = self.client.post(f'/atomicdb/names/vote/{self.pos.key}/',
                                    {'vote': 'up', 'sort': '//evil'})
        self.assertNotIn('evil', response['Location'])

    def test_the_live_tallies_include_the_visitors_own_votes(self):
        self._vote(self.pos.key, 'up')
        self._vote(self.nf3.key, 'down', client=Client())
        data = self.client.get('/atomicdb/names/votes/').json()
        self.assertEqual(data['votes'][self.pos.key], [1, 0])
        self.assertEqual(data['votes'][self.nf3.key], [0, 1])
        self.assertEqual(data['mine'], {self.pos.key: 'up'})
        self.assertEqual(data['under_review'], {})
        self.assertEqual(data['min_votes'], 4)


class FlagTests(_Base):

    def _votes(self, key, ups, downs):
        n = 0
        for value in ['up'] * ups + ['down'] * downs:
            n += 1
            self._vote(key, value, client=Client(), ip=f'10.1.0.{n}')

    def test_three_quarters_down_over_the_minimum_flags_the_name(self):
        self._votes(self.pos.key, 1, 2)
        self.assertFalse(OpeningNameFlag.objects.exists())   # 3 votes only
        self._vote(self.pos.key, 'down', client=Client(), ip='10.2.0.1')
        flag = OpeningNameFlag.objects.get()
        self.assertEqual((flag.position_key, flag.name, flag.up, flag.down,
                          flag.community, flag.named_by),
                         (self.pos.key, 'Belfast Gambit', 1, 3, True,
                          'lesha'))

    def test_half_down_does_not_flag(self):
        self._votes(self.pos.key, 2, 2)
        self.assertFalse(OpeningNameFlag.objects.exists())

    def test_one_flag_per_name_however_the_votes_move(self):
        self._votes(self.pos.key, 0, 4)
        self._votes(self.pos.key, 0, 0)
        for i in range(4):
            self._vote(self.pos.key, 'down', client=Client(),
                       ip=f'10.3.0.{i}')
        self.assertEqual(OpeningNameFlag.objects.count(), 1)

    @override_settings(ATOMICDB_NAME_REVIEW_MIN_VOTES=2)
    def test_the_minimum_is_a_setting(self):
        self._votes(self.nf3.key, 0, 2)
        flag = OpeningNameFlag.objects.get()
        self.assertEqual((flag.name, flag.community, flag.catalogue_name),
                         (CATALOGUED, False, CATALOGUED))

    def test_sync_creates_missing_flags_and_closes_stale_ones(self):
        with override_settings(ATOMICDB_NAME_REVIEW_MIN_VOTES=99):
            self._votes(self.pos.key, 0, 4)
        self.assertFalse(OpeningNameFlag.objects.exists())
        self.assertEqual(name_votes.sync_flags(), (1, 0))
        _approved(self.pos, 'Belfast Attack',
                  kind=OpeningNameSuggestion.SKind.EDIT,
                  previous_name='Belfast Gambit')
        community_names.invalidate()
        self.assertEqual(name_votes.sync_flags(), (0, 1))
        flag = OpeningNameFlag.objects.get()
        self.assertEqual(flag.resolution, 'STALE')


class DiscordTests(_Base):

    def setUp(self):
        super().setUp()
        self.flag, _ = name_votes.flag(
            self.pos.key, 'Belfast Gambit',
            community_names.in_force(self.pos.key), 1, 3)
        self.sent = []

    def _send(self, method, path, body):
        self.sent.append((method, path, body))
        return {'id': '9001' if path.endswith('/messages') else '9002'}

    def test_a_flag_is_posted_once_as_a_poll_with_a_thread(self):
        result = name_flags_discord.run(send=self._send)
        self.assertEqual(result, (0, 0, 1, 0))
        (m1, p1, body), (m2, p2, thread) = self.sent
        self.assertEqual(p1, '/channels/1570156441153835039/messages')
        self.assertEqual(p2, '/channels/1570156441153835039/messages/9001/'
                             'threads')
        self.assertEqual(body['allowed_mentions'], {'parse': []})
        answers = [a['poll_media']['text'] for a in body['poll']['answers']]
        self.assertEqual(answers, ['Keep', 'Remove',
                                   'Rename (propose in the thread)'])
        self.assertIn('Belfast Gambit', body['content'])
        self.assertIn('named by lesha', body['content'])
        self.assertIn(f'/atomicdb/explore/{self.pos.key}/', body['content'])
        self.assertIn('a3 a6', body['content'])
        self.flag.refresh_from_db()
        self.assertEqual((self.flag.discord_message_id,
                          self.flag.discord_thread_id), ('9001', '9002'))
        self.assertIsNotNone(self.flag.posted_at)
        self.assertEqual(
            name_votes.poll_url(self.flag),
            'https://discord.com/channels/1532350146367586304/'
            '1570156441153835039/9001')
        # The next run has nothing to post.
        self.sent.clear()
        self.assertEqual(name_flags_discord.run(send=self._send),
                         (0, 0, 0, 0))
        self.assertEqual(self.sent, [])

    def test_a_corrected_catalogue_name_offers_the_restore(self):
        _approved(self.nf3, 'Kings Knight Thing',
                  kind=OpeningNameSuggestion.SKind.EDIT,
                  previous_name=CATALOGUED, suggested_by='bob')
        community_names.invalidate()
        flag, _ = name_votes.flag(self.nf3.key, 'Kings Knight Thing',
                                  community_names.in_force(self.nf3.key),
                                  0, 4)
        answers = [a['poll_media']['text'] for a in
                   name_flags_discord.payload(flag)['poll']['answers']]
        self.assertEqual(answers, [
            'Keep', f'Restore catalogue name: {CATALOGUED}', 'Remove',
            'Rename (propose in the thread)'])

    def test_a_catalogue_name_offers_keep_and_rename_only(self):
        flag, _ = name_votes.flag(self.nf3.key, CATALOGUED, None, 0, 4)
        body = name_flags_discord.payload(flag)
        answers = [a['poll_media']['text'] for a in body['poll']['answers']]
        self.assertEqual(answers, ['Keep', 'Rename (propose in the thread)'])
        self.assertIn('Catalogue name.', body['content'])

    def test_a_failed_post_is_recorded_and_retried_a_few_times(self):
        def failing(method, path, body):
            raise OSError('boom')
        for _ in range(name_flags_discord.MAX_ATTEMPTS + 2):
            name_flags_discord.run(send=failing)
        self.flag.refresh_from_db()
        self.assertEqual(self.flag.post_attempts,
                         name_flags_discord.MAX_ATTEMPTS)
        self.assertEqual(self.flag.post_error, 'OSError')
        self.assertEqual(self.flag.discord_message_id, '')

    def test_a_dry_run_sends_nothing_and_reads_no_token(self):
        printed = []
        with mock.patch.object(name_flags_discord, 'read_token') as token:
            result = name_flags_discord.run(dry_run=True,
                                            out=printed.append)
        token.assert_not_called()
        self.assertEqual(result, (0, 0, 0, 0))
        self.assertEqual(json.loads(printed[0])['poll']['duration'],
                         name_flags_discord.POLL_HOURS)
        self.flag.refresh_from_db()
        self.assertEqual(self.flag.discord_message_id, '')

    def test_the_token_is_read_from_the_file_only_when_posting(self):
        with mock.patch.object(name_flags_discord, 'read_token',
                               return_value='secret') as token, \
                mock.patch.object(name_flags_discord, '_request',
                                  side_effect=lambda t, m, p, b: (
                                      self.assertEqual(t, 'secret')
                                      or {'id': '7'})):
            name_flags_discord.run()
        token.assert_called_once()


class ApproverActionTests(_Base):

    def setUp(self):
        super().setUp()
        self.boss = _user('boss', approver=True)

    def _flag(self, pos):
        name, entry = name_votes.current_name(pos.key)
        return name_votes.flag(pos.key, name, entry, 0, 4)[0]

    def _act(self, flag, action, client=None, **extra):
        return (client or self.boss).post(
            '/atomicdb/suggestions/flags/',
            {'flag': flag.id, 'action': action, **extra})

    def test_the_moderation_page_lists_the_flags(self):
        self._flag(self.pos)
        body = self.boss.get('/atomicdb/suggestions/').content.decode()
        self.assertIn('Names under review', body)
        self.assertIn('Belfast Gambit', body)
        self.assertIn('value="remove"', body)
        self.assertNotIn('value="restore"', body)   # nothing to restore
        self.assertIn('waiting to be posted', body)

    def test_remove_is_an_approved_removal_and_the_name_goes(self):
        flag = self._flag(self.pos)
        response = self._act(flag, 'remove')
        self.assertEqual(response['Location'],
                         '/atomicdb/suggestions/?review=removed#review')
        row = OpeningNameSuggestion.objects.latest('id')
        self.assertEqual((row.kind, row.status, row.previous_name,
                          row.resolved_by, row.suggested_by),
                         ('DEL', 'APPROVED', 'Belfast Gambit', 'boss', ''))
        community_names.invalidate()
        self.assertIsNone(community_names.name_for(self.pos.key))
        self.assertNotIn(self.pos.key, community_names.approved_map())
        flag.refresh_from_db()
        self.assertEqual((flag.resolution, flag.resolved_by),
                         ('REMOVE', 'boss'))

    def test_restore_brings_the_catalogue_name_back(self):
        _approved(self.nf3, 'Kings Knight Thing',
                  kind=OpeningNameSuggestion.SKind.EDIT,
                  previous_name=CATALOGUED, suggested_by='bob')
        community_names.invalidate()
        flag = self._flag(self.nf3)
        self._act(flag, 'restore')
        row = OpeningNameSuggestion.objects.latest('id')
        self.assertEqual((row.kind, row.proposed_name, row.resolved_by),
                         ('EDIT', CATALOGUED, 'boss'))
        community_names.invalidate()
        self.assertEqual(name_votes.current_name(self.nf3.key),
                         (CATALOGUED, None))

    def test_rename_is_credited_to_the_approver(self):
        flag = self._flag(self.pos)
        self._act(flag, 'rename', name='Belfast Variation')
        community_names.invalidate()
        entry = community_names.in_force(self.pos.key)
        self.assertEqual((entry['name'], community_names.credit(entry),
                          entry['approved_by']),
                         ('Belfast Variation', 'boss', 'boss'))
        self.assertEqual(
            self._act(self._flag(self.pos), 'rename', name='<b>')['Location'],
            '/atomicdb/suggestions/?review=invalid#review')

    def test_keep_closes_the_review_and_changes_nothing(self):
        flag = self._flag(self.pos)
        before = OpeningNameSuggestion.objects.count()
        self._act(flag, 'keep')
        self.assertEqual(OpeningNameSuggestion.objects.count(), before)
        flag.refresh_from_db()
        self.assertEqual(flag.resolution, 'KEEP')
        # Closed is closed.
        self.assertEqual(self._act(flag, 'remove')['Location'],
                         '/atomicdb/suggestions/?review=done#review')

    def test_a_catalogue_name_cannot_be_removed_or_restored(self):
        flag = self._flag(self.nf3)
        for action in ('remove', 'restore'):
            self.assertIn('review=invalid', self._act(flag, action)
                          ['Location'])

    def test_a_name_changed_since_the_flag_is_left_alone(self):
        flag = self._flag(self.pos)
        _approved(self.pos, 'Belfast Attack',
                  kind=OpeningNameSuggestion.SKind.EDIT,
                  previous_name='Belfast Gambit')
        community_names.invalidate()
        self.assertIn('review=moved', self._act(flag, 'remove')['Location'])

    def test_only_approvers_act(self):
        flag = self._flag(self.pos)
        plain = _user('plain')
        response = self._act(flag, 'remove', client=plain)
        self.assertEqual(response.status_code, 302)
        self.assertNotIn('review=', response['Location'])
        flag.refresh_from_db()
        self.assertIsNone(flag.resolved_at)

    def test_the_queue_approves_through_the_same_resolver(self):
        pending = OpeningNameSuggestion.objects.create(
            position=self.pos, proposed_name='Belfast Line',
            kind=OpeningNameSuggestion.SKind.EDIT,
            previous_name='Belfast Gambit', ip='127.0.0.1')
        with mock.patch.object(name_votes, 'resolve_suggestion',
                               wraps=name_votes.resolve_suggestion) as spy:
            self.boss.post('/atomicdb/suggestions/',
                           {'suggestion': pending.id, 'action': 'approve'})
        spy.assert_called_once()
        pending.refresh_from_db()
        self.assertEqual(pending.status, 'APPROVED')


class OpeningsPageTests(_Base):

    def test_every_named_row_has_thumbs_and_the_tallies(self):
        self._vote(self.pos.key, 'down')
        response = self.client.get('/atomicdb/roots/')
        body = response.content.decode()
        self.assertIn(f'action="/atomicdb/names/vote/{self.pos.key}/"', body)
        self.assertIn(f'action="/atomicdb/names/vote/{self.nf3.key}/"', body)
        self.assertIn('name-votes.js', body)
        self.assertNotIn('<style', body)
        row = next(r for r in response.context['openings']
                   if r['key'] == self.pos.key)
        self.assertEqual((row['votes_up'], row['votes_down'],
                          row['votable']), (0, 1, True))

    def test_most_disputed_sorts_by_thumbs_down(self):
        for i in range(2):
            self._vote(self.nf3.key, 'down', client=Client(), ip=f'10.9.0.{i}')
        self._vote(self.pos.key, 'down', client=Client(), ip='10.9.1.1')
        rows = self.client.get('/atomicdb/roots/?sort=disputed').context[
            'openings']
        self.assertEqual([r['key'] for r in rows[:2]],
                         [self.nf3.key, self.pos.key])

    def test_a_flagged_name_says_under_review(self):
        flag, _ = name_votes.flag(self.pos.key, 'Belfast Gambit',
                                  community_names.in_force(self.pos.key),
                                  0, 4)
        flag.discord_message_id = '42'
        flag.save()
        response = self.client.get('/atomicdb/roots/')
        row = next(r for r in response.context['openings']
                   if r['key'] == self.pos.key)
        self.assertTrue(row['under_review'])
        self.assertIn('/1570156441153835039/42', row['poll_url'])

    def test_the_api_rows_carry_the_votes(self):
        self._vote(self.pos.key, 'up')
        data = self.client.get('/atomicdb/api/roots/').json()
        row = next(r for r in data['openings'] if r['key'] == self.pos.key)
        self.assertEqual((row['votes_up'], row['votes_down'],
                          row['under_review']), (1, 0, False))
        self.assertNotIn('votes_up', data['positions'][0])

    def test_the_page_form_passes_the_csrf_check(self):
        client = Client(enforce_csrf_checks=True)
        body = client.get('/atomicdb/roots/').content.decode()
        token = re.search(r'name="csrfmiddlewaretoken"[^>]*value="([^"]+)"',
                          body).group(1)
        response = client.post(f'/atomicdb/names/vote/{self.pos.key}/',
                               {'vote': 'up', 'csrfmiddlewaretoken': token},
                               HTTP_X_REQUESTED_WITH='fetch')
        self.assertEqual(response.json()['status'], 'voted')
        self.assertEqual(Client(enforce_csrf_checks=True).post(
            f'/atomicdb/names/vote/{self.pos.key}/',
            {'vote': 'up'}).status_code, 403)

    def test_a_vote_without_script_is_answered_on_the_page(self):
        body = self.client.get('/atomicdb/roots/?voted=voted').content.decode()
        self.assertIn('Your vote is in.', body)


class HeuristicTests(TestCase):

    def test_names_are_compared_without_case_punctuation_or_spelling(self):
        self.assertEqual(name_review.normalised("Queen's Gambit"),
                         name_review.normalised('queens gambit'))
        self.assertEqual(name_review.normalised('Wolfram Defence'),
                         name_review.normalised('Wolfram Defense'))

    def test_a_line_that_continues_another_is_related(self):
        a = name_review.line_tokens('1. Nf3 f6 2. Nd4 c6 3. e3 e6')
        b = name_review.line_tokens('1. Nf3 f6 2. Nd4 c6 3. e3 e6 4. Nb5')
        c = name_review.line_tokens('1. e3 e6 2. Qg4')
        self.assertTrue(name_review.related(a, b))
        self.assertFalse(name_review.related(a, c))

    def test_jokes_spam_and_handles(self):
        catalogue = frozenset({'trojanknight'})
        handles = frozenset({'astiel', 'rhaxier'})
        cases = {
            'ReChesster Spam': True,         # spam and a handle-like word
            'KidneyTaker': True,
            'Biggest Floppa defence': True,
            'RKROUNIT ANTISPAM': True,
            'Astiel Variation': True,        # a known account
            'StealYourKidney2 line': True,
            'Atomic Attack, Nightrider Defence': False,
            'TrojanKnight Opening': False,   # a catalogue word
            'Atomic Attack, 4.Nb5': False,   # a move, not a handle
            "King's Knight Opening": False,
            'Susan Gambit': False,           # not a joke word inside a word
        }
        for name, expected in cases.items():
            self.assertEqual(bool(name_review.joke_reasons(
                name, '', handles, catalogue)), expected, name)
        self.assertIn('names the account that proposed it',
                      name_review.joke_reasons('Sircachetes Attack',
                                               'Sircachetes'))

    def test_compute_reads_the_groups_from_the_database(self):
        community_names.invalidate()
        joke = _position(('a2a3',))
        dup1 = _position(('a2a3', 'a7a6'))
        dup2 = _position(('h2h3', 'h7h6'))
        renamed = _position(('h2h3', 'h7h6', 'h1h2'))
        self.assertIsNone(openings.lookup_key(renamed.key))
        _approved(joke, 'Noob stomper')                       # anonymous too
        _approved(dup1, 'Wolfram Defense', suggested_by='x1')
        _approved(dup2, 'Wolfram Defence', suggested_by='x2')
        _approved(renamed, 'First', suggested_by='a1')
        _approved(renamed, 'Second', kind=OpeningNameSuggestion.SKind.EDIT,
                  previous_name='First', suggested_by='a2')
        _approved(renamed, 'Third Name', kind=OpeningNameSuggestion.SKind.EDIT,
                  previous_name='Second', suggested_by='a3')
        nf3 = _position(('g1f3',))
        _approved(nf3, "King's Knight Opening",
                  kind=OpeningNameSuggestion.SKind.EDIT,
                  previous_name=CATALOGUED, suggested_by='void')
        community_names.invalidate()
        marks = name_review.compute(lines={
            joke.key: '1. a3', dup1.key: '1. a3 a6', dup2.key: '1. h3 h6',
            renamed.key: '1. h3 h6 2. Rh2'})
        reasons = name_review.REASONS
        self.assertIn(reasons['joke'], marks[joke.key])
        self.assertIn(reasons['anonymous'], marks[joke.key])
        self.assertIn(reasons['same-name'], marks[dup1.key])
        self.assertIn(reasons['same-name'], marks[dup2.key])
        self.assertEqual(marks[renamed.key], [reasons['renamed']])
        self.assertEqual(marks[nf3.key], [reasons['replaces-catalogue']])

    def test_the_page_shows_the_hint_on_community_names_only(self):
        pos = _position(('a2a3',))
        _approved(pos, 'Lesha Spam')
        community_names.invalidate()
        name_review.invalidate()
        rows = roots.page_context()['openings']
        row = next(r for r in rows if r['key'] == pos.key)
        self.assertIn(name_review.REASONS['joke'], row['review_hint'])
        self.assertTrue(all(r['review_hint'] == '' for r in rows
                            if not r['community']))
        self.assertTrue(Position.objects.filter(key=pos.key).exists())
