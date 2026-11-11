"""Posting the names under review to Discord as native polls.

Owner, 11-Nov-2026: a name the votes put under review (``name_votes``) goes
to the #opening-names channel of the community server as a Discord poll,
once per flag, with its line, its current name, a link and who named it.
A thread on the message is where better names are proposed.

The job is ``manage.py post_opening_name_flags``, run by a systemd timer
like the other Discord feeds of the server.  It reads the bot token from a
root-only file and never prints or logs it.  Dedupe is the flag row itself:
a flag is posted when it has no ``discord_message_id`` yet, and the id is
saved in the same step, so a second run does nothing.  A failed post is
retried on the next runs up to ``MAX_ATTEMPTS`` times.

The poll is advisory.  The approver applies an outcome from the moderation
page, through the ordinary approval path.
"""

import json
import logging
import os
import urllib.error
import urllib.request

from django.conf import settings
from django.utils import timezone

from . import community_names, name_votes, openings
from .models import OpeningNameFlag

logger = logging.getLogger(__name__)

API = 'https://discord.com/api/v10'
USER_AGENT = 'DiscordBot (https://belzedar.duckdns.org, 1.0)'
DEFAULT_GUILD = '1532350146367586304'
DEFAULT_CHANNEL = '1570156441153835039'       # #opening-names
DEFAULT_TOKEN_FILE = '/etc/openbench/discord-bot.token'
DEFAULT_SITE = 'https://belzedar.duckdns.org'
POLL_HOURS = 72
MAX_ATTEMPTS = 5
PER_RUN = 5
# Discord's limits: question 300 characters, answer 55, message 2000,
# thread name 100.
QUESTION_MAX, ANSWER_MAX, CONTENT_MAX, THREAD_MAX = 300, 55, 2000, 100


def _setting(name, default):
    return str(getattr(settings, name, None) or os.environ.get(name)
               or default)


def guild():
    return _setting('ATOMICDB_DISCORD_GUILD', DEFAULT_GUILD)


def channel():
    return _setting('ATOMICDB_OPENING_NAMES_CHANNEL', DEFAULT_CHANNEL)


def site():
    return _setting('ATOMICDB_PUBLIC_URL', DEFAULT_SITE).rstrip('/')


def message_url(message_id):
    return f'https://discord.com/channels/{guild()}/{channel()}/{message_id}'


def read_token():
    """The bot token, from ``ATOMICDB_DISCORD_TOKEN_FILE`` (default
    ``/etc/openbench/discord-bot.token``).  Never printed."""
    path = _setting('ATOMICDB_DISCORD_TOKEN_FILE', DEFAULT_TOKEN_FILE)
    with open(path, encoding='utf-8') as handle:
        return handle.read().strip()


def _clip(text, limit):
    return text if len(text) <= limit else text[:limit - 1] + '…'


def line_of(key):
    """The SAN line of a position: the catalogue's reference line, else the
    tree's breadcrumb."""
    try:
        opening = openings.load_catalog().get(key)
    except openings.OpeningCatalogError:
        opening = None
    if opening is not None and opening.reference_line_san:
        return opening.reference_line_san
    from . import views
    _preview, full = views._line_labels_many([key]).get(key, ('', ''))
    return full


def answers_for(flag_row, catalogue, community):
    """The poll's options: Keep, then Restore and Remove where they mean
    something, then Rename."""
    options = ['Keep']
    if community and catalogue and catalogue != flag_row.name:
        options.append(_clip(f'Restore catalogue name: {catalogue}',
                             ANSWER_MAX))
    if community:
        options.append('Remove')
    options.append('Rename (propose in the thread)')
    return options


def payload(flag_row, line=None):
    """The message with its poll, as Discord's API takes it."""
    key = flag_row.position_key
    name, entry = name_votes.current_name(key)
    community = entry is not None
    catalogue = community_names.catalog_name(key) or ''
    up, down = name_votes.tally(key, flag_row.name)
    if line is None:
        line = line_of(key)
    if community:
        who = community_names.credit(entry)
        origin = f'Community name, named by {who}.'
        if catalogue and catalogue != flag_row.name:
            origin += f' Catalogue name: {catalogue}.'
    else:
        origin = 'Catalogue name.'
    url = f'{site()}/atomicdb/explore/{key}/'
    content = '\n'.join([
        f'**{flag_row.name}** is under review: '
        f'\U0001F44D {up} / \U0001F44E {down} on the Openings page.',
        f'Line: {line or "(not in the tree yet)"}',
        origin,
        url,
        'The poll is advisory: a moderator applies the outcome. '
        'Propose a better name in the thread.',
    ])
    return {
        'content': _clip(content, CONTENT_MAX),
        'allowed_mentions': {'parse': []},
        'poll': {
            'question': {'text': _clip(
                f'What should we do with "{flag_row.name}"?', QUESTION_MAX)},
            'answers': [{'poll_media': {'text': text}}
                        for text in answers_for(flag_row, catalogue,
                                                community)],
            'duration': POLL_HOURS,
            'allow_multiselect': False,
        },
    }


def _request(token, method, path, body):
    data = None if body is None else json.dumps(body).encode('utf-8')
    req = urllib.request.Request(API + path, data=data, method=method,
                                 headers={'Authorization': 'Bot ' + token,
                                          'User-Agent': USER_AGENT,
                                          'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read()
    return json.loads(raw) if raw else {}


def _error_text(exc):
    """What is kept of a failure: the status and Discord's own message,
    never a header (the token travels in one)."""
    if isinstance(exc, urllib.error.HTTPError):
        try:
            detail = json.loads(exc.read() or b'{}').get('message', '')
        except Exception:
            detail = ''
        return f'HTTP {exc.code} {detail}'.strip()[:200]
    return type(exc).__name__[:200]


def post_flag(flag_row, send):
    """Post one flag with ``send(method, path, body) -> dict``; True when
    the poll is out.  The message id is saved at once; the thread is a
    second, best-effort call."""
    body = payload(flag_row)
    try:
        message = send('POST', f'/channels/{channel()}/messages', body)
    except Exception as exc:
        flag_row.post_attempts += 1
        flag_row.post_error = _error_text(exc)
        flag_row.save(update_fields=['post_attempts', 'post_error'])
        logger.warning('could not post the review of %s: %s',
                       flag_row.position_key, flag_row.post_error)
        return False
    flag_row.discord_message_id = str(message.get('id', ''))[:32]
    flag_row.posted_at = timezone.now()
    flag_row.post_attempts += 1
    flag_row.post_error = ''
    flag_row.save(update_fields=['discord_message_id', 'posted_at',
                                 'post_attempts', 'post_error'])
    try:
        thread = send('POST', f'/channels/{channel()}/messages/'
                              f'{flag_row.discord_message_id}/threads',
                      {'name': _clip(f'Rename: {flag_row.name}', THREAD_MAX),
                       'auto_archive_duration': 4320})
        flag_row.discord_thread_id = str(thread.get('id', ''))[:32]
        flag_row.save(update_fields=['discord_thread_id'])
    except Exception as exc:
        logger.warning('no thread for the review of %s: %s',
                       flag_row.position_key, _error_text(exc))
    return True


def due():
    """Open flags still to post, oldest first."""
    return (OpeningNameFlag.objects
            .filter(resolved_at__isnull=True, discord_message_id='',
                    post_attempts__lt=MAX_ATTEMPTS)
            .order_by('flagged_at', 'id'))


def run(send=None, limit=PER_RUN, dry_run=False, out=None):
    """One pass: sync the flags with the votes, then post what is due.
    Returns ``(new, stale, posted, failed)``."""
    new, stale = name_votes.sync_flags()
    rows = list(due()[:limit])
    posted = failed = 0
    if dry_run:
        for row in rows:
            if out is not None:
                out(json.dumps(payload(row), ensure_ascii=False, indent=1))
        return new, stale, 0, 0
    if rows and send is None:
        token = read_token()

        def send(method, path, body):
            return _request(token, method, path, body)
    for row in rows:
        if post_flag(row, send):
            posted += 1
        else:
            failed += 1
    return new, stale, posted, failed
