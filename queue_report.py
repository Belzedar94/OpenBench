from datetime import timedelta

from django.utils import timezone

from OpenBench.models import Result, Test

for t in Test.objects.filter(finished=False).order_by('-priority', 'id'):
    tc = 'LTC' if t.dev_time_control.startswith('40') else 'STC'
    cr = sum((r.crashes or 0) for r in Result.objects.filter(test=t))
    extra = (' CRASHES=%d' % cr) if cr else ''
    print('T%d %s %s: %dg W%d L%d D%d | LLR %.2f | %s%s' % (
        t.id, t.dev.name[:26], tc, t.games, t.wins, t.losses, t.draws,
        t.currentllr, 'run' if t.games else 'cola', extra))
for t in Test.objects.filter(finished=True,
                             updated__gte=timezone.now() - timedelta(minutes=16)):
    print('T%d %s: %dg | LLR %.2f | RECIEN CERRADO pass=%s fail=%s' % (
        t.id, t.dev.name[:26], t.games, t.currentllr, t.passed, t.failed))
