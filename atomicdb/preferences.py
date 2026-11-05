"""Per-account settings that the site honours, and the page that edits them.

They live in ``ContributorPref``, one row per account created on first use,
the same row the "newest first" queue option already uses.  An account
without a row has every default, so reading a setting never writes.
"""

from urllib.parse import quote

from django.shortcuts import redirect, render

from .models import ContributorPref


def lifo_on(username):
    """Whether this account's new requests start at the front of its queue."""
    if not username:
        return False
    return ContributorPref.objects.filter(
        account=username, lifo_queue=True).exists()


# How an account's workers order the visitor requests: their own first (the
# default), plain queue order, or everybody else's first.
SERVE_ORDERS = ('own', 'queue', 'others')


def serve_order(username):
    """``'own'``, ``'queue'`` or ``'others'`` for this account's workers.

    Read once per lease by ``views.choose_pending``.  It only reorders the
    USER band, so it can never leave a worker without a task.
    """
    if not username:
        return 'own'
    value = (ContributorPref.objects.filter(account=username)
             .values_list('serve_order', flat=True).first())
    return value if value in SERVE_ORDERS else 'own'


# The most accounts one "serve these first" list keeps.
SERVE_FOR_MAX = 20


def parse_serve_for(raw):
    """The account names in a comma (or space) separated list, deduplicated
    without case, in the order given, at most SERVE_FOR_MAX."""
    names, seen = [], set()
    for part in (raw or '').replace(',', ' ').split():
        name = part.strip()[:64]
        if name and name.lower() not in seen:
            seen.add(name.lower())
            names.append(name)
    return names[:SERVE_FOR_MAX]


def serve_for(username):
    """The accounts this account's workers serve first, or ``[]``.

    Only with "Others first": the list refines that choice.
    """
    if not username:
        return []
    row = (ContributorPref.objects.filter(account=username)
           .values_list('serve_order', 'serve_for').first())
    if not row or row[0] != 'others':
        return []
    return parse_serve_for(row[1])


def serves_others_first(username):
    """Whether this account's workers serve everybody else's requests first."""
    return serve_order(username) == 'others'


def ready_notice_muted(usernames):
    """The accounts among ``usernames`` that turned the ready notice off.

    One query for every requester of a task; an account without a row is
    not muted.
    """
    names = [name for name in usernames if name]
    if not names:
        return set()
    return set(ContributorPref.objects.filter(
        account__in=names, notify_ready=False)
        .values_list('account', flat=True))


def notification_settings(request):
    """Which notices this account receives, and how its workers pick
    requests.  GET shows, POST saves."""
    path = '/atomicdb/notifications/settings/'
    if not request.user.is_authenticated:
        return redirect(f'/login/?next={quote(path, safe="")}')
    username = request.user.username
    if request.method == 'POST':
        pref, _created = ContributorPref.objects.get_or_create(
            account=username)
        pref.notify_ready = request.POST.get('ready') == '1'
        fields = ['notify_ready']
        # A form without the field (a page loaded before it existed) leaves
        # the setting as it was instead of switching it off.  ``serve`` is
        # the checkbox of the page before the three choices.
        order = request.POST.get('serve_order')
        if order not in SERVE_ORDERS and 'serve' in request.POST:
            order = 'others' if request.POST.get('serve') == '1' else 'own'
        if order in SERVE_ORDERS:
            pref.serve_order = order
            pref.serve_others_first = order == 'others'
            fields += ['serve_order', 'serve_others_first']
        if 'serve_for' in request.POST:
            pref.serve_for = ', '.join(
                parse_serve_for(request.POST.get('serve_for')))
            fields.append('serve_for')
        # Newest first lives here too now (seaside_cliff, 4-Nov-2026), with
        # the same reading as the cog's switch.
        if 'lifo' in request.POST:
            pref.lifo_queue = request.POST.get('lifo') == '1'
            fields.append('lifo_queue')
        pref.save(update_fields=fields)
        return redirect(f'{path}?saved=1')
    pref = ContributorPref.objects.filter(account=username).first()
    return render(request, 'atomicdb/notification_settings.html', {
        'notify_ready': pref.notify_ready if pref else True,
        'serve_order': (pref.serve_order if pref
                        and pref.serve_order in SERVE_ORDERS else 'own'),
        'serve_for': pref.serve_for if pref else '',
        'lifo_on': bool(pref and pref.lifo_queue),
        'saved': request.GET.get('saved') == '1',
    })
