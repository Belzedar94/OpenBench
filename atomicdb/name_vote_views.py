"""Pages and endpoints of the opening-name votes (§ ``name_votes``).

* ``POST /atomicdb/names/vote/<key>/`` -- a thumbs up, a thumbs down or a
  withdrawal, from the Openings page.  JSON for the page's script, and a
  plain redirect back to the page for a browser without it.
* ``GET /atomicdb/names/votes/`` -- every tally and the visitor's own votes,
  live.  The Openings page is cached; its script reads this to show the
  numbers as they are now.
* ``POST /atomicdb/suggestions/flags/`` -- an approver's decision on a name
  under review, from the moderation page.  Goes through the ordinary
  approval of ``OpeningNameSuggestion`` (``name_votes.apply_flag``).
* ``POST /atomicdb/suggestions/hide/`` -- an approver hides the name of any
  position, or shows a hidden one again (``name_votes.hide_position``,
  ``show_position``).  The same approval path.
* ``POST /atomicdb/suggestions/line/`` -- an approver declares the move
  order of a named position, or clears it (``name_votes.declare_line``).
"""

from urllib.parse import urlencode

from django.http import JsonResponse
from django.shortcuts import redirect

from . import community_names, name_votes, opening_routes, views

ROOTS_URL = '/atomicdb/roots/'
FLAGS_SHOWN = 100


def _wants_json(request):
    return (request.headers.get('X-Requested-With') == 'fetch'
            or 'application/json' in request.headers.get('Accept', ''))


def _with_cookie(response, token, minted):
    if minted:
        response.set_cookie(views.CAMPAIGN_VOTER_COOKIE, token,
                            max_age=views.CAMPAIGN_VOTER_COOKIE_SECONDS,
                            samesite='Lax', httponly=True)
    return response


def _back_url(request, key, status):
    """Back to the Openings page, on the row voted.  The sort comes back
    only if it is one the page knows, so this cannot redirect anywhere
    else.  ``voted`` changes the URL, so the cached page is not served."""
    from . import roots
    query = {'voted': status}
    sort = request.POST.get('sort', '')
    if sort.lstrip('-') in roots.SORTS:
        query['sort'] = sort
    return f'{ROOTS_URL}?{urlencode(query)}#op-{key[:64]}'


def vote(request, key):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)
    key = (key or '')[:64]
    token, minted = views._voter_token(request)
    user = getattr(request, 'user', None)
    voter = name_votes.voter_for(user, token)
    merge = None
    if user is not None and user.is_authenticated and not minted:
        merge = name_votes.TOKEN_PREFIX + token
    choice = (request.POST.get('vote') or '').strip().lower()
    result = name_votes.cast(key, choice, voter=voter,
                             ip=views._client_ip(request), minted=minted,
                             merge_voter=merge,
                             toggle=request.POST.get('toggle') == '1')
    codes = {'unnamed': 404, 'invalid': 400, 'rate-limited': 429}
    if _wants_json(request):
        response = JsonResponse(result, status=codes.get(result['status'],
                                                         200))
    else:
        response = redirect(_back_url(request, key, result['status']))
    return _with_cookie(response, token, minted)


def votes(request):
    """Every tally of the names shown, and the visitor's own votes.

    Live and small: one grouped query over the votes, and one more for the
    visitor's own rows.  Nothing is written, so no cookie is minted here.
    """
    current = name_votes.current_names()
    counts = name_votes.tallies(current)
    raw = (request.COOKIES.get(views.CAMPAIGN_VOTER_COOKIE) or '').strip()
    user = getattr(request, 'user', None)
    mine = {}
    if user is not None and user.is_authenticated:
        mine = name_votes.votes_of(name_votes.voter_for(user, ''), current)
    elif views.CAMPAIGN_VOTER_RE.fullmatch(raw):
        mine = name_votes.votes_of(name_votes.TOKEN_PREFIX + raw, current)
    flagged = {row.position_key: name_votes.poll_url(row)
               for row in name_votes.open_flags()
               if current.get(row.position_key) == row.name}
    response = JsonResponse({
        'votes': {key: [up, down] for key, (up, down) in counts.items()},
        'mine': mine,
        'under_review': flagged,
        'min_votes': name_votes.min_votes(),
    })
    response['Cache-Control'] = 'no-store'
    return response


def flags_context():
    """The "Names under review" section of the moderation page."""
    rows = list(name_votes.open_flags()[:FLAGS_SHOWN])
    names = community_names.approved_map()
    current = {row.position_key: name_votes.current_name(
        row.position_key, names=names) for row in rows}
    counts = name_votes.tallies({key: name for key, (name, _entry)
                                 in current.items() if name})
    labels = views._line_labels_many([row.position_key for row in rows])
    declared = opening_routes.routes()
    items = []
    for row in rows:
        name, entry = current[row.position_key]
        up, down = counts.get(row.position_key, (0, 0))
        preview, full = labels.get(row.position_key, ('', ''))
        catalogue = community_names.catalog_name(row.position_key) or ''
        items.append({
            'flag': row,
            'up': up, 'down': down,
            'san': preview or row.position_key[:12],
            'full': full,
            'catalogue': catalogue,
            'community': entry is not None,
            'can_restore': (entry is not None and bool(catalogue)
                            and catalogue != name),
            'can_remove': entry is not None,
            'can_hide': name is not None,
            'poll_url': name_votes.poll_url(row),
            'url': opening_routes.name_url(row.position_key, declared),
        })
    # Most decided first: the widest margin of thumbs down.
    items.sort(key=lambda item: (-(item['down'] - item['up']),
                                 item['flag'].flagged_at))
    return {'flags': items, 'flags_total': name_votes.open_flags().count(),
            'flag_min_votes': name_votes.min_votes()}


def flag_action(request):
    denied = views._approver_gate(request)
    if denied is not None:
        return denied
    if request.method != 'POST':
        return redirect('/atomicdb/suggestions/#review')
    try:
        flag_id = int(request.POST.get('flag', ''))
    except (TypeError, ValueError):
        flag_id = None
    action = request.POST.get('action', '')
    new_name = ' '.join((request.POST.get('name') or '').split())
    outcome = 'invalid'
    if flag_id is not None:
        outcome = name_votes.apply_flag(
            flag_id, action, approver=request.user.username,
            ip=views._client_ip(request), new_name=new_name)
    return redirect(f'/atomicdb/suggestions/?review={outcome}#review')


HIDDEN_SHOWN = 100


def hidden_context():
    """The "Hidden names" section of the moderation page: every position
    whose name is hidden, newest first."""
    hidden = community_names.hidden_map()
    keys = sorted(hidden, key=lambda key: (
        hidden[key]['approved_at'] is not None,
        hidden[key]['approved_at'].isoformat()
        if hidden[key]['approved_at'] is not None else '', key),
        reverse=True)[:HIDDEN_SHOWN]
    labels = views._line_labels_many(keys) if keys else {}
    items = []
    for key in keys:
        preview, full = labels.get(key, ('', ''))
        info = hidden[key]
        items.append({'key': key, 'name': info['previous'],
                      'by': info['approved_by'], 'at': info['approved_at'],
                      'san': preview or key[:12], 'full': full})
    return {'hidden_names': items, 'hidden_total': len(hidden)}


HIDE_OUTCOMES = frozenset(('hidden', 'shown', 'unnamed', 'not-hidden',
                           'invalid'))


def hide_action(request):
    """An approver hides a name, or shows a hidden one again, from the
    moderation page (§ ``name_votes.hide_position``)."""
    denied = views._approver_gate(request)
    if denied is not None:
        return denied
    if request.method != 'POST':
        return redirect('/atomicdb/suggestions/#hidden')
    key = name_votes.position_key_in(request.POST.get('position'))
    comment = ' '.join((request.POST.get('comment') or '').split())
    action = request.POST.get('action', '')
    outcome = 'invalid'
    if key and action == 'hide':
        outcome = name_votes.hide_position(
            key, approver=request.user.username,
            ip=views._client_ip(request),
            comment=comment[:views.SUGGESTION_COMMENT_MAX_CHARS])
    elif key and action == 'show':
        outcome = name_votes.show_position(
            key, approver=request.user.username,
            ip=views._client_ip(request))
    return redirect(f'/atomicdb/suggestions/?hide={outcome}#hidden')


ROUTES_SHOWN = 200


def routes_context():
    """The "Move orders" section of the moderation page: every declared
    line, newest first, with the name it opens."""
    from .models import OpeningRoute
    rows = list(OpeningRoute.objects.order_by('-set_at')[:ROUTES_SHOWN])
    names = community_names.approved_map()
    items = []
    for row in rows:
        name, _entry = name_votes.current_name(row.position_key, names=names)
        items.append({'key': row.position_key, 'name': name or '',
                      'line_san': row.line_san or row.line,
                      'url': opening_routes.explore_url(
                          row.position_key, row.line.split(',')),
                      'by': row.set_by, 'at': row.set_at})
    return {'routes': items, 'routes_total': OpeningRoute.objects.count()}


def line_action(request):
    """An approver declares the move order of a named position, or clears
    it, from the moderation page (§ ``opening_routes``)."""
    denied = views._approver_gate(request)
    if denied is not None:
        return denied
    if request.method != 'POST':
        return redirect('/atomicdb/suggestions/#lines')
    outcome = name_votes.declare_line(
        request.POST.get('position'), request.POST.get('line'),
        clear=request.POST.get('action') == 'clear',
        approver=request.user.username)
    return redirect(f'/atomicdb/suggestions/?line={outcome}#lines')
