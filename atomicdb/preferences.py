"""Per-account settings that the site honours, and the page that edits them.

They live in ``ContributorPref``, one row per account created on first use,
the same row the "newest first" queue option already uses.  An account
without a row has every default, so reading a setting never writes.
"""

from urllib.parse import quote

from django.shortcuts import redirect, render

from .models import Campaign, ContributorPref, WorkerPing, WorkerServing


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
    """The accounts this account's workers favour, or ``[]``.

    With Others first they go before anyone; with Mine first they share the
    account's own band (§ views.choose_pending).  Queue order ignores them.
    """
    if not username:
        return []
    row = (ContributorPref.objects.filter(account=username)
           .values_list('serve_order', 'serve_for').first())
    if not row or row[0] not in ('others', 'own'):
        return []
    return parse_serve_for(row[1])


def serve_for_names(username):
    """The account's listed accounts whatever its serve order.

    A ``personal`` worker serves them beside the account's own requests
    under every order, since they are the accounts it shares its workers
    with (§ views.choose_pending).
    """
    if not username:
        return []
    raw = (ContributorPref.objects.filter(account=username)
           .values_list('serve_for', flat=True).first())
    return parse_serve_for(raw)


# How one worker picks its tasks (§ ``WorkerServing``): ``''`` as the
# account says, ``server`` with no preference for the account's own requests
# or its listed accounts, ``personal`` only those.
WORKER_MODES = ('', 'server', 'personal')


def _machine_base(machine):
    """``base#3`` -> ``base``, as ``views.machine_base``: the slots of one
    machine share its setting."""
    return (machine or '').split('#', 1)[0][:64]


def worker_serving(username, machine):
    """``(mode, campaign_id)`` for the worker that sends ``machine``.

    ``mode`` is one of WORKER_MODES and ``campaign_id`` a ``Campaign.id`` or
    None.  Without a row for the worker, ``('', None)`` unless the account
    picked a campaign, so a worker nobody configured leases as before.
    """
    if not username:
        return '', None
    row = (WorkerServing.objects
           .filter(account=username, machine=_machine_base(machine))
           .values_list('mode', 'campaign').first())
    mode, pick = row if row else ('', None)
    if mode not in WORKER_MODES:
        mode = ''
    if pick is None:
        pick = (ContributorPref.objects.filter(account=username)
                .values_list('serve_campaign', flat=True).first())
    return mode, (pick if pick and pick > 0 else None)


# The most workers the settings page lists, most recently seen first.
WORKERS_LISTED = 30


def _known_workers(username):
    """The account's machines (slot-free names), most recently seen first,
    then any configured machine not seen lately, so it can be reset."""
    names, seen = [], set()
    for machine in (WorkerPing.objects.filter(user=username)
                    .order_by('-last_seen')
                    .values_list('machine', flat=True)[:500]):
        base = _machine_base(machine)
        if base and base not in seen:
            seen.add(base)
            names.append(base)
            if len(names) >= WORKERS_LISTED:
                break
    for base in (WorkerServing.objects.filter(account=username)
                 .order_by('machine').values_list('machine', flat=True)):
        if base not in seen:
            seen.add(base)
            names.append(base)
    return names


def _worker_label(username, machine):
    """``lesha-desk-atomicdb`` -> ``desk``: the host name the client puts
    between the account and its suffix, or the whole name otherwise."""
    label = machine
    prefix = f'{username}-'
    if label.startswith(prefix) and len(label) > len(prefix):
        label = label[len(prefix):]
    if label.endswith('-atomicdb') and len(label) > len('-atomicdb'):
        label = label[:-len('-atomicdb')]
    return label


def _campaign_choices(selected):
    """The campaigns a worker can follow: the open ones that have a subtree
    (active or paused) on an unsolved root, most voted first, plus any
    already picked."""
    rows = list(Campaign.objects
                .filter(state__in=(Campaign.CState.ACTIVE,
                                   Campaign.CState.PAUSED),
                        root__status='UNKNOWN')
                .order_by('-votes', 'id').values_list('id', 'name')[:50])
    have = {cid for cid, _name in rows}
    missing = [cid for cid in selected if cid and cid > 0 and cid not in have]
    if missing:
        rows += list(Campaign.objects.filter(id__in=missing)
                     .order_by('id').values_list('id', 'name'))
    return [{'id': cid, 'name': name} for cid, name in rows]


def _parse_pick(raw, allow_inherit):
    """A campaign select's value: '' follows the account (None) where that
    exists, '0' is none, a number is that campaign."""
    raw = (raw or '').strip()
    if raw == '' and allow_inherit:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None if allow_inherit else 0
    return value if value > 0 else 0


def _save_workers(request, username):
    """The per-worker rows from the settings form.  Only machines the
    account is known to run are written; a worker set back to the account's
    defaults loses its row."""
    known = set(_known_workers(username))
    for machine in request.POST.getlist('worker'):
        if machine not in known:
            continue
        mode = request.POST.get(f'mode:{machine}', '')
        if mode not in WORKER_MODES:
            mode = ''
        pick = _parse_pick(request.POST.get(f'campaign:{machine}'), True)
        if mode == '' and pick is None:
            WorkerServing.objects.filter(account=username,
                                         machine=machine).delete()
        else:
            WorkerServing.objects.update_or_create(
                account=username, machine=machine,
                defaults={'mode': mode, 'campaign': pick})


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
        if 'serve_campaign' in request.POST:
            pick = _parse_pick(request.POST.get('serve_campaign'), False)
            pref.serve_campaign = pick or None
            fields.append('serve_campaign')
        pref.save(update_fields=fields)
        _save_workers(request, username)
        return redirect(f'{path}?saved=1')
    pref = ContributorPref.objects.filter(account=username).first()
    configured = {row.machine: row for row in
                  WorkerServing.objects.filter(account=username)}
    workers = []
    for machine in _known_workers(username):
        row = configured.get(machine)
        workers.append({
            'machine': machine,
            'label': _worker_label(username, machine),
            'mode': row.mode if row and row.mode in WORKER_MODES else '',
            # '' follows the account, '0' is none, else the campaign id.
            'campaign': ('' if row is None or row.campaign is None
                         else str(row.campaign)),
        })
    account_campaign = pref.serve_campaign if pref else None
    campaigns = _campaign_choices(
        [account_campaign] + [row.campaign for row in configured.values()])
    return render(request, 'atomicdb/notification_settings.html', {
        'workers': workers,
        'campaigns': campaigns,
        'serve_campaign': str(account_campaign) if account_campaign else '',
        'notify_ready': pref.notify_ready if pref else True,
        'serve_order': (pref.serve_order if pref
                        and pref.serve_order in SERVE_ORDERS else 'own'),
        'serve_for': pref.serve_for if pref else '',
        'lifo_on': bool(pref and pref.lifo_queue),
        'saved': request.GET.get('saved') == '1',
    })
