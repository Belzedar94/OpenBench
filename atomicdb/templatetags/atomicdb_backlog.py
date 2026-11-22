"""The community backlog box of the queue page (§ ``atomicdb.backlog``)."""

from django import template

from atomicdb import backlog

register = template.Library()


def _viewer(context):
    request = context.get('request')
    user = getattr(request, 'user', None)
    signed_in = bool(user is not None and user.is_authenticated)
    return request, signed_in, (user.username if signed_in else '')


@register.inclusion_tag('atomicdb/_backlog_box.html', takes_context=True)
def backlog_box(context):
    request, signed_in, username = _viewer(context)
    rows = backlog.boxes()
    subs = backlog.subscriptions_of(username)
    for row in rows:
        row['sub'] = subs.get(row['name'])
    return {
        'backlogs': [row for row in rows if not backlog.fully_adopted(row)],
        'adopted_backlogs': [row for row in rows
                             if backlog.fully_adopted(row)],
        'signed_in': signed_in,
        'username': username,
        'waiting_adopted': backlog.adopted_waiting(username),
        'waiting_max': backlog.ADOPT_WAITING_MAX,
        'heavy': backlog.heavy_backlogs(),
        'receipt': (backlog.receipt(request.GET)
                    if signed_in and request is not None else None),
        'sub_receipt': (backlog.subscription_receipt(request.GET)
                        if signed_in and request is not None else None),
        'csrf_token': context.get('csrf_token'),
    }


@register.inclusion_tag('atomicdb/_backlog_subscriptions.html',
                        takes_context=True)
def backlog_subscriptions(context, profile_user):
    """The account's own subscriptions, on its own queue page only."""
    _request, signed_in, username = _viewer(context)
    own = signed_in and username == str(profile_user)
    return {'own': own,
            'rows': backlog.subscription_rows(username) if own else []}
