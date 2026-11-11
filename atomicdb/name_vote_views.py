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
"""

from urllib.parse import urlencode

from django.http import JsonResponse
from django.shortcuts import redirect

from . import community_names, name_votes, views

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
            'poll_url': name_votes.poll_url(row),
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
