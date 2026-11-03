"""Links from AtomicDB pages to other sites."""

from urllib.parse import quote

from django import template

register = template.Library()

LICHESS_ANALYSIS = 'https://lichess.org/analysis/atomic/'


@register.filter
def lichess_analysis(fen):
    """The position on lichess's atomic analysis board.

    Lichess reads the FEN from the path with underscores for spaces; the
    slashes between ranks stay as they are and anything else is escaped.
    """
    return LICHESS_ANALYSIS + quote(str(fen).strip().replace(' ', '_'),
                                    safe='/_-')
