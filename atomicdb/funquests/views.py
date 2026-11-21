"""Fun side quests: small games next to the analysis tree.

The first quest is Alice Atomic (``alice_atomic``): play the computer in the
browser, or a friend through two private links.  Nothing here reads or writes
the analysis tree; the games live on ``AliceGame`` rows of their own.

Endpoints of a game between two people, all JSON:

* ``POST fun/alice-atomic/api/create`` (``color`` = white, black or random):
  a new game, and the two private links, one per side.
* ``POST fun/alice-atomic/api/<token>/join``: the side of ``token`` has
  opened its link.  The game goes live once both sides have.
* ``GET fun/alice-atomic/api/<token>/state?since=<version>``: the game.  With
  ``since`` equal to the current version the answer is a tiny "nothing new";
  ``If-None-Match`` with the ETag gets a 304.
* ``POST fun/alice-atomic/api/<token>/move`` (``move``, optional ``ply``).
* ``POST fun/alice-atomic/api/<token>/resign``.
* ``POST fun/alice-atomic/api/<token>/draw`` (``action`` = offer, accept,
  decline or withdraw).
"""

import secrets
from datetime import timedelta

from django.db import transaction
from django.http import HttpResponse, HttpResponseNotAllowed, JsonResponse
from django.shortcuts import render
from django.utils import timezone

from .. import database
from ..models import AliceGame
from . import alice_atomic as rules

ABANDON_AFTER = timedelta(hours=24)
CREATE_LIMIT_PER_HOUR = 30
MAX_PLIES = 1000
BASE_URL = '/atomicdb/fun/alice-atomic/'

QUESTS = (
    {
        'slug': 'alice-atomic',
        'title': 'Alice Atomic',
        'url': BASE_URL,
        'blurb': ('Atomic chess on two boards. Every move sends the '
                  'piece through the looking glass to the other board, '
                  'and a capturing piece explodes over there. Play the '
                  'computer or send a link to a friend.'),
    },
    {
        'slug': 'tictactoedb',
        'title': 'TicTacToeDB',
        'url': '/atomicdb/fun/tictactoedb/',
        'art': 'ttt',
        'blurb': ('Tic-tac-toe, solved. Every move shows whether it wins, '
                  'draws or loses with best play, and how fast.'),
    },
)


def _client_ip(request):
    """Last X-Forwarded-For entry: our own nginx put it there."""
    return (request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')[-1].strip()
            or request.META.get('REMOTE_ADDR') or None)


def _username(request):
    user = getattr(request, 'user', None)
    if user is not None and user.is_authenticated:
        return user.get_username()[:150]
    return ''


# -- pages -------------------------------------------------------------------

def fun_index(request):
    return render(request, 'atomicdb/funquests/index.html',
                  {'quests': QUESTS})


def alice_page(request):
    return render(request, 'atomicdb/funquests/alice_atomic.html', {
        'token': '',
        'start_afen': rules.START_AFEN,
    })


def tictactoedb_page(request):
    # All of it runs in the browser: the page only hands out the script.
    return render(request, 'atomicdb/funquests/tictactoedb.html')


def alice_game_page(request, token):
    game, _color = _find(token)
    if game is None:
        return render(request, 'atomicdb/funquests/alice_missing.html',
                      status=404)
    return render(request, 'atomicdb/funquests/alice_atomic.html', {
        'token': token,
        'start_afen': rules.START_AFEN,
    })


# -- games between two people -------------------------------------------------

def _find(token):
    if not token or len(token) > 32:
        return None, None
    game = AliceGame.objects.filter(white_token=token).first()
    if game is not None:
        return game, 'w'
    game = AliceGame.objects.filter(black_token=token).first()
    if game is not None:
        return game, 'b'
    return None, None


def _locked(token):
    """The game of ``token``, locked for the rest of the transaction."""
    rows = AliceGame.objects.select_for_update()
    game = rows.filter(white_token=token).first()
    if game is not None:
        return game, 'w'
    game = rows.filter(black_token=token).first()
    if game is not None:
        return game, 'b'
    return None, None


def _touch(game, now=None):
    game.version += 1
    game.updated = now or timezone.now()


def _expire(game, now=None):
    """Mark a game nobody has moved in for a day as abandoned.  Returns
    whether it changed."""
    now = now or timezone.now()
    if (game.state in (AliceGame.State.WAITING, AliceGame.State.LIVE)
            and now - game.updated > ABANDON_AFTER):
        game.state = AliceGame.State.ABANDONED
        game.draw_offer = ''
        game.version += 1
        game.save(update_fields=['state', 'draw_offer', 'version'])
        return True
    return False


def _link(token):
    return f'{BASE_URL}play/{token}/'


def _payload(game, color):
    other = 'b' if color == 'w' else 'w'
    data = {
        'version': game.version,
        'state': game.state,
        'afen': game.afen,
        'moves': game.moves.split() if game.moves else [],
        'ply': game.ply,
        'result': game.result,
        'reason': game.reason,
        'draw_offer': game.draw_offer,
        'you': color,
        'white': {'name': game.white_name, 'joined': game.white_joined},
        'black': {'name': game.black_name, 'joined': game.black_joined},
    }
    # The side that created the game still has to send the other link.
    other_joined = game.black_joined if other == 'b' else game.white_joined
    if game.state == AliceGame.State.WAITING and not other_joined:
        data['invite_url'] = _link(game.black_token if other == 'b'
                                   else game.white_token)
    return data


def _respond(game, color, status=200, error=None):
    data = _payload(game, color)
    if error:
        data['error'] = error
    response = JsonResponse(data, status=status)
    response['ETag'] = f'"{game.version}"'
    response['Cache-Control'] = 'no-store'
    return response


def _post_only(request):
    if request.method != 'POST':
        return HttpResponseNotAllowed(['POST'])
    return None


def api_create(request):
    refused = _post_only(request)
    if refused:
        return refused
    color = request.POST.get('color', 'white')
    if color == 'random':
        color = secrets.choice(('white', 'black'))
    if color not in ('white', 'black'):
        return JsonResponse({'error': 'Pick white, black or random.'},
                            status=400)
    ip = _client_ip(request)
    now = timezone.now()
    if ip and AliceGame.objects.filter(
            creator_ip=ip, created__gte=now - timedelta(hours=1)
    ).count() >= CREATE_LIMIT_PER_HOUR:
        return JsonResponse(
            {'error': 'Too many new games from here in the last hour. '
                      'Try again a bit later.'}, status=429)
    name = _username(request)
    game = AliceGame(
        white_token=secrets.token_hex(12),
        black_token=secrets.token_hex(12),
        afen=rules.START_AFEN,
        seen=rules.position_hash(rules.start_state()),
        creator_ip=ip, created=now, updated=now, version=1)
    if color == 'white':
        game.white_joined, game.white_name = True, name
    else:
        game.black_joined, game.black_name = True, name
    game.save()
    mine = game.white_token if color == 'white' else game.black_token
    theirs = game.black_token if color == 'white' else game.white_token
    return JsonResponse({
        'color': color,
        'your_url': _link(mine),
        'invite_url': _link(theirs),
    }, status=201)


def api_join(request, token):
    refused = _post_only(request)
    if refused:
        return refused
    with transaction.atomic(using=database.current_alias()):
        game, color = _locked(token)
        if game is None:
            return JsonResponse({'error': 'No such game.'}, status=404)
        if _expire(game):
            return _respond(game, color)
        changed = False
        name = _username(request)
        if color == 'w':
            if not game.white_joined:
                game.white_joined, changed = True, True
            if name and not game.white_name:
                game.white_name, changed = name, True
        else:
            if not game.black_joined:
                game.black_joined, changed = True, True
            if name and not game.black_name:
                game.black_name, changed = name, True
        if (game.state == AliceGame.State.WAITING
                and game.white_joined and game.black_joined):
            game.state, changed = AliceGame.State.LIVE, True
        if changed:
            _touch(game)
            game.save()
    return _respond(game, color)


def api_state(request, token):
    if request.method != 'GET':
        return HttpResponseNotAllowed(['GET'])
    game, color = _find(token)
    if game is None:
        return JsonResponse({'error': 'No such game.'}, status=404)
    _expire(game)
    etag = f'"{game.version}"'
    if request.headers.get('If-None-Match') == etag:
        response = HttpResponse(status=304)
        response['ETag'] = etag
        return response
    if request.GET.get('since') == str(game.version):
        response = JsonResponse({'version': game.version, 'changed': False})
        response['ETag'] = etag
        response['Cache-Control'] = 'no-store'
        return response
    return _respond(game, color)


def _live_or_error(game, color):
    if game is None:
        return JsonResponse({'error': 'No such game.'}, status=404)
    if _expire(game):
        return _respond(game, color, 409, 'This game was abandoned.')
    if game.state == AliceGame.State.WAITING:
        return _respond(game, color, 409,
                        'Your opponent has not opened their link yet.')
    if game.state != AliceGame.State.LIVE:
        return _respond(game, color, 409, 'This game is over.')
    return None


def api_move(request, token):
    refused = _post_only(request)
    if refused:
        return refused
    text = request.POST.get('move', '').strip()
    with transaction.atomic(using=database.current_alias()):
        game, color = _locked(token)
        refused = _live_or_error(game, color)
        if refused:
            return refused
        expected = request.POST.get('ply')
        if expected not in (None, '') and expected != str(game.ply):
            return _respond(game, color, 409,
                            'The game moved on; here it is as it stands.')
        state = rules.parse(game.afen)
        if state.turn != color:
            return _respond(game, color, 409, 'It is not your turn.')
        try:
            after = rules.play(state, text)
        except rules.IllegalMove:
            return _respond(game, color, 400, 'That move is not legal.')
        seen = game.seen.split() if after.halfmove else []
        key = rules.position_hash(after)
        seen.append(key)
        game.afen = rules.to_afen(after)
        game.moves = (game.moves + ' ' + text).strip()
        game.ply += 1
        game.seen = ' '.join(seen)
        # A move answers the opponent's draw offer with a no.
        if game.draw_offer and game.draw_offer != color:
            game.draw_offer = ''
        found = rules.outcome(after, seen.count(key))
        if found is None and game.ply >= MAX_PLIES:
            found = ('1/2-1/2', 'length')
        if found:
            game.result, game.reason = found
            game.state = AliceGame.State.FINISHED
            game.draw_offer = ''
        _touch(game)
        game.save()
    return _respond(game, color)


def api_resign(request, token):
    refused = _post_only(request)
    if refused:
        return refused
    with transaction.atomic(using=database.current_alias()):
        game, color = _locked(token)
        refused = _live_or_error(game, color)
        if refused:
            return refused
        game.result = '0-1' if color == 'w' else '1-0'
        game.reason = 'resignation'
        game.state = AliceGame.State.FINISHED
        game.draw_offer = ''
        _touch(game)
        game.save()
    return _respond(game, color)


def api_draw(request, token):
    refused = _post_only(request)
    if refused:
        return refused
    action = request.POST.get('action', '')
    with transaction.atomic(using=database.current_alias()):
        game, color = _locked(token)
        refused = _live_or_error(game, color)
        if refused:
            return refused
        offer = game.draw_offer
        if action in ('accept', 'offer') and offer and offer != color:
            # Offering a draw the other side already offered agrees to it.
            game.result, game.reason = '1/2-1/2', 'agreement'
            game.state = AliceGame.State.FINISHED
            game.draw_offer = ''
        elif action == 'offer' and not offer:
            game.draw_offer = color
        elif action == 'decline' and offer and offer != color:
            game.draw_offer = ''
        elif action == 'withdraw' and offer == color:
            game.draw_offer = ''
        else:
            return _respond(game, color, 409, 'Nothing to do with that.')
        _touch(game)
        game.save()
    return _respond(game, color)
