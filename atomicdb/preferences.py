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
    """Which notices this account receives.  GET shows, POST saves."""
    path = '/atomicdb/notifications/settings/'
    if not request.user.is_authenticated:
        return redirect(f'/login/?next={quote(path, safe="")}')
    username = request.user.username
    if request.method == 'POST':
        pref, _created = ContributorPref.objects.get_or_create(
            account=username)
        pref.notify_ready = request.POST.get('ready') == '1'
        pref.save(update_fields=['notify_ready'])
        return redirect(f'{path}?saved=1')
    pref = ContributorPref.objects.filter(account=username).first()
    return render(request, 'atomicdb/notification_settings.html', {
        'notify_ready': pref.notify_ready if pref else True,
        'saved': request.GET.get('saved') == '1',
    })
