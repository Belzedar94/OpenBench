"""The community backlog box of the queue page (§ ``atomicdb.backlog``)."""

from django import template

from atomicdb import backlog

register = template.Library()


@register.inclusion_tag('atomicdb/_backlog_box.html', takes_context=True)
def backlog_box(context):
    request = context.get('request')
    user = getattr(request, 'user', None)
    signed_in = bool(user is not None and user.is_authenticated)
    username = user.username if signed_in else ''
    return {
        'backlogs': backlog.boxes(),
        'sizes': backlog.ADOPT_SIZES,
        'signed_in': signed_in,
        'username': username,
        'waiting_adopted': backlog.adopted_waiting(username),
        'waiting_max': backlog.ADOPT_WAITING_MAX,
        'receipt': (backlog.receipt(request.GET)
                    if signed_in and request is not None else None),
        'csrf_token': context.get('csrf_token'),
    }
